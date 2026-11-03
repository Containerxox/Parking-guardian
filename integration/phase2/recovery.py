"""10단계 검토 후속: Relay 의 멈춘 작업 복구를 Compose 스택(Moto S3 + ElasticMQ SQS + 실제 YOLO Worker)에서 확인한다.

ElasticMQ 는 SQS 의 보존 기간(4일)을 적용하지 않는다. 그래서 "메시지가 사라졌다"는 상황은 sqs 컨테이너를 재시작해서
만든다(메모리 큐라 메시지와 5분 중복 제거 기록이 함께 사라진다 = 1일 뒤 실제 SQS 와 같은 상태). "1일 지났다"는 시각은
DB 의 published_at / lease_until 을 2일 전으로 돌려서 만든다(복구 임계값은 기본 1일, RELAY_STALLED_AFTER_SECONDS).

  RA 메시지 유실(queued)            RB Worker 사망(processing, 리스 만료) + 메시지 유실
  RC DLQ 로 간 메시지가 사라짐      RD inbox 사진이 7일 보존으로 이미 삭제됨
  RE Relay 3개 동시 복구            RF 임계값 안쪽(23시간)은 건드리지 않음

실행: docker compose -f docker-compose.phase2.yml up -d --build 뒤 `python integration/phase2/recovery.py`.
pg-phase2 스택 전용(guard()). 개발 DB(5432)나 AWS 는 건드리지 않는다.
"""
import subprocess
import threading
import time

from harness import *  # noqa: F401,F403

guard()


def lose_messages():
    """SQS forgets everything (retention / loss), including the deduplication records."""
    dc("restart", "sqs")
    wait(lambda: qcount() is not None, 60, 1, what="sqs back")
    assert qcount()["Messages"] == 0, qcount()


def age(session, interval="2 days"):
    db("update detection_jobs set published_at = now() - %s::interval, updated_at = now() - %s::interval"
       " where session_id=%s and status='queued'", interval, interval, session)


def status(session):
    return [(r[0], r[1]) for r in db("select seq,status from detection_jobs where session_id=%s order by seq",
                                     session)]


def results(session):
    return db("select count(*) from detection_results r join detection_jobs j on j.result_id=r.id"
              " where j.session_id=%s", session)[0][0]


def quiet():
    """Stop worker and relay, then let a killed worker's 20 s SQS long poll run out: ElasticMQ keeps serving that
    orphaned poll, which would swallow the next message and hide it for the visibility timeout."""
    dc("stop", "worker", "relay")
    time.sleep(22)


def start_all():
    dc("start", "relay")
    dc("start", "worker")


def fresh(session, seq=1, event="arrived", photo=IMG_EMPTY):
    send(item(session, seq, event, photo))


print("== RA message lost while queued")
quiet()
s = new_session(); fresh(s)
assert status(s) == [(1, "queued")] and qcount()["Messages"] == 1, (status(s), qcount())
lose_messages(); age(s)
print("   before recovery:", status(s), "queue", qcount())
t0 = time.time()
dc("start", "relay")
wait(lambda: db("select status,claim_attempt from detection_jobs where session_id=%s", s)[0][1] == 1, 30, 0.5,
     what="requeued by the relay")
wait(lambda: qcount()["Messages"] == 1, 30, 0.5, what="republished")
print(f"   requeued + republished after {time.time()-t0:.1f}s:", db("select status,claim_attempt,left(last_error,40),publish_attempts from detection_jobs where session_id=%s", s))
dc("start", "worker")
wait(lambda: status(s) == [(1, "done")], 240, 2, what="RA done")
assert results(s) == 1
print(f"   RA done after {time.time()-t0:.0f}s:", status(s), "results", results(s), "queue", qcount())

print("== RB worker died holding the job, message lost")
quiet()
s = new_session(); fresh(s)
db("update detection_jobs set status='processing', claim_attempt=1, worker_id='dead-worker',"
   " lease_until=now() - interval '2 days', updated_at=now() - interval '2 days' where session_id=%s", s)
lose_messages()
t0 = time.time()
start_all()
wait(lambda: status(s) == [(1, "done")], 240, 2, what="RB done")
row = db("select claim_attempt,worker_id from detection_jobs where session_id=%s", s)[0]
assert results(s) == 1 and row[0] == 3, row       # 1 old claim, +1 recovery bump, +1 new claim
print(f"   RB done after {time.time()-t0:.0f}s:", status(s), "claim_attempt/worker", row, "results", results(s))

print("== RC the job's message sits in the DLQ and then disappears")
quiet()
s = new_session(); fresh(s)
code = ("import boto3;c=boto3.client('sqs');u=c.get_queue_url(QueueName='pg-judgement.fifo')['QueueUrl'];"
        "[c.receive_message(QueueUrl=u,VisibilityTimeout=1,WaitTimeSeconds=2) or __import__('time').sleep(1.5) for _ in range(4)]")
dc("exec", "-T", "api", "python", "-c", code)
wait(lambda: qcount("pg-judgement-dlq.fifo")["Messages"] + qcount("pg-judgement-dlq.fifo")["MessagesNotVisible"] == 1, 30, 1,
     what="message in the DLQ")
print("   DLQ holds it:", qcount("pg-judgement-dlq.fifo"), "job:", status(s))
lose_messages(); age(s)                                   # the DLQ message expires before any sweep
t0 = time.time()
start_all()
wait(lambda: status(s) == [(1, "done")], 240, 2, what="RC done")
assert results(s) == 1
print(f"   RC done after {time.time()-t0:.0f}s:", status(s), "dlq", qcount("pg-judgement-dlq.fifo"))

print("== RD inbox photo already deleted")
quiet()
before = set(s3_keys("inbox/"))
s = new_session(); fresh(s)
(gone,) = list(set(s3_keys("inbox/")) - before)
s3().delete_object(Bucket="pg-phase2", Key=gone)
lose_messages(); age(s)
dc("start", "relay")
wait(lambda: status(s) == [(1, "rejected")], 60, 1, what="rejected by recovery")
print("   RD:", db("select status,left(last_error,70) from detection_jobs where session_id=%s", s),
      "queue", qcount(), "results", results(s))
assert qcount()["Messages"] == 0 and results(s) == 0

print("== RE three relays recover six stalled jobs at once")
quiet()
s = new_session()
for n in range(1, 7):
    fresh(s, n, "arrived" if n == 1 else "refresh")
lose_messages(); age(s)
dc("start", "relay")
procs = []


def extra():
    procs.append(subprocess.run(COMPOSE + ["exec", "-T", "relay", "flask", "outbox-relay", "--once"],
                                capture_output=True, text=True).stdout.strip().splitlines()[-1])


threads = [threading.Thread(target=extra) for _ in range(2)]
[t.start() for t in threads]; [t.join() for t in threads]
rows = db("select seq,status,claim_attempt from detection_jobs where session_id=%s order by seq", s)
print("   after 3 relays:", rows)
assert all(r[2] == 1 for r in rows), rows              # each job recovered exactly once
wait(lambda: qcount()["Messages"] == 6, 30, 1, what="six messages")
dc("start", "worker")
wait(lambda: all(r[1] in ("done", "stale") for r in status(s)), 480, 3, what="RE finished")
print("   RE:", status(s), "results", results(s), "queue", qcount())
assert results(s) == 6 and qcount()["Messages"] == 0

print("== RF a job inside the threshold is left alone")
quiet()
s = new_session(); fresh(s)
lose_messages(); age(s, "23 hours")
dc("start", "relay")
time.sleep(8)                                          # several relay passes (2 s each)
assert status(s) == [(1, "queued")] and qcount()["Messages"] == 0, (status(s), qcount())
print("   RF untouched after 8 s:", status(s), "claim_attempt", db("select claim_attempt from detection_jobs where session_id=%s", s))
start_all()
print("recovery integration finished")
