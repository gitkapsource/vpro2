import time
import asyncio
from db_connection import get_db_conn
from panoramisk import Manager
from dataclasses import dataclass

# @dataclass
# class Test:
#     id: int
#     extension: str
#     name: str


async def originate_call(manager, test):
    print(f"Now Originating using AMI: Test-{test['id']}")
    await manager.send_action({
        "Action": "Originate",
        "Channel": f"Local/1000@voicebot-context/n",
        "Context": "voicebot-context",
        "Exten": "1000",
        "Priority": 1,
        "CallerID": f"Test-{test['id']}",
        # "Variable": f"TEST_ID={test['id']}",
        "Async": "true"
    })


async def main():

    print("Starting Verify Pro Test Executor")
    manager = Manager(
        host="127.0.0.1",
        port=5038,
        username="python",
        secret="PASSword",
    )

    await manager.connect()

    while True:
        tests = fetch_test_history()

        if tests:
            for test in tests:
                print(f"Fetched Test ID: Test-{test['id']}")
                await originate_call(manager, test)
                # mark_test_as_started(test["id"])

        time.sleep(3)

    print("Stopping Verify Pro Test Executor")

################################################
# FETCH TEST HISTORY FROM THE DATABASE
################################################
def fetch_test_history():

    sql = f"""
        SELECT vpterh.*,pcli.cli 
        FROM kcdb.verify_pro_test_execution_row_history AS vpterh
        LEFT OUTER JOIN provider_cli AS pcli
        ON pcli.id = vpterh.provider_cli_id
        WHERE vpterh.start_time = '0000-00-00 00:00:00'
        AND vpterh.end_time = '0000-00-00 00:00:00'
        AND vpterh.scheduled_on <= NOW()
        AND vpterh.execution_status = 1
        AND vpterh.status = 1
        AND verify_pro_test_execution_id = 151
        ORDER BY vpterh.scheduled_on ASC
        LIMIT 1
    """

    conn = get_db_conn()
    cursor = conn.cursor(dictionary=True)

    try:
        
        print(f"Fetching Test Data : {sql}")

        # 4. Execute the query
        cursor.execute(sql)
    
        # 5. COMMIT THE TRANSACTION (Crucial for INSERT, UPDATE, DELETE)
        # conn.commit()
    
        # 6. Get the auto-incremented ID (Optional)
        # print(f"Successfully inserted. New Row ID: {cursor.lastrowid}")

        rows = cursor.fetchall()

        if rows:
            for row in rows:
                print(f"Fetched Test Data : ROW HISTORY ID : {row['id']} | CLI: {row['cli']}")
                # session["test_execution_row_id"] = row["id"]
                # session["cli"] = row["cli"]
                # session["dialed_parameters_snapshot"] = row["dialed_parameters_snapshot"]
                
            return rows
        else:
            print(f"No Test Data Could be Fetched")

    except Exception as err:
        print(f"Error: {err} for {sql}")
        conn.rollback() # Undo changes if an error happens

    finally:
        # 7. Close connections
        cursor.close()
        conn.close()

if __name__ == "__main__":
    asyncio.run(main())
