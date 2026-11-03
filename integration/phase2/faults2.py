import time
from harness import *
guard()
V = lambda: db("select id,status,end_reason from violations order by id")
H = lambda: db("select count(*) from violation_history")[0][0]

# F5 reverse order + session change, worker stopped so everything is queued before any processing
v0 = len(V())
dc("stop", "worker")
sa = new_session()
for it in (item(sa, 1, "arrived", IMG_CAR), item(sa, 3, "refresh", IMG_CAR), item(sa, 2, "refresh", IMG_CAR)):
    assert send(it).kind == "accepted"
dc("start", "worker")
wait(lambda: all(r[2] in ("done", "stale", "dead_lettered") for r in job_rows(sa)) and len(job_rows(sa)) == 3, 150, what="A done")
print("F5a reverse order (send order seq1,3,2):", job_rows(sa))
print("   violations", V()[v0:])
sb = new_session()   # device rebooted: new session
for it in (item(sb, 1, "arrived", IMG_CAR), item(sb, 2, "refresh", IMG_CAR)):
    send(it)
wait(lambda: len(job_rows(sb)) == 2 and all(r[2] == "done" for r in job_rows(sb)), 150, what="B done")
print("F5b session change:", job_rows(sb))
print("   violations", V()[v0:])
sc = new_session()
for it in (item(sc, 1, "arrived", IMG_EMPTY), item(sc, 2, "left", IMG_EMPTY)):
    send(it)
wait(lambda: len(job_rows(sc)) == 2 and all(r[2] == "done" for r in job_rows(sc)), 150, what="C done")
print("F5c left:", job_rows(sc))
print("   violations", V()[v0:])
print("queue", qcount(), "dlq", qcount("pg-judgement-dlq.fifo"))
