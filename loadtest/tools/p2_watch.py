"""부하 테스트 관찰자와 중단 규칙 (읽기 전용). 운영 위험 R3(조용한 정지) · R6(비밀번호 회전) · R7(CPU 자리)을 시험 중에 감지한다.

  preflight  시험 시작 전 한 번: RDS 비밀번호 회전 일정이 시험 창과 겹치는가, Pending Pod, Argo 상태, KEDA Ready, 큐·DLQ 가 비었는가, 1000m 자리 수
  run        시험 중 interval 초마다 표본을 JSONL 로 저장하고 중단 규칙을 평가한다. ABORT 가 나면 큰 배너 + ABORT.flag 를 쓰고 종료 코드 3.
             (이 도구는 k6 나 클러스터를 멈추지 않는다 - 사람이 Ctrl+C 로 k6 를 멈추고 기록한다. 규칙은 P2_TOOLS.md "중단 기준".)
  resources  타임라인의 CPU/메모리를 앱별로 요약한다: kubectl top 표본(해상도 낮음)과 cAdvisor 누적 카운터(정확한 CPU 초)를 구분해 보여 준다
  summarize  타임라인의 표본 간격·소스별 소요·실패, 확장/축소 이벤트, 대기열 에피소드(짧은 적체/긴 적체/정지)를 요약한다
  keda-log   KEDA 연산자 로그에서 확장·축소 결정 시각을 파일로 저장한다(읽기 전용, 파드가 있는 동안만)

읽기만 한다: kubectl get/top/logs/exec(SELECT 만), `kubectl get --raw` 노드 cAdvisor 지표, AWS SQS 속성·CloudWatch 지표·Secrets Manager describe(값이 아니라 회전 시각).
쓰기 명령(scale, patch, delete, apply, annotate …)은 이 파일에 없다(시험이 확인한다). 비밀 값(get-secret-value)은 호출하지 않는다.
수집 방식(2026-11-04 개선): 소스마다 독립 폴러 + 고정 주기 틱 + 소스별 시작·완료 시각 기록. 설명과 한계는 p2_sampler.py 머리말과 P2_OBSERVABILITY.md.

중단 규칙의 핵심 원칙: "큐가 길다/오래됐다"는 과부하에서 정상이다(1.5C 에서 대기열이 쌓이는 것이 설계 시나리오). 그래서 대기열 크기만으로 중단하지 않고
"진행이 멈췄는가"(새 결과가 없다, Worker 가 안 뜬다, Pod 가 Pending, 재시작, DLQ, 회전, Argo 이상)로 중단한다.
수집이 늦거나 값이 낡았을 때는 시간에 기반한 ABORT(정지·확장 실패·Pending·Relay)를 보류하고 경고로만 남긴다 - 늦은 표본이 거짓 중단을 만들지 않게.
"""
import argparse
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p2_collect as pc  # noqa: E402
import p2_sampler as sp  # noqa: E402

for _stream in (sys.stdout, sys.stderr):      # Windows 콘솔(cp949)에서도 한글·기호 출력이 깨지거나 예외가 나지 않게
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

APP_NS, ARGO_NS, KEDA_NS = "parking-guardian", "argocd", "keda"
WATCHED = ("backend", "relay", "worker", "inference", "frontend")
CRITICAL_SOURCES = ("pods", "db")
TIME_BASED = {"POD_PENDING", "WORKER_STALL", "KEDA_NO_SCALEUP", "RELAY_STALL"}


def ts_of(v):
    return pc.ts(v) if v is not None else None


# ---------------------------------------------------------------------------------------------- 파서 (순수)
def parse_pods(text):
    """Pod 목록. Worker 단계 측정을 위해 생성·배치·init 종료·컨테이너 시작·Ready 시각(ISO)도 함께 담는다(없으면 None)."""
    pods = []
    for p in json.loads(text).get("items", []):
        labels = p["metadata"].get("labels", {})
        app = labels.get("app.kubernetes.io/name", "")
        st = p["status"]
        cs = st.get("containerStatuses", [])
        conds = {c["type"]: c for c in st.get("conditions", [])}
        init_done = [i.get("state", {}).get("terminated", {}).get("finishedAt") for i in st.get("initContainerStatuses", [])]
        started = [c.get("state", {}).get("running", {}).get("startedAt") for c in cs]
        pods.append({"name": p["metadata"]["name"], "app": app, "phase": st.get("phase"), "node": p["spec"].get("nodeName"),
                     "ready": all(c.get("ready") for c in cs) if cs else False, "restarts": sum(c.get("restartCount", 0) for c in cs),
                     "created": p["metadata"].get("creationTimestamp"), "deleting": bool(p["metadata"].get("deletionTimestamp")),
                     "unschedulable": conds.get("PodScheduled", {}).get("reason") == "Unschedulable",
                     "scheduled_at": conds.get("PodScheduled", {}).get("lastTransitionTime") if conds.get("PodScheduled", {}).get("status") == "True" else None,
                     "init_done_at": max([x for x in init_done if x], default=None) if init_done and all(init_done) else None,
                     "started_at": min([x for x in started if x], default=None),
                     "ready_at": conds.get("Ready", {}).get("lastTransitionTime") if conds.get("Ready", {}).get("status") == "True" else None})
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


CPU_REQUEST_M = {"worker": 1000, "inference": 1000, "backend": 200, "relay": 50, "frontend": 50}   # 매니페스트의 requests.cpu (m)


def summarize_resources(samples, requests=None):
    """kubectl top 표본(metrics-server 평균, 해상도 낮음)을 앱별로 요약. **짧은 CPU 버스트를 놓친다** - 스모크에서 Worker 최대가 1m 으로 나왔다.
    정확한 CPU 사용량은 cAdvisor 누적 카운터(summarize_cadvisor)로 본다."""
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
        out[app] = {"source": "kubectl top (metrics-server)", "samples": len(a["cpu"]), "pods_seen": len(a["pods"]), "cpu_m_max": max(a["cpu"]),
                    "cpu_m_mean": round(sum(a["cpu"]) / len(a["cpu"]), 1), "mem_mi_max": round(max(a["mem"]), 1), "mem_mi_mean": round(sum(a["mem"]) / len(a["mem"]), 1),
                    "cpu_request_m": req, "cpu_max_over_request": (round(max(a["cpu"]) / req, 2) if req else None),
                    "cpu_mean_over_request": (round(sum(a["cpu"]) / len(a["cpu"]) / req, 2) if req else None), "exceeded_request": bool(req and max(a["cpu"]) > req)}
    return out


def summarize_cadvisor(samples, requests=None, window_start=None):
    """타임라인의 cAdvisor 스냅샷(누적 CPU 초)으로 앱별 평균·구간 최대 CPU, 총 CPU 초, requests 대비 비율을 낸다. 스냅샷이 없으면 빈 dict."""
    snaps = [{"t": s["cadvisor"]["t"], "pods": s["cadvisor"]["pods"]} for s in samples if s.get("cadvisor") and s["cadvisor"].get("pods")]
    uniq, seen = [], set()
    for x in snaps:                                           # 같은 스냅샷이 여러 틱에 반복되므로 시각이 같은 것은 한 번만
        if x["t"] not in seen:
            seen.add(x["t"])
            uniq.append(x)
    snaps = uniq
    if len(snaps) < 2:
        return {}
    pod_app, created = {}, {}
    for s in samples:
        for p in s.get("pods") or []:
            pod_app[p["name"]] = p["app"]
            if p.get("created"):
                created[p["name"]] = pc.ts(p["created"])
    out = sp.cpu_rates(snaps, pod_app, created, window_start if window_start is not None else snaps[0]["t"], requests or CPU_REQUEST_M)
    for a in out.values():
        a["source"] = "cAdvisor 누적 카운터 (container_cpu_usage_seconds_total)"
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
        self.source_error_abort = 3      # 핵심 소스(pods, db)가 연속 이만큼 실패하면 ABORT (그 밖의 소스는 경고만)
        self.queue_url = self.dlq_url = self.secret_id = self.queue_name = ""
        # 수집 주기(초): 빠른 소스는 interval, 느린/드문 소스는 따로. AWS 호출 상한은 p2_sampler.py 머리말
        self.fast_period_s = 15
        self.db_period_s = 30            # kubectl exec 는 backend Pod 에서 앱을 import 하므로 자주 부르면 시험 대상(backend)을 흔든다
        self.cloudwatch_period_s = 60    # CloudWatch SQS 지표는 1분 스냅샷이라 더 자주 읽어도 같은 값이다
        self.secret_period_s = 300
        self.cadvisor_period_s = 0       # 0 = 끔. 지속 부하 시험에서 30 으로 켠다(노드 cAdvisor 누적 카운터)
        self.call_timeout_s = 15         # 외부 호출 하나의 제한 시간
        self.exec_timeout_s = 40         # kubectl exec(DB 읽기)의 제한 시간
        self.max_age_s = None            # 값이 이보다 오래되면 표본에서 뺀다(None = 소스 주기의 2배, 최소 3×interval)
        self.worker_logs = False         # 새 Worker Pod 로그에서 시작·모델 로딩 줄을 모은다(run 에서만 켠다)
        self.__dict__.update(kw)


class Watcher:
    def __init__(self, cfg=None, baseline_rotated=None):
        self.cfg = cfg or Config()
        self.baseline_rotated = baseline_rotated
        self.restarts0 = None
        self.since = {}                  # 조건 이름 -> 처음 관찰된 시각
        self.last_result_ts = None
        self.errors = 0                  # (옛 형식 표본용) 연속 실패 수
        self.fails = {}                  # 소스별 연속 실패 수

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
        sources = s.get("sources")
        if sources is not None:                                   # 새 형식: 소스별 연속 실패를 따로 센다
            for name, info in sources.items():
                bad = info.get("ok") is False or info.get("stale")
                self.fails[name] = self.fails.get(name, 0) + 1 if bad else 0
                if bad:
                    why = info.get("error") or ("낡은 값" if info.get("stale") else "")
                    lvl = "ABORT" if name in CRITICAL_SOURCES and self.fails[name] >= c.source_error_abort else "WARN"
                    add(lvl, "SOURCE_ERROR", f"소스 {name} 연속 {self.fails[name]}회 실패/낡음: {why}"[:200])
            tick = s.get("tick") or {}
            if tick.get("missed"):
                add("WARN", "SAMPLE_MISSED", f"틱 {tick['missed']}개를 건너뜀(표본이 늦었다)")
            elif tick.get("late_s", 0) > 2 * c.fast_period_s:
                add("WARN", "SAMPLE_LATE", f"표본이 {tick['late_s']:.0f}s 늦음")
        elif s.get("errors"):                                     # 옛 형식(소스 정보 없음)
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
        # R3: 진행 멈춤 - 일이 있는데 새 결과가 없다. DB 값이 없거나 낡았으면(표본에서 빠진다) 판단하지 않는다.
        last = db.get("last_result_ts")
        if last is not None:
            self.last_result_ts = max(self.last_result_ts or 0, last)
        ref_now = (db.get("read_t") or ((sources or {}).get("db") or {}).get("t_end") or now)   # 판단 시각 = DB 를 실제로 읽은 시각
        if db and work_pending and w_ready >= 1:
            ref = self.last_result_ts if self.last_result_ts is not None else self.since.setdefault("work_start", ref_now)
            idle = ref_now - ref
            if idle >= c.stall_after_s:
                add("ABORT", "WORKER_STALL", f"Worker {w_ready}개 Ready, 대기 작업 있음, 새 판정 없음 {idle:.0f}s (R3 조용한 정지)")
        else:
            self.since.pop("work_start", None)
        held = self._hold("no_worker", bool(db) and work_pending and w_all == 0, now)
        if held >= c.scaleup_wait_s:
            add("ABORT", "KEDA_NO_SCALEUP", f"일이 있는데 Worker 0개가 {held:.0f}s 지속(KEDA 확장 실패)")
        if (db.get("oldest_pending_publish_age_s") or 0) >= c.relay_stall_s:
            add("ABORT", "RELAY_STALL", f"pending_publish 작업이 {db['oldest_pending_publish_age_s']:.0f}s 째 발행되지 않음(Relay)")
        # R6: 회전
        sec = s.get("secret") or {}
        if self.baseline_rotated is not None and sec.get("last_rotated") and sec["last_rotated"] != self.baseline_rotated:
            add("ABORT", "RDS_ROTATED", "RDS 비밀번호가 시험 중 회전됐다(R6). runbook-rds-rotation.md 대로 갱신·재시작 전에는 결과가 무효")
        # 늦은/낡은 표본의 시간 기반 ABORT 는 보류한다(거짓 중단 방지). 상태 사실에 기반한 ABORT(DLQ, 재시작, 회전, Argo, KEDA Ready)는 그대로.
        if s.get("degraded"):
            for a in out:
                if a["level"] == "ABORT" and a["code"] in TIME_BASED:
                    a["level"], a["msg"] = "WARN", a["msg"] + " [표본이 늦거나 낡아 ABORT 보류 - 다음 정상 표본에서 다시 판단]"
        return out


def preflight_rotation(secret, now, window_h):
    """시험 창(window_h 시간) 안에 회전 예정이 있으면 문제 목록."""
    problems = []
    nxt = secret.get("next_rotation")
    if secret.get("rotation_enabled") and nxt is not None and nxt - now < window_h * 3600:
        problems.append(f"다음 회전 예정이 시험 창 안이다({(nxt - now) / 3600:.1f} 시간 뒤 < {window_h}h)")
    return problems


def scale_events(samples):
    """타임라인에서 KEDA/Worker 확장 시각을 뽑는다 (Worker Pod 수, Ready 수, ScaledObject Active 변화). 시각은 '그 변화를 처음 본 표본'의 시각이다."""
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


# ---------------------------------------------------------------------------------------------- 타임라인 요약 (표본 간격, 소스별 소요, 대기열 에피소드)
def _pct(values, q):
    return None if not values else round(pc.pm.percentile(values, q), 3)


def cadence_stats(samples):
    """표본 사이 간격과 소스별 호출 소요. 소스 정보가 없는 옛 타임라인은 간격만 계산한다."""
    ts = [s["ts"] for s in samples]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    out = {"samples": len(samples), "gap_s": {"p50": _pct(gaps, 50), "p95": _pct(gaps, 95), "max": round(max(gaps), 3) if gaps else None},
           "missed_ticks": sum((s.get("tick") or {}).get("missed", 0) for s in samples),
           "late_s_max": max([(s.get("tick") or {}).get("late_s", 0) for s in samples], default=0), "degraded_samples": sum(1 for s in samples if s.get("degraded")), "sources": {}}
    seen = set()
    for s in samples:
        for name, info in (s.get("sources") or {}).items():
            key = (name, info.get("t_end"))
            if info.get("t_end") is None or key in seen:         # 같은 호출 결과가 여러 틱에 반복되므로 호출당 한 번만 센다
                continue
            seen.add(key)
            d = out["sources"].setdefault(name, {"calls": 0, "failed": 0, "ms": []})
            d["calls"] += 1
            d["failed"] += 0 if info.get("ok", True) else 1
            d["ms"].append(info.get("ms") or 0)
    for d in out["sources"].values():
        ms = d.pop("ms")
        d.update(ms_p50=_pct(ms, 50), ms_p95=_pct(ms, 95), ms_max=max(ms))
    return out


def backlog_episodes(samples, short_s=60, stall_s=60):
    """DB 의 미완료 작업 수(pending_publish+queued+processing)가 0 보다 큰 구간(에피소드)을 찾는다. CloudWatch 1분 지표가 놓치는 짧은 적체를 보완한다.
    duration_min = 처음·마지막으로 본 시각 차, duration_max = (직전 0 이던 표본)~(다시 0 이 된 표본) 차 - 표본 간격만큼의 불확실성을 숨기지 않는다.
    kind: short(최대 지속시간 <= short_s) / long / stalled(Worker 가 Ready 인데 판정이 stall_s 이상 안 늘었다) / open(아직 안 끝남)."""
    rows = [(((s.get("db") or {}).get("read_t") or s["ts"]), s) for s in samples if s.get("db")]
    gaps = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
    res, cur, prev_zero = [], None, None
    for t, s in rows:
        n = s["db"].get("pending_total") or 0
        ready = any(p["app"] == "worker" and p["ready"] for p in (s.get("pods") or []))
        if n > 0:
            if cur is None:
                cur = {"start_seen": t, "prev_zero": prev_zero, "max_pending": 0, "last_seen": t, "worker_ready_seen": False, "first_result": s["db"].get("last_result_ts"), "last_result": None}
            cur["max_pending"] = max(cur["max_pending"], n)
            cur["last_seen"] = t
            cur["worker_ready_seen"] = cur["worker_ready_seen"] or ready
            cur["last_result"] = s["db"].get("last_result_ts")
        else:
            if cur is not None:
                cur["end_seen"] = t
                res.append(cur)
                cur = None
            prev_zero = t
    if cur is not None:
        res.append(cur)
    out = []
    for e in res:
        end = e.get("end_seen")
        dmin = e["last_seen"] - e["start_seen"]
        dmax = (end - e["prev_zero"]) if (end is not None and e["prev_zero"] is not None) else None
        progressed = (e["last_result"] or 0) > (e["first_result"] or 0)
        if end is None:
            kind = "open"
        elif dmax is not None and dmax <= short_s:
            kind = "short"
        else:
            kind = "long"
        if e["worker_ready_seen"] and not progressed and dmin >= stall_s:
            kind = "stalled"
        out.append({"start_seen_ts": round(e["start_seen"], 1), "end_seen_ts": None if end is None else round(end, 1), "max_pending": e["max_pending"],
                    "duration_min_s": round(dmin, 1), "duration_max_s": None if dmax is None else round(dmax, 1), "worker_ready_seen": e["worker_ready_seen"],
                    "progressed": progressed, "kind": kind,
                    "cloudwatch_note": "CloudWatch SQS 지표는 1분마다의 스냅샷이다: 이 적체가 약 60 s 이하이면 지표에 안 보일 수 있다(스모크에서 3건·약 30 s 적체가 0 으로 찍혔다)"})
    return {"resolution_s_median": round(statistics.median(gaps), 1) if gaps else None, "episodes": out}


# ---------------------------------------------------------------------------------------------- 표본 수집 (읽기 전용 명령만)
DB_QUERIES = {
    "pending": "select status, count(*) n, min(received_at) oldest from detection_jobs where status in ('pending_publish','queued','processing') group by status",
    "last": "select max(created_at) last_result from detection_results",
    # 최근 120초에 접수된 작업의 Relay 발행 지연(published_at - received_at)과 미발행 수. 짧은 적체/발행 지연을 CloudWatch 없이 본다
    "recent": "select count(*) n, count(published_at) published, extract(epoch from max(published_at - received_at)) max_publish_delay_s "
              "from detection_jobs where received_at >= cast(:lo as timestamptz)",
}
KUBECTL_GET_KINDS = {"pods", "scaledobject", "app", "nodes", "hpa", "deploy", "events"}      # secret 은 없다
KUBECTL_TOP_KINDS = {"pods", "nodes"}
AWS_ALLOWED = {("sqs", "get-queue-attributes"), ("cloudwatch", "get-metric-statistics"), ("cloudwatch", "get-metric-data"), ("secretsmanager", "describe-secret")}
LOG_KEYWORDS = re.compile(r"preload|load_seconds|model|ready|warm|start", re.I)
LOG_DROP = re.compile(r"password|secret|token|key|credential|authorization", re.I)
MODEL_READY = re.compile(r"load_seconds=|preload.*(done|finish|complete|끝|완료)|(준비|ready).*(완료|complete)", re.I)


class AwsViaRunner:
    """boto3 가 없을 때 CLI 로 같은 읽기 3개를 한다(느리다: aws.exe 시작 약 3 s)."""
    METHODS = sp.ReadOnlyAws.METHODS

    def __init__(self, runner, timeout=15):
        self.runner, self.timeout = runner, timeout

    def sqs_attrs(self, queue_url):
        return parse_sqs_attrs(self.runner(["aws", "sqs", "get-queue-attributes", "--queue-url", queue_url, "--attribute-names", "All", "--output", "json"], timeout=self.timeout))

    def metric_points(self, namespace, metric, dimensions, start_epoch, end_epoch, period=60, stat="Maximum"):
        dims = [f"Name={k},Value={v}" for k, v in dimensions.items()]
        out = self.runner(["aws", "cloudwatch", "get-metric-statistics", "--namespace", namespace, "--metric-name", metric, "--dimensions", *dims, "--start-time",
                           pc.iso_utc(start_epoch), "--end-time", pc.iso_utc(end_epoch), "--period", str(period), "--statistics", stat, "--output", "json"], timeout=self.timeout)
        return sorted(({"t": pc.ts(p["Timestamp"]), "value": p[stat]} for p in json.loads(out)["Datapoints"]), key=lambda x: x["t"])

    def secret_dates(self, secret_id):
        return parse_secret(self.runner(["aws", "secretsmanager", "describe-secret", "--secret-id", secret_id, "--output", "json"], timeout=self.timeout))


def make_aws(runner, mode="auto", profile=None, region="ap-northeast-2", timeout=15):
    if mode in ("auto", "boto3"):
        try:
            import boto3  # noqa: F401
            return sp.ReadOnlyAws(region=region, profile=profile)
        except ImportError:
            if mode == "boto3":
                raise
    return AwsViaRunner(runner, timeout)


def _db_sample(runner, cfg, clock):
    t_call = clock()
    script = pc.POD_SCRIPT.encode("utf-8")
    arg = json.dumps({"lo": pc.iso_utc(t_call - 120), "hi": pc.iso_utc(t_call), "queries": DB_QUERIES})
    raw = runner(["kubectl", "exec", "-i", "deploy/backend", "-n", APP_NS, "--", "python", "-", arg], input=script, timeout=cfg.exec_timeout_s)
    t_end = clock()
    rows = json.loads(raw.strip().splitlines()[-1])
    read_t = (t_call + t_end) / 2                              # DB 를 읽은 시각 ≈ 호출 중간(불확실성 ± 소요/2)
    ages = {r["status"]: round(read_t - pc.ts(r["oldest"]), 3) for r in rows["pending"] if r.get("oldest")}
    last = rows["last"][0]["last_result"] if rows.get("last") else None
    rec = (rows.get("recent") or [{}])[0]
    return {"pending_total": sum(r["n"] for r in rows["pending"]), "by_status": {r["status"]: r["n"] for r in rows["pending"]}, "oldest_age_s": ages,
            "oldest_pending_publish_age_s": ages.get("pending_publish", 0), "last_result_ts": pc.ts(last) if last else None, "read_t": read_t,
            "recent": {"n": rec.get("n"), "published": rec.get("published"), "max_publish_delay_s": float(rec["max_publish_delay_s"]) if rec.get("max_publish_delay_s") is not None else None}}


class WorkerLogs:
    """새 Worker Pod 의 로그에서 시작·모델 로딩 관련 줄만 모아 둔다(파드가 사라지기 전에). 모델 준비 완료를 로그에서 직접 볼 수 있으면 그 줄의 시각이 관측값이다.
    줄 형식은 [미확인] - 못 찾으면 phases 도구가 '추정'으로 표시한다. 비밀로 보이는 단어가 있는 줄은 버린다."""

    def __init__(self, runner, get_pods, cfg):
        self.runner, self.get_pods, self.cfg = runner, get_pods, cfg
        self.state = {}

    def __call__(self):
        for p in self.get_pods():
            if p["app"] != "worker" or p["deleting"] or not (p.get("started_at") or p.get("ready")):
                continue
            st = self.state.setdefault(p["name"], {"lines": [], "complete": False, "tries": 0})
            if st["complete"] or st["tries"] >= 12:
                continue
            st["tries"] += 1
            try:
                text = self.runner(["kubectl", "logs", p["name"], "-n", APP_NS, "--timestamps", "--tail=100"], timeout=self.cfg.call_timeout_s)
            except Exception:  # noqa: BLE001  (컨테이너가 아직 안 떴을 수 있다. 다음 주기에 다시)
                continue
            lines = [ln[:300] for ln in text.splitlines() if LOG_KEYWORDS.search(ln) and not LOG_DROP.search(ln)][:30]
            st["lines"] = lines
            st["complete"] = any(MODEL_READY.search(ln) for ln in lines)
        return {pod: {"lines": list(st["lines"]), "complete": st["complete"], "tries": st["tries"]} for pod, st in self.state.items()}


def cadvisor_source(runner, get_pods, cfg, clock):
    def fn():
        nodes = sorted({p["node"] for p in get_pods() if p.get("node") and p["app"] in WATCHED})
        merged, t_before = {}, clock()
        for node in nodes:
            if not re.fullmatch(r"[a-z0-9.-]+", node):
                continue
            text = runner(["kubectl", "get", "--raw", f"/api/v1/nodes/{node}/proxy/metrics/cadvisor"], timeout=cfg.call_timeout_s)
            merged.update(sp.parse_cadvisor(text, APP_NS))
        return {"t": (t_before + clock()) / 2, "pods": merged}
    return fn


def build_pollers(runner, aws, cfg, clock=time.time):
    """읽기 전용 소스들. 각 호출은 자기 timeout 을 가진다. 반환: Poller 목록(시작하지 않은 상태)."""
    T = cfg.call_timeout_s
    pods_p = sp.Poller("pods", lambda: parse_pods(runner(["kubectl", "get", "pods", "-n", APP_NS, "-o", "json"], timeout=T)), cfg.fast_period_s, "pods", clock)
    get_pods = lambda: ((pods_p.snapshot() or {}).get("value")) or []  # noqa: E731
    ps = [pods_p,
          sp.Poller("keda", lambda: parse_scaledobject(runner(["kubectl", "get", "scaledobject", "worker", "-n", APP_NS, "-o", "json"], timeout=T)), cfg.fast_period_s, "keda", clock),
          sp.Poller("apps", lambda: parse_apps(runner(["kubectl", "get", "app", "-n", ARGO_NS, "-o", "json"], timeout=T)), cfg.fast_period_s, "apps", clock),
          sp.Poller("top", lambda: parse_top_pods(runner(["kubectl", "top", "pods", "-n", APP_NS, "--no-headers"], timeout=T)), cfg.fast_period_s, "top", clock)]
    if cfg.queue_url:
        ps.append(sp.Poller("sqs", lambda: aws.sqs_attrs(cfg.queue_url), cfg.fast_period_s, "sqs", clock))
        ps.append(sp.Poller("sqs_age", lambda: _sqs_age(aws, cfg.queue_name, clock), cfg.cloudwatch_period_s, "sqs_age_s", clock))
    if cfg.dlq_url:
        ps.append(sp.Poller("dlq", lambda: aws.sqs_attrs(cfg.dlq_url), cfg.fast_period_s, "dlq", clock))
    if cfg.secret_id:
        ps.append(sp.Poller("secret", lambda: aws.secret_dates(cfg.secret_id), cfg.secret_period_s, "secret", clock))
    ps.append(sp.Poller("db", lambda: _db_sample(runner, cfg, clock), cfg.db_period_s, "db", clock))
    if cfg.cadvisor_period_s:
        ps.append(sp.Poller("cadvisor", cadvisor_source(runner, get_pods, cfg, clock), cfg.cadvisor_period_s, "cadvisor", clock))
    if cfg.worker_logs:
        ps.append(sp.Poller("worker_logs", WorkerLogs(runner, get_pods, cfg), 10, "worker_logs", clock))
    return ps


def _sqs_age(aws, queue_name, clock):
    now = clock()
    pts = aws.metric_points("AWS/SQS", "ApproximateAgeOfOldestMessage", {"QueueName": queue_name}, now - 300, now)
    return pts[-1]["value"] if pts else 0.0


def collect_sample(runner, cfg, now=None, aws=None, parallel=True):
    """한 번 읽는 표본(사전 점검용·시험용). 소스들을 **병렬**로 읽는다 - 소요 ≈ 가장 느린 호출 하나. 값마다 시작·완료 시각이 sources 에 남는다."""
    clock = (lambda: now) if now is not None else time.time
    aws = aws or AwsViaRunner(runner, cfg.call_timeout_s)
    cfg.worker_logs = getattr(cfg, "worker_logs", False)
    pollers = [p for p in build_pollers(runner, aws, cfg, clock) if p.name != "worker_logs"]
    if parallel:
        with ThreadPoolExecutor(max_workers=len(pollers)) as ex:
            list(ex.map(lambda p: p.poll_once(), pollers))
    else:
        for p in pollers:
            p.poll_once()
    return sp.build_sample(pollers, clock(), cfg.fast_period_s, max_age_s=1e12, critical=())


def readonly_runner(inner=None):
    """쓰기 명령은 물론 비밀을 읽는 명령도 실행하지 못하게 감싼다. 허용 목록(그 밖은 모두 거부):
    kubectl get <pods|scaledobject|app|nodes|hpa|deploy|events>, kubectl top pods|nodes, kubectl exec 는 고정된 SELECT 스크립트(POD_SCRIPT)만,
    kubectl logs 는 worker-* Pod 와 keda 연산자만, kubectl get --raw 는 노드 cAdvisor 지표 경로만,
    aws sqs get-queue-attributes, aws cloudwatch get-metric-statistics|get-metric-data, aws secretsmanager describe-secret(값이 아니라 회전 시각)."""
    inner = inner or pc.default_runner

    def run(cmd, input=None, timeout=None):
        ok = False
        if cmd[0] == "kubectl" and len(cmd) > 2 and cmd[1] == "get":
            ok = cmd[2] in KUBECTL_GET_KINDS or (cmd[2] == "--raw" and len(cmd) == 4 and re.fullmatch(r"/api/v1/nodes/[a-z0-9.-]+/proxy/metrics/cadvisor", cmd[3]) is not None)
        elif cmd[0] == "kubectl" and len(cmd) > 2 and cmd[1] == "top":
            ok = cmd[2] in KUBECTL_TOP_KINDS
        elif cmd[0] == "kubectl" and len(cmd) > 2 and cmd[1] == "logs":
            keda = cmd[2:5] == ["-n", KEDA_NS, "deploy/keda-operator"]
            ok = keda or (re.fullmatch(r"worker-[a-z0-9-]+", cmd[2]) is not None and "-n" in cmd and cmd[cmd.index("-n") + 1] == APP_NS)
        elif cmd[0] == "kubectl" and len(cmd) > 2 and cmd[1] == "exec":
            ok = "python" in cmd and input is not None and pc.POD_SCRIPT.encode() == input
        elif cmd[0] == "aws" and len(cmd) > 2:
            ok = (cmd[1], cmd[2]) in AWS_ALLOWED
        if not ok:
            raise PermissionError(f"읽기 전용 관찰자가 허용하지 않는 명령: {' '.join(cmd[:3])}")
        kw = {}
        if input is not None:
            kw["input"] = input
        if timeout is not None:
            kw["timeout"] = timeout
        return inner(cmd, **kw)
    return run


def banner(alerts):
    lines = ["", "!" * 70] + [f"  ABORT  {a['code']}: {a['msg']}" for a in alerts if a["level"] == "ABORT"] + \
            ["  시험을 멈추고(k6 Ctrl+C) 시각·상태를 기록하세요. 이 도구는 아무것도 멈추지 않았습니다.", "!" * 70, ""]
    return "\n".join(lines)


KEDA_DECISION = re.compile(r"^(?P<t>\S+)\s.*Successfully (?:updated ScaleTarget|set ScaleTarget replicas count)", re.I)
KEDA_COUNTS = re.compile(r"Original Replicas Count:\s*(?P<from>\d+),\s*New Replicas Count:\s*(?P<to>\d+)", re.I)


def parse_keda_log(text):
    """KEDA 연산자 로그에서 스케일 대상 조정 줄을 뽑는다 → [{'ts', 'from', 'to', 'raw'}]. 형식은 스모크 로그(`Successfully updated ScaleTarget … New Replicas Count: 1`)에서 가져왔다."""
    out = []
    for line in text.splitlines():
        m = KEDA_DECISION.match(line)
        if not m:
            continue
        try:
            t = pc.ts(m["t"])
        except ValueError:
            continue
        c = KEDA_COUNTS.search(line)
        out.append({"ts": t, "from": int(c["from"]) if c else None, "to": int(c["to"]) if c else None, "raw": line[:200]})
    return out


def run_loop(samples_fn, watcher, interval, duration, out_path, clock=time.time, sleep=time.sleep, printer=print):
    """고정 주기 루프. samples_fn(tick) -> 표본, 매 틱마다 규칙을 평가하고 JSONL 로 쓴다. ABORT 가 나면 (True, alerts) 를 돌려준다."""
    ticker = sp.FixedRateTicker(interval, clock, sleep)
    end = clock() + duration if duration else None
    last_logs_version = None
    out = Path(out_path)
    while end is None or clock() < end:
        tick = ticker.wait()
        t_build = clock()
        s = samples_fn(tick)
        s["tick"]["build_ms"] = int((clock() - t_build) * 1000)
        alerts = watcher.step(s)
        s["alerts"] = alerts
        logs = s.get("worker_logs")
        if logs is not None:                                   # 바뀐 때만 쓴다(매 표본에 반복하면 타임라인이 커진다)
            version = json.dumps(logs, sort_keys=True)
            if version == last_logs_version:
                s.pop("worker_logs")
            last_logs_version = version
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(s, default=str, ensure_ascii=False) + "\n")
        for al in alerts:
            printer(f"[{datetime.fromtimestamp(s['ts'], timezone.utc):%H:%M:%S}] {al['level']} {al['code']}: {al['msg']}", flush=True)
        if any(al["level"] == "ABORT" for al in alerts):
            printer(banner(alerts), flush=True)
            out.with_name("ABORT.flag").write_text(json.dumps(alerts, ensure_ascii=False), encoding="utf-8")
            return True, alerts
    return False, []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["preflight", "run", "resources", "summarize", "keda-log"])
    ap.add_argument("--out", default="timeline.jsonl"); ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--duration", type=float, default=0, help="초. 0 이면 Ctrl+C 까지")
    ap.add_argument("--queue-url", default=""); ap.add_argument("--dlq-url", default=""); ap.add_argument("--secret-id", default="")
    ap.add_argument("--expect-workers", type=int); ap.add_argument("--allow-outofsync", default=""); ap.add_argument("--window-h", type=float, default=4.0)
    ap.add_argument("--db-period", type=float, default=30.0, help="DB 읽기 주기(초). kubectl exec 가 backend Pod 에서 앱을 import 하므로 너무 자주 하면 시험 대상을 흔든다")
    ap.add_argument("--cadvisor-period", type=float, default=0.0, help="노드 cAdvisor 누적 카운터를 읽는 주기(초). 0 = 끔. 지속 부하 시험은 30")
    ap.add_argument("--aws-mode", choices=["auto", "boto3", "cli"], default="auto"); ap.add_argument("--aws-profile", default=None)
    ap.add_argument("--no-worker-logs", action="store_true"); ap.add_argument("--since-min", type=int, default=30)
    a = ap.parse_args(argv)
    if a.cmd in ("resources", "summarize"):
        samples = [json.loads(line) for line in Path(a.out).read_text(encoding="utf-8").splitlines() if line.strip()]
        if a.cmd == "resources":
            print(json.dumps({"kubectl_top": summarize_resources(samples), "cadvisor": summarize_cadvisor(samples),
                              "note": "kubectl top 은 짧은 버스트를 놓친다. cAdvisor 항목이 있으면 그쪽(누적 CPU 초)을 쓴다. requests 는 예약이지 사용량이 아니다."}, indent=1, ensure_ascii=False))
        else:
            print(json.dumps({"cadence": cadence_stats(samples), "scale_events": scale_events(samples), "backlog": backlog_episodes(samples)}, indent=1, ensure_ascii=False))
        return 0
    cfg = Config(expect_workers=a.expect_workers, allow_outofsync=tuple(x for x in a.allow_outofsync.split(",") if x), fast_period_s=a.interval, db_period_s=a.db_period,
                 cadvisor_period_s=a.cadvisor_period)
    cfg.queue_url, cfg.dlq_url, cfg.secret_id = a.queue_url, a.dlq_url, a.secret_id
    cfg.queue_name = a.queue_url.rsplit("/", 1)[-1] if a.queue_url else ""
    cfg.worker_logs = not a.no_worker_logs
    run = readonly_runner()
    if a.cmd == "keda-log":
        text = run(["kubectl", "logs", "-n", KEDA_NS, "deploy/keda-operator", f"--since={a.since_min}m", "--timestamps"], timeout=60)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"KEDA 로그 {len(text.splitlines())}줄 저장: {a.out}; 조정 결정 {len(parse_keda_log(text))}건")
        return 0
    aws = make_aws(run, a.aws_mode, a.aws_profile, timeout=cfg.call_timeout_s)
    if a.cmd == "preflight":
        s = collect_sample(run, cfg, aws=aws)
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
        total_ms = max((v.get("ms") or 0) for v in s["sources"].values())
        print(json.dumps({"slots_1000m": slots["slots"], "free_m": slots["free_m"], "problems": problems, "slowest_source_ms": total_ms}, ensure_ascii=False, indent=1))
        return 2 if problems else 0
    pollers = build_pollers(run, aws, cfg)
    for p in pollers:
        p.start()
    t_wait = time.time()
    while time.time() - t_wait < 30 and any(p.snapshot() is None for p in pollers if p.period_s <= 60):   # 첫 값이 모일 때까지 잠깐 기다린다
        time.sleep(0.5)
    w = Watcher(cfg)
    state = {"baseline": None}

    def make_sample(tick):
        s = sp.build_sample(pollers, time.time(), a.interval, cfg.max_age_s, CRITICAL_SOURCES, tick)
        if state["baseline"] is None and s.get("secret"):
            state["baseline"] = s["secret"]["last_rotated"]
            w.baseline_rotated = state["baseline"]
        return s
    try:
        aborted, _ = run_loop(make_sample, w, a.interval, a.duration, a.out)
    finally:
        for p in pollers:
            p.stop()
    return 3 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
