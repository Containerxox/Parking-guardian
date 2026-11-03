import boto3, time, uuid
from botocore.config import Config
c = boto3.client("sqs", config=Config(connect_timeout=2, read_timeout=3, retries={"mode": "standard", "total_max_attempts": 1}))
U = "http://sqs:9324/000000000000/pg-judgement.fifo"
for i in range(45):
    t = time.time()
    try:
        c.send_message(QueueUrl=U, MessageBody="probe", MessageGroupId="probe", MessageDeduplicationId=str(uuid.uuid4())); r = "ok"
    except Exception as e:
        r = type(e).__name__
    print(f"{time.strftime('%H:%M:%S')} {r} {time.time()-t:.1f}s", flush=True)
    time.sleep(1)
