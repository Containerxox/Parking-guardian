"""Ingest response time: normal queue, then SQS unreachable (before the fast-fail change: ~39 s), then recovery.
Run against the pg-phase2 stack only."""
import statistics
from harness import *
from pi_client import post_job
guard()

def timed(it):
    t = time.time(); r = post_job(API, KEY, it, timeout=(3, 150)); return r, time.time() - t

normal = []
for i in range(20):
    s = new_session(); r, dt = timed(item(s, 1, "arrived")); assert r.kind == "accepted"; normal.append(dt * 1000)
print(f"normal: n=20 first={normal[0]:.0f}ms median={statistics.median(normal):.0f}ms p90={sorted(normal)[17]:.0f}ms max={max(normal):.0f}ms")
wait(lambda: not db("select 1 from detection_jobs where status not in ('done','stale','rejected') limit 1"), 240, 2, what="normal jobs done")

NET = "pg-phase2_default"; sqs_id = dc("ps", "-q", "sqs").split()[0]
subprocess.run(["docker", "network", "disconnect", NET, sqs_id], check=True, capture_output=True)
try:
    outage = []
    sessions = []
    for i in range(3):
        s = new_session(); sessions.append(s); r, dt = timed(item(s, 1, "arrived"))
        outage.append(dt); print(f"SQS outage #{i+1}: {r.kind} {r.status} in {dt:.1f}s")
    print("rows while down:", [db("select status from detection_jobs where session_id=%s", s)[0][0] for s in sessions])
finally:
    subprocess.run(["docker", "network", "connect", "--alias", "sqs", NET, sqs_id], check=True, capture_output=True)
t0 = time.time()
wait(lambda: all(job_rows(s) and job_rows(s)[0][2] == "done" for s in sessions), 300, 2, what="recovered")
print(f"after SQS back: all 3 done in {time.time()-t0:.0f}s; results:", [job_rows(s)[0][6] for s in sessions],
      "publish_attempts:", [db("select publish_attempts from detection_jobs where session_id=%s", s)[0][0] for s in sessions])
