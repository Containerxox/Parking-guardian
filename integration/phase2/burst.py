import subprocess
from harness import *
guard()
dc("up", "-d", "--no-recreate", "--scale", "relay=2", "--scale", "worker=2", "relay", "worker")
time.sleep(40)
print(dc("ps", "--format", "{{.Service}} {{.Status}}").replace("\n", "; "))
n0 = {t: db(f"select count(*) from {t}")[0][0] for t in ("detection_jobs", "detection_results", "violations", "notifications")}
sess = [new_session() for _ in range(4)]
for s in sess:
    for seq, ev in enumerate(("arrived", "refresh", "refresh", "refresh", "left"), 1):
        assert send(item(s, seq, ev, IMG_CAR if ev != "left" else IMG_EMPTY)).kind == "accepted"
wait(lambda: all(r[2] == "done" for s in sess for r in job_rows(s)) and all(len(job_rows(s)) == 5 for s in sess), 400, 3, what="burst done")
n1 = {t: db(f"select count(*) from {t}")[0][0] for t in n0}
print("delta", {t: n1[t] - n0[t] for t in n0})
print("workers used:", db("select worker_id, count(*) from detection_jobs group by 1 order by 2 desc"))
print("claim_attempt >1:", db("select count(*) from detection_jobs where claim_attempt>1 and created_at > now() - interval '10 minutes'")[0][0])
print("dup notifications:", db("select count(*) from (select violation_id,admin_id,channel from notifications group by 1,2,3 having count(*)>1) x")[0][0])
print("open violations per machine:", db("select count(*) from violations where end_reason is null"))
print("queue", qcount(), "dlq", qcount("pg-judgement-dlq.fifo"))
dc("up", "-d", "--no-recreate", "--scale", "relay=1", "--scale", "worker=1", "relay", "worker")
