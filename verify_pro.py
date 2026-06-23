import websocket
import json
import requests
import socket
import threading
import os
import time
import boto3
import subprocess
import audioop
import random
import datetime
#from datetime import datetime, timezone
from dataclasses import asdict

from groq import Groq
from rapidfuzz import fuzz

from prompt_validator import validate
from vpro_json_parser import IVRTestCase
from db_connection import get_db_conn

import re
from difflib import SequenceMatcher


SILENCE_THRESHOLD = 300

ASTERISK_URL = "http://127.0.0.1:8088"
ARI_USER = "python"
ARI_PASS = "PASSword"
APP_NAME = "verify-pro"

RTP_PORT = 6004
ASTERISK_RTP_PORT = 7000
ASTERISK_IP = "127.0.0.1"

SOUNDS_DIR = "/var/lib/asterisk/sounds"

DEEPGRAM_API_KEY = "8451356dec473b846cb24b5dd8e275b2aa2c4a56"

call_sessions = {}
rtp_sessions = {}

keywords_list = {
        "block",
        "balance",
        "inquiry",
        "card services",
        "rewarding"
        # "account",
        # "automated"
}

def fuzzy_match(transcript, keywords, session):
    transcript = transcript.lower()

    for keyword in keywords:
        if keyword in session["keywords_matched"]:
            #print("Already matched keyword: ", keyword)
            continue

        score = fuzz.partial_ratio(keyword, transcript)

        if score > 85:
            print("Keyword matched:",keyword, "with score:", score)
            session["keywords_matched"].append(keyword)
            print("Keywords Matched List Now : ", session["keywords_matched"])
            return keyword

    return None

def is_speech_packet(payload):

    pcm = audioop.ulaw2lin(payload, 2)

    energy = audioop.rms(pcm, 2)

    #print("Energy : ", energy)

    if energy > SILENCE_THRESHOLD:
        return True
    else:
        return False
    
################################################
# RTP Receiver (GLOBAL)
################################################
def rtp_receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", RTP_PORT))

    print("RTP receiver started")

    while True:
        packet, addr = sock.recvfrom(2048)

        src_ip, src_port = addr
        payload = packet[12:]

        session = rtp_sessions.get((src_ip, src_port))

        if not session:
            # first packet for this call
            for channel_id, s in call_sessions.items():

                if not s.get("rtp_addr"):

                    s["rtp_addr"] = (src_ip, src_port)
                    rtp_sessions[(src_ip, src_port)] = s

                    session = s

                    print("Mapped RTP", addr, "->", channel_id)

                    break

        if not session:
            continue
        
        if is_speech_packet(payload):
            #print("Speech packet")

            if session["bot_rtp_start_time"] > 0:
                print("\n",session["voicebot_channel"],":Bot RTP Start Time was:", session["bot_rtp_start_time"],"Current Time is:",time.monotonic())
                session["bot_last_rtp_time"] = time.monotonic()
                current_latency = time.monotonic() - session["bot_rtp_start_time"]

                # if session["bot_avg_latency"] > 0:
                #     session["bot_avg_latency"] = (session["bot_avg_latency"] + current_latency)/2
                # else:
                  
                session["bot_avg_latency"] = current_latency

                print("\n",session,":Current Latency: ", current_latency ,"Bot Average Latency Recorded:", session["bot_avg_latency"])
                session["bot_rtp_start_time"] = 0
        # else:
        #     print("Silence packet")

        ws = session.get("stt_ws")

        if ws:
            try:
                ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
            except:
                pass


################################################
# Deepgram STT
################################################

def start_stt(channel_id):

    url = "wss://api.deepgram.com/v1/listen?encoding=mulaw&sample_rate=8000"

    headers = [
        f"Authorization: Token {DEEPGRAM_API_KEY}"
    ]

    ws = websocket.WebSocketApp(
        url,
        header=headers,
        on_message=lambda ws, msg: on_stt(ws, msg, channel_id)
    )

    threading.Thread(target=ws.run_forever, daemon=True).start()

    return ws


def on_stt(ws, message, channel_id):

    session = call_sessions[channel_id]

    data = json.loads(message)

    #print(time.time())
    #print("ON STT: Data Received", data)

    if "channel" not in data:
        return

    transcript = data["channel"]["alternatives"][0]["transcript"]

    if transcript.strip():

        print("on_stt: Transcript:", transcript)

        session["transcript"] += " " + transcript

        write_transcript(channel_id, "Caller", transcript)

        # Let's check the interim Transcript to match any test keywords and barge-in
        if fuzzy_match(transcript, keywords_list, session) is not None:

            process_transcript = session["transcript"]
            session["transcript"] = ""

            sound_file = data["metadata"]["request_id"]
            base_path = f"{SOUNDS_DIR}/{sound_file}"

            voicebot_channel_id = session["voicebot_channel"]

            threading.Thread(
                target=process_nodedata,
                args=(process_transcript, base_path, voicebot_channel_id, channel_id)
            ).start()

    elif session["transcript"]:

        print("on_stt: Current Transcript:", session["transcript"])

        process_transcript = session["transcript"]
        session["transcript"] = ""

        sound_file = data["metadata"]["request_id"]
        base_path = f"{SOUNDS_DIR}/{sound_file}"

        voicebot_channel_id = session["voicebot_channel"]

        threading.Thread(
            target=process_nodedata,
            args=(process_transcript, base_path, voicebot_channel_id, channel_id)
        ).start()

    else:
        print("on_stt: Skipping Action")

################################################
# WRITE TRANSCRIPT TO A FILE
################################################

def write_transcript(channel_id, speaker, text):

    session = call_sessions.get(channel_id)

    if not session:
        return

    file_path = session.get("transcript_file")

    if not file_path:
        print("Transcript file not Found for Channel ID: ", channel_id)
        return

    if speaker == "KCAI":
        text = "Last avg bot latency recorded: " + f'{session["bot_avg_latency"]:.2f}' + " Seconds \n" + text

    timestamp = time.strftime("%H:%M:%S")

    line = f"[{timestamp}] {speaker}: {text}\n"

    with open(file_path, "a") as f:
        f.write(line)

################################################
# LLM + TTS
################################################

def process_nodedata(transcript_text, base_filename, channel_id, parent_channel_id):

    expected_prompt = "welcome to the {choice x=automated:1|manual:2} ivr testing system please listen carefully to the following options press {Digits} one for account information press two for technical support press three for payment services press nine to repeat this menu press zero to exit{*}"
    #nodedata_match = match_nodedata(expected_prompt, transcript_text)

    result = validate_prompts(expected_prompt,transcript_text)

    #print("Tag Match Result: ", result)      

    session = call_sessions[parent_channel_id]


    if session:
        print("Session Captured Variables:",session["captured_variables"])

        if (result.captured_variables and any(result.captured_variables.values())):
            session["captured_variables"].update(result.captured_variables)

        print("Session Object: ",session)
        session["ivr_step_number"] += 1

        # Set Test Data Node Data and Result

        if session["current_node_id"] is None:
            current_node = session["test_case"].get_start_node()
        elif session["current_node_id"] == "EOF":
            print("End Of Test Case Detected, Ending the TestCall")
            hangup_channel(channel_id)
            return
        else:
            current_node = session["test_case"].get_node(session["current_node_id"])

        session["current_node_id"] = current_node.node_id
        session["expected_text"] = current_node.expected_text

        # For debugging print the node test details
        print_node_test_details(current_node)
        
        session["node_result"] = {
            "node_id": session["ivr_step_number"], #session["current_node_id"],
            "expected_text": session["expected_text"],
            "actual_text": transcript_text,
            "transcription_match": result.match_percentage,
            "response_time": session["bot_avg_latency"],
            "test_result": json.dumps(asdict(result),ensure_ascii=False),
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ") #"2026-06-22T18:21:55Z" 
        }
    
        try:
            if current_node.persona:
                for language_code in current_node.persona:
                    if current_node.persona[language_code]["VI"]:
                        print("Persona: ", language_code, " VoiceID ", current_node.persona[language_code]["VI"])

            if current_node.action_to_take:
                print("INJECT TYPE: ", current_node.action_to_take.inject_type)
                print("ACTION DATA: ", current_node.action_to_take.value)
            else:
                print("No Action to take: Let's Skip this Node")
                # Insert Test History Data into the DB
                record_test_history(session)

                # Evaluate the Next Node ID
                next_node_id = current_node.transitions.get("on_success")

                if next_node_id:
                    print("Next Node ID: ", next_node_id)
                    current_node = session["test_case"].get_node(next_node_id)
                    if current_node:
                        session["current_node_id"] = current_node.node_id
                    else:
                        session["current_node_id"] = "EOF"

                return

        except:
            pass

        # Response generation based on Node Data
        print("IVR Step Number:", session["ivr_step_number"])

        if current_node.action_to_take.inject_type == "DTMF":
            send_dtmf(channel_id, current_node.action_to_take.value)

        elif current_node.action_to_take.inject_type == "Speech":
            play_audio(channel_id, current_node.action_to_take.value)

        elif current_node.action_to_take.inject_type == "TTS":
            reply_path = synthesize_speech_polly(current_node.action_to_take.value, base_filename, language_code, current_node.persona[language_code]["VI"])
            play_audio(channel_id, base_filename)

        elif current_node.action_to_take.inject_type == "Silence":
            min_val, max_val = map(int,current_node.action_to_take.value.split("-"))
            random_silence = random.randint(min_val, max_val)
            play_silence(channel_id, random_silence)
            # play_silence_duration(channel_id, random_silence)

            
        print("NEXT TRANSITIONS: ",current_node.transitions)

        # Insert Test History Data into the DB
        record_test_history(session)

        # Evaluate the Next Node ID
        next_node_id = current_node.transitions.get("on_success")

        if next_node_id:
            print("Next Node ID: ", next_node_id)
            current_node = session["test_case"].get_node(next_node_id)
            if current_node:
                session["current_node_id"] = current_node.node_id
            else:
                session["current_node_id"] = "EOF"

    else:
        print("Session Not Found for IVR Traversal") 

        
def match_nodedata(expected_prompt, actual_prompt):

    # Entire sentence similarity
    match_percent = similarity(
        expected_prompt,
        actual_prompt
    )

    print("\n----- MATCH DETAILS -----")
    print("Expected :", expected_prompt)
    print("Actual   :", actual_prompt)
    print("Match %  :", match_percent)

    return False


def similarity(a, b):

    return round(
        SequenceMatcher(
            None,
            a.lower(),
            b.lower()
        ).ratio() * 100,
        2
    )

def print_node_test_details(current_node):

        print("Language IDs:", current_node.language_ids)
        print("Persona:", current_node.persona)

        print("Minor Threshold Time:", current_node.minor_threshold_time)
        print("Major Threshold Time:", current_node.major_threshold_time)
        print("Minor Confidence Level:", current_node.minor_confidence_level)
        print("Major Confidence Level:", current_node.major_confidence_level)
    

################################################
# POLLY TTS
################################################

def synthesize_speech_polly(text, base_filename, language_code="en-US", voice_id="Joey"):

    polly = boto3.client("polly", region_name="ap-south-1")

    response = polly.synthesize_speech(
        Text=text,
        OutputFormat="pcm",
        LanguageCode=language_code,
        VoiceId=voice_id,
        SampleRate="8000"
    )

    print("TTS Response:",response)

    raw_file = f"{base_filename}.pcm"
    ulaw_file = f"{base_filename}.ulaw"

    with open(raw_file, "wb") as f:
        f.write(response["AudioStream"].read())

    subprocess.run([
        "sox",
        "-t", "raw",
        "-r", "8000",
        "-e", "signed",
        "-b", "16",
        "-c", "1",
        raw_file,
        "-t", "ul",
        ulaw_file
    ], check=True)

    os.remove(raw_file)

    return ulaw_file

################################################
# PLAY AUDIO TO THE VOICEBOT CHANNEL
################################################

def play_audio(channel_id, sound):

    print("Audio playback for DTMF check:", sound)

    r= requests.post(
        f"{ASTERISK_URL}/ari/channels/{channel_id}/play",
        auth=(ARI_USER, ARI_PASS),
        json={"media": f"sound:{sound}"}
    )

    print("beep response:", r)

def play_silence(channel_id, seconds):

    print(f"Playing {seconds} seconds of silence")

    r = requests.post(
        f"{ASTERISK_URL}/ari/channels/{channel_id}/play",
        auth=(ARI_USER, ARI_PASS),
        json={"media": f"sound:silence/{seconds}"}
    )

    print("Silence playback response:", r.status_code, r.text)

    return r

def play_silence_duration(channel_id, seconds):

    print(f"Playing {seconds} seconds of silence")

    # r = requests.post(
    #     f"{ASTERISK_URL}/ari/channels/{channel_id}/play",
    #     auth=(ARI_USER, ARI_PASS),
    #     json={"media": f"sound:silence/{seconds}"}
    # )

    print("Silence period initiated: waiting for seconds:", seconds)

    time.sleep(seconds)

    print("Silence period completed:")

def play_audio_bridge(channel_id, sound):

    session = call_sessions[channel_id]
    bridge_id = session["bridge_id"]

    print("Audio playback:", sound)

    requests.post(
        f"{ASTERISK_URL}/ari/bridges/{bridge_id}/play",
        auth=(ARI_USER, ARI_PASS),
        json={"media": f"sound:{sound}"}
    )

def hangup_channel(channel_id):

    r = requests.delete(
        f"{ASTERISK_URL}/ari/channels/{channel_id}",
        auth=(ARI_USER, ARI_PASS)
    )

    print(
        f"Hangup channel {channel_id}: "
        f"{r.status_code}"
    )

    return r.status_code == 204
################################################
# PLAY DTMF TO THE VOICEBOT CHANNEL
################################################

def send_dtmf(channel_id, digits):

    print("Sending DTMF :", digits, " on channel:", channel_id)
    url = f"{ASTERISK_URL}/ari/channels/{channel_id}/dtmf"

    params = {
        "dtmf": digits,
        "before": 0,
        "between": 200,
        "duration": 500
    }

    print("Response: ", requests.post(url, params=params, auth=(ARI_USER, ARI_PASS)))

################################################
# ARI FUNCTIONS
################################################

def create_bridge():

    r = requests.post(
        f"{ASTERISK_URL}/ari/bridges",
        auth=(ARI_USER, ARI_PASS),
        params={"type": "mixing,dtmf_events"}
    )

    bridge = r.json()

    print("Bridge created:", bridge["id"])

    return bridge["id"]

def record_bridge(bridge_id):

    r = requests.post(
        f"{ASTERISK_URL}/ari/bridges/{bridge_id}/record",
        auth=(ARI_USER, ARI_PASS),
        params={
            "name": f"rec_{bridge_id}",
            "format": "wav",
            "maxDurationSeconds": 0,
            "ifExists": "overwrite"
        }
    )

    print("Bridge recording started:", bridge_id)

def record_channel(channel_id):

    r = requests.post(
        f"{ASTERISK_URL}/ari/channels/{channel_id}/record",
        auth=(ARI_USER, ARI_PASS),
        params={
            "name": f"rec_{channel_id}",
            "format": "wav",
            "maxDurationSeconds": 0,
            "ifExists": "overwrite"
        }
    )

    print("Channel recording started for the channel:", channel_id)

def create_external_media():

    r = requests.post(
        f"{ASTERISK_URL}/ari/channels/externalMedia",
        auth=(ARI_USER, ARI_PASS),
        params={
            "app": APP_NAME,
            "external_host": f"127.0.0.1:{RTP_PORT}",
            "format": "ulaw"
        }
    )

    ch = r.json()

    print("ExternalMedia:", ch["id"])

    return ch["id"]


def add_channel_to_bridge(bridge, channel):

    requests.post(
        f"{ASTERISK_URL}/ari/bridges/{bridge}/addChannel",
        auth=(ARI_USER, ARI_PASS),
        params={"channel": channel}
    )


def dial_voicebot(channel_id, test_case):

    print("Originating Voicebot Channel on the Phone Number:", test_case.meta.phone_to_dial)

    requests.post(
        f"{ASTERISK_URL}/ari/channels",
        auth=(ARI_USER, ARI_PASS),
        params={
            "endpoint": f"Local/{test_case.meta.phone_to_dial}@ivr-test-final",
            "app": APP_NAME,
            "appArgs": channel_id
        }
    )


################################################
# ARI EVENT HANDLER
################################################

def on_ari(ws, message):

    event = json.loads(message)
    #print("on_ari: Incoming Event:", event)
    #print("on_ari: MR KAPS Event TYPE:", event["type"])

    # if event["channel"]["name"].startswith("Local/") and event["channel"]["name"].endswith(";2"):
    #     print("This is ;2 leg, ignore")
    #     return
    
    parent_channel = None

    if event["type"] == "PlaybackFinished":
        # print("KAPS KAPS KAPS Event Type:", event["type"], " for Channel ID: ", event["playback"]["target_uri"].split(":")[1])
        target_uri = event["playback"]["target_uri"]
        channel_id = target_uri.split(":")[1]

        for cid, s in call_sessions.items():
                if s.get("voicebot_channel") == channel_id:
                    print("Found Parent Channel for Playback Finished Logic:", channel_id," at:",time.monotonic())
                    session = s
                    session["bot_rtp_start_time"] = time.monotonic()
                    break

        return

    elif event["type"] == "ChannelDtmfReceived":
        channel_id = event["channel"]["id"]
        channel_name = event["channel"]["name"].split(";")[0]

        print("KAPS KAPS KAPS Event Type:", event["type"], " for Channel ID: ", channel_id, " channel_name" ,channel_name)


        for cid, s in call_sessions.items():
                if s.get("voicebot_channel_name") == channel_name:
                    print("Found Parent Channel for DTMF Finished Logic:", channel_id," at:",time.monotonic())
                    session = s
                    session["bot_rtp_start_time"] = time.monotonic()
                    break
        
        return

    try:
        # if channel_id is None:
            channel_id = event["channel"]["id"]
            channel_name = event["channel"]["name"]
    except:
        print("on_ari: Exception: Some Error for the Event:", event)

    if event["type"] == "StasisEnd":

        if channel_id is None:
            channel_id = event["channel"]["id"]

        print("Channel left Stasis:", channel_id)

        # Let's find the session for the channel_id
        session = call_sessions.get(channel_id)

        if not session:
            # maybe the voicebot channel triggered it
            for cid, s in call_sessions.items():
                if s.get("voicebot_channel") == channel_id:
                    session = s
                    channel_id = cid
                    break

        if not session:
            print("Session not found")
            return

        # Clean-up Channel objects and data
        cleanup_call(session)
        return
    
    # If this event is from the Voicebot Channel then extract the Caller Channel
    try:
        parent_channel = event["args"][0]
        if parent_channel is not None:
            print("Parent Channel Data : ", parent_channel)

    except:
        pass
        #print ("Exception at event args")

    #print("Incoming channel:", channel_name)

    if channel_name.startswith("UnicastRTP"):
        #print("Ignoring ExternalMedia channel")
        return

    if event["type"] != "StasisStart":
        return
    
    #  # Extension that was dialed
    context = event["channel"]["dialplan"]["context"]
    exten = event["channel"]["dialplan"]["exten"]

    print(f"Dialed Extension: {exten}")
    print(f"Context: {context}")

    if parent_channel is None:

        #Let's populate the test cases for this call
        test_case = load_test_case()
        if test_case is None:
            print("Test Case could not be parsed")
            return

        play_audio(channel_id, "beep")
    
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        transcript_file = f"/tmp/voicebot_{channel_id}_{timestamp}.txt"

        print("Caller Channel is Not Available: Fresh Call")

        bridge_id = create_bridge()

        stt_ws = start_stt(channel_id)

        ext_media = create_external_media()
        
        # Initiating Call Session Array
        call_sessions[channel_id] = {
            # Call-specific
            "call_id": None,
            "dialed_extension": exten,
            "caller_channel": channel_id,
            "bridge_id": bridge_id,
            "voicebot_channel": None,
            "voicebot_channel_name": None,
            "external_media" : ext_media,
            "transcript": "",
            "transcript_file": transcript_file,
            "stt_ws": stt_ws,
            "rtp_addr": None,

            # Voicebot Channel RTP specific
            "bot_dial_time":time.monotonic(),
            "bot_connect_time":0,
            "bot_answer_duration":0,
            "bot_last_rtp_time":0,
            "bot_rtp_start_time":0,
            "bot_avg_latency":0,

            "keywords_matched":[],
            
            # Prompt engine variables
            "captured_variables": {},

            # Test execution
            "ivr_step_number":0,
            "test_execution_row_id": int(time.monotonic()/1000), #Temporary Logic for Dynamic Value
            "phone_to_dial": "+18005550199",
            "current_node_id": None,
            "execution_status": "RUNNING",

            # Node results
            "node_result": [],

            # Overall metrics
            "summary": {
                "total_nodes": 0,
                "passed_nodes": 0,
                "failed_nodes": 0,
                "overall_result": None
            },

            # Test Case Data
            "test_case": test_case
        }

        open(transcript_file, "a").write(f"CALL START {timestamp}\n")

        add_channel_to_bridge(bridge_id, channel_id)
        add_channel_to_bridge(bridge_id, ext_media)

        dial_voicebot(channel_id, test_case)

    else:
        print("Caller Channel is Available: Voicebot Channel")

        session = call_sessions[parent_channel]

        session["bot_connect_time"] = time.monotonic()
        session["bot_answer_duration"] = session["bot_connect_time"] - session["bot_dial_time"]

        print("Adding voicebot to bridge")

        session["voicebot_channel"] = channel_id
        session["voicebot_channel_name"] = channel_name.split(";")[0]

        #record_channel(channel_id)

        add_channel_to_bridge(session["bridge_id"], channel_id)

################################################
# LOAD THE TEST CASE FROM THE IVR TEST JSON
################################################

def load_test_case(json_file="ivr_test.json"):
    # Load and Parse ivr_test.json Test Case File
    test_case = json_file_parse(json_file)

    if not test_case:
        return None

    print("PHONE NUMBER: ", test_case.meta.phone_to_dial)
    print("_" * 100)
        # print("Language IDs:", test_case.test_model_settings.language_ids)
        # print("Persona:", test_case.test_model_settings.persona)
        # print("Minor Threshold Time:", test_case.test_model_settings.minor_threshold_time)
        # print("Major Threshold Time:", test_case.test_model_settings.major_threshold_time)
        # print("Minor Confidence Level:", test_case.test_model_settings.minor_confidence_level)
        # print("Major Confidence Level:", test_case.test_model_settings.major_confidence_level)

        # if test_case.test_model_settings.extended_attributes:
        #     print("Extended Attributes:", test_case.test_model_settings.extended_attributes)

        # node = test_case.get_node("node_102")

        # print(node.node_type)
        # print(node.expected_text)

        # if node.action_to_take:
        #     print(node.action_to_take.inject_type)
        #     print(node.action_to_take.value)

        # print(node.transitions)


    current_node = test_case.get_start_node()

    while current_node:

        print("_" * 100)

        print(f"NODE ID: {current_node.node_id}: ")
        print(f"EXPECTED TEXT: {current_node.expected_text}")

        if current_node.action_to_take:
            print("INJECT TYPE: ", current_node.action_to_take.inject_type)
            print("ACTION DATA: ", current_node.action_to_take.value)

        print("Language IDs:", current_node.language_ids)
        print("Persona:", current_node.persona)

        try:
            if current_node.persona:
                for language_code in current_node.persona:
                    if current_node.persona[language_code]["VI"]:
                        print("Persona: ", language_code, " VoiceID ", current_node.persona[language_code]["VI"])
        except:
            pass

        print("Minor Threshold Time:", current_node.minor_threshold_time)
        print("Major Threshold Time:", current_node.major_threshold_time)
        print("Minor Confidence Level:", current_node.minor_confidence_level)
        print("Major Confidence Level:", current_node.major_confidence_level)

        if current_node.extended_attributes:
            print("Extended Attributes:", current_node.extended_attributes)

        print("TRANSITIONS: ",current_node.transitions)
   
        next_node_id = current_node.transitions.get(
            "on_success"
        )

        if not next_node_id:
            break

        current_node = test_case.get_node(
            next_node_id
        )

    return test_case


################################################
# RECORD TEST HISTORY INTO THE DATABASE
################################################

def record_test_history(session):

    # bridge_id = session.get("bridge_id")
    # caller = session.get("caller_channel")
    # voicebot = session.get("voicebot_channel")
    # external_media = session.get("external_media")
    # stt_ws = session.get("stt_ws")

    sql = f""" 
            INSERT INTO kcdb.verify_pro_test_execution_row_node_history
            (
            verify_pro_test_execution_row_history_id, 
            verify_pro_node_id, 
            mos, 
            time_to_silence, 
            actual_text, 
            transcription_match, 
            response_time, 
            test_result, 
            created_on)
            VALUES
            (
            %s,%s,%s,%s,%s,%s,%s,%s,current_timestamp()
            )
            """
    
    conn = get_db_conn()

    cursor = conn.cursor()

    try:
        
        print(f"Insert SQL : {sql}")

        # 4. Execute the query
        cursor.execute(sql,
            (
            session["test_execution_row_id"], 
            session["node_result"]["node_id"], 
            0.00, 
            0.00, 
            session["node_result"]["actual_text"], 
            session["node_result"]["transcription_match"],
            session["node_result"]["response_time"],
            session["node_result"]["test_result"]
            ))
    
        # 5. COMMIT THE TRANSACTION (Crucial for INSERT, UPDATE, DELETE)
        conn.commit()
    
        # 6. Get the auto-incremented ID (Optional)
        print(f"Successfully inserted. New Row ID: {cursor.lastrowid}")

    except Exception as err:
        print(f"Error: {err}")
        conn.rollback() # Undo changes if an error happens

    finally:
        # 7. Close connections
        cursor.close()
        conn.close()

################################################
# HANGUP EVENT HANDLER
################################################

def cleanup_call(session):

    bridge_id = session.get("bridge_id")
    caller = session.get("caller_channel")
    voicebot = session.get("voicebot_channel")
    external_media = session.get("external_media")
    stt_ws = session.get("stt_ws")

    print(bridge_id, caller, voicebot, external_media, stt_ws, sep=" | ")

    print("Cleaning up call for Session: ",session)

    # session = call_sessions.get(channel_id)

    # if not session:
    #     # maybe the voicebot channel triggered it
    #     for cid, s in call_sessions.items():
    #         if s.get("voicebot_channel") == channel_id:
    #             session = s
    #             channel_id = cid
    #             break

    # if not session:
    #     print("Session not found")
    #     return

    try:
        if caller:
            print("Deleting Caller Channel:",caller)
            requests.delete(
                f"{ASTERISK_URL}/ari/channels/{caller}",
                auth=(ARI_USER, ARI_PASS)
            )
    except:
        pass

    try:
        if voicebot:
            print("Deleting Voicebot Channel:",voicebot)
            requests.delete(
                f"{ASTERISK_URL}/ari/channels/{voicebot}",
                auth=(ARI_USER, ARI_PASS)
            )
    except:
        pass

    try:
        if bridge_id:
            print("Deleting Bridge:",bridge_id)
            requests.delete(
                f"{ASTERISK_URL}/ari/bridges/{bridge_id}",
                auth=(ARI_USER, ARI_PASS)
            )
    except:
        pass

    try:
        if external_media:
            print("Deleting External Media Channel:",external_media)
            requests.delete(
                f"{ASTERISK_URL}/ari/channels/{external_media}",
                auth=(ARI_USER, ARI_PASS)
            )
    except:
        pass

    try:
        if session.get("rtp_addr") in rtp_sessions:
            print("Deleting RTP Session Map:",rtp_sessions[session["rtp_addr"]])
            del rtp_sessions[session["rtp_addr"]]
    except:
        pass

    try:
        if stt_ws:
            print("Deleting STT WS:", stt_ws)
            stt_ws.close()
    except:
        pass

    # if channel_id in call_sessions:
    #     del call_sessions[channel_id]

    del session

    print("Call cleanup completed")


################################################
# TEMPORARY TAG MATCH LOGIC [ REMOVE THIS FOR PRODUCTION ]
################################################
def validate_prompts(expect_to_hear:str, actual_prompt:str):

    result = validate(
        expect_to_hear,
        actual_prompt,
        language="en-US"   # en-AU, en-GB, es-US, nl-NL, ja-JP
    )
    print("=====================================================")
    print("Expect to Hear:",expect_to_hear)
    print("Actual Prompt:",actual_prompt)
    print("Matched: ",result.matched)
    print("Match %:",result.match_percentage)
    print("FULL RESULT:\n",result)
    print("=====================================================")

    return result
  
################################################
# IVR TEST JSON PARSER
################################################
  
def json_file_parse(json_file):

    with open(json_file, "r") as f:

        data = json.load(f)

    return IVRTestCase(data)

################################################
# MAIN
################################################

def main():

    print("Starting Voicebot")

    # TEMPORARY TAG MATCH CHECK [ REMOVE FOR PRODUCTION ]
    # expect_to_hear="Hi {*} Hi Good Morning {BypassRecognition}Your PIN is {Digits Length=4} Thank you {*} Remaining Balance is {Currency} Let's meet {Date} at {Time} Pay {Number} to {AlphaNum Length=5} {Choice x=kalpan:1|aditya:2|satish:3}"
    # actual_prompt="Your PIN is 9 8 9 8 Thank you Buddy Remaining Balance is twenty dollars Let's meet yesterday at noon Pay twelve hundred five to ABC12 kalpan"
    expect_to_hear="Hi {choice x=kalpan:name|naik:surname} speaking Good {choice y=morning:AM|evening:PM}"
    actual_prompt="Hi naik speaking Good evening"

    result = validate_prompts(expect_to_hear, actual_prompt)

    print("Captured Variables:")
    for var_name, var_value in result.captured_variables.items():
        print(f"{var_name} = {var_value}")

    # print("Captured variable is x=",result.captured_variables["x"])

    # START SINGLE RTP RECEIVER
    threading.Thread(target=rtp_receiver, daemon=True).start()

    ari_ws = f"ws://127.0.0.1:8088/ari/events?app={APP_NAME}&api_key={ARI_USER}:{ARI_PASS}"

    ws = websocket.WebSocketApp(
        ari_ws,
        on_message=on_ari
    )

    ws.run_forever()


if __name__ == "__main__":
    main()
