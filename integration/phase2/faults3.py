import time
from harness import *
guard()
keys = lambda: {o["Key"] for o in s3().list_objects_v2(Bucket="pg-phase2", Prefix="inbox/").get("Contents", [])}

# F6 worker killed while it holds the job
dc("restart", "worker")
s = new_session(); send(item(s, 1, "arrived"))
wait(lambda: db("select claim_attempt from detection_jobs where session_id=%s and status='processing'", s), 120, 0.3, what="claimed")
t_kill = time.time()
subprocess.run(["docker", "kill", "pg-phase2-worker-1"], check=True, capture_output=True)
print("F6 killed while processing:", job_rows(s))
dc("start", "worker")
wait(lambda: job_rows(s) and job_rows(s)[0][2] == "done", 240, 2, what="F6 done")
print(f"F6 recovered after {time.time()-t_kill:.0f}s:", job_rows(s), "results:", db("select count(*) from detection_results r join detection_jobs j on j.result_id=r.id where j.session_id=%s", s))

# F7 DLQ: the stored photo goes missing before the worker reads it -> 3 failed receipts -> DLQ -> dead_lettered -> redrive
dc("stop", "worker")
before = keys()
s = new_session(); it = item(s, 1, "arrived", IMG_CAR); send(it)
lost = list(keys() - before); assert len(lost) == 1, lost
body = s3().get_object(Bucket="pg-phase2", Key=lost[0])["Body"].read()
s3().delete_object(Bucket="pg-phase2", Key=lost[0])
dc("start", "worker")
wait(lambda: job_rows(s)[0][2] == "dead_lettered", 420, 3, what="dead_lettered")
print("F7 DLQ:", job_rows(s), db("select left(last_error,120) from detection_jobs where session_id=%s", s))
print("   queue", qcount(), "dlq", qcount("pg-judgement-dlq.fifo"))
s3().put_object(Bucket="pg-phase2", Key=lost[0], Body=body, ContentType="image/jpeg")   # photo restored (operator action)
t0 = time.time()
print(dc("exec", "-T", "api", "flask", "jobs", "redrive", "--all-dead").strip()[-200:])
wait(lambda: job_rows(s)[0][2] == "done", 600, 5, what="redriven job done")
print(f"F7 redrive -> done after {time.time()-t0:.0f}s:", job_rows(s))
print("   queue", qcount(), "dlq", qcount("pg-judgement-dlq.fifo"))
