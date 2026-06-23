import mysql.connector
import json

with open("config.json", "r") as f:
    CONFIG = json.load(f)

def get_db_config():
    return CONFIG["database"]

def get_db_conn():

    db = get_db_config()
    # Initiate DB Connection
    conn = mysql.connector.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"]
      )
    
    return conn