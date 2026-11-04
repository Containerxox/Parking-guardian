"""부하 테스트 관찰자와 중단 규칙 (읽기 전용). 운영 위험 R3(조용한 정지) · R6(비밀번호 회전) · R7(CPU 자리)을 시험 중에 감지한다.

  resources  타임라인의 Pod CPU/메모리 표본(kubectl top)을 앱별로 요약하고 CPU requests 대비 사용률을 낸다(읽기 전용, 파일만 읽는다)
  preflight  시험 시작 전 한 번: RDS 비밀번호 회전 일정이 시험 창과 겹치는가, Pending Pod, Argo 상태, KEDA Ready, 큐·DLQ 가 비었는가, 1000m 자리 수
  run        시험 중 interval 초마다 표본을 JSONL 로 저장하고 중단 규칙을 평가한다. ABORT 가 나오면 큰 배너 + ABORT.flag 를 쓰고 종료 코드 3.
             (이 도구는 k6 나 클러스터를 멈추지 않는다 — 사람이 Ctrl+C 로 k6 를 멈추고 기록한다. 규칙은 P2_TOOLS.md "중단 기준".)

읽기만 한다: kubectl get/exec(SELECT 만), aws sqs get-queue-attributes, aws cloudwatch get-metric-statistics, aws secretsmanager describe-secret.
쓰기 명령(scale, patch, delete, apply, annotate …)은 이 파일에 없다(시험이 확인한다). 비밀 값(get-secret-value)은 호출하지 않는다.

중단 규칙의 핵심 원칙: "큐가 길다/오래됐다"는 과부하에서 정상이다(1.5C 에서 대기열이 쌓이는 것이 설계 시나리오). 그래서 대기열 크기만으로 중단하지 않고
"진행이 멈췄는가"(새 결과가 없다, Worker 가 안 뜬다, Pod 가 Pending, 재시작, DLQ, 회전, Argo 이상)로 중단한다.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p2_collect as pc  # noqa: E402

for _stream in (sys.stdout, sys.stderr):      # Windows 콘솔(cp949)에서도 한글·기호 출력이 깨지거나 예외가 나지 않게
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

APP_NS, ARGO_NS = "parking-guardian", "argocd"
WATCHED = ("backend", "relay", "worker", "inference", "frontend")


def ts_of(v):
    return pc.ts(v) if v is not None else None


# ---------------------------------------------------------------------------------------------- 파서 (순수)
def parse_pods(text):
    pods = []
    for p in json.loads(text).get("items", []):
        labels = p["metadata"].get("labels", {})
        app = labels.get("app.kubernetes.io/name", "")
        cs = p["status"].get("containerStatuses", [])
        conds = {c["type"]: c for c in p["status"].get("conditions", [])}
        pods.append({"name": p["metadata"]["name"], "app": app, "phase": p["status"].get("phase"), "node": p["spec"].get("nodeName"),
                     "ready": all(c.get("ready") for c in cs) if cs else False, "restarts": sum(c.get("restartCount", 0) for c in cs),
                     "created": p["metadata"].get("creationTimestamp"), "deleting": bool(p["metadata"].get("deletionTimestamp")),
                     "unschedulable": conds.get("PodScheduled", {}).get("reason") == "Unschedulable"})
    return pods


def parse_scaledobject(text):
    o = json.loads(text)
    conds = {c["type"]: c["status"] for c in o.get("status", {}).get("conditions", [])}
    return {"ready": conds.get("Ready"), "active": conds.get("Active"), "paused": o["metadata"].get("annotations", {}).get("autoscaling.keda.sh/paused-replicas"),
            "min": o["spec"].get("minReplicaCount"), "max": o["spec"].get("maxReplicaCount")}


def parse_apps(text):
    return {a["metadata"]["name"]: (a["status"]["sync"]["status"], a["status"]["health"]["status"]) for a in json.loads(text).get("items", [])}


def _milli(v):
    return int(float(v[:-1])) if v.endswith("m") else int(float(v) * 1000)


def _mib(v):
    for suf, mult in (("Ki", 1 / 1024), ("Mi", 1), ("Gi", 1024)):
        if v.endswith(suf):
            return float(v[:-2]) * mult
    return float(v) / (1024 * 1024)


def parse_top_pods(text):
    """`kubectl top pods --no-headers` 출력 → {pod: (cpu_m, mem_mi)}"""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out[parts[0]] = (_milli(parts[1]), _mib(parts[2]))
    return out


def summarize_resources(samples, requests=None):
    """타임라인 표본에서 앱별 CPU(m)·메모리(MiB)의 최대·평균·표본 수와 CPU requests 대비 비율. 같은 표본의 pods 목록으로 Pod→앱을 맞춘다.
    kubectl top 은 약 15~60 초 평균이라 순간 최대가 아니다 - 'requests 를 넘었는가'의 대략적인 증거일 뿐이다."""
    requests = requests or CPU_REQUEST_M
    acc = {}
    for s in samples:
        top, pods = s.get("top"), {p["name"]: p["app"] for p in (s.get("pods") or [])}
        if not top:
            continue
        for pod, (cpu, mem) in top.items():
            app = pods.get(pod)
            if app is None:
                continue
            a = acc.setdefault(app, {"cpu": [], "mem": [], "pods": set()})
            a["cpu"].append(cpu); a["mem"].append(mem); a["pods"].add(pod)
    out = {}
    for app, a in acc.items():
        req = requests.get(app)
        out[app] = {"samples": len(a["cpu"]), "pods_seen": len(a["pods"]), "cpu_m_max": max(a["cpu"]), "cpu_m_mean": round(sum(a["cpu"]) / len(a["cpu"]), 1),
                    "mem_mi_max": round(max(a["mem"]), 1), "mem_mi_mean": round(sum(a["mem"]) / len(a["mem"]), 1), "cpu_request_m": req,
                    "cpu_max_over_request": (round(max(a["cpu"]) / req, 2) if req else None), "cpu_mean_over_request": (round(sum(a["cpu"]) / len(a["cpu"]) / req, 2) if req else None),
                    "exceeded_request": bool(req and max(a["cpu"]) > req)}
    return out


def parse_sqs_attrs(text):
    a = json.loads(text)["Attributes"]
    return {"visible": int(a.get("ApproximateNumberOfMessages", 0)), "not_visible": int(a.get("ApproximateNumberOfMessagesNotVisible", 0)),
            "delayed": int(a.get("ApproximateNumberOfMessagesDelayed", 0))}


def parse_secret(text):
    d = json.loads(text)
    return {"last_rotated": ts_of(d.get("LastRotatedDate")), "next_rotation": ts_of(d.get("NextRotationDate")), "rotation_enabled": d.get("RotationEnabled")}


def cpu_slots(nodes_json, pods_json, need_m=1000):
    """노드별 남는 CPU 요청 여유(m)와 need_m 짜리가 들어가는 자리 수. 요청 합은 종료된 Pod 를 뺀 Pod 의 컨테이너 requests."""
    def milli(v):
        v = str(v)
        return int(float(v[:-1])) if v.endswith("m") else int(float(v) * 1000)
    free = {}
    for n in json.loads(nodes_json)["items"]:
        free[n["metadata"]["name"]] = milli(n["status"]["allocatable"]["cpu"])
    for p in json.loads(pods_json)["items"]:
        node = p["spec"].get("nodeName")
        if node not in free or p["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        free[node] -= sum(milli(c.get("resources", {}).get("requests", {}).get("cpu", "0m")) for c in p["spec"]["containers"])
    return {"free_m": free, "slots": sum(1 for v in free.values() if v >= need_m), "need_m": need_m}


# ---------------------------------------------------------------------------------------------- 중단 규칙 (순수, 상태 보유)
class Config:
    def __init__(self, **kw):
        self.stall_after_s = 60          # Worker 가 있는데 이만큼 새 결과가 없으면 정지
        self.scaleup_wait_s = 90         # 일이 있는데 Worker 0 으로 이만큼 지속하면 KEDA 확장 실패
        self.pending_pod_s = 60          # Pod 가 Pending 으로 이만큼 지속하면 CPU 자리 부족
        self.relay_stall_s = 30          # pending_publish 가 이만큼 오래된 채로 있으면 Relay 정지
        self.sqs_age_warn_s = 120
        self.sqs_age_abort_s = 900
        self.expect_workers = None       # 고정 실험이면 N (paused-replicas 와 맞는지 본다)
        self.allow_outofsync = ()        # Phase 1 고정 구성에서는 ("parking-guardian",)
        self.source_error_abort = 3      # 표본 수집 실패가 연속 이만큼이면 ABORT
        self.queue_url = self.dlq_url = self.secret_id = self.queue_name = ""
        self.__dict__.update(kw)


class Watcher:
    def __init__(self, cfg=None, baseline_rotated=None):
        self.cfg = cfg or Config()
        self.baseline_rotated = baseline_rotated
        self.restarts0 = None
        self.since = {}                  # 조건 이름 -> 처음 관찰된 시각
        self.last_result_ts = None
        self.errors = 0

    def _hold(self, name, active, now):
        if not active:
            self.since.pop(name, None)
            return 0.0
        self.since.setdefault(name, now)
        return now - self.since[name]

    def step(self, s):
        """표본 하나를 평가한다. 반환: [{'level': 'ABORT'|'WARN', 'code': ..., 'msg': ...}]"""
        c, now, out = self.cfg, s["ts"], []
        add = lambda lvl, code, msg: out.append({"level": lvl, "code": code, "msg": msg})  # noqa: E731
        if s.get("errors"):
            self.errors += 1
            add("ABORT" if self.errors >= c.source_error_abort else "WARN", "SOURCE_ERROR", "표본 수집 실패: " + "; ".join(s["errors"])[:200])
        else:
            self.errors = 0
        pods = s.get("pods") or []
        workers = [p for p in pods if p["app"] == "worker" and not p["deleting"]]
        w_ready = sum(1 for p in workers if p["ready"] and p["phase"] == "Running")
        w_all = len(workers)

        # R7: 자리 부족
        pending = [p for p in pods if p["phase"] == "Pending"]
        held = self._hold("pending_pod", bool(pending), now)
        if pending and held >= c.pending_pod_s:
            add("ABORT", "POD_PENDING", f"Pod {','.join(p['name'] for p in pending)[:80]} 가 {held:.0f}s 째 Pending(CPU 자리 부족 R7)")
        # 재시작
        total = {p["name"]: p["restarts"] for p in pods if p["app"] in WATCHED}
        if self.restarts0 is None:
            self.restarts0 = dict(total)
        grew = [n for n, r in total.items() if r > self.restarts0.get(n, r)]
        if grew:
            add("ABORT", "POD_RESTART", f"재시작 증가: {','.join(grew)[:100]}")
        # Argo
        for app, (sync, health) in (s.get("apps") or {}).items():
            if health != "Healthy":
                add("ABORT", "ARGO_UNHEALTHY", f"{app} health={health}")
            if sync != "Synced" and app not in c.allow_outofsync:
                add("ABORT", "ARGO_OUTOFSYNC", f"{app} sync={sync}")
        # KEDA
        keda = s.get("keda")
        if keda is not None:
            if keda["ready"] != "True":
                add("ABORT", "KEDA_NOT_READY", f"ScaledObject Ready={keda['ready']}")
            if c.expect_workers is not None and str(keda["paused"]) != str(c.expect_workers):
                add("WARN", "KEDA_PAUSE_MISMATCH", f"paused-replicas={keda['paused']} (기대 {c.expect_workers})")
        # 대기열
        db = s.get("db") or {}
        sqs, dlq = s.get("sqs") or {}, s.get("dlq") or {}
        work_pending = (db.get("pending_total") or 0) > 0 or (sqs.get("visible") or 0) > 0
        if dlq and (dlq.get("visible", 0) + dlq.get("not_visible", 0)) > 0:
            add("ABORT", "DLQ_MESSAGE", f"DLQ 에 메시지 {dlq['visible'] + dlq['not_visible']}개 (처리 실패 3회 초과)")
        age = s.get("sqs_age_s")
        if age is not None and age >= c.sqs_age_abort_s:
            add("ABORT", "SQS_OLD_MESSAGE", f"가장 오래된 메시지 {age:.0f}s")
        elif age is not None and age >= c.sqs_age_warn_s:
            add("WARN", "SQS_AGE", f"가장 오래된 메시지 {age:.0f}s (과부하 단계에서는 정상일 수 있다)")
        # R3: 진행 멈춤 — 일이 있는데 새 결과가 없다
        last = db.get("last_result_ts")
        if last is not None:
            self.last_result_ts = max(self.last_result_ts or 0, last)
        if work_pending and w_ready >= 1:
            ref = self.last_result_ts if self.last_result_ts is not None else self.since.setdefault("work_start", now)
            idle = now - ref
            if idle >= c.stall_after_s:
                add("ABORT", "WORKER_STALL", f"Worker {w_ready}개 Ready, 대기 작업 있음, 새 판정 없음 {idle:.0f}s (R3 조용한 정지)")
        else:
            self.since.pop("work_start", None)
        held = self._hold("no_worker", work_pending and w_all == 0, now)
        if held >= c.scaleup_wait_s:
            add("ABORT", "KEDA_NO_SCALEUP", f"일이 있는데 Worker 0개가 {held:.0f}s 지속(KEDA 확장 실패)")
        if (db.get("oldest_pending_publish_age_s") or 0) >= c.relay_stall_s:
            add("ABORT", "RELAY_STALL", f"pending_publish 작업이 {db['oldest_pending_publish_age_s']:.0f}s 째 발행되지 않음(Relay)")
        # R6: 회전
        sec = s.get("secret") or {}
        if self.baseline_rotated is not None and sec.get("last_rotated") and sec["last_rotated"] != self.baseline_rotated:
            add("ABORT", "RDS_ROTATED", "RDS 비밀번호가 시험 중 회전됐다(R6). runbook-rds-rotation.md 대로 갱신·재시작 전에는 결과가 무효")
        return out


def preflight_rotation(secret, now, window_h):
    """시험 창(window_h 시간) 안에 회전 예정이 있으면 문제 목록."""
    problems = []
    nxt = secret.get("next_rotation")
    if secret.get("rotation_enabled") and nxt is not None and nxt - now < window_h * 3600:
        problems.append(f"다음 회전 예정이 시험 창 안이다({(nxt - now) / 3600:.1f} 시간 뒤 < {window_h}h)")
    return problems


def scale_events(samples):
    """타임라인에서 KEDA/Worker 확장 시각을 뽑는다 (Worker Pod 수, Ready 수, ScaledObject Active 변화)."""
    ev, prev = [], {"replicas": None, "ready": None, "active": None}
    for s in samples:
        workers = [p for p in (s.get("pods") or []) if p["app"] == "worker" and not p["deleting"]]
        cur = {"replicas": len(workers), "ready": sum(1 for p in workers if p["ready"]),
               "active": (s.get("keda") or {}).get("active")}
        for k in cur:
            if prev[k] is not None and cur[k] != prev[k]:
                ev.append({"ts": s["ts"], "kind": f"worker_{k}" if k != "active" else "keda_active", "from": prev[k], "to": cur[k]})
            prev[k] = cur[k]
    return ev


# ---------------------------------------------------------------------------------------------- 표본 수집 (읽기 전용 명령만)
DB_QUERIES = {
    "pending": "select status, count(*) n, min(received_at) oldest from detection_jobs where status in ('pending_publish','queued','processing') group by status",
    "last": "select max(created_at) last_result from detection_results",
}
KUBECTL_GET_KINDS = {"pods", "scaledobject", "app", "nodes", "hpa", "deploy", "events"}      # secret 은 없다
KUBECTL_TOP_KINDS = {"pods", "nodes"}
CPU_REQUEST_M = {"worker": 1000, "inference": 1000, "backend": 200, "relay": 50, "frontend": 50}   # 매니페스트의 requests.cpu (m)
AWS_ALLOWED = {("sqs", "get-queue-attributes"), ("cloudwatch", "get-metric-statistics"), ("cloudwatch", "get-metric-data"), ("secretsmanager", "describe-secret")}


def collect_sample(runner, cfg, now=None):
    now = now or time.time()
    s, errors = {"ts": now}, []

    def attempt(name, fn):
        try:
            s[name] = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}")
    attempt("pods", lambda: parse_pods(runner(["kubectl", "get", "pods", "-n", APP_NS, "-o", "json"])))
    attempt("keda", lambda: parse_scaledobject(runner(["kubectl", "get", "scaledobject", "worker", "-n", APP_NS, "-o", "json"])))
    attempt("apps", lambda: parse_apps(runner(["kubectl", "get", "app", "-n", ARGO_NS, "-o", "json"])))
    attempt("top", lambda: parse_top_pods(runner(["kubectl", "top", "pods", "-n", APP_NS, "--no-headers"])))
    if cfg.queue_url:
        attempt("sqs", lambda: parse_sqs_attrs(runner(["aws", "sqs", "get-queue-attributes", "--queue-url", cfg.queue_url, "--attribute-names", "All", "--output", "json"])))
        attempt("sqs_age_s", lambda: sqs_age(runner, cfg.queue_name, now))
    if cfg.dlq_url:
        attempt("dlq", lambda: parse_sqs_attrs(runner(["aws", "sqs", "get-queue-attributes", "--queue-url", cfg.dlq_url, "--attribute-names", "All", "--output", "json"])))
    if cfg.secret_id:
        attempt("secret", lambda: parse_secret(runner(["aws", "secretsmanager", "describe-secret", "--secret-id", cfg.secret_id, "--output", "json"])))

    def db_sample():
        script = pc.POD_SCRIPT.encode("utf-8")
        arg = json.dumps({"lo": pc.iso_utc(now), "hi": pc.iso_utc(now), "queries": DB_QUERIES})
        rows = json.loads(runner(["kubectl", "exec", "-i", "deploy/backend", "-n", APP_NS, "--", "python", "-", arg], input=script).strip().splitlines()[-1])
        total = sum(r["n"] for r in rows["pending"])
        pub = [r for r in rows["pending"] if r["status"] == "pending_publish"]
        oldest_pub = (now - pc.ts(pub[0]["oldest"])) if pub else 0
        last = rows["last"][0]["last_result"] if rows["last"] else None
        return {"pending_total": total, "by_status": {r["status"]: r["n"] for r in rows["pending"]},
                "oldest_pending_publish_age_s": oldest_pub, "last_result_ts": pc.ts(last) if last else None}
    attempt("db", db_sample)
    if errors:
        s["errors"] = errors
    return s


def sqs_age(runner, queue_name, now):
    out = runner(["aws", "cloudwatch", "get-metric-statistics", "--namespace", "AWS/SQS", "--metric-name", "ApproximateAgeOfOldestMessage", "--dimensions",
                  f"Name=QueueName,Value={queue_name}", "--start-time", pc.iso_utc(now - 300), "--end-time", pc.iso_utc(now), "--period", "60",
                  "--statistics", "Maximum", "--output", "json"])
    pts = sorted(json.loads(out)["Datapoints"], key=lambda p: pc.ts(p["Timestamp"]))
    return pts[-1]["Maximum"] if pts else 0.0


def readonly_runner(inner=None):
    """쓰기 명령은 물론 비밀을 읽는 명령도 실행하지 못하게 감싼다. 허용 목록(그 밖은 모두 거부):
    kubectl get <pods|scaledobject|app|nodes|hpa|deploy|events>, kubectl exec 는 고정된 SELECT 스크립트(POD_SCRIPT)만,
    aws sqs get-queue-attributes, aws cloudwatch get-metric-statistics|get-metric-data, aws secretsmanager describe-secret(값이 아니라 회전 시각)."""
    inner = inner or pc.default_runner

    def run(cmd, input=None):
        ok = False
        if cmd[0] == "kubectl" and len(cmd) > 2 and cmd[1] == "get":
            ok = cmd[2] in KUBECTL_GET_KINDS
        elif cmd[0] == "kubectl" and len(cmd) > 2 and cmd[1] == "top":
            ok = cmd[2] in KUBECTL_TOP_KINDS
        elif cmd[0] == "kubectl" and len(cmd) > 2 and cmd[1] == "exec":
            ok = "python" in cmd and input is not None and pc.POD_SCRIPT.encode() == input
        elif cmd[0] == "aws" and len(cmd) > 2:
            ok = (cmd[1], cmd[2]) in AWS_ALLOWED
        if not ok:
            raise PermissionError(f"읽기 전용 관찰자가 허용하지 않는 명령: {' '.join(cmd[:3])}")
        return inner(cmd, input=input) if input is not None else inner(cmd)
    return run


def banner(alerts):
    lines = ["", "!" * 70] + [f"  ABORT  {a['code']}: {a['msg']}" for a in alerts if a["level"] == "ABORT"] + \
            ["  시험을 멈추고(k6 Ctrl+C) 시각·상태를 기록하세요. 이 도구는 아무것도 멈추지 않았습니다.", "!" * 70, ""]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["preflight", "run", "resources"])
    ap.add_argument("--out", default="timeline.jsonl"); ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--duration", type=float, default=0, help="초. 0 이면 Ctrl+C 까지")
    ap.add_argument("--queue-url", default=""); ap.add_argument("--dlq-url", default=""); ap.add_argument("--secret-id", default="")
    ap.add_argument("--expect-workers", type=int); ap.add_argument("--allow-outofsync", default=""); ap.add_argument("--window-h", type=float, default=4.0)
    a = ap.parse_args(argv)
    if a.cmd == "resources":
        samples = [json.loads(line) for line in Path(a.out).read_text(encoding="utf-8").splitlines() if line.strip()]
        print(json.dumps(summarize_resources(samples), indent=1, ensure_ascii=False))
        return 0
    cfg = Config(expect_workers=a.expect_workers, allow_outofsync=tuple(x for x in a.allow_outofsync.split(",") if x))
    cfg.queue_url, cfg.dlq_url, cfg.secret_id = a.queue_url, a.dlq_url, a.secret_id
    cfg.queue_name = a.queue_url.rsplit("/", 1)[-1] if a.queue_url else ""
    run = readonly_runner()
    if a.cmd == "preflight":
        s = collect_sample(run, cfg)
        problems = [x for x in (s.get("errors") or [])]
        if s.get("secret"):
            problems += preflight_rotation(s["secret"], s["ts"], a.window_h)
        problems += [f"{p['name']} Pending" for p in s.get("pods", []) if p["phase"] == "Pending"]
        problems += [f"Argo {k} {v}" for k, v in (s.get("apps") or {}).items() if v[1] != "Healthy" or (v[0] != "Synced" and k not in cfg.allow_outofsync)]
        if s.get("keda") and s["keda"]["ready"] != "True":
            problems.append("KEDA ScaledObject Ready 아님")
        for q in ("sqs", "dlq"):
            if s.get(q) and s[q]["visible"] + s[q]["not_visible"] > 0:
                problems.append(f"{q} 큐가 비어 있지 않다")
        slots = cpu_slots(run(["kubectl", "get", "nodes", "-o", "json"]), run(["kubectl", "get", "pods", "-A", "-o", "json"]))
        print(json.dumps({"slots_1000m": slots["slots"], "free_m": slots["free_m"], "problems": problems}, ensure_ascii=False, indent=1))
        return 2 if problems else 0
    baseline = None
    w = Watcher(cfg)
    out = Path(a.out)
    end = time.time() + a.duration if a.duration else None
    while end is None or time.time() < end:
        s = collect_sample(run, cfg)
        if baseline is None and s.get("secret"):
            baseline = s["secret"]["last_rotated"]
            w.baseline_rotated = baseline
        alerts = w.step(s)
        s["alerts"] = alerts
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(s, default=str, ensure_ascii=False) + "\n")
        for al in alerts:
            print(f"[{datetime.fromtimestamp(s['ts'], timezone.utc):%H:%M:%S}] {al['level']} {al['code']}: {al['msg']}", flush=True)
        if any(al["level"] == "ABORT" for al in alerts):
            print(banner(alerts), flush=True)
            out.with_name("ABORT.flag").write_text(json.dumps(alerts, ensure_ascii=False), encoding="utf-8")
            return 3
        time.sleep(a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
