"""F9: the same pending-effects job run by several processes at once (Worker redelivery + 2 relays + extra), repeated.
Counts what must stay exactly-once: notifications per (violation, admin, channel), evidence object, history rows."""
import json, subprocess, sys
from harness import *
guard()
vid, job_id = db("select violation_id, id from detection_jobs where violation_id is not null order by created_at desc limit 1")[0]
ROUNDS, PROCS = 6, 4
SNIP = ("from app import create_app\nfrom app.services.worker import JudgementWorker\nimport uuid,sys\n"
        "app=create_app()\nwith app.app_context():\n    JudgementWorker(worker_id='race-'+sys.argv[2])._run_effects(uuid.UUID(sys.argv[1]))\n")
def counts():
    n = db("select count(*) from notifications where violation_id=%s", vid)[0][0]
    h = db("select action, count(*) from violation_history where violation_id=%s group by action order by 1", vid)
    img = db("select image_status from violations where id=%s", vid)[0][0]
    return n, dict(h), str(img)
base = counts(); print("violation", vid, "job", job_id, "before:", base)
for r in range(ROUNDS):
    db("update detection_jobs set effects_status='pending', last_error=null where id=%s", job_id)
    db("update violations set image_status='FAILED' where id=%s", vid)
    ps = [subprocess.Popen(COMPOSE + ["exec", "-T", "relay", "python", "-c", SNIP.replace("\n", chr(10)), str(job_id), f"{r}-{i}"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(PROCS)]
    errs = [p.communicate()[1][-300:] for p in ps]
    c = counts()
    keys = [k for k in s3_keys("violations/") if f"violation-{vid}." in k]
    print(f"round {r}: notifications={c[0]} history={c[1]} image={c[2]} evidence_objects={len(keys)} effects={db('select effects_status from detection_jobs where id=%s', job_id)[0][0]}",
          "ERR:" + errs[0].strip().splitlines()[-1] if any("Error" in e or "Traceback" in e for e in errs) else "")
