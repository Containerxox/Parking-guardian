"""E3 N=2: 클러스터 안 k6 임시 Pod 가 inference Service(Pod 2개)를 closed-loop 로 호출해 서비스 처리량 C 를 잰다.

    python loadtest/tools/e3_run.py <시작 회차> <끝 회차>      예: python loadtest/tools/e3_run.py 1 3

조건: VUS=4, k6 --no-connection-reuse(요청마다 새 연결 = kube-proxy 가 Pod 를 확률로 선택), 3분, 워밍업 5장은 setup.
읽기 전용: inference 만 호출한다. DB, 기기, 위반, Deployment, HPA, Argo 는 건드리지 않는다.
회차 사이에 P3 고정 상태 검사를 하고, 이상이 있으면 다음 회차로 가지 않는다. 끝나면 loadtest 네임스페이스를 지운다.
"""
import csv
import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
RESULTS = REPO / "docs" / "measurements" / "phase1" / "results"
SNAP = REPO / "docs" / "measurements" / "phase1" / "eks" / "s1-p3-snapshot-20261031-222457"
NODE_A = "ip-10-0-20-149.ap-northeast-2.compute.internal"
VUS, DURATION = 4, "3m"
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


def kube(*a, data=None, check=True):
    r = subprocess.run(["kubectl", *a], capture_output=True, text=True, encoding="utf-8", input=data)
    if check and r.returncode:
        raise RuntimeError(f"kubectl {' '.join(a)}: {r.stderr.strip()[:300]}")
    return r.stdout


def log(m):
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}Z] {m}", flush=True)


def p3_fixed():
    r = subprocess.run([sys.executable, str(REPO / "loadtest/tools/p3.py"), "status", "--expect", "fixed", "--snap", str(SNAP)],
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


def mv(summary, name):
    m = summary.get("metrics", {}).get(name, {})
    return m.get("values", m)


def run_one(n):
    name = f"e3n2-{n}"
    folder = RESULTS / f"{dt.date.today().isoformat()}-{name}"
    if folder.exists():
        sys.exit(f"{folder} 가 이미 있다")
    folder.mkdir(parents=True)
    # ---- 실행 직전 간단 확인
    ok, bad = p3_fixed()
    eps = json.loads(kube("-n", "parking-guardian", "get", "endpointslices", "-l", "kubernetes.io/service-name=inference", "-o", "json"))
    ready = [e["addresses"][0] for s in eps["items"] for e in s.get("endpoints", []) if e["conditions"].get("ready")]
    k6pods = kube("-n", "loadtest", "get", "pods", "--no-headers", check=False).strip()
    log(f"{name}: P3 fixed={'OK' if ok else 'FAIL'} 엔드포인트 ready={len(ready)} 남은 k6 Pod={'있음' if k6pods else '없음'}")
    if not ok or len(ready) != 2 or k6pods:
        (folder / "ABORTED.txt").write_text("사전 점검 실패: " + "; ".join(bad) + f" ready={ready} k6pods={k6pods}\n", encoding="utf-8")
        return {"run": name, "valid": False, "aborted": "사전 점검 실패 " + "; ".join(bad)}, False

    start = dt.datetime.now(dt.timezone.utc)
    kube("create", "-f", "-", data=POD_YAML.format(name=name, node=NODE_A, vus=VUS, duration=DURATION))
    aborted = None
    t_end = time.time() + 600
    while time.time() < t_end:
        time.sleep(10)
        phase = kube("-n", "loadtest", "get", "pod", name, "-o", "jsonpath={.status.phase}", check=False).strip()
        if phase in ("Succeeded", "Failed"):
            break
        okp, badp = p3_fixed()  # 안전 중단: Pod 재시작/Pending/HPA 변화
        if not okp:
            aborted = "안전 중단: " + "; ".join(badp)
            kube("-n", "loadtest", "delete", "pod", name, "--wait=false", check=False)
            break
    end = dt.datetime.now(dt.timezone.utc)
    pod_json = json.loads(kube("-n", "loadtest", "get", "pod", name, "-o", "json", check=False) or "{}")
    try:
        fin = pod_json["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"]
        finished_ts = dt.datetime.strptime(fin, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except Exception:  # noqa: BLE001
        finished_ts = time.time() - 30
    klog = kube("-n", "loadtest", "logs", name, check=False)
    (folder / "k6.log").write_text(klog, encoding="utf-8")
    kube("-n", "loadtest", "delete", "pod", name, "--wait=false", check=False)
    log(f"{name}: k6 종료 phase={phase if not aborted else 'aborted'} {end:%H:%M:%S}Z")

    # ---- summary 는 Pod 안 파일이라 로그에서 읽는다: summary JSON 대신 k6 가 출력하는 요약을 파싱하지 않고 아래 지표를 Pod 로그에서 추출
    time.sleep(25)
    pf = subprocess.Popen(["kubectl", "-n", "monitoring", "port-forward", "svc/monitoring-prometheus", "19093:9090"],
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
    }
    pj = {k: by(prom(19093, e, at), "horizontalpodautoscaler" if k == "hpa_desired_max" else ("stage" if k == "stage_mean_s" else ("instance" if k.startswith("node_") else "pod"))) for k, e in q.items()}
    # requests_by_pod_outcome 는 (pod,outcome) 두 라벨
    rr = prom(19093, q["requests_by_pod_outcome"], at)
    pj["requests_by_pod_outcome"] = {f"{x['metric'].get('pod','?')}|{x['metric'].get('outcome','?')}": round(float(x["value"][1]), 1) for x in rr} if not isinstance(rr, str) else rr
    pf.terminate()
    pods_after = kube("-n", "parking-guardian", "get", "pods", "-o", "wide", "--no-headers")
    (folder / "prometheus.json").write_text(json.dumps({"window_s": win, "results": pj}, ensure_ascii=False, indent=1), encoding="utf-8")

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
    done = busy = failed = unav = None
    import re
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
    both_saturated = len(cpu) == 2 and all(v >= 0.8 for v in cpu.values())
    balanced = len(share) == 2 and all(0.40 <= s <= 0.60 for s in share.values())
    restarts = [l for l in pods_after.splitlines() if l.split()[3] != "0"]
    invalid = []
    if aborted: invalid.append(aborted)
    if failed: invalid.append(f"inference_failed={failed}")
    if unav: invalid.append(f"inference_unavailable={unav}")
    if restarts: invalid.append("Pod 재시작/이상: " + "; ".join(restarts))
    if done is None: invalid.append("k6 요약을 읽지 못함")
    result = {"run": name, "start_utc": start.isoformat(timespec="seconds"), "end_utc": end.isoformat(timespec="seconds"),
              "valid": not invalid, "invalid_reasons": invalid,
              "throughput_ok_per_s": rate, "ok_count": done, "busy_503": busy, "failed": failed, "unavailable": unav,
              "failure_rate": ((busy or 0) + (failed or 0) + (unav or 0)) / (((done or 0) + (busy or 0) + (failed or 0) + (unav or 0)) or 1),
              "client_ms_measure": client, "server_elapsed_ms": server_ms,
              "share_by_pod": share, "balanced_40_60": balanced, "cpu_avg_cores_by_pod": cpu, "both_saturated_ge_0_8": both_saturated,
              "prometheus": pj}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    conditions = f"""# {name} 조건과 결과 — {dt.date.today().isoformat()}
- 로컬 전용. E3 N=2: 클러스터 안 k6 Pod(노드 A, `grafana/k6:2.3.0`, 100m/128Mi) → `http://inference.parking-guardian.svc.cluster.local:8000`(Service), Pod 2개(HPA 고정 2).
- 조건: closed-loop VUS={VUS}, `--no-connection-reuse`, {DURATION}, 워밍업 5장(setup, 지표 제외). 스크립트 `loadtest/k6/inference.js`(변경 없음).
- 시간: {result['start_utc']} ~ {result['end_utc']} UTC. P3 고정 상태(스냅샷 `{SNAP.name}`).
- 판정: **{'유효' if result['valid'] else '무효'}** {'; '.join(invalid)}
- 처리량(성공) {rate} 장/초 (성공 {done}건), busy 503 {busy}, 실패 {failed}, 모델 불가 {unav}, 실패율 {result['failure_rate']:.4f}
- k6 클라이언트(ms, measure): {json.dumps(client)}
- 서버 추론 시간(ms): {json.dumps(server_ms)}
- Pod 별 성공 처리 비율: {share} (균형 40~60%: {balanced}) / Pod 별 평균 CPU(코어): {cpu} (둘 다 ≥0.8: {both_saturated})
"""
    (folder / "conditions.md").write_text(conditions, encoding="utf-8")
    (folder / "Pods-after.txt").write_text(pods_after, encoding="utf-8")
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()} *{p.name}" for p in sorted(folder.iterdir()) if p.name != "SHA256SUMS"]
    (folder / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode("utf-8"))
    for p in folder.iterdir():
        os.chmod(p, 0o444)
    log(f"{name}: {'유효' if result['valid'] else '무효'} 처리량={rate}/s 성공={done} busy={busy} 실패={failed} 점유={share} CPU={cpu} p95={client.get('p(95)')}ms")
    return result, result["valid"]


def main():
    a, b = int(sys.argv[1]), int(sys.argv[2])
    # ConfigMap: 현재 스크립트와 샘플 사진으로 다시 만든다 (기존 것은 지운다)
    kube("create", "namespace", "loadtest", check=False)
    kube("-n", "loadtest", "delete", "configmap", "k6-scripts", check=False)
    kube("-n", "loadtest", "create", "configmap", "k6-scripts",
         f"--from-file=lib.js={REPO/'loadtest/k6/lib.js'}", f"--from-file=inference.js={REPO/'loadtest/k6/inference.js'}",
         f"--from-file=sample_a.jpg={REPO/'backend/uploads/sample_a.jpg'}", f"--from-file=sample_b.jpg={REPO/'backend/uploads/sample_b.jpg'}")
    results = []
    for n in range(a, b + 1):
        res, ok = run_one(n)
        results.append(res)
        if not ok:
            log("무효/중단: 다음 회차를 진행하지 않는다")
            break
        if n < b:
            log("60초 휴식"); time.sleep(60)
    (RESULTS / f"{dt.date.today().isoformat()}-e3n2-summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    # 정리: 임시 리소스 (이 스크립트가 만든 것만)
    kube("delete", "namespace", "loadtest", "--wait=false", check=False)
    log("loadtest 네임스페이스 삭제 요청")


if __name__ == "__main__":
    main()
