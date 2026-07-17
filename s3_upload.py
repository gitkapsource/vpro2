#!/usr/bin/env python3

"""
Author:Satish Barot
Purpose: Upload rerun files to S3
Created On: 08-11-2020
"""
import sys
import os
import datetime
import time
import glob
import socket
import boto3
from botocore.exceptions import ClientError
sys.path.append("/usr/src/scripts")

try:
    process = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    process.bind(f"\0s3_upload")
except (socket.error,Exception) as e:
    print("EXIT: Instance already running")
    sys.exit(1)
recording_dir = "/var/lib/asterisk/recordings/verifypro_nodes"
backup_dir = "/var/audio_backup"
error_dir = "/var/audio_error"
s3_bucket = "demo-kc-calls" #str(os.getenv('S3_BUCKET'))
#print(s3_bucket)
s3_client = boto3.client('s3')
time_delta=int(time.time() - 5)

for audio_file in sorted(glob.iglob(f"{recording_dir}/*.wav")):
    
    if(os.path.getctime(audio_file) > time_delta):
        print(f"Wait sometime before processing {audio_file}")
        continue

    print(f"Uploading audio file {audio_file}")
    file_name=os.path.basename(audio_file)
    try:
        raw_file_name = file_name.split(".")[0]
        (id,node_id,callid) = raw_file_name.split("_")
        #lang = lang.replace(".wav","")
    except Exception as e:
        print(f"ERROR: Parsing issue for file {file_name}")
        os.system(f"mv -f {recording_dir}/{file_name} {error_dir}/")
        continue
    #sequence="1" if(sequence == "X")
    print(f"id={id},node_id={node_id},callid={callid}")
    try:
        test = "vpro"
        s3_file_name=f"{id}_{node_id}_{test}_{callid}.wav"
        response = s3_client.upload_file(f"{recording_dir}/{file_name}", s3_bucket, s3_file_name, ExtraArgs={'ACL': 'public-read'})
    except ClientError as e:
        print(f"ERROR: uploading to S3 {e}")
        continue
    print(f"{file_name} uploaded")
    os.system(f"mv -f {recording_dir}/{file_name} {backup_dir}/")

print(f"Exiting script")

