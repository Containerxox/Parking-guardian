import time, requests
from harness import *

guard()
res = {}

# F1 duplicate send / lost response: same event_id twice -> accepted then duplicate, one row, one result
s = new_session(); it = item(s, 1, "arrived")
a = send(it); b = send(it)
res["F1 dup"] = (a.kind, a.status, b.kind, b.status)
wait(lambda: job_rows(s) and job_rows(s)[0][2] == "done", 90, what="F1 done")
assert len(job_rows(s)) == 1, job_rows(s)
print("F1", res["F1 dup"], job_rows(s))

# F2 API outage: stop api, send fails with a network error, restart, same item delivered
s = new_session(); it = item(s, 1, "arrived")
dc("stop", "api")
try:
    send(it); outage = "NO ERROR?!"
except requests.RequestException as e:
    outage = type(e).__name__
dc("start", "api")
wait(lambda: requests.get("http://127.0.0.1:15000/readyz", timeout=2).status_code == 200, 90, what="api back")
r = send(it)
wait(lambda: job_rows(s) and job_rows(s)[0][2] == "done", 90, what="F2 done")
print("F2 outage:", outage, "-> retry", r.kind, r.status, job_rows(s))

# F3 queue send failure: disconnect sqs from the network; ingest must still accept (job stays pending_publish)
net = [l for l in dc("ps", "-q", "sqs").split()][0]
netname = "pg-phase2_default"
subprocess.run(["docker", "network", "disconnect", netname, net], check=True, capture_output=True)
s = new_session(); it = item(s, 1, "arrived")
r = send(it)
time.sleep(8)
st_during = job_rows(s)[0][2]
subprocess.run(["docker", "network", "connect", "--alias", "sqs", netname, net], check=True, capture_output=True)
wait(lambda: job_rows(s)[0][2] == "done", 150, what="F3 done after sqs back")
print("F3 queue down: ingest", r.kind, r.status, "| status while down:", st_during, "| after:", job_rows(s))

# F4 worker stopped: jobs wait in queue, worker returns, all processed once
s = new_session()
dc("stop", "worker")
its = [item(s, 1, "arrived"), item(s, 2, "refresh")]
for i in its: send(i)
time.sleep(5)
st_stopped = [r[2] for r in job_rows(s)]
dc("start", "worker")
wait(lambda: all(r[2] == "done" for r in job_rows(s)) and len(job_rows(s)) == 2, 180, what="F4 done")
print("F4 worker stop: while stopped", st_stopped, "| after", job_rows(s))
print("queue", qcount(), "dlq", qcount("pg-judgement-dlq.fifo"))
