"""P4/P5 검증 (2026-11-03): Worker 의 모델 선로딩과 SIGTERM 정상 종료를 Compose 스택(Moto S3 + ElasticMQ + 실제 YOLO Worker)에서 확인한다.

  W1 선로딩      Worker 가 모델을 읽고 두 모델을 한 번씩 돌린 "뒤에" 메시지를 받는다. 대기 중이던 첫 사진의 추론 시간이 정상 수준이다
  W2 유휴 종료   `docker stop` → 종료 코드 0, 긴 폴링(20초) 안에 끝난다 (핸들러가 없으면 유예 시간을 다 기다리고 137)
  W3 처리 중 종료 판정 직전(DB 행 잠금으로 붙잡음)에 SIGTERM → 그 작업은 끝나고(done, 결과 1건, 메시지 삭제) 새 작업은 받지 않고 종료 코드 0
  W4 강제 종료   같은 상황에서 SIGKILL → 리스(55초)·가시성(60초) 뒤 다른 Worker 가 이어받아 done, 결과 1건, claim_attempt 2
  W5 로딩 중 종료 모델 로딩 중에 SIGTERM → 메시지를 하나도 받지 않고(claim 0) 종료 코드 0
  W6 로딩 실패   모델 파일이 없으면 오류 종료(코드 1), 큐는 그대로
  W7 Relay 종료  `docker stop` → 종료 코드 0, 수 초 안

실행: docker compose -f docker-compose.phase2.yml up -d --build 뒤 `python integration/phase2/shutdown.py`. pg-phase2 스택 전용(guard()).
"잠금으로 붙잡기": 접수는 장비 행을 잠그므로 Worker 를 멈춘 채 작업을 접수한 뒤, 장비 행을 잠그고 Worker 를 시작한다. Worker 는 추론까지 끝낸 다음
`_apply` 에서 그 잠금을 기다리며 status=processing 으로 멈춘다 → 그때 신호를 보낸다(타이밍에 의존하지 않는다).
"""
import re
import subprocess
import time
from datetime import datetime, timezone

import psycopg

from harness import *  # noqa: F401,F403

guard()
WORKER = "pg-phase2-worker-1"
RELAY = "pg-phase2-relay-1"
results = {}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def logs(name, since):
    out = docker("logs", "--timestamps", "--since", since, name)
    return (out.stdout + out.stderr).splitlines()


def ts(line):
    return datetime.fromisoformat(line.split(" ", 1)[0][:26].rstrip("Z") + "+00:00")


def exit_code(name):
    return int(docker("inspect", "-f", "{{.State.ExitCode}}", name).stdout.strip())


def running(name):
    return docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip() == "true"


def timed_stop(name, grace=60):
    started = time.time()
    docker("stop", "-t", str(grace), name)
    return time.time() - started


def machine_id():
    return db("select id from machines where upper(serial_number)='RPI-A-GANGNAM-01'")[0][0]


def hold_machine(mid):
    conn = psycopg.connect(DSN, autocommit=False)
    conn.execute("select id from machines where id=%s for update", (mid,))
    return conn


def state(session):
    return db("select status, claim_attempt from detection_jobs where session_id=%s", session)[0]


def result_rows(session):
    return db("select r.created_at, r.elapsed_ms from detection_results r join detection_jobs j on j.result_id=r.id where j.session_id=%s", session)


def queue_total():
    q = qcount()
    return q["Messages"] + q["MessagesNotVisible"]

import sys
WANT = set(sys.argv[1:]) or {f"W{i}" for i in range(1, 8)}
mid = machine_id()

if "W1" in WANT:
    # ---- W1 ------------------------------------------------------------------------------------------------------------------
    print("== W1 the models are loaded and run once BEFORE the first message is received")
    docker("stop", "-t", "60", WORKER)
    s = new_session()
    send(item(s, 1, "arrived", IMG_CAR))                      # waits in the queue while the worker is down
    assert state(s) == ("queued", 0)
    since = now_iso()
    docker("start", WORKER)
    ready = wait(lambda: next((l for l in logs(WORKER, since) if " ready: " in l), None), 180, 1, what="worker ready line")
    started_line = next(l for l in logs(WORKER, since) if "started" in l)
    print("  ", re.sub(r"^\S+ ", "", ready)[:200])
    wait(lambda: state(s)[0] == "done", 120, 1, what="W1 done")
    created, elapsed_ms = result_rows(s)[0]
    assert created >= ts(ready), (created, ts(ready))      # the verdict was written after the worker declared itself ready
    assert state(s)[1] == 1
    print(f"   worker started -> ready: {(ts(ready) - ts(started_line)).total_seconds():.1f}s | first real photo judged in {elapsed_ms} ms "
          f"(result written {(created - ts(ready)).total_seconds():.1f}s after 'ready')")
    results["W1"] = ready

if "W2" in WANT:
    # ---- W2 ------------------------------------------------------------------------------------------------------------------
    print("== W2 idle worker: docker stop")
    time.sleep(5)
    took = timed_stop(WORKER)
    code = exit_code(WORKER)
    print(f"   stop took {took:.1f}s, exit code {code}")
    assert code == 0 and took < 30, (code, took)
    results["W2"] = (took, code)

if "W3" in WANT:
    # ---- W3 ------------------------------------------------------------------------------------------------------------------
    print("== W3 SIGTERM while a job is in flight: it finishes, no new job is taken")
    s_a, s_b = new_session(), new_session()
    send(item(s_a, 1, "arrived", IMG_CAR))                    # two jobs of the same machine = one FIFO group; b is deliverable only after a is deleted
    send(item(s_b, 1, "arrived", IMG_CAR))
    conn = hold_machine(mid)
    since = now_iso()
    docker("start", WORKER)
    wait(lambda: state(s_a)[0] == "processing", 180, 0.5, what="W3 processing (held at the machine lock)")
    stopper = subprocess.Popen(["docker", "stop", "-t", "60", WORKER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    time.sleep(4)
    assert running(WORKER) and state(s_a)[0] == "processing", "the worker must wait for the job in flight, not die"
    conn.rollback(); conn.close()                             # release the lock: the worker can finish the job in hand
    stopper.wait(timeout=70)
    took = time.time() - t0
    code = exit_code(WORKER)
    status_a, attempt_a = state(s_a)
    status_b, attempt_b = state(s_b)
    print(f"   exit code {code} after {took:.1f}s | job in flight: {status_a} (claim {attempt_a}), results {len(result_rows(s_a))} | next job: {status_b} (claim {attempt_b})")
    tail = [l for l in logs(WORKER, since) if "handled" in l]
    print("  ", re.sub(r"^\S+ ", "", tail[-1]) if tail else "(no summary line)")
    assert code == 0 and status_a == "done" and len(result_rows(s_a)) == 1 and attempt_a == 1
    assert (status_b, attempt_b) == ("queued", 0), "no new job after the stop"
    results["W3"] = (took, code)

if "W4" in WANT:
    # ---- W4 ------------------------------------------------------------------------------------------------------------------
    print("== W4 SIGKILL while a job is in flight (grace period exhausted): another worker takes over")
    docker("stop", "-t", "60", WORKER)
    left = db("select session_id::text from detection_jobs where status='queued' order by created_at limit 1")
    if left:
        s_b = left[0][0]                                       # W3's leftover job (it must go first: same FIFO group)
    else:
        s_b = new_session()
        send(item(s_b, 1, "arrived", IMG_CAR))
    conn = hold_machine(mid)
    docker("start", WORKER)
    wait(lambda: state(s_b)[0] == "processing", 180, 0.5, what="W4 processing")
    t_kill = time.time()
    docker("kill", "-s", "KILL", WORKER)
    conn.rollback(); conn.close()
    print(f"   killed: exit code {exit_code(WORKER)} (docker treats `docker kill` as a manual stop, so its restart policy does not apply;")
    print("   Kubernetes' kubelet WOULD restart the container -> we start it by hand, right away)")
    assert state(s_b) == ("processing", 1)
    docker("start", WORKER)
    wait(lambda: state(s_b)[0] == "done", 240, 2, what="W4 done by the restarted worker")
    status_b, attempt_b = state(s_b)
    print(f"   done {time.time() - t_kill:.0f}s after the kill | claim_attempt {attempt_b} | results {len(result_rows(s_b))} | queue messages left {queue_total()}")
    assert attempt_b == 2 and len(result_rows(s_b)) == 1 and queue_total() == 0
    results["W4"] = time.time() - t_kill

if "W5" in WANT:
    # ---- W5 ------------------------------------------------------------------------------------------------------------------
    print("== W5 SIGTERM while the models load: nothing is received")
    docker("stop", "-t", "60", WORKER)
    s5 = new_session()
    send(item(s5, 1, "arrived", IMG_CAR))
    since = now_iso()
    docker("start", WORKER)
    wait(lambda: any("started" in l for l in logs(WORKER, since)), 60, 0.2, what="worker started line (handler installed)")
    t0 = time.time()
    docker("stop", "-t", "60", WORKER)                          # SIGTERM: the load is still running
    took = time.time() - t0
    code = exit_code(WORKER)
    st = state(s5)
    tail = [l for l in logs(WORKER, since) if "handled" in l]
    print(f"   exit code {code} after {took:.1f}s | job: {st} | {re.sub(r'^.*? ', '', tail[-1]) if tail else '(no summary)'}")
    assert code == 0 and st == ("queued", 0), "the stop during the load must not consume the message"
    results["W5"] = (took, code)

if "W6" in WANT:
    # ---- W6 ------------------------------------------------------------------------------------------------------------------
    print("== W6 models cannot load: error exit, queue untouched")
    done = subprocess.run(["docker", "compose", "-f", COMPOSE[3], "run", "--rm", "--no-deps", "-e", "WINDOW_MODEL_PATH=/models/does-not-exist.pt",
                           "-e", "MODEL_LOAD_RETRY_SECONDS=0", "worker"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (done.stdout + done.stderr).strip().splitlines()
    print(f"   exit code {done.returncode} | last line: {out[-1][:140] if out else ''}")
    assert done.returncode == 1 and state(s5) == ("queued", 0)
    assert any("nothing was consumed" in l for l in out)
    results["W6"] = done.returncode

if "W7" in WANT:
    # ---- W7 ------------------------------------------------------------------------------------------------------------------
    print("== W7 relay: docker stop")
    took = timed_stop(RELAY, 15)
    code = exit_code(RELAY)
    print(f"   stop took {took:.1f}s, exit code {code}")
    assert code == 0 and took < 10, (code, took)
    docker("start", RELAY)
    docker("start", WORKER)
    wait(lambda: state(s5)[0] == "done", 180, 2, what="the job left over from W5 is judged by the restarted worker")
    print("   leftover job from W5 judged after restart:", state(s5))
    print("shutdown verification finished:", {k: v for k, v in results.items() if k != "W1"})
