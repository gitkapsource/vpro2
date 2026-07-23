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
import asyncio
import signal
import atexit
import wave
import shlex
#from datetime import datetime, timezone
from dataclasses import asdict

from groq import Groq
from rapidfuzz import fuzz

from prompt_validator import validate
from vpro_json_parser import IVRTestCase
from db_connection import get_db_conn

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

AWS_REGION = "ap-south-1"   # or your Transcribe-supported region # TO BE READ FROM DEPLOYMENT CONFIG FILE
# An AWS "final" result only completes one natural speech segment.  Wait for
# transcript inactivity before treating the collected segments as one prompt.
# Keep this comfortably above the roughly 300 ms pauses used by the IVR.
AWS_PROMPT_IDLE_TIMEOUT_SECONDS = 2.0

import re
from difflib import SequenceMatcher

################## Queue based Logging #########################################
import queue
import logging
from logging.handlers import QueueHandler
from logging.handlers import QueueListener
from logging.handlers import RotatingFileHandler

log_queue = queue.Queue()

file_handler = RotatingFileHandler(
    "/var/log/verifypro.log",
    maxBytes=50 * 1024 * 1024,
    backupCount=10
)

formatter = logging.Formatter(
    "[%(asctime)s] [%(callid)s] [%(testid)s] %(message)s"
)

file_handler.setFormatter(formatter)

listener = QueueListener(
    log_queue,
    file_handler
)

listener.start()

logger = logging.getLogger("verifypro")
logger.setLevel(logging.INFO)

logger.addHandler(
    QueueHandler(log_queue)
)


###########################################################

#phonexia config

PHONEXIA_ENABLED = 0
PHONEXIA_PATH = "/usr/src/scripts/phonexia/SQE-D1-cmd-3.50.5-lin64"

NODE_TEST_FAILED = 0
NODE_TEST_SATISFACTORY = 1
NODE_TEST_SUCCESS = 2

SILENCE_THRESHOLD = 300

ASTERISK_URL = "http://127.0.0.1:8088"
ARI_USER = "python"
ARI_PASS = "PASSword"
APP_NAME = "verify-pro"

RTP_PORT = 6004
ASTERISK_RTP_PORT = 7000
ASTERISK_IP = "127.0.0.1"

SOUNDS_DIR = "/var/lib/asterisk/sounds"

NODE_RECORDING_DIR = "/var/lib/asterisk/recordings/verifypro_nodes"
MOS_COMMAND = os.getenv("MOS_COMMAND", "").strip()

DEEPGRAM_API_KEY = "c55fbae3b46e73928316d000f602b8b200c9e4d0"

call_sessions = {}
rtp_sessions = {}

shutdown_event = threading.Event()
shutdown_lock = threading.Lock()
shutdown_started = False
ari_websocket = None
rtp_socket = None

# Dedicated queue for node processing. A single long-lived worker avoids
# creating/scheduling a new thread at the exact barge-in boundary.
node_processing_queue = queue.Queue()
node_processing_worker_thread = None
node_processing_worker_lock = threading.Lock()

keywords_list = {
        "block",
        "balance",
        "inquiry",
        "card services",
        "rewarding"
        # "account",
        # "automated"
}

def get_phoneix_pesq_score(session, filepath,try_cnt=1):
        filename = filepath.split('/')[-1]
        op_filename = '/tmp/'+filename.replace('.wav','.txt')
        command = f"""{PHONEXIA_PATH}/sqestim -enable-pesq -c {PHONEXIA_PATH}/settings/sqestim.bs -i {filepath} -o {op_filename}"""

        result = os.system(command)
        if(result!=0):
            #if there were some issue in the command execution
            # logger.error(f"[{self.id}_{self.counter}_{self.test}]Error in the phonexia command execution..! try_cnt={try_cnt}")
            logger.info(
                f"Phonexia Command {command} result: {result}",
                extra={
                    "callid": session.get("call_id", "UNKNOWN"),
                    "testid": session.get("test_execution_row_id", "UNKNOWN"),
                },
            )

            if try_cnt==10:
                return None
            #let's try for 10 times
            return get_phoneix_pesq_score(session, filepath,try_cnt+1)
        
        #let's read the output file generated through phonexia for the audio file
        result = ''
        try:
            with open(op_filename,'r') as file:
                result = file.readlines()
        except Exception as e:
            # logger.error(f"[{self.id}_{self.counter}_{self.test}]Exception while reading the {op_filename} file: {e}")
            return None

        #extracting pesq score from the output file
        if len(result)>0 and 'pesq' in result[0] and 'value' in result[0]:
            ph_score = result[0].split(',')[1].strip().split('=')[-1].strip()
            if((ph_score)):
                ph_score = float(ph_score)
                #3.8 is starting point anything above is 4.4. Deviation to start with is 1.130 and for each next value we add 0.015
                if(ph_score > 3.89):
                    kc_score = 4.4
                elif(ph_score < 3.89 and ph_score >=2):
                    kc_score = ph_score * ((1.130 + (round(3.8-float(str(ph_score)[0:3]),1)*10)*0.015))
                    if(kc_score > 4.4):
                        kc_score = 4.4
                elif(ph_score < 2 and ph_score > 1.76):
                    kc_score = ph_score * 1.2
                elif(ph_score < 1.76 and ph_score > 1):
                    kc_score = ph_score * 1.135
                else:
                    kc_score = ph_score
                # logger.info(f"[{self.id}_{self.counter}_{self.test}] Phonexia Score={ph_score} -- KC Score={kc_score}")
                logger.info(
                    f"Phonexia Score={ph_score} -- KC Score={kc_score}",
                    extra={
                        "callid": session.get("call_id", "UNKNOWN"),
                        "testid": session.get("test_execution_row_id", "UNKNOWN"),
                    },
                )
                return float(kc_score)
        #if pesq score not found in the output file then phonexia unabled to score that audio file.
        # logger.error(f"[{self.id}_{self.counter}_{self.test}]Phonexia unable to process the audio file!")
        return None

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
# NODE-LEVEL RTP RECORDING + EXTERNAL MOS
################################################

def _safe_recording_component(value):
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value))
    return cleaned.strip("_") or "unknown"


def start_node_recording(session, node_id):
    """
    Start recording inbound G.711 mu-law RTP for one IVR node.

    Final WAV filename:
        <recording_session["call_id"]>_<node_id>.wav
    """
    stop_node_recording(session, run_mos=False)

    recording_session = session.setdefault("recording_session", {})
    # sip_call_id = _safe_recording_component(
    #     recording_session.get("call_id",session.get("call_id", session.get("caller_channel", "unknown")))
    # )

    sip_call_id = session["sip_call_id"]
    safe_node_id = _safe_recording_component(node_id)

    os.makedirs(NODE_RECORDING_DIR, exist_ok=True)

    base_name = f"{session['test_execution_row_id']}_{safe_node_id}_{sip_call_id}"
    raw_path = os.path.join(NODE_RECORDING_DIR, f"{base_name}.ulaw")
    wav_path = os.path.join(NODE_RECORDING_DIR, f"{base_name}.wav")

    raw_file = open(raw_path, "wb")

    session["recording_session"] = {
        "call_id": sip_call_id,
        "node_id": safe_node_id,
        "raw_path": raw_path,
        "wav_path": wav_path,
        "file": raw_file,
        "bytes_written": 0,
        "started_at": time.monotonic(),
        "active": True,
    }

    logger.info(
        f"Node recording started: {wav_path}",
        extra={
            "callid": session.get("call_id", "UNKNOWN"),
            "testid": session.get("test_execution_row_id", "UNKNOWN"),
        },
    )

    return wav_path

def write_node_audio(session, ulaw_payload):
    """Append one inbound RTP payload to the active node recording."""
    recording = session.get("recording_session")

    if not recording or not recording.get("active"):
        return

    raw_file = recording.get("file")
    if raw_file is None:
        return

    try:
        raw_file.write(ulaw_payload)
        recording["bytes_written"] += len(ulaw_payload)
    except Exception:
        logger.exception(
            "Failed writing node RTP audio",
            extra={
                "callid": session.get("call_id", "UNKNOWN"),
                "testid": session.get("test_execution_row_id", "UNKNOWN"),
            },
        )


def _convert_ulaw_to_wav(raw_path, wav_path):
    """Convert raw 8-kHz mono mu-law audio to 16-bit PCM WAV."""
    with open(raw_path, "rb") as source:
        ulaw_audio = source.read()

    pcm_audio = audioop.ulaw2lin(ulaw_audio, 2)

    with wave.open(wav_path, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(pcm_audio)


def run_external_mos(wav_path, session, node_id):
    """
    Run the configured external MOS command.

    MOS_COMMAND placeholders:
      {file}
      {call_id}
      {node_id}

    Example:
      export MOS_COMMAND='python3 /opt/mos/score.py --audio {file}'
    """
    if not MOS_COMMAND:
        logger.info(
            f"MOS command not configured; recording retained at {wav_path}",
            extra={
                "callid": session.get("call_id", "UNKNOWN"),
                "testid": session.get("test_execution_row_id", "UNKNOWN"),
            },
        )
        return None

    call_id = session.get("recording_session", {}).get(
        "call_id",
        session.get("call_id", "unknown"),
    )

    command_text = MOS_COMMAND.format(
        file=wav_path,
        call_id=call_id,
        node_id=node_id,
    )

    try:
        result = subprocess.run(
            shlex.split(command_text),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception:
        logger.exception(
            f"MOS command execution failed for {wav_path}",
            extra={
                "callid": session.get("call_id", "UNKNOWN"),
                "testid": session.get("test_execution_row_id", "UNKNOWN"),
            },
        )
        return None

    if result.returncode != 0:
        logger.info(
            f"MOS command returned {result.returncode} for {wav_path}: "
            f"{result.stderr.strip()}",
            extra={
                "callid": session.get("call_id", "UNKNOWN"),
                "testid": session.get("test_execution_row_id", "UNKNOWN"),
            },
        )
        return None

    output = result.stdout.strip()

    logger.info(
        f"External MOS result for node {node_id}: {output}",
        extra={
            "callid": session.get("call_id", "UNKNOWN"),
            "testid": session.get("test_execution_row_id", "UNKNOWN"),
        },
    )

    # Accept either a plain number or JSON-like/string output.
    try:
        return float(output)
    except (TypeError, ValueError):
        return output


def stop_node_recording(session, run_mos=True):
    """
    Close the active node recording, convert it to WAV, and optionally run MOS.
    """
    recording = session.get("recording_session")

    if not recording or not recording.get("active"):
        return None

    recording["active"] = False

    raw_file = recording.get("file")
    if raw_file is not None:
        try:
            raw_file.flush()
            raw_file.close()
        except Exception:
            pass

    raw_path = recording.get("raw_path")
    wav_path = recording.get("wav_path")
    node_id = recording.get("node_id", "unknown")
    bytes_written = recording.get("bytes_written", 0)

    if not raw_path or bytes_written == 0:
        logger.info(
            f"Node recording skipped; no RTP audio for node {node_id}",
            extra={
                "callid": session.get("call_id", "UNKNOWN"),
                "testid": session.get("test_execution_row_id", "UNKNOWN"),
            },
        )

        try:
            if raw_path and os.path.exists(raw_path):
                os.remove(raw_path)
        except OSError:
            pass

        return None

    try:
        _convert_ulaw_to_wav(raw_path, wav_path)
    except Exception:
        logger.exception(
            f"Node WAV conversion failed for {raw_path}",
            extra={
                "callid": session.get("call_id", "UNKNOWN"),
                "testid": session.get("test_execution_row_id", "UNKNOWN"),
            },
        )
        return None
    finally:
        try:
            if raw_path and os.path.exists(raw_path):
                os.remove(raw_path)
        except OSError:
            pass

    duration_seconds = bytes_written / 8000.0

    mos_result = None
    if run_mos:
        if PHONEXIA_ENABLED:
            mos_score = get_phoneix_pesq_score(session, wav_path)
        else:
            mos_result = run_external_mos(
                wav_path,
                session,
                node_id,
            )
            mos_score = 4
        

    result = {
        "node_id": node_id,
        "wav_path": wav_path,
        "duration_seconds": round(duration_seconds, 3),
        "mos_result": mos_score,
    }

    session["last_node_recording"] = result
    session.setdefault("node_recordings", []).append(result)

    logger.info(
        f"Node recording completed: {result}",
        extra={
            "callid": session.get("call_id", "UNKNOWN"),
            "testid": session.get("test_execution_row_id", "UNKNOWN"),
        },
    )

    return result


def switch_node_recording(session, next_node_id):
    """
    Finalize the current node recording and immediately start the next node.
    """
    completed = stop_node_recording(session, run_mos=True)
    start_node_recording(session, next_node_id)
    return completed


################################################
# RTP Receiver (GLOBAL)
################################################
def rtp_receiver():
    global rtp_socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    sock.bind(("0.0.0.0", RTP_PORT))
    rtp_socket = sock

    logger.info(
        f"RTP receiver started on 0.0.0.0:{RTP_PORT}",
        extra={"callid": "SYSTEM", "testid": "SYSTEM"}
    )

    while not shutdown_event.is_set():
        try:
            packet, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            if shutdown_event.is_set():
                break
            raise

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

        #Yet not found session
        if not session:
            continue
        
        #At this stage we have the session object
        if session["node_transition"] == 1:
            session["node_transition"] = 0
            session["transcript"] = ""		
            logger.info(f"Next Node Audio", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        try:
            if session["bargein_timeout"] > 0 and session["bargein_timer_start"] == 0:
                logger.info(f"BargeIn to be handled for {session['bargein_timeout']} Seconds", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
                session["bargein_timer_start"] = time.monotonic() #Starting Bargein Timer
                session["bargein_timer"] = 0
                # session["bargein_timeout"] = 0 #Resetting the Bargein Timeout
            elif session["bargein_timer_start"] > 0:
                bargein_lapsed_time = (time.monotonic() - session["bargein_timer_start"])
                # logger.info(f"Bargein Lapsed Time: {bargein_lapsed_time} Seconds", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
                if bargein_lapsed_time >= session["bargein_timeout"]:

                    session["bargein_timeout"] = 0
                    session["bargein_timer_start"] = 0
                    session["bargein_timer"] = 0
                    session["stt_accept_audio"] = False

                    logger.info(
                        f"Bargein Timed Out {session['bargein_timeout']}: "
                        f"Triggering BargeIn after {bargein_lapsed_time:.3f} Seconds",
                        extra={"callid": session.get("call_id", "UNKNOWN"),
                               "testid": session.get("test_execution_row_id", "UNKNOWN")}
                    )

                    # Capture only transcription received before the barge-in point.
                    process_transcript = session.get("transcript", "").strip()

                    # Completely invalidate the current AWS stream. Any late
                    # partial/final result from it will fail _active_session().
                    if not reset_stt_after_bargein(session, session["caller_channel"]):
                        logger.info(
                            "Failed to reset AWS STT after barge-in",
                            extra={"callid": session.get("call_id", "UNKNOWN"),
                                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
                        )
                        continue

                    sound_file = f"bargein_{int(time.time() * 1000)}"
                    base_path = f"{SOUNDS_DIR}/{sound_file}"
                    voicebot_channel_id = session["voicebot_channel"]

                    logger.info(
                        f"Starting node processing after barge-in; transcript={process_transcript!r}",
                        extra={"callid": session.get("call_id", "UNKNOWN"),
                               "testid": session.get("test_execution_row_id", "UNKNOWN")}
                    )

                    threading.Thread(
                        target=process_nodedata,
                        args=(
                            process_transcript,
                            base_path,
                            voicebot_channel_id,
                            session["caller_channel"]
                        ),
                        daemon=True
                    ).start()

                    # try:
                    #     if not ensure_node_processing_worker():
                    #         raise RuntimeError(
                    #             "Node processing worker is not running"
                    #         )

                    #     node_processing_queue.put_nowait(
                    #         (
                    #             process_transcript,
                    #             base_path,
                    #             voicebot_channel_id,
                    #             channel_id
                    #         )
                    #     )

                    #     logger.info(
                    #         f"Queued barge-in node processing; "
                    #         f"queue_size={node_processing_queue.qsize()}, "
                    #         f"worker_alive="
                    #         f"{node_processing_worker_thread.is_alive()}",
                    #         extra={"callid": session.get("call_id", "UNKNOWN"),
                    #                "testid": session.get("test_execution_row_id", "UNKNOWN")}
                    #     )
                    # except Exception:
                    #     logger.exception(
                    #         "Failed to queue node processing after barge-in",
                    #         extra={"callid": session.get("call_id", "UNKNOWN"),
                    #                "testid": session.get("test_execution_row_id", "UNKNOWN")}
                    #     )

                    # Do not continue here. The current RTP packet is routed to
                    # the newly created stream below via send_audio_to_stt().
                else:
                    session["bargein_timer"] += time.monotonic()-session["bargein_timer"]
                    # logger.info(f"Bargein Timer : {session['bargein_timer']}, Bargein Timer Start: {session['bargein_timer_start']}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        except Exception as e:              
            print(f"An error occurred: {e}")
        
        if is_speech_packet(payload):
            #print("Speech packet")

            if session["next_prompt_heard"] == 0:
                # print("Next Prompt Heard for this Session:", session)
                session["next_prompt_heard"] = 1

            if session["bot_rtp_start_time"] > 0:
                logger.info(f"Bot RTP Start Time was: {session['bot_rtp_start_time']}, Current Time is: {time.monotonic()}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

                session["bot_last_rtp_time"] = time.monotonic()
                current_latency = round(time.monotonic() - session["bot_rtp_start_time"],2)

                # if session["bot_avg_latency"] > 0:
                #     session["bot_avg_latency"] = (session["bot_avg_latency"] + current_latency)/2
                # else:
                  
                session["bot_avg_latency"] = current_latency
                logger.info(f"Current Latency: {current_latency}, Bot Average Latency Recorded: {session['bot_avg_latency']}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
                session["bot_rtp_start_time"] = 0
        # else:
        #     print("Silence packet")

        # ws = session["stt_ws"]

        # if ws:
        #     try:
        #         # print("Sending payload to ws: ", ws)
        #         ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
        #     except:
        #         pass
        # else:
        #     # print("No stt ws available to send audio")
        #     logger.info(f"No stt ws available to send audio", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        write_node_audio(session, payload)
        send_audio_to_stt(session, payload)

    try:
        sock.close()
    except OSError:
        pass

    rtp_socket = None

    logger.info(
        "RTP receiver stopped",
        extra={"callid": "SYSTEM", "testid": "SYSTEM"}
    )


################################################
# Deepgram STT
################################################

# def start_stt(channel_id):

#     url = "wss://api.deepgram.com/v1/listen?encoding=mulaw&sample_rate=8000&model=nova-3&language=multi"
#     #url = "wss://api.deepgram.com/v1/listen?encoding=mulaw&sample_rate=8000"


#     headers = [
#         f"Authorization: Token {DEEPGRAM_API_KEY}"
#     ]

#     ws = websocket.WebSocketApp(
#         url,
#         header=headers,
#         on_message=lambda ws, msg: on_stt(ws, msg, channel_id)
#     )

#     threading.Thread(target=ws.run_forever, daemon=True).start()

#     return ws

################################################
# AWS Transcribe STT
################################################

class AwsTranscriptHandler(TranscriptResultStreamHandler):
    def __init__(self, output_stream, channel_id, stt_generation):
        super().__init__(output_stream)
        self.channel_id = channel_id
        self.stt_generation = stt_generation
        self._final_segments = []
        self._last_result_id = None
        self._finalize_task = None

    def _active_session(self):
        session = call_sessions.get(self.channel_id)
        if not session:
            return None
        if session.get("stt_generation") != self.stt_generation:
            return None
        return session

    def _combined_transcript(self, partial_transcript=""):
        parts = [*self._final_segments]
        if partial_transcript:
            parts.append(partial_transcript)
        return " ".join(parts).strip()

    def _restart_finalize_timer(self):
        if not self._final_segments:
            return

        if self._finalize_task and not self._finalize_task.done():
            self._finalize_task.cancel()

        self._finalize_task = asyncio.create_task(
            self._finalize_after_inactivity()
        )

    async def _finalize_after_inactivity(self):
        try:
            await asyncio.sleep(AWS_PROMPT_IDLE_TIMEOUT_SECONDS)
            self._flush_final_segments()
        except asyncio.CancelledError:
            # A new partial/final result arrived, so the prompt is continuing.
            raise
        finally:
            if self._finalize_task is asyncio.current_task():
                self._finalize_task = None

    def _flush_final_segments(self):
        process_transcript = self._combined_transcript()
        if not process_transcript:
            return

        session = self._active_session()
        result_id = self._last_result_id or "aws_transcript"

        self._final_segments = []
        self._last_result_id = None

        if not session:
            return

        session["transcript"] = ""

        logger.info(
            f"on_stt: Prompt finalized after "
            f"{AWS_PROMPT_IDLE_TIMEOUT_SECONDS:.2f}s inactivity: "
            f"{process_transcript}",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )

        base_path = f"{SOUNDS_DIR}/{result_id}"
        voicebot_channel_id = session["voicebot_channel"]

        threading.Thread(
            target=process_nodedata,
            args=(process_transcript, base_path, voicebot_channel_id, self.channel_id)
        ).start()

    async def flush_pending(self):
        """Flush buffered final segments when the AWS stream is closing."""
        pending_task = self._finalize_task
        self._finalize_task = None

        if pending_task and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass

        self._flush_final_segments()

    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        session = self._active_session()
        if not session:
            return

        for result in transcript_event.transcript.results:
            if not result.alternatives:
                continue

            transcript = result.alternatives[0].transcript.strip()
            if not transcript:
                continue

            if result.is_partial:
                
                logger.info(f"on_stt: Transcript:{transcript}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

                session["transcript"] = self._combined_transcript(transcript)

                # Partial speech after a final segment means that the prompt is
                # still continuing.  Restart the inactivity window.
                self._restart_finalize_timer()

                # write_transcript(self.channel_id, "Caller", transcript)

                # Let's check the interim Transcript to match any test keywords and barge-in
                """
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
                """
            else:
                self._final_segments.append(transcript)
                self._last_result_id = result.result_id
                session["transcript"] = self._combined_transcript()

                logger.info(
                    f"on_stt: AWS segment final; waiting for prompt inactivity: "
                    f"{session['transcript']}",
                    extra={"callid": session.get("call_id", "UNKNOWN"),
                           "testid": session.get("test_execution_row_id", "UNKNOWN")}
                )

                self._restart_finalize_timer()

            """
            # Similar to Deepgram interim/final handling
            if result.is_partial:
                logger.info(
                    f"AWS STT partial: {transcript}",
                    extra={"callid": session.get("call_id", "UNKNOWN"),
                           "testid": session.get("test_execution_row_id", "UNKNOWN")}
                )
            else:
                logger.info(
                    f"AWS STT final: {transcript}",
                    extra={"callid": session.get("call_id", "UNKNOWN"),
                           "testid": session.get("test_execution_row_id", "UNKNOWN")}
                )

                session["transcript"] += " " + transcript
            """

def parse_transcribe_language_options(language_ids):
    """Normalize a node's comma-separated AWS language codes."""
    if isinstance(language_ids, str):
        raw_options = language_ids.split(",")
    elif language_ids:
        raw_options = language_ids
    else:
        raw_options = []

    language_options = []
    for option in raw_options:
        language_code = str(option).strip().strip("\"'")
        if language_code and language_code not in language_options:
            language_options.append(language_code)

    if not language_options:
        raise ValueError("At least one AWS Transcribe language ID is required")

    # AWS language identification accepts only one dialect per language.
    language_dialects = {}
    for language_code in language_options:
        language = language_code.split("-", 1)[0].lower()
        previous_dialect = language_dialects.get(language)
        if previous_dialect and previous_dialect != language_code:
            raise ValueError(
                "AWS Transcribe language identification cannot combine "
                f"{previous_dialect} and {language_code} in one stream"
            )
        language_dialects[language] = language_code

    return language_options


def build_transcribe_settings(language_options):
    settings = {
        "media_sample_rate_hz": 8000,
        "media_encoding": "pcm",
    }

    if len(language_options) == 1:
        settings["language_code"] = language_options[0]
    else:
        settings.update({
            "language_code": None,
            "identify_multiple_languages": True,
            "language_options": language_options,
            "preferred_language": language_options[0],
        })

    return settings


async def _discard_audio_and_stop(audio_queue):
    # Discard audio already queued for the interrupted/obsolete prompt.
    while True:
        try:
            audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    await audio_queue.put(None)


def stop_stt(stt):
    if not stt:
        return

    loop = stt.get("loop")
    audio_queue = stt.get("queue")
    if not loop or not audio_queue or loop.is_closed():
        return

    try:
        asyncio.run_coroutine_threadsafe(
            _discard_audio_and_stop(audio_queue),
            loop
        )
    except RuntimeError:
        pass


def start_stt(channel_id, language_ids, stt_generation):

    language_options = parse_transcribe_language_options(language_ids)

    audio_queue = asyncio.Queue(maxsize=100)

    loop = asyncio.new_event_loop()

    def runner():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                aws_transcribe_worker(
                    channel_id,
                    audio_queue,
                    language_options,
                    stt_generation,
                    ready_event
                )
            )
        except Exception as e:
            session = call_sessions.get(channel_id, {})
            logger.info(
                f"AWS STT worker failed: {e}",
                extra={"callid": session.get("call_id", "UNKNOWN"),
                       "testid": session.get("test_execution_row_id", "UNKNOWN")}
            )
        finally:
            loop.close()

    ready_event = threading.Event()

    worker_thread = threading.Thread(
        target=runner,
        daemon=True,
        name=f"aws-stt-{channel_id}-{stt_generation}"
    )
    worker_thread.start()

    return {
        "provider": "aws_transcribe",
        "loop": loop,
        "queue": audio_queue,
        "language_options": language_options,
        "generation": stt_generation,
        "ready_event": ready_event,
        "thread": worker_thread
    }


def configure_stt_for_node(session, channel_id, current_node, force_restart=False):
    language_options = parse_transcribe_language_options(
        current_node.language_ids
    )
    current_stt = session.get("stt_ws")

    if (
        not force_restart
        and current_stt
        and current_stt.get("language_options") == language_options
    ):
        return current_stt

    next_generation = session.get("stt_generation", 0) + 1
    new_stt = start_stt(
        channel_id,
        language_options,
        next_generation
    )

    # Route new audio first, then invalidate and stop the old stream.  The
    # generation check prevents late results from the old stream being used.
    session["stt_ws"] = new_stt
    session["stt_generation"] = next_generation
    stop_stt(current_stt)

    logger.info(
        f"AWS STT configured for node {current_node.node_id}: "
        f"{language_options}",
        extra={"callid": session.get("call_id", "UNKNOWN"),
               "testid": session.get("test_execution_row_id", "UNKNOWN")}
    )
    return new_stt


def ensure_stt_for_node(session, channel_id, current_node, force_restart=False):
    try:
        configure_stt_for_node(
            session,
            channel_id,
            current_node,
            force_restart=force_restart
        )
        return True
    except ValueError as e:
        logger.info(
            f"Invalid AWS STT language options for node "
            f"{current_node.node_id}: {e}",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
        hangup_channel(session, channel_id)
        return False

def reset_stt_for_node(session, channel_id, current_node):
    """Always start a fresh Amazon Transcribe stream for current_node."""
    session["stt_accept_audio"] = False
    session["transcript"] = ""

    try:
        new_stt = configure_stt_for_node(
            session,
            channel_id,
            current_node,
            force_restart=True
        )
    except Exception:
        logger.exception(
            f"Failed to reset AWS STT for node {current_node.node_id}",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
        return False

    ready_event = new_stt.get("ready_event")
    if not ready_event or not ready_event.wait(timeout=5.0):
        logger.info(
            f"AWS STT stream for node {current_node.node_id} "
            f"did not become ready within 5 seconds",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
        stop_stt(new_stt)
        return False

    session["stt_accept_audio"] = True
    session["bargein_stt_reset_pending"] = False
    session["prepared_stt_node_id"] = current_node.node_id

    logger.info(
        f"AWS STT reset for node {current_node.node_id}; "
        f"languages={new_stt.get('language_options')}, "
        f"generation={new_stt.get('generation')}",
        extra={"callid": session.get("call_id", "UNKNOWN"),
               "testid": session.get("test_execution_row_id", "UNKNOWN")}
    )
    return True


def reset_stt_after_bargein(session, channel_id):
    """
    Open the replacement AWS stream first, then atomically switch routing.

    This prevents a window where RTP is sent to a stream whose event loop is
    alive but whose Amazon bidirectional stream has not opened yet.
    """
    old_stt = session.get("stt_ws")

    if not old_stt:
        logger.info(
            "Cannot reset AWS STT after barge-in: current stream is missing",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
        return False

    language_options = old_stt.get("language_options")
    next_generation = session.get("stt_generation", 0) + 1

    try:
        new_stt = start_stt(
            channel_id,
            language_options,
            next_generation
        )
    except Exception:
        logger.exception(
            "Failed to create replacement AWS STT stream after barge-in",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
        return False

    ready_event = new_stt.get("ready_event")
    if not ready_event or not ready_event.wait(timeout=5.0):
        logger.info(
            "Replacement AWS STT stream did not become ready within 5 seconds",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
        stop_stt(new_stt)
        return False

    session["stt_ws"] = new_stt
    session["stt_generation"] = next_generation
    session["stt_accept_audio"] = True
    session["bargein_stt_reset_pending"] = True
    session["transcript"] = ""

    stop_stt(old_stt)

    logger.info(
        f"AWS STT switched after barge-in; generation={next_generation}, "
        f"languages={language_options}",
        extra={"callid": session.get("call_id", "UNKNOWN"),
               "testid": session.get("test_execution_row_id", "UNKNOWN")}
    )
    return True

def send_audio_to_stt(session, payload):
    if not session.get("stt_accept_audio", True):
        logger.info(
        f"STT Accept Audio is still False: NOT SENDING AUDIO TO STT ",
        extra={"callid": session.get("call_id", "UNKNOWN"),
               "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
        return

    stt = session.get("stt_ws")

    if not stt or stt["loop"].is_closed():
        return

    pcm = audioop.ulaw2lin(payload, 2)

    try:
        asyncio.run_coroutine_threadsafe(
            stt["queue"].put(pcm),
            stt["loop"]
        )
    except Exception as e:
        logger.info(
            f"AWS STT queue send failed: {e}",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )


async def aws_transcribe_worker(
    channel_id,
    audio_queue,
    language_options,
    stt_generation,
    ready_event
):
    client = TranscribeStreamingClient(region=AWS_REGION)

    transcription_settings = build_transcribe_settings(language_options)
    stream = await client.start_stream_transcription(**transcription_settings)
    ready_event.set()

    session = call_sessions.get(channel_id, {})
    logger.info(
        f"AWS STT stream ready; generation={stt_generation}",
        extra={"callid": session.get("call_id", "UNKNOWN"),
               "testid": session.get("test_execution_row_id", "UNKNOWN")}
    )

    async def send_audio():
        silence = b"\x00" * 320  # 20ms of 8kHz 16-bit PCM silence

        while True:
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                chunk = silence

            if chunk is None:
                break

            await stream.input_stream.send_audio_event(audio_chunk=chunk)

        await stream.input_stream.end_stream()
        
    handler = AwsTranscriptHandler(
        stream.output_stream,
        channel_id,
        stt_generation
    )

    async def receive_transcripts():
        try:
            await handler.handle_events()
        finally:
            await handler.flush_pending()

    await asyncio.gather(
        send_audio(),
        receive_transcripts()
    )


def on_stt(ws, message, channel_id):

    session = call_sessions[channel_id]

    data = json.loads(message)

    #print(time.time())
    # print("ON STT: Data Received", data)

    if "channel" not in data:
        print("ON STT: Channel not in data, returning", data)
        return

    transcript = data["channel"]["alternatives"][0]["transcript"]

    if transcript.strip():

        logger.info(f"on_stt: Transcript:{transcript}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        session["transcript"] += " " + transcript

        # write_transcript(channel_id, "Caller", transcript)

        # Let's check the interim Transcript to match any test keywords and barge-in
        """
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
        """

    elif session["transcript"]:

        logger.info(f"on_stt: Current Transcript:{session['transcript']}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

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
        logger.info(f"on_stt: Skipping Action", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
        
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

def ensure_node_processing_worker():
    """Start the node worker once, even when this module is imported."""
    global node_processing_worker_thread

    with node_processing_worker_lock:
        if (
            node_processing_worker_thread
            and node_processing_worker_thread.is_alive()
        ):
            return True

        node_processing_worker_thread = threading.Thread(
            target=node_processing_worker,
            daemon=True,
            name="verifypro-node-worker"
        )
        node_processing_worker_thread.start()

    # Give the new worker a brief opportunity to enter its queue loop.
    time.sleep(0.01)

    if not node_processing_worker_thread.is_alive():
        logger.info(
            "Node processing worker failed to start",
            extra={"callid": "SYSTEM", "testid": "SYSTEM"}
        )
        return False

    return True


def node_processing_worker():
    print("Node processing worker started", flush=True)

    logger.info(
        "Node processing worker started",
        extra={"callid": "SYSTEM", "testid": "SYSTEM"}
    )

    while True:
        job = node_processing_queue.get()

        if job is None:
            node_processing_queue.task_done()
            return

        args = job
        parent_channel_id = args[3] if len(args) > 3 else None
        session = call_sessions.get(parent_channel_id, {})

        print(
            f"Node processing worker received job: channel={parent_channel_id}",
            flush=True
        )

        logger.info(
            f"Node processing worker received job; channel={parent_channel_id}",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )

        try:
            process_nodedata_safe(*args)
        except Exception:
            logger.exception(
                "Node processing worker failed",
                extra={"callid": session.get("call_id", "UNKNOWN"),
                       "testid": session.get("test_execution_row_id", "UNKNOWN")}
            )
        finally:
            node_processing_queue.task_done()


def process_nodedata_safe(*args):
    parent_channel_id = args[3] if len(args) > 3 else None
    session = call_sessions.get(parent_channel_id, {})

    print(
        f"Entered process_nodedata_safe after barge-in: "
        f"channel={parent_channel_id}",
        flush=True
    )

    logger.info(
        "Entered process_nodedata thread after barge-in",
        extra={"callid": session.get("call_id", "UNKNOWN"),
               "testid": session.get("test_execution_row_id", "UNKNOWN")}
    )

    try:
        process_nodedata(*args)
        logger.info(
            "process_nodedata thread completed after barge-in",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
    except Exception:
        logger.exception(
            "Unhandled exception in process_nodedata after barge-in",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )


WAIT_FOR_HANGUP_TAG = "{WaitForHangup}"
WAIT_FOR_HANGUP_RE = re.compile(r"^\{waitforhangup\}$", re.IGNORECASE)


def is_wait_for_hangup_node(node):
    expected_text = getattr(node, "expected_text", None) if node else None
    return bool(WAIT_FOR_HANGUP_RE.fullmatch(expected_text or ""))


def _record_wait_for_hangup_result(session, elapsed, result_code, reason):
    node_id = session.get("wait_for_hangup_node_id")
    node = session["test_case"].get_node(node_id)
    match_result = validate_prompts(WAIT_FOR_HANGUP_TAG, "")

    session["node_result"] = {
        "node_id": node_id,
        "expected_text": WAIT_FOR_HANGUP_TAG,
        "actual_text": "",
        "transcription_match": 100.0,
        "response_time": round(elapsed, 3),
        "test_result": json.dumps(asdict(match_result), ensure_ascii=False),
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node_test_response_time": result_code,
        "node_test_match_percentage": NODE_TEST_SUCCESS,
        "node_test_result": result_code,
        "rtp_stats": None,
        "mos": 0.0,
        "wait_for_hangup_reason": reason,
    }
    session.setdefault("node_results", []).append(dict(session["node_result"]))
    record_test_history(session)


def complete_wait_for_hangup(session, called_party_hung_up):
    lock = session.setdefault("wait_for_hangup_lock", threading.Lock())
    with lock:
        if not session.get("wait_for_hangup_active"):
            return False
        elapsed = max(0.0, time.monotonic() - session["wait_for_hangup_started_at"])
        minor = session["wait_for_hangup_minor"]
        major = session["wait_for_hangup_major"]

        if called_party_hung_up and elapsed <= minor:
            code, reason = NODE_TEST_SUCCESS, "called party hung up within minor threshold"
        elif called_party_hung_up and elapsed <= major:
            code, reason = NODE_TEST_SATISFACTORY, "called party hung up between minor and major thresholds"
        else:
            code, reason = NODE_TEST_FAILED, "major threshold expired before called-party hangup"

        session["wait_for_hangup_active"] = False
        session["wait_for_hangup_completed"] = True
        session["wait_for_hangup_result"] = code
        _record_wait_for_hangup_result(session, elapsed, code, reason)

        logger.info(
            f"WaitForHangup completed after {elapsed:.3f}s; result={code}; reason={reason}",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
        return True


def _wait_for_hangup_watchdog(session, channel_id):
    deadline = session["wait_for_hangup_started_at"] + session["wait_for_hangup_major"]
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    if complete_wait_for_hangup(session, called_party_hung_up=False):
        logger.info(
            "WaitForHangup major threshold exceeded; Platform is terminating the call",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )
        hangup_channel(session, channel_id)


def begin_wait_for_hangup(session, node, channel_id):
    if not is_wait_for_hangup_node(node):
        return False

    # No STT, PSST, or step recording applies to this control step.
    session["stt_accept_audio"] = False
    stop_stt(session.get("stt_ws"))
    session["stt_ws"] = None
    session["stt_generation"] = session.get("stt_generation", 0) + 1
    stop_node_recording(session, run_mos=False)

    session["current_node_id"] = node.node_id
    session["expected_text"] = WAIT_FOR_HANGUP_TAG
    session["wait_for_hangup_node_id"] = node.node_id
    session["wait_for_hangup_minor"] = float(node.minor_threshold_time)
    session["wait_for_hangup_major"] = float(node.major_threshold_time)
    session["wait_for_hangup_started_at"] = time.monotonic()
    session.setdefault("wait_for_hangup_lock", threading.Lock())
    session["wait_for_hangup_active"] = True
    session["wait_for_hangup_completed"] = False
    session["node_transition"] = 0

    logger.info(
        f"WaitForHangup started for node {node.node_id}; "
        f"minor={session['wait_for_hangup_minor']}s, "
        f"major={session['wait_for_hangup_major']}s",
        extra={"callid": session.get("call_id", "UNKNOWN"),
               "testid": session.get("test_execution_row_id", "UNKNOWN")}
    )

    threading.Thread(
        target=_wait_for_hangup_watchdog,
        args=(session, channel_id),
        daemon=True,
        name=f"wait-hangup-{session.get('call_id', 'unknown')}"
    ).start()
    return True


def process_nodedata(transcript_text, base_filename, channel_id, parent_channel_id):

    #expected_prompt = "welcome to the {choice x=automated:1|manual:2} ivr testing system please listen carefully to the following options press {Digits} one for account information press two for technical support press three for payment services press nine to repeat this menu press zero to exit{*}"
    #nodedata_match = match_nodedata(expected_prompt, transcript_text)

    #print("Tag Match Result: ", result)      

    session = call_sessions[parent_channel_id]

    if session:
        # Resetting next_prompt_heard session variable
        session["next_prompt_heard"] = 0

        # Resetting bargein_timeout session variable
        session["bargein_timeout"] = 0
        session["bargein_timer_start"] = 0

        logger.info(f"Session Captured Variables:{session['captured_variables']}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
        # logger.info(f"Session Object:{session}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        session["ivr_step_number"] += 1

        # Set Test Data Node Data and Result

        if session["current_node_id"] is None:
            current_node = session["test_case"].get_start_node()
        elif session["current_node_id"] == "EOF":
            logger.info(f"End Of Test Case Detected, Ending the TestCall", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
            hangup_channel(session, channel_id)
            return
        else:
            current_node = session["test_case"].get_node(session["current_node_id"])

        session["current_node_id"] = current_node.node_id
        session["expected_text"] = current_node.expected_text

        if begin_wait_for_hangup(session, current_node, channel_id):
            return

        completed_node_recording = stop_node_recording(
            session,
            run_mos=True
        )

        # Replace {$variable} with value for Expected Text
        for var_name, var_value in session["captured_variables"].items():
            # placeholder = f"${{{var_name}}}" # to match variable like ${x}
            placeholder = f"{{${var_name}}}" # to match variable like {$x}

            if placeholder in current_node.expected_text:
                current_node.expected_text = current_node.expected_text.replace(placeholder, str(var_value))

        # For debugging print the node test details
        print_node_test_details(session, current_node)
        
        # Validate Expected_Text vs Actual_Text
        logger.info(f"Expected Text: {current_node.expected_text}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
        logger.info(f"Actual Text: {transcript_text}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        result = validate_prompts(current_node.expected_text,transcript_text)

        if (result.captured_variables and any(result.captured_variables.values())):
            session["captured_variables"].update(result.captured_variables)

        # Calculate Node Test Result
        # Calculate Node Test Response Result

        latency = float(session["bot_avg_latency"])

        if latency < current_node.minor_threshold_time:
            node_test_response_time = NODE_TEST_SUCCESS
        elif latency <= current_node.major_threshold_time:
            node_test_response_time = NODE_TEST_SATISFACTORY
        else:
            node_test_response_time = NODE_TEST_FAILED

        # Calculate Node Test Match Result

        confidence = float(result.word_match_percentage)

        if confidence < current_node.minor_confidence_level:
            node_test_match_percentage = NODE_TEST_FAILED
        elif confidence <= current_node.major_confidence_level:
            node_test_match_percentage = NODE_TEST_SATISFACTORY
        else:
            node_test_match_percentage = NODE_TEST_SUCCESS

        node_test_result = min(node_test_response_time, node_test_match_percentage)

        logger.info(f"Node Test Response Result: {latency}|{node_test_response_time}, Node Test Match Result: {confidence}|{node_test_match_percentage}, Node Test Final Result: {node_test_result} ", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        session["node_result"] = {
            "node_id": session["current_node_id"],
            "expected_text": session["expected_text"],
            "actual_text": transcript_text,
            "transcription_match": result.word_match_percentage,#result.match_percentage,
            "response_time": session["bot_avg_latency"],
            "test_result": json.dumps(asdict(result),ensure_ascii=False),
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), #"2026-06-22T18:21:55Z"
            "node_test_response_time": node_test_response_time,
            "node_test_match_percentage": node_test_match_percentage,
            "node_test_result": node_test_result 
        }

        # Fetch on-demand RTP stats from ARI and calculate RTP-based estimated MOS.
        # Use the actual voicebot media channel, not the ExternalMedia channel.
        rtp_stats = get_rtp_stats(session, session.get("voicebot_channel"))
        session["node_result"]["rtp_stats"] = rtp_stats

        external_mos = None
        if completed_node_recording:
            external_mos = completed_node_recording.get("mos_result")
            session["node_result"]["recording_file"] = (
                completed_node_recording.get("wav_path")
            )
            session["node_result"]["recording_duration"] = (
                completed_node_recording.get("duration_seconds")
            )

        if isinstance(external_mos, (int, float)):
            session["node_result"]["mos"] = round(float(external_mos), 2)
        else:
            session["node_result"]["mos"] = estimate_mos_from_rtp_stats(
                rtp_stats
            )

        logger.info(
            f"RTP Stats: {rtp_stats}, Estimated MOS: {session['node_result']['mos']}",
            extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")}
        )

        # Retain every node's classifications for the call-level majority
        # calculation performed when the call ends.
        session.setdefault("node_results", []).append(
            dict(session["node_result"])
        )

        # Insert Test History Data into the DB
        record_test_history(session)

        try:
            if current_node.persona:
                for language_code in current_node.persona:
                    if current_node.persona[language_code]["VI"]:
                        logger.info(f"Persona: {language_code}: VoiceID: {current_node.persona[language_code]['VI']}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

            # Let's replace the Choice Tag Captured Variables being used in the Reply With Text with corresponding Values
            if current_node.action_to_take:
                action_value = str(current_node.action_to_take.value)

                # Replace {$variable} with value for Action Item
                for var_name, var_value in session["captured_variables"].items():
                    # placeholder = f"${{{var_name}}}" # to match variable like ${x}
                    placeholder = f"{{${var_name}}}" # to match variable like {$x}

                    if placeholder in action_value:
                        action_value = action_value.replace(placeholder, str(var_value))

                current_node.action_to_take.value = action_value
                logger.info(f"Updated action value: {current_node.action_to_take.value}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

            # Looks like a Promopt which does not expect any Reply / Input    
            else:
                logger.info(f"No Action to take: Let's Skip this Node", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

                # Evaluate the Next Node ID
                if current_node.transitions:
                    next_node_id = get_transition_node_id(current_node.transitions, "on_success")

                if next_node_id:
                    logger.info(f"Next Node ID: {next_node_id}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
                    current_node = session["test_case"].get_node(next_node_id)
                    if current_node:
                        if begin_wait_for_hangup(session, current_node, channel_id):
                            return
                        if not reset_stt_for_node(
                            session,
                            parent_channel_id,
                            current_node
                        ):
                            return
                        session["current_node_id"] = current_node.node_id
                        # Next node audio should be barged in after x seconds
                        if current_node and current_node.bargein_timeout is not None:
                            if current_node.bargein_timeout > 0:
                                session["bargein_timeout"] = current_node.bargein_timeout
                    else:
                        session["current_node_id"] = "EOF" #Set End Of Flow here
                        # End the Test Case
                        logger.info(f"End Of Test Case Detected, Ending the TestCall", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
                        hangup_channel(session, channel_id)
                        return

                session["node_transition"] = 1

                return

        except Exception:
            logger.exception(
                "Error while preparing node action or transition",
                extra={"callid": session.get("call_id", "UNKNOWN"),
                       "testid": session.get("test_execution_row_id", "UNKNOWN")}
            )

        # Response generation based on Node Data
        logger.info(f"IVR Step Number:{session['ivr_step_number']}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        # Prepare the STT stream for the prompt expected after this action.
        # This happens before DTMF/TTS/Speech triggers the next IVR response so
        # its first audio packets are queued for the correctly configured stream.
        predicted_next_node_id = None
        if current_node.transitions:
            predicted_next_node_id = get_transition_node_id(
                current_node.transitions,
                "on_success"
            )
        predicted_next_node = None
        if predicted_next_node_id:
            predicted_next_node = session["test_case"].get_node(
                predicted_next_node_id
            )

        logger.info(
            f"Predicted next node before action: {predicted_next_node_id}",
            extra={"callid": session.get("call_id", "UNKNOWN"),
                   "testid": session.get("test_execution_row_id", "UNKNOWN")}
        )

        if predicted_next_node and not is_wait_for_hangup_node(predicted_next_node):
            if not reset_stt_for_node(
                session,
                parent_channel_id,
                predicted_next_node
            ):
                return

            start_node_recording(
                session,
                predicted_next_node.node_id
            )

        if current_node.action_to_take.inject_type == "DTMF":
            send_dtmf(session, channel_id, current_node.action_to_take.value)

        elif current_node.action_to_take.inject_type == "Speech":
            play_audio(channel_id, current_node.action_to_take.value)

        elif current_node.action_to_take.inject_type == "TTS":
            reply_path = synthesize_speech_polly(session, current_node.action_to_take.value, base_filename, language_code, current_node.persona[language_code]["VI"])
            play_audio(channel_id, base_filename)

        elif current_node.action_to_take.inject_type == "Silence":
            min_val, max_val = map(int,current_node.action_to_take.value.split("-"))
            random_silence = random.randint(min_val, max_val)
            play_silence(session,channel_id, random_silence)
            # play_silence_duration(channel_id, random_silence)

        logger.info(f"NEXT TRANSITIONS:{current_node.transitions}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        # Wait for next_prompt_heard till timeout
        result = wait_next_response_till_timeout(session, current_node.timeout)

        if result["status"] == "timeout":
            logger.info(f"Prompt timeout occurred after DTMF", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
            if current_node.transitions:
                next_node_id = get_transition_node_id(current_node.transitions, "on_timeout")
        else:
            logger.info(f"Prompt heard within timeout", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
            
            # Evaluate the Next Node ID for Success
            if current_node.transitions:
                next_node_id = get_transition_node_id(current_node.transitions, "on_success")


        if next_node_id:
            logger.info(f"Next Node ID:{next_node_id}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
           
            current_node = session["test_case"].get_node(next_node_id)
            if current_node:
                if begin_wait_for_hangup(session, current_node, channel_id):
                    return
                if session.get("prepared_stt_node_id") != current_node.node_id:
                    if not reset_stt_for_node(
                        session,
                        parent_channel_id,
                        current_node
                    ):
                        return

                session["prepared_stt_node_id"] = None
                session["current_node_id"] = current_node.node_id

                active_recording = session.get("recording_session", {})
                if (
                    not active_recording.get("active")
                    or active_recording.get("node_id")
                        != _safe_recording_component(current_node.node_id)
                ):
                    start_node_recording(
                        session,
                        current_node.node_id
                    )

                # Next node audio should be barged in after x seconds
                if current_node and current_node.bargein_timeout is not None:
                    if current_node.bargein_timeout > 0:
                        session["bargein_timeout"] = current_node.bargein_timeout
            else:
                session["current_node_id"] = "EOF" #currently node_fail_hangup matches here as Node Information is not available
                # End the Test Case
                logger.info(f"End Of Test Case Detected, Ending the TestCall", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

                hangup_channel(session, channel_id)
                return

        session["node_transition"] = 1
    else:
        logger.info(f"Session Not Found for IVR Traversal", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

    return


def wait_next_response_till_timeout(session, timeout=8):

    logger.info(f"Waiting for {timeout} seconds for the Next Response", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

    start_time = time.time()

    while time.time() - start_time < timeout:

        if session["next_prompt_heard"] == 1:
            return {
                "status": "success",
                "prompt": "Prompt Heard within {timeout} seconds",
                "time": time.time() - start_time
            }

        time.sleep(0.5)

    return {
        "status": "timeout",
        "message": f"No prompt heard within {timeout} seconds",
        "time": time.time() - start_time
    }

# def match_nodedata(expected_prompt, actual_prompt):

#     # Entire sentence similarity
#     match_percent = similarity(
#         expected_prompt,
#         actual_prompt
#     )

#     print("\n----- MATCH DETAILS -----")
#     print("Expected :", expected_prompt)
#     print("Actual   :", actual_prompt)
#     print("Match %  :", match_percent)

#     return False


# def similarity(a, b):

#     return round(
#         SequenceMatcher(
#             None,
#             a.lower(),
#             b.lower()
#         ).ratio() * 100,
#         2
#     )

def print_node_test_details(session, current_node):

    logger.info(f"Language IDs: {current_node.language_ids}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
    logger.info(f"Persona: {current_node.persona}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
    logger.info(f"Minor Threshold Time: {current_node.minor_threshold_time}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
    logger.info(f"Major Threshold Time: {current_node.major_threshold_time}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
    logger.info(f"Minor Confidence Level: {current_node.minor_confidence_level}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
    logger.info(f"Major Confidence Level: {current_node.major_confidence_level}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

    

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

    logger.info(f"TTS Response: {response}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

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
# PLAY AUDIO TO THE VOICEBOT CHANNEL (ASYNCHRONOUS)
################################################

# def play_audio(channel_id, sound):

#     print("Audio playback for DTMF check:", sound)

#     r= requests.post(
#         f"{ASTERISK_URL}/ari/channels/{channel_id}/play",
#         auth=(ARI_USER, ARI_PASS),
#         json={"media": f"sound:{sound}"}
#     )

#     print("Audio response:", r)

################################################
# PLAY AUDIO TO THE VOICEBOT CHANNEL (SYNCHRONOUS)
################################################

def play_audio(channel_id, sound):

    r = requests.post(
        f"{ASTERISK_URL}/ari/channels/{channel_id}/play",
        auth=(ARI_USER, ARI_PASS),
        json={"media": f"sound:{sound}"}
    )

    playback = r.json()
    playback_id = playback["id"]

    # print("Audio Playback started:", playback_id)

    while True:

        r = requests.get(
            f"{ASTERISK_URL}/ari/playbacks/{playback_id}",
            auth=(ARI_USER, ARI_PASS)
        )

        if r.status_code == 404:
            break

        state = r.json().get("state")

        if state == "done":
            break

        time.sleep(0.1)

    # print("Audio Playback finished")

    return


def play_silence(session, channel_id, seconds):

    print(f"Playing {seconds} seconds of silence")

    r = requests.post(
        f"{ASTERISK_URL}/ari/channels/{channel_id}/play",
        auth=(ARI_USER, ARI_PASS),
        json={"media": f"sound:silence/{seconds}"}
    )

    # print("Silence playback response:", r.status_code, r.text)

    playback = r.json()
    playback_id = playback["id"]

    # print("Silence Playback started:", playback_id)

    while True:

        r = requests.get(
            f"{ASTERISK_URL}/ari/playbacks/{playback_id}",
            auth=(ARI_USER, ARI_PASS)
        )

        if r.status_code == 404:
            break

        state = r.json().get("state")

        if state == "done":
            break

        time.sleep(0.1)

    # print("Silence Playback finished")

    return

def play_silence_duration(channel_id, seconds):

    # print(f"Playing {seconds} seconds of silence")

    # r = requests.post(
    #     f"{ASTERISK_URL}/ari/channels/{channel_id}/play",
    #     auth=(ARI_USER, ARI_PASS),
    #     json={"media": f"sound:silence/{seconds}"}
    # )

    # print("Silence period initiated: waiting for seconds:", seconds)

    time.sleep(seconds)

    # print("Silence period completed:")

# def play_audio_bridge(channel_id, sound):

#     session = call_sessions[channel_id]
#     bridge_id = session["bridge_id"]

#     print("Audio playback:", sound)

#     requests.post(
#         f"{ASTERISK_URL}/ari/bridges/{bridge_id}/play",
#         auth=(ARI_USER, ARI_PASS),
#         json={"media": f"sound:{sound}"}
#     )

def hangup_channel(session, channel_id):

    r = requests.delete(
        f"{ASTERISK_URL}/ari/channels/{channel_id}",
        auth=(ARI_USER, ARI_PASS)
    )

    logger.info(f"Hangup channel {channel_id}:{r.status_code}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})


    return r.status_code == 204
################################################
# PLAY DTMF TO THE VOICEBOT CHANNEL
################################################

def send_dtmf(session,channel_id, digits):

    logger.info(f"Sending DTMF : {digits}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

    url = f"{ASTERISK_URL}/ari/channels/{channel_id}/dtmf"

    params = {
        "dtmf": digits,
        "before": 0,
        "between": 200,
        "duration": 500
    }

    r = requests.post(url, params=params, auth=(ARI_USER, ARI_PASS))
    logger.info(f"Senddtmf Response:{r}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

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

def record_bridge(session, bridge_id):

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

    logger.info(
        f"Bridge recording started: {bridge_id}: {r.status_code} {r.text}",
        extra={
            "callid": session.get("call_id", "UNKNOWN"),
            "testid": session.get("test_execution_row_id", "UNKNOWN")
        }
    )

# def record_channel(channel_id):

#     r = requests.post(
#         f"{ASTERISK_URL}/ari/channels/{channel_id}/record",
#         auth=(ARI_USER, ARI_PASS),
#         params={
#             "name": f"rec_{channel_id}",
#             "format": "wav",
#             "maxDurationSeconds": 0,
#             "ifExists": "overwrite"
#         }
#     )

#     print("Channel recording started for the channel:", channel_id)

def record_channel(session, channel_id):
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

    logger.info(
        f"Channel recording start response for {channel_id}: {r.status_code} {r.text}",
        extra={
            "callid": session.get("call_id", "UNKNOWN"),
            "testid": session.get("test_execution_row_id", "UNKNOWN")
        }
    )

    # return r.status_code in (200, 201)

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


def dial_voicebot(channel_id, session):

    print("Originating Voicebot Channel on the Phone Number:", session["phone_to_dial"])

    requests.post(
        f"{ASTERISK_URL}/ari/channels",
        auth=(ARI_USER, ARI_PASS),
        params={
            "endpoint": f"PJSIP/{session['phone_to_dial']}@local-trunk",
            # "endpoint": f"Local/{session['phone_to_dial']}@ivr-test-final",
            "app": APP_NAME,
            "appArgs": channel_id,
            "callerId": f"Vpro Test <{session['cli']}>"
        }
    )


################################################
# ARI EVENT HANDLER
################################################

def on_ari(ws, message):

    event = json.loads(message)
    # print("on_ari: Incoming Event:", event)
    #print("on_ari: Event TYPE:", event["type"])

    if event["channel"]["name"].startswith("Local/") and event["channel"]["name"].endswith(";2"):
        print("This is ;2 leg, ignore")
        return
    
    parent_channel = None

    if event["type"] == "PlaybackFinished":
        # print("Event Type:", event["type"], " for Channel ID: ", event["playback"]["target_uri"].split(":")[1])
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

        print("Event Type:", event["type"], " for Channel ID: ", channel_id, " channel_name" ,channel_name)


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
        # print("on_ari: Exception: Some Error for the Event:", event)
        pass

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

        ended_channel_id = event["channel"]["id"]
        if (
            session.get("wait_for_hangup_active")
            and ended_channel_id == session.get("voicebot_channel")
        ):
            complete_wait_for_hangup(session, called_party_hung_up=True)

        # Clean-up Channel objects and data
        threading.Thread(
            target=handle_stasis_end,
            args=(session,),
            daemon=True
        ).start()
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
    
    # Moving the rest of the Stasis Start logic to a thread

    threading.Thread(
        target=handle_stasis_start,
        args=(event,channel_id, channel_name, parent_channel),
        daemon=True
    ).start()
    return

def handle_stasis_start(event, channel_id, channel_name, parent_channel):
    #  # Extension that was dialed
    context = event["channel"]["dialplan"]["context"]
    exten = event["channel"]["dialplan"]["exten"]

    print(f"Dialed Extension: {exten}")
    print(f"Context: {context}")

    if parent_channel is None:
                
        sip_call_id = channel_id #get_pjsip_call_id(channel_id)

        # Initiating Call Session Array
        call_sessions[channel_id] = {
            # Call-specific
            "call_id": sip_call_id,
            "sip_call_id": None,
            "cli": None,
            "dialed_extension": exten,
            "caller_channel": channel_id,
            "bridge_id": None,
            "voicebot_channel": None,
            "voicebot_channel_name": None,
            "external_media" : None,
            "transcript": "",
            "transcript_file": None,
            "stt_ws": None,
            "stt_generation": 0,
            "stt_accept_audio": True,
            "bargein_stt_reset_pending": False,
            "prepared_stt_node_id": None,
            "rtp_addr": None,

            # Voicebot Channel RTP specific
            "bot_dial_time":datetime.datetime.now(),
            "bot_connect_time":0,
            "bot_answer_duration":0,
            "bot_end_time":0,
            "call_ended_by":0,
            "bot_last_rtp_time":0,
            "bot_rtp_start_time":0,
            "bot_avg_latency":0,

            "keywords_matched":[],
            
            # Prompt engine variables
            "captured_variables": {},

            # Test execution
            "dialed_parameters_snapshot": None,
            "ivr_step_number":0,
            "test_execution_row_id": None, #Temporary Logic for Dynamic Value
            "phone_to_dial": "+18005550199",
            "current_node_id": None,
            "node_transition": 0,
            "execution_status": "RUNNING",
            "next_prompt_heard": 0,
            # "bargein" : 0,
            "initial_bargein_timeout": 0,
            "bargein_timeout": 0,
            "bargein_timer":0,
            "bargein_timer_start":0,

            # Node results
            "node_result": [],
            "node_results": [],
            "recording_session": {
                "call_id": sip_call_id,
                "active": False
            },
            "last_node_recording": None,
            "node_recordings": [],

            # Overall metrics
            "summary": {
                "total_nodes": 0,
                "passed_nodes": 0,
                "satisfactory_nodes": 0,
                "failed_nodes": 0,
                "node_test_response_time": None,
                "node_test_match_percentage": None,
                "overall_result": None
            },

            # Test Case Data
            "test_case": None
        }

        session = call_sessions[channel_id]
        logger.info(f"Caller Channel is Not Available: Fresh Call", extra={"callid":sip_call_id,"testid":session.get("test_execution_row_id", "UNKNOWN")})

        # Fetch the Test Data
        fetch_test_history(session)

        if session["test_execution_row_id"] is None:
            logger.info(f"Test Case could not be fetched", extra={"callid":sip_call_id, "testid":session.get("test_execution_row_id", "UNKNOWN")})
            hangup_channel(session, channel_id)
            return

        #Let's populate the test cases for this call
        test_case = load_test_case(session)
        if test_case is None:
            logger.info(f"Test Case could not be parsed", extra={"callid":sip_call_id, "testid":session.get("test_execution_row_id", "UNKNOWN")})
            hangup_channel(session, channel_id)
            return
        
        session["test_case"] = test_case
        if test_case.meta.phone_to_dial is not None:
            session["phone_to_dial"] = test_case.meta.phone_to_dial

        current_node = test_case.get_start_node()
        if current_node is None:
            logger.info(
                "Test case has no start node for AWS STT configuration",
                extra={"callid": session.get("call_id", "UNKNOWN"),
                       "testid": session.get("test_execution_row_id", "UNKNOWN")}
            )
            hangup_channel(session, channel_id)
            return

        if not ensure_stt_for_node(session, channel_id, current_node):
            return

        # start_node_recording(
        #     session,
        #     current_node.node_id
        # )

        # logger.info(f"Stasis Start", extra={"callid":sip_call_id, "testid":session.get("test_execution_row_id", "UNKNOWN")})
        #(f"[Exec:{session['test_execution_row_id']}] "

        # # Initiate beep audio to help other end learn our RTP Public IP Address
        # play_audio(channel_id, "beep")
    
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # transcript_file = f"/tmp/voicebot_{channel_id}_{timestamp}.txt"
        # open(transcript_file, "a").write(f"CALL START {timestamp}\n")
        # session["transcript_file"] = transcript_file

        bridge_id = create_bridge()
        session["bridge_id"] = bridge_id

        ext_media = create_external_media()
        session["external_media"] = ext_media

        add_channel_to_bridge(bridge_id, channel_id)
        add_channel_to_bridge(bridge_id, ext_media)

        # Initiate beep audio to help other end learn our RTP Public IP Address
        play_audio(channel_id, "beep")
        dial_voicebot(channel_id, session)

    else:
        session = call_sessions[parent_channel]
        logger.info(f"Caller Channel is Available", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        session["bargein_timeout"] = session["initial_bargein_timeout"]

        session["sip_call_id"] = get_pjsip_call_id(channel_id)

        current_node = session["test_case"].get_start_node()
        # experimental
        start_node_recording(
            session,
            current_node.node_id
        )

        session["bot_connect_time"] = datetime.datetime.now()
        session["bot_answer_duration"] = session["bot_connect_time"] - session["bot_dial_time"]
        session["pdd"] = round(((session["bot_connect_time"] - session["bot_dial_time"]).total_seconds() * 1000),2) # to be changed with 1xx response logic

        logger.info(f"Dial Time: {session['bot_dial_time']}, Connect Time: {session['bot_connect_time']}, PDD: {session['pdd']}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        # Update test row execution history table with Call Start Information
        update_test_history(session, 0)

        logger.info(f"Adding voicebot to bridge", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        session["voicebot_channel"] = channel_id
        session["voicebot_channel_name"] = channel_name.split(";")[0]

        add_channel_to_bridge(session["bridge_id"], channel_id)

        #Let's start recording the call
        
        threading.Thread(
        target=record_bridge,
            args=(session, session["bridge_id"]),
            daemon=True
        ).start()

        # record_channel(session, channel_id)

################################################
# LOAD THE TEST CASE FROM THE IVR TEST JSON
################################################

def load_test_case(session): #, json_file="ivr_test.json"):
    # Load and Parse ivr_test.json Test Case File
    test_case = None
    try:
        test_case = json_parse(session)
        # test_case = json_file_parse(session)
        if test_case:
            print("test case successfully loaded")
        else:
            return None
        
        # print("PHONE NUMBER: ", test_case.meta.phone_to_dial)
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

        # Capture BargeIn Setting for the First Node
        if current_node and current_node.bargein_timeout is not None:
            if current_node.bargein_timeout > 0:
                session["initial_bargein_timeout"] = current_node.bargein_timeout

        while current_node:

            next_node_id = None

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
    
            if current_node.transitions:
                next_node_id = get_transition_node_id(
                    current_node.transitions,
                    "on_success"
                )

            if next_node_id:
                current_node = test_case.get_node(next_node_id)
            else:
                break
            
    except Exception as e:
        logger.exception(
            f"Unable to load test case: {e}",
            extra={
                "callid": session.get("call_id", "UNKNOWN"),
                "testid": session.get("test_execution_row_id", "UNKNOWN"),
            },
        )
        return None

    return test_case

################################################
# FETCH TEST HISTORY FROM THE DATABASE
################################################
def fetch_test_history(session):

    sql = f"""
        SELECT vpterh.*,pcli.cli 
        FROM kcdb.verify_pro_test_execution_row_history AS vpterh
        LEFT OUTER JOIN provider_cli AS pcli
        ON pcli.id = vpterh.provider_cli_id
        WHERE vpterh.start_time = '0000-00-00 00:00:00'
        AND vpterh.end_time = '0000-00-00 00:00:00'
        AND vpterh.scheduled_on <= NOW()
        AND verify_pro_test_execution_id = 151
        AND vpterh.execution_status = 1
        AND vpterh.status = 1
        ORDER BY vpterh.scheduled_on ASC
        LIMIT 1
    """

    conn = get_db_conn()
    cursor = conn.cursor(dictionary=True)

    try:
        
        logger.info(f"Fetching Test Data : {sql}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        # 4. Execute the query
        cursor.execute(sql)
    
        # 5. COMMIT THE TRANSACTION (Crucial for INSERT, UPDATE, DELETE)
        # conn.commit()
    
        # 6. Get the auto-incremented ID (Optional)
        # logger.info(f"Successfully inserted. New Row ID: {cursor.lastrowid}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        rows = cursor.fetchall()

        if rows:
            for row in rows:
                logger.info(f"Fetched Test Data : ROW HISTORY ID : {row['id']} | CLI: {row['cli']}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
                session["test_execution_row_id"] = row["id"]
                session["cli"] = row["cli"]
                session["dialed_parameters_snapshot"] = row["dialed_parameters_snapshot"]
        else:
            logger.info(f"No Test Data Could be Fetched", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

    except Exception as err:
        print(f"Error: {err} for {sql}")
        conn.rollback() # Undo changes if an error happens

    finally:
        # 7. Close connections
        cursor.close()
        conn.close()


################################################
# RECORD NODE TEST HISTORY INTO THE DATABASE
################################################

################################################
# RTP STATS + MOS ESTIMATION
################################################

def get_rtp_stats(session, channel_id):

    if not channel_id:
        return None

    try:
        r = requests.get(
            f"{ASTERISK_URL}/ari/channels/{channel_id}/rtp_statistics",
            auth=(ARI_USER, ARI_PASS),
            timeout=2
        )

        if r.status_code == 200:
            return r.json()

        logger.info(
            f"RTP stats unavailable for channel {channel_id}: {r.status_code} {r.text}",
            extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")}
        )

    except Exception as e:
        logger.info(
            f"RTP stats error for channel {channel_id}: {e}",
            extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")}
        )

    return None


def _safe_float(value, default=0.0):

    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def estimate_mos_from_rtp_stats(stats):
    """
    Lightweight RTP-based estimated MOS.

    This is not PESQ/POLQA. It is a practical node-test score derived from
    ARI RTP stats such as jitter and packet loss so that every node can store
    a useful, comparable RTP quality estimate.
    """

    if not stats:
        return 0.00

    jitter = max(
        _safe_float(stats.get("remote_maxjitter")),
        _safe_float(stats.get("local_maxjitter")),
        _safe_float(stats.get("rxjitter")),
        _safe_float(stats.get("txjitter")),
        _safe_float(stats.get("jitter"))
    )

    packet_loss = max(
        _safe_float(stats.get("remote_maxrxploss")),
        _safe_float(stats.get("local_maxrxploss")),
        _safe_float(stats.get("rxploss")),
        _safe_float(stats.get("txploss")),
        _safe_float(stats.get("packet_loss")),
        _safe_float(stats.get("loss"))
    )

    mos = 4.5
    mos -= min(jitter / 20.0, 1.0)
    mos -= min(packet_loss * 0.25, 1.5)

    return round(max(1.0, min(4.5, mos)), 2)


def get_transition_node_id(transitions, event_name):

    if not transitions:
        return None

    if isinstance(transitions, dict):
        return transitions.get(event_name)

    if isinstance(transitions, list):
        for transition in transitions:
            if not isinstance(transition, dict):
                continue

            if transition.get("type") == event_name or transition.get("condition") == event_name or transition.get("event") == event_name:
                return (
                    transition.get("next_node_id")
                    or transition.get("target")
                    or transition.get("node_id")
                )

    return None


def majority_test_result(results):
    """Return the most common result, choosing the worse result on ties."""
    valid_results = [
        result for result in results
        if result in (
            NODE_TEST_FAILED,
            NODE_TEST_SATISFACTORY,
            NODE_TEST_SUCCESS
        )
    ]
    if not valid_results:
        return None

    counts = {
        NODE_TEST_FAILED: valid_results.count(NODE_TEST_FAILED),
        NODE_TEST_SATISFACTORY: valid_results.count(NODE_TEST_SATISFACTORY),
        NODE_TEST_SUCCESS: valid_results.count(NODE_TEST_SUCCESS),
    }
    highest_count = max(counts.values())

    # The constants are ordered from worst (0) to best (2), so min() gives a
    # deterministic, conservative result if two categories have equal votes.
    return min(
        result for result, count in counts.items()
        if count == highest_count
    )


def calculate_call_level_summary(session):
    node_results = session.get("node_results", [])

    response_time_results = [
        result.get("node_test_response_time")
        for result in node_results
    ]
    match_percentage_results = [
        result.get("node_test_match_percentage")
        for result in node_results
    ]
    combined_results = [
        result.get("node_test_result")
        for result in node_results
    ]

    response_time_result = majority_test_result(response_time_results)
    match_percentage_result = majority_test_result(match_percentage_results)

    call_results = [
        result for result in (
            response_time_result,
            match_percentage_result
        )
        if result is not None
    ]

    summary = session.setdefault("summary", {})
    summary.update({
        "total_nodes": len(node_results),
        "passed_nodes": combined_results.count(NODE_TEST_SUCCESS),
        "satisfactory_nodes": combined_results.count(NODE_TEST_SATISFACTORY),
        "failed_nodes": combined_results.count(NODE_TEST_FAILED),
        "node_test_response_time": response_time_result,
        "node_test_match_percentage": match_percentage_result,
        "overall_result": min(call_results) if call_results else None,
    })

    return summary


def record_test_history(session):

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
        
        logger.info(f"Insert Node Test Data : {sql}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

        # 4. Execute the query
        cursor.execute(sql,
            (
            session["test_execution_row_id"], 
            session["node_result"]["node_id"],
            session["node_result"].get("mos", 0.00), 
            0.00, 
            session["node_result"]["actual_text"], 
            session["node_result"]["transcription_match"],
            session["node_result"]["response_time"],
            session["node_result"]["test_result"]
            ))
    
        # 5. COMMIT THE TRANSACTION (Crucial for INSERT, UPDATE, DELETE)
        conn.commit()
    
        # 6. Get the auto-incremented ID (Optional)
        logger.info(f"Successfully inserted. New Row ID: {cursor.lastrowid}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

    except Exception as err:
        print(f"Error: {err}")
        conn.rollback() # Undo changes if an error happens

    finally:
        # 7. Close connections
        cursor.close()
        conn.close()

################################################
# UPDATE TEST HISTORY INTO THE DATABASE
################################################
# def update_test_history(session, stage=0):

#     try:
#         if stage == 0:
#             sql = f""" 
#                     UPDATE kcdb.verify_pro_test_execution_row_history
#                     SET
#                     start_time = %s,
#                     connect_time = %s,
#                     pdd = %s,
#                     callid = %s,
#                     execution_status = %s
#                     WHERE
#                     id = %s
#                     """
#                     # end_time = %s,
#                     # call_ended_by = %d,

#             conn = get_db_conn()
#             cursor = conn.cursor()
#             logger.info(f"Update Test Row Data : {sql}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

#             # 4. Execute the query
#             cursor.execute(sql,
#                 (
#                 session["bot_dial_time"], 
#                 session["bot_connect_time"], 
#                 session["pdd"],
#                 session["call_id"],
#                 2, # Executing
#                 session["test_execution_row_id"]
#                 ))
#         else:
#             sql = f""" 
#                     UPDATE kcdb.verify_pro_test_execution_row_history
#                     SET
#                     end_time = %s,
#                     call_ended_by = %s,
#                     execution_status = %s,
#                     callid=%s
#                     WHERE
#                     id = %s
#                     """

#             conn = get_db_conn()
#             cursor = conn.cursor()
#             logger.info(f"Update Test Row Data : {sql}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

#             # 4. Execute the query
#             cursor.execute(sql,
#                 (
#                 session["bot_end_time"],
#                 session["call_ended_by"], 
#                 3, # Completed
#                 session["sip_call_id"],
#                 session["test_execution_row_id"]
#                 ))
                
#         # 5. COMMIT THE TRANSACTION (Crucial for INSERT, UPDATE, DELETE)
#         conn.commit()

#         # 6. Get the auto-incremented ID (Optional)
#         logger.info(f"Successfully updated. Updated Row ID: {cursor.lastrowid}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

#     except Exception as err:
#         print(f"Error: {err}")
#         conn.rollback() # Undo changes if an error happens

#     finally:
#         # 7. Close connections
#         cursor.close()
#         conn.close()

def update_test_history(session, stage=0):

    try:

        if stage == 0:
            EXECUTION_STATUS = 2 # Executing
            sql = f""" 
                    UPDATE kcdb.verify_pro_test_execution_row_history
                    SET
                    start_time = %s,
                    connect_time = %s,
                    pdd = %s,
                    callid = %s,
                    execution_status = %s
                    WHERE
                    id = %s
                    """
                    # end_time = %s,
                    # call_ended_by = %d,

            conn = get_db_conn()
            cursor = conn.cursor()
            logger.info(f"Update Test Row Data : {sql}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

            # 4. Execute the query
            cursor.execute(sql,
                (
                session["bot_dial_time"], 
                session["bot_connect_time"], 
                session["pdd"],
                session["call_id"],
                EXECUTION_STATUS,
                session["test_execution_row_id"]
                ))
        else:

            EXECUTION_STATUS = 0
            FAIL_TYPE=0
            TEST_RESULT = 0

            if session["summary"]["node_test_match_percentage"] == NODE_TEST_FAILED:
                FAIL_TYPE = 31
            elif session["summary"]["node_test_response_time"] == NODE_TEST_FAILED:
                FAIL_TYPE = 32

            TEST_RESULT = min(session["summary"]["node_test_match_percentage"], session["summary"]["node_test_response_time"])

            logger.info(f"Final Update Test Row History Table:", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
            logger.info(f"FAIL_TYPE: {FAIL_TYPE} MATCH_%: {session['summary']['node_test_match_percentage']} RESP_TIME: {session['summary']['node_test_response_time']} TEST_RESULT: {TEST_RESULT}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

            if TEST_RESULT == NODE_TEST_FAILED:
                EXECUTION_STATUS = 5
            elif TEST_RESULT == NODE_TEST_SATISFACTORY:
                EXECUTION_STATUS = 4
            else:
                EXECUTION_STATUS = 3
                
            sql = f""" 
                    UPDATE kcdb.verify_pro_test_execution_row_history
                    SET
                    end_time = %s,
                    call_ended_by = %s,
                    execution_status = %s,
                    fail_type_id = %s,
                    callid=%s
                    WHERE
                    id = %s
                    """

            conn = get_db_conn()
            cursor = conn.cursor()
            logger.info(f"Update Test Row Data : {sql}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

            # 4. Execute the query
            cursor.execute(sql,
                (
                session["bot_end_time"],
                session["call_ended_by"], 
                EXECUTION_STATUS, # Completed
                None if FAIL_TYPE == 0 else FAIL_TYPE,
                session["sip_call_id"],
                session["test_execution_row_id"]
                ))
                
        # 5. COMMIT THE TRANSACTION (Crucial for INSERT, UPDATE, DELETE)
        conn.commit()

        # 6. Get the auto-incremented ID (Optional)
        logger.info(f"Successfully updated. Updated Row ID: {cursor.lastrowid}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})

    except Exception as err:
        print(f"Error: {err}")
        conn.rollback() # Undo changes if an error happens

    finally:
        # 7. Close connections
        cursor.close()
        conn.close()


################################################
# APPLICATION SHUTDOWN + ARI CLEANUP
################################################

def _system_log(message, level="info"):
    log_method = getattr(logger, level, logger.info)
    log_method(
        message,
        extra={"callid": "SYSTEM", "testid": "SYSTEM"}
    )


def safe_ari_delete(resource_type, resource_id, timeout=3):
    if not resource_id:
        return False

    try:
        response = requests.delete(
            f"{ASTERISK_URL}/ari/{resource_type}/{resource_id}",
            auth=(ARI_USER, ARI_PASS),
            timeout=timeout
        )

        if response.status_code in (200, 202, 204, 404):
            return True

        _system_log(
            f"ARI delete failed for {resource_type}/{resource_id}: "
            f"{response.status_code} {response.text}",
            level="warning"
        )
    except Exception as exc:
        _system_log(
            f"ARI delete error for {resource_type}/{resource_id}: {exc}",
            level="warning"
        )

    return False


def cleanup_session_resources(session, remove_session=True):
    if not session:
        return

    caller_channel = session.get("caller_channel")
    voicebot_channel = session.get("voicebot_channel")
    external_media = session.get("external_media")
    snoop_channel = session.get("snoop_channel")
    bridge_id = session.get("bridge_id")
    stt_ws = session.get("stt_ws")
    rtp_addr = session.get("rtp_addr")

    session["stt_accept_audio"] = False

    try:
        stop_node_recording(session, run_mos=True)
    except Exception:
        logger.exception(
            "Failed finalizing node recording during cleanup",
            extra={
                "callid": session.get("call_id", "UNKNOWN"),
                "testid": session.get("test_execution_row_id", "UNKNOWN"),
            },
        )

    try:
        stop_stt(stt_ws)
    except Exception as exc:
        _system_log(
            f"Failed stopping STT for caller={caller_channel}: {exc}",
            level="warning"
        )

    for channel_id in (
        voicebot_channel,
        external_media,
        snoop_channel,
        caller_channel,
    ):
        safe_ari_delete("channels", channel_id)

    safe_ari_delete("bridges", bridge_id)

    if rtp_addr:
        rtp_sessions.pop(rtp_addr, None)

    if remove_session and caller_channel:
        call_sessions.pop(caller_channel, None)


def cleanup_all_sessions():
    global shutdown_started

    with shutdown_lock:
        if shutdown_started:
            return
        shutdown_started = True

    shutdown_event.set()
    _system_log("Application cleanup started")

    try:
        node_processing_queue.put_nowait(None)
    except Exception:
        pass

    for session in list(call_sessions.values()):
        try:
            cleanup_session_resources(session, remove_session=False)
        except Exception:
            logger.exception(
                "Unexpected error cleaning session",
                extra={
                    "callid": session.get("call_id", "UNKNOWN"),
                    "testid": session.get("test_execution_row_id", "UNKNOWN")
                }
            )

    call_sessions.clear()
    rtp_sessions.clear()

    if rtp_socket:
        try:
            rtp_socket.close()
        except OSError:
            pass

    if ari_websocket:
        try:
            ari_websocket.close()
        except Exception:
            pass

    _system_log("Application cleanup completed")


def handle_shutdown_signal(signum, frame):
    _system_log(f"Shutdown signal received: {signum}")
    cleanup_all_sessions()


def cleanup_stale_ari_resources():
    _system_log("Checking for stale ARI resources")

    try:
        response = requests.get(
            f"{ASTERISK_URL}/ari/channels",
            auth=(ARI_USER, ARI_PASS),
            timeout=5
        )
        response.raise_for_status()
        channels = response.json()
    except Exception as exc:
        _system_log(
            f"Unable to list ARI channels during startup cleanup: {exc}",
            level="warning"
        )
        channels = []

    stale_channel_ids = []

    for channel in channels:
        channel_id = channel.get("id")
        channel_name = channel.get("name", "")
        dialplan = channel.get("dialplan") or {}
        app_name = dialplan.get("app_name", "")
        app_data = dialplan.get("app_data", "")

        belongs_to_app = (
            app_name == "Stasis"
            and APP_NAME in str(app_data)
        )
        is_external_media = channel_name.startswith("UnicastRTP/")

        if belongs_to_app or is_external_media:
            stale_channel_ids.append(channel_id)

    for channel_id in stale_channel_ids:
        safe_ari_delete("channels", channel_id)

    try:
        response = requests.get(
            f"{ASTERISK_URL}/ari/bridges",
            auth=(ARI_USER, ARI_PASS),
            timeout=5
        )
        response.raise_for_status()
        bridges = response.json()
    except Exception as exc:
        _system_log(
            f"Unable to list ARI bridges during startup cleanup: {exc}",
            level="warning"
        )
        bridges = []

    for bridge in bridges:
        bridge_id = bridge.get("id")
        bridge_name = str(bridge.get("name", ""))
        bridge_channels = bridge.get("channels") or []

        if not bridge_channels or APP_NAME in bridge_name:
            safe_ari_delete("bridges", bridge_id)

    _system_log(
        f"Startup cleanup removed {len(stale_channel_ids)} stale channels"
    )


################################################
# CLEAN UP CALL STASIS END EVENT HANDLER
################################################

def handle_stasis_end(session):
    if not session:
        return

    logger.info(
        "Cleaning up call",
        extra={
            "callid": session.get("call_id", "UNKNOWN"),
            "testid": session.get("test_execution_row_id", "UNKNOWN")
        }
    )

    try:
        if session.get("test_case") is not None:
            session["bot_end_time"] = datetime.datetime.now()
            call_summary = calculate_call_level_summary(session)
            logger.info(
                f"Call-level test summary: {call_summary}",
                extra={
                    "callid": session.get("call_id", "UNKNOWN"),
                    "testid": session.get("test_execution_row_id", "UNKNOWN")
                }
            )
            update_test_history(session, 1)
    except Exception:
        logger.exception(
            "Failed updating final call history",
            extra={
                "callid": session.get("call_id", "UNKNOWN"),
                "testid": session.get("test_execution_row_id", "UNKNOWN")
            }
        )

    cleanup_session_resources(session, remove_session=True)

    logger.info(
        "Call cleanup completed",
        extra={
            "callid": session.get("call_id", "UNKNOWN"),
            "testid": session.get("test_execution_row_id", "UNKNOWN")
        }
    )



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
    # print("Matched: ",result.matched)
    # print("Match %:",result.match_percentage)
    print("Match %:",result.word_match_percentage)
    print("FULL RESULT:\n",result)
    print("=====================================================")

    return result
  
################################################
# IVR TEST JSON PARSER
################################################
  
def json_parse(session):
    data = json.loads(session["dialed_parameters_snapshot"])
    if data:
        logger.info(f"JSON data loaded: {data}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
        
    return IVRTestCase(data)

def json_file_parse(session,json_file='ivr_test.json'):

    with open(json_file, "r") as f:
        data = json.load(f)

    if data:
        logger.info(f"JSON data loaded: {data}", extra={"callid":session.get("call_id", "UNKNOWN"),"testid":session.get("test_execution_row_id", "UNKNOWN")})
 
    return IVRTestCase(data)

def get_pjsip_call_id(channel_id):

    r = requests.get(
        f"{ASTERISK_URL}/ari/channels/{channel_id}/variable",
        auth=(ARI_USER, ARI_PASS),
        params={
            "variable": "CHANNEL(pjsip,call-id)"
        }
    )

    if r.status_code == 200:
        return r.json().get("value")

    return None

################################################
# MAIN
################################################

def main():
    global ari_websocket

    _system_log("Starting Verify Pro Application")

    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    atexit.register(cleanup_all_sessions)

    cleanup_stale_ari_resources()
    ensure_node_processing_worker()

    rtp_thread = threading.Thread(
        target=rtp_receiver,
        daemon=True,
        name="verifypro-rtp-receiver"
    )
    rtp_thread.start()

    ari_ws_url = (
        f"ws://127.0.0.1:8088/ari/events?"
        f"app={APP_NAME}&api_key={ARI_USER}:{ARI_PASS}"
    )

    ari_websocket = websocket.WebSocketApp(
        ari_ws_url,
        on_message=on_ari
    )

    try:
        ari_websocket.run_forever()
    except KeyboardInterrupt:
        handle_shutdown_signal(signal.SIGINT, None)
    except Exception:
        logger.exception(
            "ARI WebSocket terminated unexpectedly",
            extra={"callid": "SYSTEM", "testid": "SYSTEM"}
        )
    finally:
        cleanup_all_sessions()

        if rtp_thread.is_alive():
            rtp_thread.join(timeout=3)

        try:
            listener.stop()
        except Exception:
            pass

if __name__ == "__main__":
    main()
