from harness import *
from pi_client import post_job
guard()
NET = "pg-phase2_default"; sqs_id = dc("ps", "-q", "sqs").split()[0]
subprocess.run(["docker", "network", "disconnect", NET, sqs_id], check=True, capture_output=True)
try:
    s = new_session(); print("start", time.strftime("%H:%M:%S", time.gmtime())); t = time.time()
    r = post_job(API, KEY, item(s, 1, "arrived"), timeout=(3, 150))
    print(r.kind, r.status, f"{time.time()-t:.1f}s", "end", time.strftime("%H:%M:%S", time.gmtime()))
finally:
    subprocess.run(["docker", "network", "connect", "--alias", "sqs", NET, sqs_id], check=True, capture_output=True)
