import time
from harness import *
guard()
keys = lambda: {o["Key"] for o in s3().list_objects_v2(Bucket="pg-phase2", Prefix="inbox/").get("Contents", [])}

# F7 DLQ: storage unreachable for the worker (retryable error) -> 3 failed receipts -> DLQ -> dead_lettered -> redrive
# (a photo that is simply missing is rejected at once as `missing_image`, it never reaches the DLQ: checked before)
NET = "pg-phase2_default"
dc("stop", "worker")
s = new_session(); send(item(s, 1, "arrived", IMG_CAR))
s3c = dc("ps", "-q", "s3").split()[0]
subprocess.run(["docker", "network", "disconnect", NET, s3c], check=True, capture_output=True)
subprocess.run(["docker", "start", "pg-phase2-worker-1"], check=True, capture_output=True)   # `compose start` refuses while s3 is off the network
try:
    wait(lambda: job_rows(s)[0][2] == "dead_lettered", 600, 3, what="dead_lettered")
finally:
    subprocess.run(["docker", "network", "connect", "--alias", "s3", NET, s3c], check=True, capture_output=True)
print("F7 DLQ:", job_rows(s), db("select left(last_error,160) from detection_jobs where session_id=%s", s))
print("   queue", qcount(), "dlq", qcount("pg-judgement-dlq.fifo"))
t0 = time.time()
print(dc("exec", "-T", "api", "flask", "jobs", "redrive", "--all-dead").strip()[-200:])
wait(lambda: job_rows(s)[0][2] == "done", 900, 5, what="redriven job done")
print(f"F7 redrive -> done after {time.time()-t0:.0f}s:", job_rows(s))
print("   queue", qcount(), "dlq", qcount("pg-judgement-dlq.fifo"))
