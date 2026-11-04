"""로컬 검증 전용 모의 서버. 실제 서버·AWS·DB 에 접속하지 않는다(127.0.0.1 만 연다).

Phase 1·2 의 k6 스크립트와 측정 도구를 AWS 없이 시험하려고, 실제 API 의 "계약"만 흉내 낸다:
  POST /api/v1/device/detection-jobs  Phase 2 접수. X-Device-Key 인증, multipart(file, event, event_id, session_id, seq),
                                      새 작업 202, 같은 (장비, event_id) 는 200 + duplicate=true, 형식 오류 400, 키 오류 401, 주입 실패 503
  POST /api/v1/device/detections      Phase 1 동기 접수(서비스 시간 동안 붙잡음, 동시 처리 한도 초과 대기열이 길면 503)
  POST /api/v1/device/heartbeat, POST /api/v1/auth/login, GET /api/v1/{readyz,violations,dashboard/summary,notifications}
  GET  /_mock/stats                   장비별 동시 요청 최대치, seq 순서 위반, 중복 등 "검증용 통계"

접수한 Phase 2 작업은 SQLite 파일(detection_jobs, detection_results)에 실제 컬럼 이름으로 기록하고, 가짜 Worker 스레드가
장비(MessageGroupId) 별 직렬 + 전체 N 개 동시로 처리해 결과를 쓴다. p2_collect.py 가 이 파일을 읽어 지표를 만든다.

실행 예:  python p2_mock_server.py --port 18081 --db mock.sqlite --keys k1:7,k2:8,k3:9 --workers 2 --service-ms 200
"""
import argparse
import json
import random
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


for _stream in (sys.stdout, sys.stderr):      # Windows 콘솔(cp949)에서도 한글·기호 출력이 깨지거나 예외가 나지 않게
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SCHEMA = """
create table if not exists detection_jobs (
  id text primary key, machine_id integer not null, event_id text not null, session_id text not null, seq integer not null,
  event text not null, received_at text not null, published_at text, status text not null, publish_attempts integer default 0,
  claim_attempt integer default 0, lease_until text, worker_id text, result_id integer, last_error text, updated_at text,
  unique (machine_id, event_id));
create table if not exists detection_results (
  id integer primary key autoincrement, job_id text, machine_id integer not null, event_id text not null, session_id text not null,
  seq integer not null, event text not null, received_at text not null, verdict text not null, applied integer not null,
  elapsed_ms integer not null, created_at text not null, unique (machine_id, event_id));
"""


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="microseconds")


class State:
    def __init__(self, args):
        self.args = args
        self.rng = random.Random(args.seed)
        self.keys = dict(pair.split(":") for pair in args.keys.split(","))   # key -> machine id (문자열)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(args.db, check_same_thread=False, isolation_level=None)
        self.db.executescript(SCHEMA)
        self.inflight = {}          # 장비 -> 지금 처리 중인 접수 요청 수
        self.max_inflight = {}      # 장비 -> 최대 동시 요청 수 (1 이어야 "장비별 직렬")
        self.max_global_inflight = 0
        self.global_inflight = 0
        self.arrivals = {}          # 장비 -> [(seq, event_id, ts)]
        self.order_violations = []  # (장비, 세션, 이전 seq, 지금 seq)
        self.last_seq = {}          # (장비, 세션) -> 마지막으로 도착한 seq
        self.sessions = {}
        self.duplicates = 0
        self.accepted = 0
        self.bad = 0
        self.injected_failures = 0
        self.lost_responses = 0
        self.p1 = {"ok": 0, "busy": 0}
        self.p1_active = 0
        self.last_applied = {}      # 장비 -> 마지막 반영 seq (stale 판정)
        self.busy_machines = set()
        self.worker_stop = threading.Event()
        # 장비 관리 API 흉내(p2_devices.py 시험용): 주차장 6(E2E-TEST-LOT, 회사 E2E-TEST-COMPANY)에 시험 장비 1대가 이미 있다
        self.company = args.company
        self.machines = {6: {"id": 6, "serial_number": "RPI-E2E-TEST-01", "name": "E2E 시험 장비", "status": "ACTIVE", "parking_lot_id": 6, "company_id": 3,
                             "company_name": self.company, "parking_lot_name": "E2E-TEST-LOT"}}
        self.key_counter = 0
        self.mutations = []        # (동작, 시리얼) - 장비 등록·키 회전 기록


def parse_multipart(content_type, body):
    msg = BytesParser().parsebytes(b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields, files = {}, {}
    if not msg.is_multipart():
        return None, None
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if part.get_filename():
            files[name] = data
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def make_handler(st):
    args = st.args

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # 조용히
            pass

        def send_json(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        # ------------------------------------------------------------------ GET
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/api/v1/readyz", "/api/v1/healthz"):
                return self.send_json(200, {"status": "ok", "inference": "ready"})
            if path in ("/api/v1/violations", "/api/v1/dashboard/summary", "/api/v1/notifications"):
                if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                    return self.send_json(401, {"error": "unauthorized"})
                return self.send_json(200, {"items": [], "total": 0})
            if path.startswith("/api/v1/machines") or path.startswith("/api/v1/parking-lots/"):
                if self.headers.get("Authorization") != "Bearer mock-token":
                    return self.send_json(401, {"error": "unauthorized"})
                with st.lock:
                    if path == "/api/v1/machines":
                        items = list(st.machines.values())
                        return self.send_json(200, {"items": items, "total": len(items)})
                    if path == "/api/v1/parking-lots/6":
                        return self.send_json(200, {"id": 6, "company_id": 3, "company_name": st.company, "name": "E2E-TEST-LOT", "status": "ACTIVE",
                                                    "machine_count": len(st.machines)})
                return self.send_json(404, {"error": "not_found"})
            if path == "/_mock/stats":
                with st.lock:
                    return self.send_json(200, {
                        "max_inflight_per_device": st.max_inflight, "max_global_inflight": st.max_global_inflight,
                        "order_violations": st.order_violations, "duplicates": st.duplicates, "accepted": st.accepted,
                        "machines": [m["serial_number"] for m in st.machines.values()], "mutations": st.mutations, "bad_requests": st.bad, "injected_failures": st.injected_failures, "lost_responses": st.lost_responses, "p1": st.p1,
                        "arrivals": {k: len(v) for k, v in st.arrivals.items()},
                        "arrival_seqs": {k: [a[0] for a in v] for k, v in st.arrivals.items()}})
            self.send_json(404, {"error": "not_found"})

        # ------------------------------------------------------------------ POST
        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            raw = self.body()
            if path == "/api/v1/auth/login":
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                if args.super_password and body.get("password") != args.super_password:
                    return self.send_json(401, {"error": "invalid_credentials"})
                return self.send_json(200, {"access_token": "mock-token"})
            if path == "/api/v1/machines" or (path.startswith("/api/v1/machines/") and path.endswith("/rotate-key")):
                if self.headers.get("Authorization") != "Bearer mock-token":
                    return self.send_json(401, {"error": "unauthorized"})
                with st.lock:
                    if path == "/api/v1/machines":
                        body = json.loads(raw or b"{}")
                        if any(m["serial_number"] == body.get("serial_number") for m in st.machines.values()):
                            return self.send_json(409, {"error": "이미 등록된 시리얼 번호입니다."})
                        mid = max(st.machines) + 1
                        st.machines[mid] = {"id": mid, "serial_number": body["serial_number"], "name": body.get("name", ""), "status": "ACTIVE",
                                            "parking_lot_id": body["parking_lot_id"], "company_id": 3, "company_name": st.company, "parking_lot_name": "E2E-TEST-LOT"}
                        st.key_counter += 1
                        key = f"created-key-{st.key_counter}"
                        st.keys[key] = str(mid)
                        st.mutations.append(["register", body["serial_number"]])
                        return self.send_json(201, {"machine": st.machines[mid], "api_key": key})
                    mid = int(path.split("/")[4])
                    if mid not in st.machines:
                        return self.send_json(404, {"error": "not_found"})
                    for k in [k for k, v in st.keys.items() if v == str(mid)]:
                        del st.keys[k]
                    st.key_counter += 1
                    key = f"rotated-key-{st.key_counter}"
                    st.keys[key] = str(mid)
                    st.mutations.append(["rotate", st.machines[mid]["serial_number"]])
                    return self.send_json(200, {"machine": st.machines[mid], "api_key": key})
            machine = st.keys.get(self.headers.get("X-Device-Key") or "")
            if path == "/api/v1/device/heartbeat":
                return self.send_json(200 if machine else 401, {"ok": bool(machine)})
            if path == "/api/v1/device/detections":
                return self.phase1(machine, raw)
            if path == "/api/v1/device/detection-jobs":
                return self.phase2(machine, raw)
            self.send_json(404, {"error": "not_found"})

        def phase1(self, machine, raw):
            if not machine:
                return self.send_json(401, {"error": "invalid_device_key"})
            with st.lock:
                if st.p1_active >= args.p1_capacity + args.p1_wait:
                    st.p1["busy"] += 1
                    return self.send_json(503, {"error": "inference_unavailable"})
                st.p1_active += 1
            try:
                time.sleep(args.p1_service_ms / 1000 * (1 + st.p1_active / 10) if args.p1_service_ms else 0)
            finally:
                with st.lock:
                    st.p1_active -= 1
                    st.p1["ok"] += 1
            self.send_json(200, {"verdict": "permitted"})

        def phase2(self, machine, raw):
            if not machine:
                return self.send_json(401, {"error": "invalid_device_key"})
            with st.lock:
                n = st.inflight.get(machine, 0) + 1
                st.inflight[machine] = n
                st.max_inflight[machine] = max(st.max_inflight.get(machine, 0), n)
                st.global_inflight += 1
                st.max_global_inflight = max(st.max_global_inflight, st.global_inflight)
            try:
                received = now()
                time.sleep(max(0.0, (args.latency_ms + st.rng.uniform(0, args.jitter_ms)) / 1000))
                with st.lock:
                    if st.rng.random() < args.fail_rate:
                        st.injected_failures += 1
                        return self.send_json(503, {"error": "ingest_unavailable"})
                fields, files = parse_multipart(self.headers.get("Content-Type", ""), raw)
                if fields is None or "file" not in files or not files["file"] or files["file"][:2] != b"\xff\xd8":
                    with st.lock:
                        st.bad += 1
                    return self.send_json(400, {"error": "file 필드에 JPEG 이미지를 첨부해 주세요."})
                try:
                    event_id = uuid.UUID(fields.get("event_id", "")).hex
                    session_id = uuid.UUID(fields.get("session_id", "")).hex
                    seq = int(fields.get("seq", ""))
                    assert seq >= 1 and fields.get("event") in ("arrived", "confirm", "refresh", "left")
                except (ValueError, AssertionError):
                    with st.lock:
                        st.bad += 1
                    return self.send_json(400, {"error": "필드 형식 오류"})
                with st.lock:
                    st.arrivals.setdefault(machine, []).append((seq, event_id, time.time()))
                    prev = st.last_seq.get((machine, session_id))
                    if prev is not None and seq <= prev:
                        st.order_violations.append([machine, session_id, prev, seq])
                    if prev is None or seq > prev:
                        st.last_seq[(machine, session_id)] = seq
                    dup = st.db.execute("select id, status from detection_jobs where machine_id=? and event_id=?", (int(machine), event_id)).fetchone()
                    if dup:
                        st.duplicates += 1
                        return self.send_json(200, {"job_id": dup[0], "event_id": event_id, "status": dup[1], "duplicate": True, "server_time": iso(now())})
                    job_id = str(uuid.uuid4())
                    pub = received + timedelta(milliseconds=args.publish_ms)
                    st.db.execute("insert into detection_jobs (id, machine_id, event_id, session_id, seq, event, received_at, published_at, status, updated_at) "
                                  "values (?,?,?,?,?,?,?,?,?,?)", (job_id, int(machine), event_id, session_id, seq, fields["event"], iso(received), iso(pub), "queued", iso(received)))
                    st.accepted += 1
                    if st.rng.random() < args.lost_response_rate:   # 서버는 저장했지만 응답이 유실된 상황(장비는 재시도한다 -> 200 duplicate)
                        st.lost_responses += 1
                        return self.send_json(503, {"error": "response_lost"})
                return self.send_json(202, {"job_id": job_id, "event_id": str(uuid.UUID(event_id)), "status": "queued", "duplicate": False, "server_time": iso(now())})
            finally:
                with st.lock:
                    st.inflight[machine] -= 1
                    st.global_inflight -= 1

    return Handler


def worker_loop(st, worker_id):
    """장비별 직렬(같은 장비의 앞 작업이 끝나야 다음) + 여러 Worker 가 서로 다른 장비를 처리."""
    args = st.args
    while not st.worker_stop.is_set():
        job = None
        with st.lock:
            rows = st.db.execute("select id, machine_id, event_id, session_id, seq, event, received_at from detection_jobs "
                                 "where status='queued' order by received_at, seq").fetchall()
            for r in rows:
                if r[1] not in st.busy_machines:
                    job = r
                    st.busy_machines.add(r[1])
                    st.db.execute("update detection_jobs set status='processing', worker_id=?, claim_attempt=claim_attempt+1, lease_until=?, updated_at=? where id=?",
                                  (worker_id, iso(now() + timedelta(seconds=55)), iso(now()), r[0]))
                    break
        if job is None:
            time.sleep(0.01)
            continue
        time.sleep(args.service_ms / 1000)
        with st.lock:
            applied = job[4] > st.last_applied.get(job[1], 0)
            if applied:
                st.last_applied[job[1]] = job[4]
            cur = st.db.execute("insert into detection_results (job_id, machine_id, event_id, session_id, seq, event, received_at, verdict, applied, elapsed_ms, created_at) "
                                "values (?,?,?,?,?,?,?,?,?,?,?)", (job[0], job[1], job[2], job[3], job[4], job[5], job[6], "permitted", int(applied), args.service_ms, iso(now())))
            st.db.execute("update detection_jobs set status=?, result_id=?, lease_until=null, updated_at=? where id=?",
                          ("done" if applied else "stale", cur.lastrowid, iso(now()), job[0]))
            st.busy_machines.discard(job[1])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=18081)
    ap.add_argument("--db", default="mock.sqlite")
    ap.add_argument("--keys", default="test-key-a:7,test-key-b:8,test-key-c:9", help="키:장비번호 쉼표 목록(모의 키만, 실제 키 금지)")
    ap.add_argument("--latency-ms", type=float, default=0, help="접수 요청의 기본 지연")
    ap.add_argument("--jitter-ms", type=float, default=0, help="접수 지연에 0~이 값을 더한다(요청이 뒤섞이는지 보려고)")
    ap.add_argument("--fail-rate", type=float, default=0, help="접수를 503 으로 실패시키는 확률")
    ap.add_argument("--lost-response-rate", type=float, default=0, help="작업은 저장하되 응답을 503 으로 유실시키는 확률(재시도 -> duplicate 시험)")
    ap.add_argument("--publish-ms", type=float, default=5)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--service-ms", type=int, default=100)
    ap.add_argument("--p1-capacity", type=int, default=2)
    ap.add_argument("--p1-wait", type=int, default=2)
    ap.add_argument("--p1-service-ms", type=float, default=100)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--super-password", default="", help="로그인 비밀번호 검사(비어 있으면 아무 값이나 통과). 모의 값만")
    ap.add_argument("--company", default="E2E-TEST-COMPANY")
    args = ap.parse_args(argv)
    st = State(args)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(st))
    srv.daemon_threads = True
    for i in range(args.workers):
        threading.Thread(target=worker_loop, args=(st, f"mock-worker-{i}"), daemon=True).start()
    print(f"mock server on 127.0.0.1:{args.port} db={args.db}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        st.worker_stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
