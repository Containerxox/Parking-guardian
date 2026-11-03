import statistics
from harness import *
from pi_client import post_job
guard()
wait(lambda: not db("select 1 from detection_jobs where status in ('pending_publish','queued','processing') limit 1"), 300, 2, what="idle")
for rnd in range(3):
    v = []
    for i in range(25):
        s = new_session(); t = time.time(); r = post_job(API, KEY, item(s, 1, "arrived"), timeout=(3, 60)); v.append((time.time() - t) * 1000); assert r.kind == "accepted"
        time.sleep(0.3)
    print(f"round {rnd}: median={statistics.median(v):.0f}ms p90={sorted(v)[22]:.0f}ms max={max(v):.0f}ms")
    wait(lambda: not db("select 1 from detection_jobs where status in ('pending_publish','queued','processing') limit 1"), 300, 2, what="idle")
