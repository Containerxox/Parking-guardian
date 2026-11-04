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
import subprocess
import sys
import time
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
LOOP_SECONDS = 10
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


def restart_baseline_of(pods):
    return {p["metadata"]["name"]: restarts_of(p) for p in pods["items"]}


# ---------------------------------------------------------------------------------------------- k6 부하 생성 병목 판정
def assess_k6(k6_cpu_max, k6_cpu_avg, k6_throttle, k6_node_busy, inference_cpu_avg):
    """k6 CPU 가 0.7 코어 이상이면 경고. 아래 중 하나가 함께 확인될 때만 무효(부하 생성 병목):
       (a) k6 CPU throttling >= 5 %, (b) k6 노드 사용률 >= 90 %, (c) inference Pod 중 하나라도 평균 CPU < 0.8 코어(서버가 포화하지 못함).
       측정값이 없으면(None) 그 조건은 '확인 불가'로 두고 경고에 남긴다. 반환: {'level': ok|warn|invalid, 'warnings': [..], 'evidence': [..]}."""
    warnings, evidence = [], []
    if k6_cpu_max is None:
        return {"level": "warn", "warnings": ["k6 CPU 를 읽지 못해 부하 생성 병목을 판정하지 못함"], "evidence": []}
    if k6_cpu_max < K6_WARN_CORES:
        return {"level": "ok", "warnings": [], "evidence": []}
    warnings.append(f"k6 최대 CPU {k6_cpu_max:.2f} 코어 >= {K6_WARN_CORES} (평균 {k6_cpu_avg if k6_cpu_avg is None else round(k6_cpu_avg, 2)})")
    if k6_throttle is not None and k6_throttle >= K6_THROTTLE_INVALID:
        evidence.append(f"k6 CPU throttling {k6_throttle:.1%} >= {K6_THROTTLE_INVALID:.0%}")
    if k6_node_busy is not None and k6_node_busy >= K6_NODE_BUSY_INVALID:
        evidence.append(f"k6 노드 사용률 {k6_node_busy:.1%} >= {K6_NODE_BUSY_INVALID:.0%}")
    if inference_cpu_avg:
        low = {p: round(v, 3) for p, v in inference_cpu_avg.items() if v < SATURATED_CORES}
        if low:
            evidence.append(f"inference 평균 CPU < {SATURATED_CORES}: {low}")
    for name, v in (("throttling", k6_throttle), ("노드 사용률", k6_node_busy)):
        if v is None:
            warnings.append(f"{name} 확인 불가")
    return {"level": "invalid" if evidence else "warn", "warnings": warnings, "evidence": evidence}


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
    images = {d["metadata"]["name"]: d["spec"]["template"]["spec"]["containers"][0]["image"].rsplit(":", 1)[-1] for d in kj("-n", APP_NS, "get", "deploy")["items"]}

    start = dt.datetime.now(dt.timezone.utc)
    kube("create", "-f", "-", data=POD_YAML.format(name=name, node=ctx["node"], vus=VUS, duration=DURATION))
    aborted, phase = None, ""
    guard_log = []
    t_end = time.time() + 600
    while time.time() < t_end:
        time.sleep(LOOP_SECONDS)
        phase = kube("-n", LOAD_NS, "get", "pod", name, "-o", "jsonpath={.status.phase}", check=False).strip()
        if phase in ("Succeeded", "Failed"):
            break
        g = read_guard(base_restarts)
        guard_log.append({"t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "ok": g["ok"], "obs": g["obs"], "fails": g["fails"]})
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
    end = dt.datetime.now(dt.timezone.utc)
    pod_json = json.loads(kube("-n", LOAD_NS, "get", "pod", name, "-o", "json", check=False) or "{}")
    k6_oom = oom_of(pod_json) if pod_json else []
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

    time.sleep(25)
    pf = subprocess.Popen(["kubectl", "-n", "monitoring", "port-forward", "svc/monitoring-prometheus", f"{PROM_PORT}:9090"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    at, win = time.time(), 180
    off = max(0, int(at - finished_ts))  # 종료 시각에 맞춰 마지막 180초만 본다
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
    kq = k6_queries(name, win, off)
    pj = {k: by(prom(PROM_PORT, e, at), "horizontalpodautoscaler" if k == "hpa_desired_max" else ("stage" if k == "stage_mean_s" else ("instance" if k.startswith("node_") else "pod"))) for k, e in q.items()}
    k6m = {k: scalar(prom(PROM_PORT, e, at)) for k, e in kq.items()}
    rr = prom(PROM_PORT, q["requests_by_pod_outcome"], at)
    pj["requests_by_pod_outcome"] = {f"{x['metric'].get('pod','?')}|{x['metric'].get('outcome','?')}": round(float(x["value"][1]), 1) for x in rr} if not isinstance(rr, str) else rr
    pf.terminate()
    pods_after = kube("-n", APP_NS, "get", "pods", "-o", "wide", "--no-headers")
    (folder / "prometheus.json").write_text(json.dumps({"window_s": win, "results": pj, "k6": k6m}, ensure_ascii=False, indent=1), encoding="utf-8")

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
    done, rate = num(lines["inference_done"]); busy, _ = num(lines["inference_busy"])
    failed, _ = num(lines["inference_failed"]); unav, _ = num(lines["inference_unavailable"])

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
    cpu = pj["cpu_avg_cores_by_pod"] if isinstance(pj["cpu_avg_cores_by_pod"], dict) else {}
    both_saturated = len(cpu) == 2 and all(v >= SATURATED_CORES for v in cpu.values())
    balanced = len(share) == 2 and all(0.40 <= s <= 0.60 for s in share.values())
    node_busy = pj["node_busy_fraction"] if isinstance(pj["node_busy_fraction"], dict) else {}
    k6_node_busy = next((v for inst, v in node_busy.items() if ctx.get("node_ip") and inst.startswith(ctx["node_ip"] + ":")), None)
    k6a = assess_k6(k6m["k6_cpu_max_cores"], k6m["k6_cpu_avg_cores"], k6m["k6_throttle_ratio"], k6_node_busy, cpu)
    g_end = read_guard(base_restarts)
    max_hpa = {h: {"current": max((x["obs"]["hpa"].get(h, {}).get("current") or 0) for x in guard_log + [{"obs": g_end["obs"]}]),
                   "desired": max((x["obs"]["hpa"].get(h, {}).get("desired") or 0) for x in guard_log + [{"obs": g_end["obs"]}])} for h in HPA_FIXED}
    restarts = [l for l in pods_after.splitlines() if l.split()[3] != "0"]
    invalid = []
    if aborted: invalid.append(aborted)
    if failed: invalid.append(f"inference_failed={failed}")
    if unav: invalid.append(f"inference_unavailable={unav}")
    if restarts: invalid.append("Pod 재시작/이상: " + "; ".join(restarts))
    if not g_end["ok"]: invalid.append("종료 시점 고정 상태 위반: " + "; ".join(g_end["fails"]))
    if k6_oom: invalid.append("k6 Pod OOMKilled: " + ",".join(k6_oom))
    if k6a["level"] == "invalid": invalid.append("부하 생성 병목 확인: " + "; ".join(k6a["evidence"]))
    if done is None: invalid.append("k6 요약을 읽지 못함")
    warnings = list(k6a["warnings"])
    result = {"run": name, "start_utc": start.isoformat(timespec="seconds"), "end_utc": end.isoformat(timespec="seconds"),
              "valid": not invalid, "invalid_reasons": invalid, "warnings": warnings,
              "throughput_ok_per_s": rate, "ok_count": done, "busy_503": busy, "failed": failed, "unavailable": unav,
              "failure_rate": ((busy or 0) + (failed or 0) + (unav or 0)) / (((done or 0) + (busy or 0) + (failed or 0) + (unav or 0)) or 1),
              "client_ms_measure": client, "server_elapsed_ms": server_ms,
              "share_by_pod": share, "balanced_40_60": balanced, "cpu_avg_cores_by_pod": cpu, "both_saturated_ge_0_8": both_saturated,
              "k6": {"node": ctx["node"], "cpu_avg_cores": k6m["k6_cpu_avg_cores"], "cpu_max_cores": k6m["k6_cpu_max_cores"], "throttle_ratio": k6m["k6_throttle_ratio"],
                     "memory_max_mib": k6m["k6_memory_max_mib"], "node_busy": k6_node_busy, "oom": k6_oom, "assessment": k6a["level"], "evidence": k6a["evidence"],
                     "note": "k6 Pod 에는 CPU limit 이 없어 throttling 지표가 비어 있을 수 있다(None = 시리즈 없음)"},
              "inference_throttle_ratio_by_pod": pj["inference_throttle_ratio_by_pod"],
              "guard": {"hpa_max_seen": max_hpa, "samples": len(guard_log), "worker_pods_seen": max([x["obs"]["worker_pods"] for x in guard_log] + [g_end["obs"]["worker_pods"]]),
                        "oom": g_end["obs"]["oom"]},
              "image_tags": images, "snapshot": str(pathlib.Path(snap).name),
              "prometheus": pj}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    conditions = f"""# {name} 조건과 결과 — {dt.date.today().isoformat()}
- 로컬 전용. B0(E3 N=2): 클러스터 안 k6 Pod(노드 {ctx['node']}, `grafana/k6:2.3.0`, 100m/128Mi) → `http://inference.parking-guardian.svc.cluster.local:8000`(Service), Pod 2개(HPA 고정 2).
- 조건: closed-loop VUS={VUS}, `--no-connection-reuse`, {DURATION}, 워밍업 5장(setup, 지표 제외). 스크립트 `loadtest/k6/inference.js`(변경 없음). 이미지 {images.get('inference')}.
- 시간: {result['start_utc']} ~ {result['end_utc']} UTC. P3 고정 상태(스냅샷 `{result['snapshot']}`).
- 판정: **{'유효' if result['valid'] else '무효'}** {'; '.join(invalid)}
- 경고: {'; '.join(warnings) if warnings else '없음'}
- 처리량(성공) {rate} 장/초 (성공 {done}건), busy 503 {busy}, 실패 {failed}, 모델 불가 {unav}, 실패율 {result['failure_rate']:.4f}
- k6 클라이언트(ms, measure): {json.dumps(client)}
- 서버 추론 시간(ms): {json.dumps(server_ms)}
- Pod 별 성공 처리 비율: {share} (균형 40~60%: {balanced}) / Pod 별 평균 CPU(코어): {cpu} (둘 다 ≥{SATURATED_CORES}: {both_saturated})
- k6 Pod: CPU 평균 {k6m['k6_cpu_avg_cores']} / 최대 {k6m['k6_cpu_max_cores']} 코어, throttling {k6m['k6_throttle_ratio']}, 노드 사용률 {k6_node_busy}, OOM {k6_oom or '없음'} → {k6a['level']}
- 고정 상태: HPA 최대 관측 {json.dumps(max_hpa)}, Worker Pod 관측 최대 {result['guard']['worker_pods_seen']}, 검증 표본 {len(guard_log)}회
- 관찰자 DB 수집 등 외부 조건은 실행자가 별도 기록한다(수치에서 임의로 빼지 않는다).
"""
    (folder / "conditions.md").write_text(conditions, encoding="utf-8")
    (folder / "Pods-after.txt").write_text(pods_after, encoding="utf-8")
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()} *{p.name}" for p in sorted(folder.iterdir()) if p.name != "SHA256SUMS"]
    (folder / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode("utf-8"))
    for p in folder.iterdir():
        os.chmod(p, 0o444)
    log(f"{name}: {'유효' if result['valid'] else '무효'} 처리량={rate}/s 성공={done} busy={busy} 실패={failed} 점유={share} CPU={cpu} k6={k6a['level']} p95={client.get('p(95)')}ms")
    return result, result["valid"]


# ---------------------------------------------------------------------------------------------- dry-run / main
def dry_run(ctx):
    """읽기 전용 점검: 스냅샷, 노드 선택, 고정 상태 검증(결과를 보여 줄 뿐 실패해도 계속), k6 Pod 정의. 아무것도 만들지 않는다."""
    ok, why = verify_snapshot(ctx["snap"])
    print(f"스냅샷 {ctx['snap']}: {'OK' if ok else 'FAIL - ' + '; '.join(why)}")
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
    ap.add_argument("first", type=int, help="시작 회차")
    ap.add_argument("last", type=int, help="끝 회차")
    ap.add_argument("--snap", required=True, help="고정 직전에 만든 p3.py 스냅샷 폴더 (p3.py pause 가 출력하는 경로)")
    ap.add_argument("--node", default=None, help="k6 를 둘 노드를 직접 지정(같은 조건을 통과해야 한다)")
    ap.add_argument("--prefix", default="b0", help="결과 폴더 이름 접두어(기본 b0)")
    ap.add_argument("--dry-run", action="store_true", help="읽기 전용 점검만 하고 아무것도 만들지 않는다")
    return ap.parse_args(argv)


def main(argv=None):
    global DRY_RUN
    a = parse_args(argv)
    DRY_RUN = a.dry_run
    ctx = {"snap": pathlib.Path(a.snap), "prefix": a.prefix, "node_override": a.node}
    if a.dry_run:
        return dry_run(ctx)
    ok, why = verify_snapshot(ctx["snap"])
    if not ok:
        print("스냅샷 검증 실패: " + "; ".join(why))
        return 2
    summary_path = unique_summary_path(a.prefix)
    # ConfigMap: 현재 스크립트와 샘플 사진으로 다시 만든다 (이 스크립트가 만든 loadtest 네임스페이스 안에서만)
    kube("create", "namespace", LOAD_NS, check=False)
    kube("-n", LOAD_NS, "delete", "configmap", "k6-scripts", check=False)
    kube("-n", LOAD_NS, "create", "configmap", "k6-scripts",
         f"--from-file=lib.js={REPO/'loadtest/k6/lib.js'}", f"--from-file=inference.js={REPO/'loadtest/k6/inference.js'}",
         f"--from-file=sample_a.jpg={REPO/'backend/uploads/sample_a.jpg'}", f"--from-file=sample_b.jpg={REPO/'backend/uploads/sample_b.jpg'}")
    results = []
    try:
        for n in range(a.first, a.last + 1):
            try:
                res, good = run_one(ctx, n)
            except FileExistsError as exc:
                log(str(exc)); results.append({"run": f"{a.prefix}-{n}", "valid": False, "aborted": str(exc)}); break
            results.append(res)
            if not good:
                log("무효/중단: 다음 회차를 진행하지 않는다")
                break
            if n < a.last:
                log("60초 휴식"); time.sleep(60)
    finally:
        summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        # 정리: 임시 리소스 (이 스크립트가 만든 것만)
        kube("delete", "namespace", LOAD_NS, "--wait=false", check=False)
        log("loadtest 네임스페이스 삭제 요청")
    return 0


if __name__ == "__main__":
    sys.exit(main())
