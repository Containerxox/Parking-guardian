"""Outage with the relay running: sample who waits on whom in PostgreSQL while the ingest request hangs."""
import threading
from harness import *
from pi_client import post_job
guard()
NET = "pg-phase2_default"; sqs_id = dc("ps", "-q", "sqs").split()[0]
samples, done = [], threading.Event()
def sampler():
    while not done.is_set():
        rows = db("""select a.pid, a.state, a.wait_event_type, a.wait_event, pg_blocking_pids(a.pid), now()-a.xact_start,
                            left(a.query, 90) from pg_stat_activity a
                     where a.datname='pg_phase2' and a.pid<>pg_backend_pid() and cardinality(pg_blocking_pids(a.pid))>0""")
        for r in rows:
            holder = db("select pid, state, now()-xact_start, left(query,90) from pg_stat_activity where pid = any(%s)", list(r[4]))
            samples.append((time.strftime("%H:%M:%S"), r, holder))
        time.sleep(1.5)
th = threading.Thread(target=sampler, daemon=True); th.start()
subprocess.run(["docker", "network", "disconnect", NET, sqs_id], check=True, capture_output=True)
try:
    s = new_session(); t = time.time()
    r = post_job(API, KEY, item(s, 1, "arrived"), timeout=(3, 150))
    print("ingest", r.kind, r.status, f"{time.time()-t:.1f}s")
finally:
    done.set(); th.join()
    subprocess.run(["docker", "network", "connect", "--alias", "sqs", NET, sqs_id], check=True, capture_output=True)
print(len(samples), "blocked samples")
for x in (samples[:1] + samples[-1:]):
    print("waiter :", x[1][0], x[1][1], x[1][2], x[1][3], "blocked by", x[1][4], "| xact age", x[1][5], "|", x[1][6])
    for h in x[2]: print("blocker:", h)
wait(lambda: job_rows(s) and job_rows(s)[0][2] == "done", 300, 2, what="done")
print("final", job_rows(s))
