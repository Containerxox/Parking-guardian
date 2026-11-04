"""B0 / E3 N=2: 클러스터 안 k6 임시 Pod 가 inference Service(Pod 2개)를 closed-loop 로 호출해 서비스 처리량 C 를 잰다.

    python loadtest/tools/e3_run.py <시작 회차> <끝 회차> --snap <p3 스냅샷 폴더> [--node 노드] [--prefix b0] [--dry-run]
    예: python loadtest/tools/e3_run.py 1 3 --snap docs/measurements/phase1/eks/s1-p3-snapshot-<시각>

[2026-11-04 B0 수정. 원본: e3_run.py.bak-before-b0]
  * 옛 클러스터의 하드코딩(SNAP, NODE_A)을 없앴다. 스냅샷은 --snap 으로 받고(SHA256SUMS·6시간 이내 확인), k6 를 둘 노드는
    실행 시점에 고른다: Ready, 스케줄 가능, inference Pod 가 없고, CPU 요청 여유가 k6 요청(100m) 이상인 노드 중 여유가 가장 큰 곳.
    --node 로 정해도 같은 조건을 통과해야 한다. 조건에 맞는 노드가 없으면 시작하지 않는다.
  * 고정 상태 검증(`guard`): inference 2·backend 2 Pod 가 정확히 Running+Ready, Worker Pod·Worker replicas 가 0, 새 Pending Pod 없음,
    HPA backend·inference 가 min=max=2, HPA current/desired 가 2 를 넘으면 **즉시 중단**(k6 Pod 삭제). 재시작 증가·OOMKilled 도 중단.
    기존 P3 고정 검사(`p3.py status --expect fixed --snap`)와 엔드포인트 ready 2·남은 k6 Pod 없음 점검은 그대로 유지한다.
  * 기록: k6 Pod CPU 평균·최대, CPU throttling(k6·inference), 재시작·OOM, 경고. k6 CPU >= 0.7 코어는 **경고**이고,
    부하 생성 병목이 확인될 때만 무효다: (k6 최대 CPU >= 0.7) 이면서 (k6 throttling >= 5 %  또는  k6 노드 사용률 >= 90 %
    또는  inference 두 Pod 중 하나라도 평균 CPU < 0.8 코어).
  * 결과는 새 폴더 `<날짜>-<prefix>-<회차>`(기본 b0)에만 쓴다. 이미 있는 폴더·요약 파일은 건드리지 않고 중단한다.
  * --dry-run: 읽기 전용 명령(kubectl get)만으로 스냅샷·노드 선택·고정 상태 검증·k6 Pod 정의를 점검하고 출력한다. 아무것도 만들지 않는다.

[2026-11-04 W1b 보완]
  * 재질의(W1b-1): `prometheus.json` 에 평가 시각·식·원본 응답·조회 상태(ok/empty/error)·누락 사유·Pod 식별 정보를 저장한다.
    `--requery <결과폴더>` 는 같은 식을 같은 평가 시각으로 다시 조회해 **새 파일**(prometheus-requery-N.json, grade-upgrade-N.json, requery-N.sha256)만 만든다.
    B 등급(CPU 증거 부족)은 재질의로 두 Pod 의 CPU 를 실제로 얻었을 때만 A 로 올린다. 값을 추정하거나 채우지 않는다. 원본 파일은 덮어쓰지 않는다.
  * S0 검증(W1b-2): `--snap` 은 변경 전 baseline(S0) 스냅샷만 허용한다(원본 HPA 2/3, 자동 동기화 켜짐, 필수 파일·메타, config HEAD 일치). 고정 후(S1) 스냅샷은 거부한다.
  * throttling(W1b-3): CPU limit 없음+지표 없음 / limit 있음+누락 / 조회 오류 / 관측을 구분한다. limit 이 없다고 병목이 없다고 판정하지 않는다.
  * 등급(사전 등록 v1): A 공식 유효 / B 참고(CPU 증거 부족 또는 포화 미확인) / C 무효. A 3회, 시도 최대 5회. 안전 중단은 즉시 멈춘다.
  * 감시 주기: 토큰 캐시(KubeAuth)로 kubectl 마다 aws eks get-token(약 3초)을 없앴다. 가드 한 바퀴가 약 56초에서 크게 줄어든다.
  * ConfigMap 크기를 로컬에서 먼저 계산하고(한도 1 MiB), 생성 실패 시 k6 Pod 없이 정리하고 중단한다.

조건(변경 없음): VUS=4, k6 --no-connection-reuse(요청마다 새 연결 = kube-proxy 가 Pod 를 확률로 선택), 3분, 워밍업 5장은 setup.
inference 만 호출한다. DB, 기기, 위반, Deployment, HPA, Argo 는 건드리지 않는다(이 스크립트가 만드는 것: loadtest 네임스페이스의 ConfigMap·k6 Pod).
회차 사이에 P3 고정 상태 검사를 하고, 이상이 있으면 다음 회차로 가지 않는다. 끝나면 loadtest 네임스페이스를 지운다.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
RESULTS = REPO / "docs" / "measurements" / "phase1" / "results"
APP_NS, LOAD_NS = "parking-guardian", "loadtest"
VUS, DURATION = 4, "3m"
K6_REQUEST_M = 100                 # k6 Pod 의 CPU 요청(m). 노드 선택에 쓴다
K6_WARN_CORES = 0.7                # 이 이상이면 경고. 병목이 확인되어야 무효
K6_THROTTLE_INVALID = 0.05
K6_NODE_BUSY_INVALID = 0.90
SATURATED_CORES = 0.8
SNAP_MAX_AGE_H = 6
PROM_PORT = 19093
LOOP_SECONDS = 5                   # 가드 루프의 대기. 한 바퀴 = 대기 + read_guard + p3 검사. 토큰 캐시가 없으면 한 바퀴가 약 56초였다(kubectl 호출마다 aws eks get-token 약 3초)
CONFIG = REPO.parent / "parking-guardian-config"
TARGET_A, MAX_ATTEMPTS = 3, 5      # 사전 등록 v1: A 등급 3회, 시도 한도 5회
MIN_GUARD_SAMPLES = 5              # 한 회차(약 3.5분)에서 가드 감시 표본 최소 수
MAX_GUARD_GAP_S = 30               # 가드 감시 표본 사이(시작·끝 포함) 최대 공백(초)
CM_LIMIT = 1048576                 # Kubernetes ConfigMap 총 크기 한도(1 MiB)
CM_WARN_FRACTION = 0.90
EXPECTED_HPA_ORIGINAL = (2, 3)     # 변경 전 HPA backend·inference 의 min/max
CORE_SNAPSHOT_FILES = ("pods.json", "meta.json", "app-root.json", "app-parking-guardian.json", "hpa-backend.json", "hpa-inference.json",
                       "deploy-backend.json", "deploy-inference.json", "deploy-frontend.json")
P2_SNAPSHOT_FILES = ("deploy-worker.json", "deploy-relay.json", "scaledobject-worker.json", "hpa-keda-hpa-worker.json")
READ_VERBS = {"get", "diff"}       # --dry-run 에서 허용하는 kubectl 동사
HPA_FIXED = ("backend", "inference")
POD_YAML = """apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: loadtest
  labels: {{app: k6}}
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: {node}
  containers:
  - name: k6
    image: grafana/k6:2.3.0
    command: ["k6", "run", "--no-connection-reuse", "--summary-export=/tmp/summary.json", "--summary-trend-stats=avg,min,med,max,p(90),p(95),p(99)", "/scripts/inference.js"]
    env:
    - {{name: INFERENCE_URL, value: "http://inference.parking-guardian.svc.cluster.local:8000"}}
    - {{name: IMG_DIR, value: /scripts}}
    - {{name: RUN_ID, value: "{name}"}}
    - {{name: VUS, value: "{vus}"}}
    - {{name: DURATION, value: "{duration}"}}
    resources:
      requests: {{cpu: 100m, memory: 128Mi}}
      limits: {{memory: 512Mi}}
    volumeMounts:
    - {{name: scripts, mountPath: /scripts}}
  volumes:
  - name: scripts
    configMap: {{name: k6-scripts}}
"""

DRY_RUN = False


def verb_of(args):
    """kubectl 인수에서 동사(get, create, delete ...)를 찾는다. -n/--namespace/-o/-l 같은 옵션과 그 값은 건너뛴다."""
    skip = False
    for t in args:
        if skip:
            skip = False
        elif t in ("-n", "--namespace", "-o", "-l", "--selector", "-c", "--context"):
            skip = True
        elif not t.startswith("-"):
            return t
    return ""


def kube(*a, data=None, check=True):
    if DRY_RUN and verb_of(a) not in READ_VERBS:
        raise RuntimeError(f"--dry-run 에서는 읽기 전용 kubectl 만 허용한다: {' '.join(a[:3])}")
    r = subprocess.run(["kubectl", *a], capture_output=True, text=True, encoding="utf-8", input=data)
    if check and r.returncode:
        raise RuntimeError(f"kubectl {' '.join(a)}: {r.stderr.strip()[:300]}")
    return r.stdout


def kj(*a):
    return json.loads(kube(*a, "-o", "json"))


def log(m):
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}Z] {m}", flush=True)


# ---------------------------------------------------------------------------------------------- 스냅샷
def verify_snapshot(snap, now=None, max_age_h=SNAP_MAX_AGE_H):
    """p3.py 스냅샷 폴더 검증: 존재, SHA256SUMS 일치, meta.json 의 시각이 max_age_h 이내. 반환: (ok, 사유 목록)."""
    snap = pathlib.Path(snap)
    bad = []
    if not snap.is_dir():
        return False, [f"스냅샷 폴더가 없다: {snap}"]
    sums = snap / "SHA256SUMS"
    if not sums.is_file():
        bad.append("SHA256SUMS 가 없다")
    else:
        for line in sums.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, _, name = line.partition(" *")
            f = snap / name
            if not f.is_file():
                bad.append(f"{name} 없음")
            elif hashlib.sha256(f.read_bytes()).hexdigest() != digest:
                bad.append(f"{name} 해시 불일치")
    for need in ("pods.json", "meta.json", "app-root.json", "app-parking-guardian.json"):
        if not (snap / need).is_file():
            bad.append(f"{need} 없음")
    try:
        taken = dt.datetime.fromisoformat(json.loads((snap / "meta.json").read_text(encoding="utf-8"))["taken_at"])
        age_h = ((now or dt.datetime.now(dt.timezone.utc)) - taken).total_seconds() / 3600
        if age_h > max_age_h:
            bad.append(f"스냅샷이 {age_h:.1f}시간 전 것이다(한도 {max_age_h}시간): 고정 직전 스냅샷을 쓴다")
    except Exception as exc:  # noqa: BLE001
        bad.append(f"meta.json 을 읽지 못함({type(exc).__name__})")
    return not bad, bad


def read_config_head():
    """config 리포 작업 폴더의 HEAD 짧은 해시(읽기). 읽지 못하면 None."""
    try:
        r = subprocess.run(["git", "-C", str(CONFIG), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def verify_baseline_snapshot(snap, now=None, max_age_h=SNAP_MAX_AGE_H, config_head_fn=None):
    """`--snap` 에는 **변경 전 baseline 스냅샷(S0)** 만 쓸 수 있다. S0 는 p3.py pause 가 아무것도 바꾸기 전에 만든(또는 그 직전에 수동으로 뜬)
    복원 기준이다. 고정 후에 뜬 스냅샷(S1)은 '고정된 상태'를 담고 있어 복원 기준이 될 수 없으므로 거부한다.
    검사: ① SHA256SUMS 무결성·6시간 ② 필수 리소스 파일과 복원 메타데이터 ③ HPA backend·inference 원본이 2/3 ④ root·parking-guardian 의 자동 동기화가
    원래 켜져 있었음 ⑤ config HEAD 가 스냅샷 시점과 같음. 읽기만 한다(p3.py 의 snapshot/pause/restore 는 이 함수를 쓰지 않는다).
    반환: (ok, 거부 사유 목록, 경고 목록)."""
    ok, bad = verify_snapshot(snap, now, max_age_h)
    snap = pathlib.Path(snap)
    warns = []

    def load(name):
        try:
            return json.loads((snap / name).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    for need in CORE_SNAPSHOT_FILES:
        if not (snap / need).is_file():
            bad.append(f"필수 리소스 파일 없음: {need}")
    for opt in P2_SNAPSHOT_FILES:
        if not (snap / opt).is_file():
            warns.append(f"Phase 2 객체 파일 없음(복원 비교에서 빠짐): {opt}")
    meta = load("meta.json") or {}
    for key in ("taken_at", "config_repo_head", "argo_revision"):
        if not meta.get(key):
            bad.append(f"복원 메타데이터 없음: meta.json.{key}")
    not_s0 = []
    for app in ("root", "parking-guardian"):
        auto = (((load(f"app-{app}.json") or {}).get("spec") or {}).get("syncPolicy") or {}).get("automated")
        if not (isinstance(auto, dict) and auto.get("selfHeal") is True):
            not_s0.append(f"Application {app} 의 자동 동기화가 꺼져 있다(automated={auto})")
    for h in ("backend", "inference"):
        spec = (load(f"hpa-{h}.json") or {}).get("spec") or {}
        mm = (spec.get("minReplicas"), spec.get("maxReplicas"))
        if mm != EXPECTED_HPA_ORIGINAL:
            not_s0.append(f"HPA {h} min/max={mm[0]}/{mm[1]} (원본은 {EXPECTED_HPA_ORIGINAL[0]}/{EXPECTED_HPA_ORIGINAL[1]})")
    if not_s0:
        bad.append("변경 전 baseline(S0) 스냅샷이 아니다 - 고정 후(S1)이거나 이미 바뀐 상태에서 뜬 스냅샷이다: " + "; ".join(not_s0)
                   + " → p3.py pause 가 만든(또는 그 직전에 뜬) 스냅샷만 쓴다")
    head = (config_head_fn or read_config_head)()
    if not head:
        bad.append("config 리포 HEAD 를 읽지 못함(확인 불가) - 스냅샷 시점과 같은지 확인할 수 없다")
    elif meta.get("config_repo_head") and head != meta["config_repo_head"]:
        bad.append(f"config 리포 HEAD 불일치: 스냅샷 {meta['config_repo_head']} / 현재 {head} (복원 때 Argo 가 새 리비전으로 롤링할 수 있다)")
    return not bad, bad, warns


# ---------------------------------------------------------------------------------------------- k6 노드 선택
def cpu_m(v):
    v = str(v or "0")
    return int(v[:-1]) if v.endswith("m") else int(round(float(v) * 1000))


def pod_request_m(pod):
    return sum(cpu_m(c.get("resources", {}).get("requests", {}).get("cpu", "0")) for c in pod["spec"]["containers"])


def node_table(nodes, pods, need_m=K6_REQUEST_M):
    """노드마다 k6 Pod 후보 여부와 제외 사유를 계산한다. nodes/pods 는 `kubectl get -o json` 결과(dict).
    조건: Ready, spec.unschedulable 아님, inference Pod(종료되지 않은 것)가 없음, 할당 가능 CPU - 요청 합 >= need_m."""
    live = [p for p in pods["items"] if p["status"].get("phase") not in ("Succeeded", "Failed") and p["spec"].get("nodeName")]
    table = []
    for n in nodes["items"]:
        name = n["metadata"]["name"]
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"].get("conditions", []))
        mine = [p for p in live if p["spec"]["nodeName"] == name]
        has_inf = [p["metadata"]["name"] for p in mine if p["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "inference"]
        free = cpu_m(n["status"]["allocatable"]["cpu"]) - sum(pod_request_m(p) for p in mine)
        ip = next((a["address"] for a in n["status"].get("addresses", []) if a["type"] == "InternalIP"), None)
        why = []
        if not ready:
            why.append("Ready 아님")
        if n["spec"].get("unschedulable"):
            why.append("스케줄 불가(cordon)")
        if has_inf:
            why.append("inference Pod 가 있음")
        if free < need_m:
            why.append(f"CPU 여유 {free}m < {need_m}m")
        table.append({"node": name, "ip": ip, "free_m": free, "eligible": not why, "why": why})
    return table


def pick_node(nodes, pods, override=None, need_m=K6_REQUEST_M):
    """k6 Pod 를 둘 노드를 고른다. 여러 곳이면 여유가 큰 노드, 같으면 이름순. override 가 있으면 그 노드가 같은 조건을 통과해야 한다.
    반환: (이름, 내부 IP, 여유 m, 후보 표). 조건에 맞는 노드가 없으면 RuntimeError."""
    table = node_table(nodes, pods, need_m)
    ok = sorted((t for t in table if t["eligible"]), key=lambda t: (-t["free_m"], t["node"]))
    if override:
        hit = next((t for t in table if t["node"] == override), None)
        if hit is None:
            raise RuntimeError(f"--node {override} 는 클러스터에 없다")
        if not hit["eligible"]:
            raise RuntimeError(f"--node {override} 는 k6 를 둘 수 없다: {', '.join(hit['why'])}")
        return hit["node"], hit["ip"], hit["free_m"], table
    if not ok:
        raise RuntimeError("k6 를 둘 노드가 없다(inference 가 없고 CPU 여유가 있는 Ready 노드): " + "; ".join(f"{t['node']}: {', '.join(t['why'])}" for t in table))
    best = ok[0]
    return best["node"], best["ip"], best["free_m"], table


# ---------------------------------------------------------------------------------------------- 고정 상태 검증 (inference·backend 2개, Worker 0, HPA ≤ 2)
def app_of(pod):
    return pod["metadata"].get("labels", {}).get("app.kubernetes.io/name", "")


def pod_ready(pod):
    return pod["status"].get("phase") == "Running" and any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))


def restarts_of(pod):
    return {c["name"]: c.get("restartCount", 0) for c in pod["status"].get("containerStatuses", [])}


def oom_of(pod):
    out = []
    for c in pod["status"].get("containerStatuses", []):
        for where in ("state", "lastState"):
            t = (c.get(where) or {}).get("terminated") or {}
            if t.get("reason") == "OOMKilled":
                out.append(f"{pod.get('metadata', {}).get('name', '?')}/{c['name']}")
    return out


def evaluate_guard(pods, hpas, worker_deploy=None, restart_baseline=None):
    """읽어 온 JSON(dict)으로 고정 상태를 판정한다. 반환: {'ok', 'abort_hpa', 'fails': [..], 'checks': [(이름, ok, 상세)], 'obs': {...}}.
    abort_hpa: HPA current/desired 가 2 를 넘는 경우(가장 중대 - 즉시 중단 사유로 따로 표시)."""
    checks = []

    def add(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    items = pods["items"]
    by = {}
    for p in items:
        by.setdefault(app_of(p), []).append(p)
    for app, n in (("inference", 2), ("backend", 2)):
        mine = by.get(app, [])
        ready = [p for p in mine if pod_ready(p)]
        add(f"{app} Pod 정확히 {n}개, 모두 Running+Ready", len(mine) == n and len(ready) == n, f"{len(ready)}/{len(mine)}")
    workers = [p["metadata"]["name"] for p in by.get("worker", []) if p["status"].get("phase") not in ("Succeeded", "Failed")]
    add("Worker Pod 없음", not workers, ",".join(workers))
    if worker_deploy is not None:
        want, have = worker_deploy["spec"].get("replicas", 0), worker_deploy["status"].get("replicas", 0)
        add("worker Deployment replicas 0", (want or 0) == 0 and (have or 0) == 0, f"spec={want} status={have}")
    pend = [p["metadata"]["name"] for p in items if p["status"].get("phase") == "Pending"]
    add("Pending Pod 없음", not pend, ",".join(pend))
    abort_hpa, hobs = [], {}
    for h in HPA_FIXED:
        o = next((x for x in hpas["items"] if x["metadata"]["name"] == h), None)
        if o is None:
            add(f"HPA {h} 있음", False, "없음")
            continue
        mn, mx = o["spec"]["minReplicas"], o["spec"]["maxReplicas"]
        cur, des = o["status"].get("currentReplicas"), o["status"].get("desiredReplicas")
        lim = next((c for c in o["status"].get("conditions", []) if c["type"] == "ScalingLimited"), {})
        hobs[h] = {"min": mn, "max": mx, "current": cur, "desired": des, "scaling_limited": lim.get("status"), "reason": lim.get("reason")}
        add(f"HPA {h} min=max=2 (고정)", (mn, mx) == (2, 2), f"{mn}/{mx}")
        over = (cur or 0) > 2 or (des or 0) > 2
        add(f"HPA {h} current/desired <= 2", not over, f"current={cur} desired={des}")
        if over:
            abort_hpa.append(f"{h}: current={cur} desired={des}")
    base = restart_baseline or {}
    grew, ooms = [], []
    for p in items:
        if app_of(p) in ("inference", "backend", "frontend", "relay", "worker"):
            ooms += oom_of(p)
            now = restarts_of(p)
            before = base.get(p["metadata"]["name"])
            if before is not None and any(now.get(k, 0) > v for k, v in before.items()):
                grew.append(p["metadata"]["name"])
            elif before is None and base and any(v > 0 for v in now.values()):
                grew.append(p["metadata"]["name"] + "(새 Pod 가 재시작)")
    add("재시작 증가 없음", not grew, ",".join(grew))
    add("OOMKilled 없음", not ooms, ",".join(ooms))
    fails = [n for n, ok, _ in checks if not ok]
    obs = {"hpa": hobs, "inference": len(by.get("inference", [])), "backend": len(by.get("backend", [])), "worker_pods": len(workers), "oom": ooms}
    return {"ok": not fails, "abort_hpa": abort_hpa, "fails": fails, "checks": checks, "obs": obs}


def read_guard(restart_baseline=None):
    wd = None
    try:
        wd = kj("-n", APP_NS, "get", "deploy", "worker")
    except RuntimeError:
        wd = None            # 비교 구성처럼 worker 가 없을 수도 있다(그 경우 Pod 검사만)
    return evaluate_guard(kj("-n", APP_NS, "get", "pods"), kj("-n", APP_NS, "get", "hpa"), wd, restart_baseline)


def safe_read_guard(restart_baseline=None):
    """종료 시점 가드 조회. 조회가 실패하면 예외 대신 '실패한 가드'를 돌려준다(그 회차는 안전 중단 등급 C)."""
    try:
        return read_guard(restart_baseline)
    except RuntimeError as exc:
        return {"ok": False, "abort_hpa": [], "fails": [f"종료 시점 가드 조회 실패: {str(exc)[:100]}"], "checks": [],
                "obs": {"hpa": {}, "inference": 0, "backend": 0, "worker_pods": 0, "oom": []}}


def restart_baseline_of(pods):
    return {p["metadata"]["name"]: restarts_of(p) for p in pods["items"]}


# ---------------------------------------------------------------------------------------------- k6 부하 생성 병목 판정
def classify_throttle(has_limit, state, ratio):
    """CFS throttling 지표를 해석한다. state 는 prom_status 의 상태(ok/empty/error).
    - query_error: 조회 오류 → 확인 불가
    - limit_metric_missing: CPU limit 이 있는데 지표가 없음 → 이상, 확인 불가
    - no_limit_no_metric: CPU limit 이 없고 지표도 없음 → CFS throttling 은 구조상 없다. 그러나 **노드 CPU 경쟁이나 다른 자원 경합이 없다는 뜻은 아니다**
    - observed / none_observed: 지표가 있고 비율이 > 0 / = 0"""
    if state == "error":
        return {"state": "query_error", "ratio": None, "note": "조회 오류: throttling 확인 불가"}
    if state != "ok" or ratio is None:
        if has_limit:
            return {"state": "limit_metric_missing", "ratio": None, "note": "CPU limit 이 있는데 throttling 지표가 없다: 이상(확인 불가)"}
        return {"state": "no_limit_no_metric", "ratio": None,
                "note": "CPU limit 없음 + throttling 지표 없음: CFS throttling 은 구조상 없다. 노드 CPU 경쟁·다른 자원 경합이 없다는 뜻은 아니다"}
    return {"state": "observed" if ratio > 0 else "none_observed", "ratio": ratio,
            "note": f"throttling 비율 {ratio:.4f}"}


CAVEAT = "이 판정은 부하 생성 병목이 '확인되지 않았다'는 뜻이지 '없다'는 증명이 아니다. CPU 평균 0.8 코어는 포화 확인의 사전 기준일 뿐 최대 처리량의 단독 증거가 아니다"


def assess_k6(k6_cpu_max, k6_cpu_avg, k6_throttle, k6_node_busy, inference_cpu_avg, throttle_state=None):
    """k6 CPU 가 0.7 코어 이상이면 경고. 아래 중 하나가 함께 확인될 때만 무효(부하 생성 병목):
       (a) k6 CPU throttling >= 5 % (실제 관측일 때만), (b) k6 노드 사용률 >= 90 %, (c) inference Pod 중 하나라도 평균 CPU < 0.8 코어.
       throttle_state(classify_throttle 의 상태)에 따라 'CPU limit 없음 + 지표 없음' 은 경고하지 않고 메모만 남기며, 조회 오류·limit 있는 컨테이너의 누락은 경고한다.
       측정값이 없으면(None) 그 조건은 '확인 불가'로 둔다. 반환: {'level': ok|warn|invalid, 'warnings', 'evidence', 'notes', 'caveat'}."""
    warnings, evidence, notes = [], [], []
    if throttle_state == "no_limit_no_metric":
        notes.append("CPU limit 없음 + throttling 지표 없음 - 병목 없음의 증거가 아님")
    if k6_cpu_max is None:
        return {"level": "warn", "warnings": ["k6 CPU 를 읽지 못해 부하 생성 병목을 판정하지 못함"], "evidence": [], "notes": notes, "caveat": CAVEAT}
    if k6_cpu_max < K6_WARN_CORES:
        return {"level": "ok", "warnings": [], "evidence": [], "notes": notes, "caveat": CAVEAT}
    warnings.append(f"k6 최대 CPU {k6_cpu_max:.2f} 코어 >= {K6_WARN_CORES} (평균 {k6_cpu_avg if k6_cpu_avg is None else round(k6_cpu_avg, 2)})")
    if k6_throttle is not None and k6_throttle >= K6_THROTTLE_INVALID:
        evidence.append(f"k6 CPU throttling {k6_throttle:.1%} >= {K6_THROTTLE_INVALID:.0%}")
    if k6_node_busy is not None and k6_node_busy >= K6_NODE_BUSY_INVALID:
        evidence.append(f"k6 노드 사용률 {k6_node_busy:.1%} >= {K6_NODE_BUSY_INVALID:.0%}")
    if inference_cpu_avg:
        low = {p: round(v, 3) for p, v in inference_cpu_avg.items() if v < SATURATED_CORES}
        if low:
            evidence.append(f"inference 평균 CPU < {SATURATED_CORES}: {low}")
    if k6_throttle is None and throttle_state != "no_limit_no_metric":
        warnings.append({"query_error": "throttling 조회 오류: 확인 불가", "limit_metric_missing": "CPU limit 이 있는데 throttling 지표 누락: 확인 불가"}.get(throttle_state, "throttling 확인 불가"))
    if k6_node_busy is None:
        warnings.append("노드 사용률 확인 불가")
    return {"level": "invalid" if evidence else "warn", "warnings": warnings, "evidence": evidence, "notes": notes, "caveat": CAVEAT}


# ---------------------------------------------------------------------------------------------- 결과 폴더
def new_result_folder(prefix, n, today=None):
    """이번 회차 전용 새 폴더. 이미 있으면 거부한다(기존 결과를 수정·삭제하지 않는다)."""
    folder = RESULTS / f"{(today or dt.date.today()).isoformat()}-{prefix}-{n}"
    if folder.exists():
        raise FileExistsError(f"{folder} 가 이미 있다: 기존 결과를 건드리지 않고 중단한다")
    return folder


def unique_summary_path(prefix, today=None):
    base = RESULTS / f"{(today or dt.date.today()).isoformat()}-{prefix}-summary"
    p = pathlib.Path(str(base) + ".json")
    i = 2
    while p.exists():
        p = pathlib.Path(f"{base}-{i}.json")
        i += 1
    return p


# ---------------------------------------------------------------------------------------------- 기존 P3 검사와 Prometheus
def p3_fixed(snap):
    r = subprocess.run([sys.executable, str(REPO / "loadtest/tools/p3.py"), "status", "--expect", "fixed", "--snap", str(snap)],
                       capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    bad = [l.strip() for l in r.stdout.splitlines() if "FAIL" in l]
    return r.returncode == 0, bad


def prom(port, expr, at):
    u = f"http://127.0.0.1:{port}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": at})
    try:
        return json.load(urllib.request.urlopen(u, timeout=30))["data"]["result"]
    except Exception as e:  # noqa: BLE001
        return f"ERR {e}"


def by(res, key="pod"):
    if isinstance(res, str):
        return res
    return {x["metric"].get(key, x["metric"].get("instance", "_")): round(float(x["value"][1]), 4) for x in res}


def scalar(res):
    if isinstance(res, str) or not res:
        return None
    try:
        v = float(res[0]["value"][1])
        return None if v != v else v          # NaN(분모 0) 은 값 없음
    except (KeyError, ValueError, IndexError):
        return None


def k6_queries(name, win, off):
    sel = f'namespace="{LOAD_NS}",pod="{name}",container="k6"'
    return {
        "k6_cpu_avg_cores": f'sum(rate(container_cpu_usage_seconds_total{{{sel}}}[{win}s] offset {off}s))',
        "k6_cpu_max_cores": f'max_over_time(sum(rate(container_cpu_usage_seconds_total{{{sel}}}[30s]))[{win}s:15s] offset {off}s)',
        "k6_throttle_ratio": f'sum(increase(container_cpu_cfs_throttled_periods_total{{{sel}}}[{win}s] offset {off}s)) / sum(increase(container_cpu_cfs_periods_total{{{sel}}}[{win}s] offset {off}s))',
        "k6_memory_max_mib": f'max_over_time(container_memory_working_set_bytes{{{sel}}}[{win}s] offset {off}s) / 1048576',
    }


def inference_throttle_query(win, off):
    sel = 'namespace="parking-guardian",pod=~"inference-.*",container="inference"'
    return f'sum by (pod)(increase(container_cpu_cfs_throttled_periods_total{{{sel}}}[{win}s] offset {off}s)) / sum by (pod)(increase(container_cpu_cfs_periods_total{{{sel}}}[{win}s] offset {off}s))'


def mv(summary, name):
    m = summary.get("metrics", {}).get(name, {})
    return m.get("values", m)


def prom_status(raw):
    """Prometheus 원본 응답의 상태. error(조회 오류) / empty(시리즈 0건) / ok."""
    if isinstance(raw, str):
        return {"state": "error", "reason": raw[:200]}
    if not raw:
        return {"state": "empty", "reason": "시리즈 0건: 그 시각·구간에 지표가 없다(아직 생기지 않았거나 수집되지 않음)"}
    return {"state": "ok", "series": len(raw)}


QUERY_KEY = {"stage_mean_s": "stage", "hpa_desired_max": "horizontalpodautoscaler", "node_busy_fraction": "instance", "node_steal_fraction": "instance"}


def build_queries(name, win, off):
    """한 회차의 모든 PromQL (이름 -> 식). 식에는 `offset {off}s` 가 들어 있어 평가 시각(`eval_time`)과 함께 저장하면 나중에 같은 구간을 다시 조회할 수 있다."""
    ns = 'namespace="parking-guardian"'
    ip = 'pod=~"inference-.*"'
    q = {
        "requests_by_pod_outcome": f'sum by (pod,outcome)(increase(pg_inference_requests_total{{{ns},{ip}}}[{win}s] offset {off}s))',
        "rejected_busy_by_pod": f'sum by (pod)(increase(pg_inference_rejected_total{{{ns},{ip}}}[{win}s] offset {off}s))',
        "cpu_avg_cores_by_pod": f'sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},{ip},container="inference"}}[{win}s] offset {off}s))',
        "cpu_max_cores_by_pod": f'max_over_time(sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},{ip},container="inference"}}[30s]))[{win}s:15s] offset {off}s)',
        "memory_max_mib_by_pod": f'max_over_time(container_memory_working_set_bytes{{{ns},{ip},container="inference"}}[{win}s] offset {off}s) / 1048576',
        "in_flight_max_by_pod": f'max_over_time(pg_inference_in_flight{{{ns},{ip}}}[{win}s] offset {off}s)',
        "running_max_by_pod": f'max_over_time(pg_inference_running{{{ns},{ip}}}[{win}s] offset {off}s)',
        "waiting_max_by_pod": f'max_over_time(pg_inference_waiting{{{ns},{ip}}}[{win}s] offset {off}s)',
        "lock_wait_p95_s_by_pod": f'histogram_quantile(0.95, sum by (le,pod)(increase(pg_inference_lock_wait_seconds_bucket{{{ns},{ip}}}[{win}s] offset {off}s)))',
        "stage_mean_s": f'sum by (stage)(increase(pg_inference_stage_seconds_sum{{{ns},{ip}}}[{win}s] offset {off}s)) / sum by (stage)(increase(pg_inference_stage_seconds_count{{{ns},{ip}}}[{win}s] offset {off}s))',
        "node_busy_fraction": f'1 - avg by (instance)(rate(node_cpu_seconds_total{{mode="idle"}}[{win}s] offset {off}s))',
        "node_steal_fraction": f'avg by (instance)(rate(node_cpu_seconds_total{{mode="steal"}}[{win}s] offset {off}s))',
        "backend_cpu_avg_cores_by_pod": f'sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},pod=~"backend-.*",container="backend"}}[{win}s] offset {off}s))',
        "hpa_desired_max": f'max_over_time(kube_horizontalpodautoscaler_status_desired_replicas{{{ns}}}[{win}s] offset {off}s)',
        "inference_throttle_ratio_by_pod": inference_throttle_query(win, off),
    }
    q.update(k6_queries(name, win, off))
    return q


def shape(name, raw):
    """원본 응답(list 또는 'ERR …')을 보고용 모양으로: k6_* 는 스칼라, 나머지는 라벨별 dict. 오류 문자열은 그대로."""
    if name.startswith("k6_"):
        return None if isinstance(raw, str) else scalar(raw)       # 오류면 값 없음(원본 응답·상태는 prometheus.json 에 남는다)
    if isinstance(raw, str):
        return raw
    if name == "requests_by_pod_outcome":
        return {f"{x['metric'].get('pod','?')}|{x['metric'].get('outcome','?')}": round(float(x["value"][1]), 1) for x in raw}
    return by(raw, QUERY_KEY.get(name, "pod"))


@contextmanager
def prometheus_forward():
    """로컬 포트로 Prometheus 에 연결하는 읽기 전용 port-forward (kubectl)."""
    pf = subprocess.Popen(["kubectl", "-n", "monitoring", "port-forward", "svc/monitoring-prometheus", f"{PROM_PORT}:9090"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(5)
        yield
    finally:
        pf.terminate()


def parse_vus(klog):
    """k6 로그의 `vus ... : 4 min=4 max=4` 줄에서 max 를 읽는다. 없으면 None."""
    hit = None
    for line in klog.splitlines():
        m = re.match(r"\s*vus[ .]*:\s*(\d+)\s+min=(\d+)\s+max=(\d+)", line)
        if m:
            hit = int(m.group(3))
    return hit


def throughput_breakdown(done, rate, busy, busy_rate, failed, failed_rate, unav, unav_rate, per_pod_ok, win):
    """성공(HTTP 200)·busy 503(대기열 상한에 의한 정상 거절)·실패·모델 불가를 구분해 기록한다. busy 503 은 자동 무효 사유가 아니지만 **성공 처리량에 넣지 않는다**
       (시도율을 처리량으로 오해하면 과대평가). 성공 처리량의 기준은 k6 의 성공 건수/전체 실행 시간(setup 포함이라 측정 구간 평균보다 작거나 같다 = 보수적).
       서버(Prometheus) 집계의 성공 수와 교차 확인하고 10 % 넘게 다르면 경고한다(등급에는 영향 없음)."""
    z = lambda v: v or 0  # noqa: E731
    attempted = z(done) + z(busy) + z(failed) + z(unav)
    server_ok = sum(per_pod_ok.values()) if per_pod_ok else None
    server_rate = round(server_ok / win, 3) if server_ok is not None else None
    warnings = []
    if server_rate and rate and abs(rate / server_rate - 1) > 0.10:
        warnings.append(f"k6 성공 처리량 {rate}/s 와 서버 집계 {server_rate}/s 가 10 % 넘게 다르다(둘 다 기록, C 에는 k6 성공 처리량을 쓴다)")
    return {"success_ok": done, "success_per_s": rate, "success_per_s_over_measure_window": round(done / win, 3) if done else None,
            "busy_503": busy, "busy_503_per_s": busy_rate, "busy_503_ratio": round(z(busy) / attempted, 4) if attempted else None,
            "failed": failed, "unavailable": unav, "attempted": attempted,
            "attempted_per_s": round(z(rate) + z(busy_rate) + z(failed_rate) + z(unav_rate), 3),
            "server_ok": server_ok, "server_ok_per_s": server_rate,
            "basis": "C 산정에는 success_per_s(k6 성공 건수/전체 실행 시간)만 쓴다. busy 503·시도율은 처리량이 아니다",
            "warnings": warnings}


def max_gap(ts_list, t0, t1):
    pts = [t0] + sorted(ts_list) + [t1]
    return max(b - a for a, b in zip(pts, pts[1:]))

def compute_grade(ctx, cpu, k6a):
    """사전 등록 v1 의 A/B/C 판정 (계획서 8.9절, README 변경 기록).
       C 무효: (안전) 중단·고정 상태 위반·재시작·OOM / (측정) k6 요약 없음, inference_failed·unavailable > 0, 부하 생성 병목 확인,
               가드 표본 부족·공백, 부하(VUS) 미달.
       B 참고: 위는 모두 통과했으나 k6 VUS 관측값이 없음(load_unverified), 두 inference Pod 의 CPU 를 모두 알 수 없음(insufficient_evidence),
               또는 알지만 0.8 코어 미만(not_saturated).
       A 공식 유효: 위 전부 통과하고 두 Pod 의 평균 CPU 가 모두 >= 0.8 코어.
       CPU 평균 0.8 은 포화 확인의 사전 기준일 뿐 최대 처리량의 단독 증거가 아니다."""
    def g(level, category, reasons):
        return {"level": level, "category": category, "reasons": reasons, "cpu_by_pod": cpu, "k6_assessment": k6a["level"],
                "criteria": {"saturated_cores": SATURATED_CORES, "min_guard_samples": MIN_GUARD_SAMPLES, "max_guard_gap_s": MAX_GUARD_GAP_S,
                             "k6_warn_cores": K6_WARN_CORES}, "caveat": CAVEAT}
    safety = []
    if ctx.get("aborted"):
        safety.append(ctx["aborted"])
    if ctx.get("guard_end_fails"):
        safety.append("종료 시점 고정 상태 위반: " + "; ".join(ctx["guard_end_fails"]))
    if ctx.get("restarts"):
        safety.append("Pod 재시작/이상: " + "; ".join(ctx["restarts"]))
    if ctx.get("oom"):
        safety.append("OOMKilled: " + ",".join(ctx["oom"]))
    if safety:
        return g("C", "safety_stop", safety)
    bad = []
    if not ctx.get("parsed"):
        bad.append("k6 요약을 읽지 못함")
    if ctx.get("failed"):
        bad.append(f"inference_failed={ctx['failed']}")
    if ctx.get("unavailable"):
        bad.append(f"inference_unavailable={ctx['unavailable']}")
    if k6a["level"] == "invalid":
        bad.append("부하 생성 병목 확인: " + "; ".join(k6a["evidence"]))
    if ctx.get("guard_samples", 0) < MIN_GUARD_SAMPLES:
        bad.append(f"가드 감시 표본 {ctx.get('guard_samples', 0)}개 < {MIN_GUARD_SAMPLES}")
    gap = ctx.get("guard_max_gap_s")
    if gap is None or gap > MAX_GUARD_GAP_S:
        bad.append(f"가드 감시 공백 {gap if gap is None else round(gap, 1)}초 > {MAX_GUARD_GAP_S}초")
    vmax, vtarget = ctx.get("vus_max"), ctx.get("vus_target", VUS)
    if vmax is not None and vmax < vtarget:
        bad.append(f"부하 미달: k6 VUS 최대 {vmax} < 목표 {vtarget}")
    if bad:
        return g("C", "measurement_invalid", bad)
    # B 사유를 모아 우선순위로 범주를 정한다: load_unverified(부하 목표 미확인, 재질의로 못 바꿈) > insufficient_evidence(CPU 증거 부족, 재질의로만 승격) > not_saturated
    weak = []
    if vmax is None:
        weak.append(("load_unverified", f"k6 VUS 최대(목표 {vtarget})를 로그에서 확인하지 못했다(판정 불가). 값을 추정하지 않는다"))
    expected = set(ctx.get("expected_pods") or [])
    if cpu is None or len(expected) != 2 or set(cpu) != expected or any(not isinstance(v, (int, float)) for v in cpu.values()):
        weak.append(("insufficient_evidence", "두 inference Pod 의 평균 CPU 를 모두 알 수 없다(지표 누락·불완전). 값을 추정하지 않는다"))
    else:
        low = {p: round(v, 3) for p, v in cpu.items() if v < SATURATED_CORES}
        if low:
            weak.append(("not_saturated", f"평균 CPU 가 {SATURATED_CORES} 코어 미만인 Pod: {low} - 처리량은 기록하되 포화를 확인하지 못했다"))
    if weak:
        order = ("load_unverified", "insufficient_evidence", "not_saturated")
        return g("B", min((c for c, _ in weak), key=order.index), [r for _, r in weak])
    return g("A", "ok", ["모든 조건 충족 (CPU 평균 0.8 이상은 포화 확인 조건이며 최대 처리량의 증거가 아니다)"])


def judge(ctx, pj, k6m, statuses):
    """Prometheus 에 의존하는 판단(CPU·노드 사용률·throttling·k6 병목·등급)을 한 곳에서 한다. 첫 결과와 재질의 결과가 같은 코드를 쓴다."""
    cpu_raw = pj.get("cpu_avg_cores_by_pod")
    cpu_ok = isinstance(cpu_raw, dict) and statuses.get("cpu_avg_cores_by_pod", {}).get("state") == "ok"
    cpu = dict(cpu_raw) if cpu_ok else None
    busy = pj.get("node_busy_fraction") if isinstance(pj.get("node_busy_fraction"), dict) else {}
    k6_node_busy = next((v for inst, v in busy.items() if ctx.get("node_ip") and inst.startswith(ctx["node_ip"] + ":")), None)
    thr_k6 = classify_throttle(False, statuses.get("k6_throttle_ratio", {}).get("state"), k6m.get("k6_throttle_ratio"))
    ratios = pj.get("inference_throttle_ratio_by_pod")
    inf_ratio = max(ratios.values()) if isinstance(ratios, dict) and ratios else None
    thr_inf = classify_throttle(bool(ctx.get("inference_has_cpu_limit")), statuses.get("inference_throttle_ratio_by_pod", {}).get("state"), inf_ratio)
    k6a = assess_k6(k6m.get("k6_cpu_max_cores"), k6m.get("k6_cpu_avg_cores"), thr_k6["ratio"], k6_node_busy, cpu or {}, throttle_state=thr_k6["state"])
    return {"cpu": cpu or {}, "k6_node_busy": k6_node_busy, "throttle": {"k6": thr_k6, "inference": thr_inf}, "k6a": k6a,
            "grade": compute_grade(ctx, cpu, k6a)}


def next_action(results, target_a=TARGET_A, max_attempts=MAX_ATTEMPTS):
    """회차 결과 목록으로 다음 회차를 할지 정한다. 반환: (계속 여부, 사유).
       멈춤: 안전 중단(가드·HPA·Worker·OOM·재시작·사전 점검 실패) / A 등급이 target_a 회 / 시도 max_attempts 회 / 무효(C) 3연속."""
    if not results:
        return True, ""
    last = results[-1]
    g = last.get("grade")
    if g is None or g.get("category") == "safety_stop":
        return False, "안전 중단 또는 사전 점검 실패: 원인을 보고하고 사용자 판단을 기다린다"
    if sum(1 for r in results if (r.get("grade") or {}).get("level") == "A") >= target_a:
        return False, f"A 등급 {target_a}회 확보"
    if len(results) >= max_attempts:
        return False, f"시도 {max_attempts}회 소진"
    if len(results) >= 3 and all((r.get("grade") or {}).get("level") == "C" for r in results[-3:]):
        return False, "무효(C) 3연속"
    return True, ""


def summarize_attempts(results, target_a=TARGET_A, max_attempts=MAX_ATTEMPTS):
    a_runs = [r for r in results if (r.get("grade") or {}).get("level") == "A"]
    counts = {}
    for r in results:
        lv = (r.get("grade") or {}).get("level", "C(사전 점검 실패)")
        counts[lv] = counts.get(lv, 0) + 1
    out = {"attempts": len(results), "max_attempts": max_attempts, "target_a": target_a, "grades": counts, "a_runs": [r["run"] for r in a_runs],
           "all_runs": [{"run": r.get("run"), "level": (r.get("grade") or {}).get("level"), "category": (r.get("grade") or {}).get("category"),
                         "throughput_ok_per_s": r.get("throughput_ok_per_s")} for r in results],
           "note": "C(처리 용량) 는 A 등급 회차의 k6 성공 처리량 중앙값이다. CPU 평균 0.8 코어는 포화 확인의 사전 기준일 뿐 최대 처리량의 증거가 아니다. B 등급은 C 산정에 쓰지 않는다."}
    if len(a_runs) >= target_a:
        vals = [r["throughput_ok_per_s"] for r in a_runs[:target_a]]
        out.update({"status": "C 확정", "C_median_ok_per_s": statistics.median(vals), "C_range": [min(vals), max(vals)], "C_basis_runs": [r["run"] for r in a_runs[:target_a]]})
    else:
        out.update({"status": "C 미확정", "reason": f"A 등급 {len(a_runs)}회 < {target_a}회 (시도 {len(results)}/{max_attempts})"})
    return out

def sha256_file(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def next_index(folder, pattern):
    i = 1
    while (pathlib.Path(folder) / pattern.format(n=i)).exists():
        i += 1
    return i


def write_new(path, text):
    """이미 있는 파일은 절대 덮어쓰지 않는다(배타적 생성)."""
    with open(path, "x", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o444)


def evaluate_upgrade(result, rec):
    """재질의 결과(rec)로 B 등급을 A 로 올릴 수 있는지 판정한다. 새 증거가 있을 때만 올린다: 같은 평가 시각·같은 식으로 다시 조회한 결과에
       두 inference Pod 의 평균 CPU 가 실제로 있어야 한다. 없으면 그대로 B. 값을 추정하거나 채우지 않는다. 반환: dict(원본 result 는 수정하지 않는다)."""
    orig = result.get("grade") or {}
    out = {"origin": "requery-evaluation", "original_grade": orig.get("level"), "original_category": orig.get("category"), "upgraded": False}
    if orig.get("level") != "B" or orig.get("category") != "insufficient_evidence":
        out["reason"] = f"등급 상승 대상이 아니다(원본 {orig.get('level')}/{orig.get('category')}): 재질의로 바꿀 수 없다"
        out["new_grade"] = orig.get("level")
        return out
    ctx = result.get("grade_inputs") or {}
    statuses = rec["status"]
    pj = {k: shape(k, v) for k, v in rec["raw"].items() if not k.startswith("k6_")}
    k6m = {k: shape(k, v) for k, v in rec["raw"].items() if k.startswith("k6_")}
    new = judge(ctx, pj, k6m, statuses)
    out["new_grade"] = new["grade"]["level"]
    out["new_category"] = new["grade"]["category"]
    out["new_reasons"] = new["grade"]["reasons"]
    out["cpu_by_pod_requery"] = new["cpu"]
    out["cpu_status_requery"] = statuses.get("cpu_avg_cores_by_pod")
    if new["grade"]["level"] == "A" and new["cpu"]:
        out["upgraded"] = True
        out["evidence"] = {"requery_file": rec.get("file"), "requery_sha256": rec.get("sha256"), "eval_time": rec["eval_time"],
                           "queries_from": "prometheus.json (최초 결과와 같은 식·같은 평가 시각)", "series": statuses["cpu_avg_cores_by_pod"].get("series")}
    else:
        out["reason"] = "재질의 결과로도 두 Pod 의 CPU 를 확보하지 못했거나 A 조건을 채우지 못했다 - B 유지(추정하지 않음)"
    return out


def requery_folder(folder):
    """종료된 회차의 Prometheus 지표를 같은 식·같은 평가 시각으로 다시 조회한다. 원본(prometheus.json, result.json, SHA256SUMS)은 건드리지 않고
       새 파일(prometheus-requery-N.json, grade-upgrade-N.json, requery-N.sha256)만 만든다. 재질의 결과는 origin=requery 로 표시한다."""
    folder = pathlib.Path(folder)
    base_path, res_path = folder / "prometheus.json", folder / "result.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    result = json.loads(res_path.read_text(encoding="utf-8"))
    n = next_index(folder, "prometheus-requery-{n}.json")
    with prometheus_forward():
        raw = {k: prom(PROM_PORT, e, base["eval_time"]) for k, e in base["queries"].items()}
    statuses = {k: prom_status(v) for k, v in raw.items()}
    rec_name = f"prometheus-requery-{n}.json"
    rec = {"origin": "requery", "requery_of": "prometheus.json", "requery_of_sha256": sha256_file(base_path),
           "requeried_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "eval_time": base["eval_time"],
           "window_s": base.get("window_s"), "step": base.get("step"), "queries": base["queries"], "raw": raw, "status": statuses,
           "missing": [k for k, s in statuses.items() if s["state"] != "ok"]}
    write_new(folder / rec_name, json.dumps(rec, ensure_ascii=False, indent=1))
    rec["file"], rec["sha256"] = rec_name, sha256_file(folder / rec_name)
    up = evaluate_upgrade(result, rec)
    up_name = f"grade-upgrade-{n}.json"
    write_new(folder / up_name, json.dumps(up, ensure_ascii=False, indent=1))
    write_new(folder / f"requery-{n}.sha256", f"{rec['sha256']} *{rec_name}\n{sha256_file(folder / up_name)} *{up_name}\n")
    return {"requery_file": rec_name, "upgrade_file": up_name, "missing": rec["missing"], "upgrade": up}

def configmap_budget(files):
    """k6 ConfigMap 의 크기를 로컬에서 계산한다(서버 호출 없음). files: {키: 경로}.
       raw  = 서버 검증이 세는 합(키 길이 + 디코드한 값). base64 최악 = JSON 본문에서 이진 값이 base64(4/3)로 늘어난 합.
       한도 1 MiB: raw 가 넘으면 서버가 거부한다. 최악(base64)이 넘으면 안전을 위해 시작하지 않는다. 최악이 90 % 를 넘으면 경고."""
    raw = worst = 0
    parts = {}
    for key, path in files.items():
        data = pathlib.Path(path).read_bytes()
        is_text = False
        try:
            data.decode("utf-8")
            is_text = key.endswith(".js")
        except UnicodeDecodeError:
            pass
        size = len(data)
        w = size if is_text else (size + 2) // 3 * 4
        raw += len(key) + size
        worst += len(key) + w
        parts[key] = {"bytes": size, "worst_case_bytes": w}
    return {"raw": raw, "worst_case": worst, "limit": CM_LIMIT, "raw_pct": round(raw / CM_LIMIT * 100, 1), "worst_pct": round(worst / CM_LIMIT * 100, 1),
            "ok": worst <= CM_LIMIT and raw <= CM_LIMIT, "warn": worst > CM_LIMIT * CM_WARN_FRACTION, "parts": parts}


def k6_configmap_files():
    return {"lib.js": REPO / "loadtest/k6/lib.js", "inference.js": REPO / "loadtest/k6/inference.js",
            "sample_a.jpg": REPO / "backend/uploads/sample_a.jpg", "sample_b.jpg": REPO / "backend/uploads/sample_b.jpg"}

def enable_token_cache():
    """kubectl 마다 `aws eks get-token`(약 3초)을 실행하지 않도록 관찰자의 KubeAuth(임시 kubeconfig 에 토큰 캐시)를 쓴다.
       KUBECONFIG 환경변수로 이 프로세스와 자식(p3.py 등)의 kubectl 이 모두 같은 파일을 쓰게 한다. 가드 루프가 refresh() 로 만료 전에 갱신한다.
       효과: p3.py status 약 38 초 -> 한 자릿수 초, 가드 한 바퀴 약 56 초 -> 수 초 [측정]. 실패하면 None(원래 방식으로 계속)."""
    try:
        import p2_collect as pc
        import p2_watch as pw
        ka = pw.KubeAuth.try_create(pc.default_runner)
    except Exception as exc:  # noqa: BLE001
        print(f"[토큰 캐시] 쓰지 않는다(원래 방식으로 계속): {type(exc).__name__}", file=sys.stderr)
        return None
    if ka:
        os.environ["KUBECONFIG"] = ka.path
    return ka


# ---------------------------------------------------------------------------------------------- 한 회차
def preflight(ctx):
    """실행 직전 점검 (기존 점검 + 새 점검). 반환: (ok, 사유 목록, 고정 상태 검증 결과)."""
    reasons = []
    ok, bad = p3_fixed(ctx["snap"])
    if not ok:
        reasons += ["P3 고정 검사 FAIL: " + "; ".join(bad)]
    g = read_guard()
    if not g["ok"]:
        reasons += ["고정 상태 검증 FAIL: " + "; ".join(g["fails"])]
    eps = json.loads(kube("-n", APP_NS, "get", "endpointslices", "-l", "kubernetes.io/service-name=inference", "-o", "json"))
    ready = [e["addresses"][0] for s in eps["items"] for e in s.get("endpoints", []) if e["conditions"].get("ready")]
    if len(ready) != 2:
        reasons.append(f"inference 엔드포인트 ready={len(ready)} (2 여야 함)")
    k6pods = kube("-n", LOAD_NS, "get", "pods", "--no-headers", check=False).strip()
    if k6pods:
        reasons.append("남은 k6 Pod 가 있음")
    node = None
    try:
        node, ip, free, _ = pick_node(kj("get", "nodes"), kj("get", "pods", "-A"), ctx.get("node_override"))
        ctx["node"], ctx["node_ip"] = node, ip
    except RuntimeError as exc:
        reasons.append(f"k6 노드 선택 실패: {exc}")
        free = None
    log(f"사전 점검: P3 fixed={'OK' if ok else 'FAIL'} 고정 검증={'OK' if g['ok'] else 'FAIL'} 엔드포인트 ready={len(ready)} k6 노드={node}(여유 {free}m) 남은 k6 Pod={'있음' if k6pods else '없음'}")
    return not reasons, reasons, g


def run_one(ctx, n):
    name = f"{ctx['prefix']}-{n}"
    folder = new_result_folder(ctx["prefix"], n)
    folder.mkdir(parents=True)
    snap = ctx["snap"]
    ok, reasons, g0 = preflight(ctx)
    if not ok:
        (folder / "ABORTED.txt").write_text("사전 점검 실패: " + "; ".join(reasons) + "\n", encoding="utf-8")
        return {"run": name, "valid": False, "aborted": "사전 점검 실패 " + "; ".join(reasons)}, False
    pods0 = kj("-n", APP_NS, "get", "pods")
    base_restarts = restart_baseline_of(pods0)
    inference_pods = sorted(p["metadata"]["name"] for p in pods0["items"] if app_of(p) == "inference")
    inference_has_limit = any("cpu" in (c.get("resources", {}).get("limits") or {}) for p in pods0["items"] if app_of(p) == "inference" for c in p["spec"]["containers"])
    identity = {"inference_pods": [{"name": p["metadata"]["name"], "uid": p["metadata"].get("uid"), "node": p["spec"].get("nodeName")}
                                   for p in pods0["items"] if app_of(p) == "inference"]}
    images = {d["metadata"]["name"]: d["spec"]["template"]["spec"]["containers"][0]["image"].rsplit(":", 1)[-1] for d in kj("-n", APP_NS, "get", "deploy")["items"]}

    start = dt.datetime.now(dt.timezone.utc)
    t_create = time.time()
    kube("create", "-f", "-", data=POD_YAML.format(name=name, node=ctx["node"], vus=VUS, duration=DURATION))
    aborted, phase = None, ""
    guard_log, guard_errors = [], 0
    t_end = time.time() + 600
    while time.time() < t_end:
        time.sleep(LOOP_SECONDS)
        if ctx.get("auth"):
            ctx["auth"].kubeconfig()                       # 토큰 만료 전 갱신(같은 파일을 원자적으로 교체)
        try:
            phase = kube("-n", LOAD_NS, "get", "pod", name, "-o", "jsonpath={.status.phase}", check=False).strip()
            if phase in ("Succeeded", "Failed"):
                break
            g = read_guard(base_restarts)
            guard_errors = 0
        except RuntimeError as exc:
            guard_errors += 1                              # 일시적 조회 실패는 2번까지 넘기고, 3번째 연속이면 감시 불능으로 중단
            if guard_errors >= 3:
                aborted = f"감시 불능으로 중단(가드 조회 {guard_errors}회 연속 실패): {str(exc)[:120]}"
                kube("-n", LOAD_NS, "delete", "pod", name, "--wait=false", check=False)
                break
            continue
        guard_log.append({"ts": time.time(), "t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "ok": g["ok"], "obs": g["obs"], "fails": g["fails"]})
        if g["abort_hpa"]:
            aborted = "HPA current/desired > 2 로 즉시 중단: " + "; ".join(g["abort_hpa"])
        elif not g["ok"]:
            aborted = "고정 상태 위반으로 중단: " + "; ".join(g["fails"])
        else:
            okp, badp = p3_fixed(snap)            # 안전 중단: Pod 재시작/Pending/HPA 변화 (기존 검사)
            if not okp:
                aborted = "안전 중단: " + "; ".join(badp)
        if aborted:
            kube("-n", LOAD_NS, "delete", "pod", name, "--wait=false", check=False)
            break
    t_fin = time.time()
    end = dt.datetime.now(dt.timezone.utc)
    pod_json = json.loads(kube("-n", LOAD_NS, "get", "pod", name, "-o", "json", check=False) or "{}")
    k6_oom = oom_of(pod_json) if pod_json else []
    identity["k6_pod"] = {"name": name, "uid": (pod_json.get("metadata") or {}).get("uid"), "node": ctx["node"], "node_ip": ctx.get("node_ip")}
    try:
        fin = pod_json["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"]
        finished_ts = dt.datetime.strptime(fin, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except Exception:  # noqa: BLE001
        finished_ts = time.time() - 30
    klog = kube("-n", LOAD_NS, "logs", name, check=False)
    (folder / "k6.log").write_text(klog, encoding="utf-8")
    kube("-n", LOAD_NS, "delete", "pod", name, "--wait=false", check=False)
    log(f"{name}: k6 종료 phase={phase if not aborted else 'aborted'} {end:%H:%M:%S}Z")
    (folder / "guard.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in guard_log), encoding="utf-8")

    # ---- Prometheus: 모든 식과 원본 응답·상태·평가 시각을 저장한다(나중에 같은 구간을 다시 조회할 수 있게)
    time.sleep(25)
    at, win = time.time(), 180
    off = max(0, int(at - finished_ts))  # 종료 시각에 맞춰 마지막 180초만 본다
    queries = build_queries(name, win, off)
    with prometheus_forward():
        raw = {k: prom(PROM_PORT, e, at) for k, e in queries.items()}
    statuses = {k: prom_status(v) for k, v in raw.items()}
    pj = {k: shape(k, v) for k, v in raw.items() if not k.startswith("k6_")}
    k6m = {k: shape(k, v) for k, v in raw.items() if k.startswith("k6_")}
    pods_after = kube("-n", APP_NS, "get", "pods", "-o", "wide", "--no-headers")
    missing = {k: s for k, s in statuses.items() if s["state"] != "ok"}
    prom_record = {"origin": "initial", "window_s": win, "step": "instant 평가 + 하위 쿼리 15s", "eval_time": at,
                   "eval_time_utc": dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(timespec="seconds"),
                   "finished_ts": finished_ts, "finished_utc": dt.datetime.fromtimestamp(finished_ts, dt.timezone.utc).isoformat(timespec="seconds"),
                   "run": {"name": name, "start_utc": start.isoformat(timespec="seconds"), "end_utc": end.isoformat(timespec="seconds"), **identity},
                   "queries": queries, "raw": raw, "status": statuses, "missing": missing,
                   "results": pj, "k6": k6m}
    (folder / "prometheus.json").write_text(json.dumps(prom_record, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- k6 로그에서 핵심 수치
    def grab(label):
        hit = None
        for line in klog.splitlines():
            t = line.strip()
            if t.startswith(label) and ("..." in t or "=" in t):
                hit = t  # 마지막(요약 표) 줄
        return hit
    lines = {k: grab(k) for k in ("inference_done", "inference_busy", "inference_failed", "inference_unavailable", "inference_elapsed_ms",
                                  "{ phase:measure }", "iterations")}

    def num(line):
        m = re.search(r":\s*([0-9.]+)\s+([0-9.]+)/s", line or "")
        return (float(m.group(1)), float(m.group(2))) if m else (None, None)
    done, rate = num(lines["inference_done"]); busy, busy_rate = num(lines["inference_busy"])
    failed, failed_rate = num(lines["inference_failed"]); unav, unav_rate = num(lines["inference_unavailable"])
    done, busy, failed, unav = (None if v is None else int(round(v)) for v in (done, busy, failed, unav))     # k6 는 건수를 소수로 출력한다 - 정수로

    def trend(line):
        out = {}
        for k in ("avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"):
            m = re.search(re.escape(k) + r"=([0-9.]+)(µs|ms|s)", line or "")
            if m:
                v = float(m.group(1)); out[k] = v * (1000 if m.group(2) == "s" else (0.001 if m.group(2) == "µs" else 1))
        return out
    client = trend(lines["{ phase:measure }"])
    server_ms = trend(lines["inference_elapsed_ms"])

    reqs = pj["requests_by_pod_outcome"]
    per_pod_ok = {}
    if isinstance(reqs, dict):
        for k, v in reqs.items():
            pod, outcome = k.split("|")
            if outcome == "ok":
                per_pod_ok[pod] = per_pod_ok.get(pod, 0) + v
    total_ok = sum(per_pod_ok.values()) or 1
    share = {p: round(v / total_ok, 3) for p, v in per_pod_ok.items()}
    balanced = len(share) == 2 and all(0.40 <= s <= 0.60 for s in share.values())
    tp = throughput_breakdown(done, rate, busy, busy_rate, failed, failed_rate, unav, unav_rate, per_pod_ok, win)
    g_end = safe_read_guard(base_restarts)
    gh = guard_log + [{"obs": g_end["obs"]}]
    max_hpa = {h: {"current": max((x["obs"]["hpa"].get(h, {}).get("current") or 0) for x in gh),
                   "desired": max((x["obs"]["hpa"].get(h, {}).get("desired") or 0) for x in gh)} for h in HPA_FIXED}
    restarts = [l for l in pods_after.splitlines() if l.split()[3] != "0"]
    gctx = {"aborted": aborted, "guard_end_fails": g_end["fails"] if not g_end["ok"] else [], "restarts": restarts, "oom": k6_oom + g_end["obs"]["oom"],
            "parsed": done is not None, "failed": failed, "unavailable": unav, "guard_samples": len(guard_log),
            "guard_max_gap_s": max_gap([x["ts"] for x in guard_log], t_create, t_fin), "vus_max": parse_vus(klog), "vus_target": VUS,
            "expected_pods": inference_pods, "node_ip": ctx.get("node_ip"), "inference_has_cpu_limit": inference_has_limit}
    j = judge(gctx, pj, k6m, statuses)
    grade, k6a = j["grade"], j["k6a"]
    invalid = list(grade["reasons"]) if grade["level"] == "C" else []
    warnings = list(k6a["warnings"]) + tp["warnings"] + ([f"Prometheus 지표 누락 {len(missing)}건: {', '.join(sorted(missing))[:200]}"] if missing else [])
    if grade["level"] == "B":
        warnings.append(f"등급 B({grade['category']}): " + "; ".join(grade["reasons"]))
    cpu = j["cpu"]
    both_saturated = len(cpu) == 2 and all(v >= SATURATED_CORES for v in cpu.values())
    result = {"run": name, "start_utc": start.isoformat(timespec="seconds"), "end_utc": end.isoformat(timespec="seconds"),
              "grade": grade, "valid": grade["level"] != "C", "invalid_reasons": invalid, "warnings": warnings,
              "throughput_ok_per_s": rate, "ok_count": done, "busy_503": busy, "failed": failed, "unavailable": unav, "throughput": tp,
              "failure_rate": ((busy or 0) + (failed or 0) + (unav or 0)) / (((done or 0) + (busy or 0) + (failed or 0) + (unav or 0)) or 1),
              "client_ms_measure": client, "server_elapsed_ms": server_ms, "vus": {"target": VUS, "max_observed": gctx["vus_max"]},
              "share_by_pod": share, "balanced_40_60": balanced, "cpu_avg_cores_by_pod": cpu, "both_saturated_ge_0_8": both_saturated,
              "k6": {"node": ctx["node"], "cpu_avg_cores": k6m["k6_cpu_avg_cores"], "cpu_max_cores": k6m["k6_cpu_max_cores"],
                     "memory_max_mib": k6m["k6_memory_max_mib"], "node_busy": j["k6_node_busy"], "oom": k6_oom, "assessment": k6a["level"],
                     "evidence": k6a["evidence"], "notes": k6a["notes"], "caveat": k6a["caveat"]},
              "throttling": j["throttle"],
              "guard": {"hpa_max_seen": max_hpa, "samples": len(guard_log), "max_gap_s": round(gctx["guard_max_gap_s"], 1),
                        "worker_pods_seen": max([x["obs"]["worker_pods"] for x in guard_log] + [g_end["obs"]["worker_pods"]]), "oom": g_end["obs"]["oom"]},
              "prometheus_missing": sorted(missing), "image_tags": images, "snapshot": str(pathlib.Path(snap).name),
              "grade_inputs": {**gctx, "oom": list(gctx["oom"])},
              "run_identity": identity, "prometheus": pj}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    conditions = f"""# {name} 조건과 결과 — {dt.date.today().isoformat()}
- 로컬 전용. B0(E3 N=2): 클러스터 안 k6 Pod(노드 {ctx['node']}, `grafana/k6:2.3.0`, 100m/128Mi) → `http://inference.parking-guardian.svc.cluster.local:8000`(Service), Pod 2개(HPA 고정 2).
- 조건: closed-loop VUS={VUS}, `--no-connection-reuse`, {DURATION}, 워밍업 5장(setup, 지표 제외). 스크립트 `loadtest/k6/inference.js`(변경 없음). 이미지 {images.get('inference')}.
- 시간: {result['start_utc']} ~ {result['end_utc']} UTC. 고정 전 스냅샷(S0) `{result['snapshot']}`.
- **등급 {grade['level']} ({grade['category']})**: {'; '.join(grade['reasons'])}
- 경고: {'; '.join(warnings) if warnings else '없음'}
- 처리량(성공) {rate} 장/초 (성공 {done}건) - **C 산정에는 성공 처리량만 쓴다**. busy 503 {busy}건 ({tp['busy_503_ratio']}, {tp['busy_503_per_s']} 건/초), 실패 {failed}, 모델 불가 {unav}, 시도 합계 {tp['attempted']}건 ({tp['attempted_per_s']} 건/초). 서버 집계 성공 {tp['server_ok_per_s']} 건/초(참고)
- k6 클라이언트(ms, measure): {json.dumps(client)} / VUS 최대 관측 {gctx['vus_max']} (목표 {VUS})
- 서버 추론 시간(ms): {json.dumps(server_ms)}
- Pod 별 성공 처리 비율: {share} (균형 40~60%: {balanced}) / Pod 별 평균 CPU(코어): {cpu} (둘 다 ≥{SATURATED_CORES}: {both_saturated})
- k6 Pod: CPU 평균 {k6m['k6_cpu_avg_cores']} / 최대 {k6m['k6_cpu_max_cores']} 코어, 노드 사용률 {j['k6_node_busy']}, OOM {k6_oom or '없음'} → {k6a['level']}
- throttling: k6 {j['throttle']['k6']['state']}, inference {j['throttle']['inference']['state']} ({j['throttle']['k6']['note']})
- 고정 상태: HPA 최대 관측 {json.dumps(max_hpa)}, Worker Pod 관측 최대 {result['guard']['worker_pods_seen']}, 가드 표본 {len(guard_log)}회 (최대 공백 {result['guard']['max_gap_s']}초)
- Prometheus 누락: {', '.join(sorted(missing)) or '없음'} (재질의: `e3_run.py --requery <이 폴더>`)
- {CAVEAT}
- 관찰자 DB 수집 등 외부 조건은 실행자가 별도 기록한다(수치에서 임의로 빼지 않는다).
"""
    (folder / "conditions.md").write_text(conditions, encoding="utf-8")
    (folder / "Pods-after.txt").write_text(pods_after, encoding="utf-8")
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()} *{p.name}" for p in sorted(folder.iterdir()) if p.name != "SHA256SUMS"]
    (folder / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode("utf-8"))
    for p in folder.iterdir():
        os.chmod(p, 0o444)
    log(f"{name}: 등급 {grade['level']}({grade['category']}) 처리량={rate}/s 성공={done} busy={busy} 실패={failed} 점유={share} CPU={cpu} k6={k6a['level']} p95={client.get('p(95)')}ms")
    return result, grade["level"] != "C"


# ---------------------------------------------------------------------------------------------- dry-run / main
def dry_run(ctx):
    """읽기 전용 점검: 스냅샷(S0 검증), 노드 선택, 고정 상태 검증(결과를 보여 줄 뿐 실패해도 계속), ConfigMap 크기, k6 Pod 정의. 아무것도 만들지 않는다."""
    ok, why, warns = verify_baseline_snapshot(ctx["snap"])
    print(f"스냅샷(S0) {ctx['snap']}: {'OK' if ok else 'FAIL - ' + '; '.join(why)}")
    for w in warns:
        print(f"   경고: {w}")
    budget = configmap_budget(k6_configmap_files())
    print(f"ConfigMap k6-scripts: raw {budget['raw']} B ({budget['raw_pct']}%) / 최악(base64) {budget['worst_case']} B ({budget['worst_pct']}%) / 한도 {budget['limit']} B -> "
          f"{'OK' if budget['ok'] else '초과'}{' (경고: 90% 초과)' if budget['warn'] else ''}")
    nodes, pods = kj("get", "nodes"), kj("get", "pods", "-A")
    try:
        node, ip, free, table = pick_node(nodes, pods, ctx.get("node_override"))
        print(f"k6 노드: {node} (IP {ip}, CPU 여유 {free}m)")
    except RuntimeError as exc:
        node, ip, table = "<없음>", None, []
        print(f"k6 노드: 선택 실패 - {exc}")
    for t in node_table(nodes, pods):
        print(f"   {t['node']:50} 여유 {t['free_m']:5}m  {'후보' if t['eligible'] else '제외: ' + ', '.join(t['why'])}")
    g = read_guard()
    for name, good, detail in g["checks"]:
        print(f"  {'PASS' if good else 'FAIL'}  {name}  {detail}")
    print(f"고정 상태 검증: {'통과' if g['ok'] else '미통과(고정 전이라면 정상: HPA min=max=2 가 아직 아님)'}; HPA 상한 초과 중단 대상: {g['abort_hpa'] or '없음'}")
    okp, bad = p3_fixed(ctx["snap"])
    print(f"P3 고정 검사: {'통과' if okp else '미통과 ' + str(len(bad)) + '항목(고정 전이라면 정상)'}")
    print("---- 만들게 될 k6 Pod 정의(미생성) ----")
    print(POD_YAML.format(name=f"{ctx['prefix']}-<회차>", node=node, vus=VUS, duration=DURATION))
    print("(--dry-run: 아무것도 만들지 않았다)")
    return 0 if ok else 2


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("first", type=int, nargs="?", help="시작 회차 번호")
    ap.add_argument("last", type=int, nargs="?", help="끝 회차 번호 (시도 수는 --max-attempts 가 따로 제한한다)")
    ap.add_argument("--snap", default=None, help="변경 전 baseline 스냅샷(S0) 폴더 - p3.py pause 가 만든 것. 고정 후(S1) 스냅샷은 거부된다")
    ap.add_argument("--node", default=None, help="k6 를 둘 노드를 직접 지정(같은 조건을 통과해야 한다)")
    ap.add_argument("--prefix", default="b0", help="결과 폴더 이름 접두어(기본 b0)")
    ap.add_argument("--dry-run", action="store_true", help="읽기 전용 점검만 하고 아무것도 만들지 않는다")
    ap.add_argument("--target-a", type=int, default=TARGET_A, help="A 등급이 이만큼 모이면 멈춘다(기본 3)")
    ap.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS, help="시도 한도(기본 5)")
    ap.add_argument("--requery", default=None, metavar="결과폴더", help="종료된 회차의 Prometheus 지표를 같은 식·같은 시각으로 다시 조회한다(새 파일만 만든다)")
    ap.add_argument("--no-token-cache", action="store_true", help="kubectl 마다 aws eks get-token 을 실행하는 원래 방식(감시 주기가 느려진다)")
    a = ap.parse_args(argv)
    if not a.requery:
        if a.first is None or a.last is None or not a.snap:
            ap.error("회차 범위(first last)와 --snap 이 필요하다 (또는 --requery <결과폴더>)")
    return a


def main(argv=None):
    global DRY_RUN
    a = parse_args(argv)
    DRY_RUN = a.dry_run
    old_kubeconfig = os.environ.get("KUBECONFIG")
    auth = None if a.no_token_cache else enable_token_cache()
    try:
        if a.requery:
            out = requery_folder(a.requery)
            up = out["upgrade"]
            print(f"재질의 저장: {out['requery_file']}, 평가: {out['upgrade_file']}; 누락 {len(out['missing'])}건; 등급 {up.get('original_grade')} -> {up.get('new_grade')} (상승 {up['upgraded']}) {up.get('reason', '')}")
            return 0
        ctx = {"snap": pathlib.Path(a.snap), "prefix": a.prefix, "node_override": a.node, "auth": auth}
        if a.dry_run:
            return dry_run(ctx)
        ok, why, warns = verify_baseline_snapshot(ctx["snap"])
        for w in warns:
            log(f"경고: {w}")
        if not ok:
            print("스냅샷(S0) 검증 실패: " + "; ".join(why))
            return 2
        budget = configmap_budget(k6_configmap_files())
        if not budget["ok"]:
            print(f"ConfigMap 크기 초과 위험: raw {budget['raw']} B / 최악 {budget['worst_case']} B > 한도 {budget['limit']} B - 아무것도 만들지 않고 중단한다")
            return 2
        if budget["warn"]:
            log(f"경고: ConfigMap 최악 크기가 한도의 {budget['worst_pct']}% (raw {budget['raw_pct']}%)")
        summary_path = unique_summary_path(a.prefix)
        # ConfigMap: 현재 스크립트와 샘플 사진으로 다시 만든다 (이 스크립트가 만든 loadtest 네임스페이스 안에서만)
        try:
            kube("create", "namespace", LOAD_NS, check=False)
            kube("-n", LOAD_NS, "delete", "configmap", "k6-scripts", check=False)
            kube("-n", LOAD_NS, "create", "configmap", "k6-scripts", *[f"--from-file={k}={v}" for k, v in k6_configmap_files().items()])
        except RuntimeError as exc:
            kube("delete", "namespace", LOAD_NS, "--wait=false", check=False)
            print(f"ConfigMap 생성 실패(크기 한도 등): {str(exc)[:200]} - k6 Pod 를 만들지 않고 정리했다")
            return 3
        results = []
        try:
            for n in range(a.first, a.last + 1):
                try:
                    res, good = run_one(ctx, n)
                except FileExistsError as exc:
                    log(str(exc)); results.append({"run": f"{a.prefix}-{n}", "valid": False, "aborted": str(exc)}); break
                results.append(res)
                go, why_stop = next_action(results, a.target_a, a.max_attempts)
                if not go:
                    log(f"중단: {why_stop}")
                    break
                if n < a.last:
                    log("60초 휴식"); time.sleep(60)
        finally:
            summary = {"runs": results, "summary": summarize_attempts(results, a.target_a, a.max_attempts)}
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
            # 정리: 임시 리소스 (이 스크립트가 만든 것만)
            kube("delete", "namespace", LOAD_NS, "--wait=false", check=False)
            log("loadtest 네임스페이스 삭제 요청")
        s = summary["summary"]
        log(f"{s['status']}: {s.get('C_median_ok_per_s', s.get('reason'))}")
        return 0
    finally:
        if auth:
            auth.close()
            if old_kubeconfig is None:
                os.environ.pop("KUBECONFIG", None)
            else:
                os.environ["KUBECONFIG"] = old_kubeconfig


if __name__ == "__main__":
    sys.exit(main())
