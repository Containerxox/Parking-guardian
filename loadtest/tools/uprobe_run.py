"""U-probe (P0-c) 실행기: smoke1 -> smoke20 -> U1 -> U2 순서로, 각 단계가 통과해야 다음으로 간다.

    python loadtest/tools/uprobe_run.py [시작 단계]       기본 smoke1. 예: python loadtest/tools/uprobe_run.py U1

기준 (wifi-network-plan.md 5절, uprobe-safety-review.md):
  모든 단계: 응답 전부 400, DB(위반 1~4, 기기, 행 수)·S3 객체 수 불변, Pod 재시작/UID 변화 0, HPA 2/2 유지, 서버 지표로 본문 전량 수신 확인
  U1(필수): 달성 업로드율 >= 목표 95%, dropped_iterations = 0, http_req_sending p95 <= 1.0 s 이고 max <= 3.0 s, 목표 95% 미만이 30초 이상 지속되지 않음
  U2(권장): 달성율 >= 90% (미달이면 조건부 통과)
  PC: k6 CPU < 70%, 전체 CPU < 80%
장비 키는 환경변수로만 k6 에 넘기고 어디에도 기록하지 않는다(마지막에 결과 파일 검사). 이상이면 즉시 멈추고 재시도하지 않는다.
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

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from s0_run import SNAPSHOT, prom, vals  # noqa: E402
from p0b_run import wifi_state  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
RESULTS = REPO / "docs" / "measurements" / "phase1" / "results"
K6_PATH = r"C:\Program Files\k6"
K6_EXE = K6_PATH + r"\k6.exe"
SERIAL = "RPI-A-PANGYO-01"  # 기기 2
SIZE = 369857
WIRE = 370400  # multipart 포함 추정(안전성 검토 4절)
BITS = WIRE * 8
R_MBPS = 13.8
PHASES = {  # 이름: (초당 요청 x100, 초, 목표 Mbps)
    "smoke1": (100, 0, None),
    "smoke20": (100, 20, 100 * BITS / 100 / 1e6),
    "U1": (None, 120, 16.6),
    "U2": (None, 30, 27.6),
}
for name, mbps in (("U1", 16.6), ("U2", 27.6)):
    PHASES[name] = (round(mbps * 1e6 / BITS * 100), PHASES[name][1], mbps)
ORDER = ["smoke1", "smoke20", "U1", "U2"]


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def log(msg):
    print(f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


def kj(*args):
    r = sh(["kubectl", *args, "-o", "json"])
    if r.returncode:
        sys.exit("kubectl 실패: " + r.stderr[:200])
    return json.loads(r.stdout)


def snapshot():
    r = sh(["kubectl", "-n", "parking-guardian", "exec", "-i", "deploy/backend", "-c", "backend", "--", "python", "-"], input=SNAPSHOT)
    for line in r.stdout.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    sys.exit("DB 스냅샷 실패: " + (r.stderr or r.stdout)[-300:])


def photos_bucket():
    """사진 버킷 이름. 계정 ID 는 코드에 두지 않고 실행 신원(STS)에서 읽는다(버킷 이름 규칙: parking-guardian-photos-<계정ID>)."""
    r = sh(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"])
    if r.returncode or not r.stdout.strip():
        sys.exit("AWS 계정 조회 실패(AWS_PROFILE 확인): " + r.stderr[:200])
    return "parking-guardian-photos-" + r.stdout.strip()


def s3_count():
    r = sh(["aws", "s3api", "list-objects-v2", "--bucket", photos_bucket(), "--query", "length(Contents||`[]`)", "--output", "text"])
    if r.returncode:
        sys.exit("S3 목록 실패: " + r.stderr[:200])
    return int(r.stdout.strip())


def cluster_state():
    pods = kj("-n", "parking-guardian", "get", "pods")["items"]
    rows = {}
    for p in pods:
        app = p["metadata"].get("labels", {}).get("app.kubernetes.io/name", "")
        if app in ("backend", "inference", "frontend"):
            cs = p["status"].get("containerStatuses", [])
            rows[p["metadata"]["name"]] = {"uid": p["metadata"]["uid"], "phase": p["status"].get("phase"),
                                           "ready": bool(cs) and all(c.get("ready") for c in cs),
                                           "restarts": sum(c.get("restartCount", 0) for c in cs)}
    hpa = {h["metadata"]["name"]: (h["spec"]["minReplicas"], h["spec"]["maxReplicas"], h["status"].get("currentReplicas"))
           for h in kj("-n", "parking-guardian", "get", "hpa")["items"]}
    nodes = [n["metadata"]["name"] for n in kj("get", "nodes")["items"]
             if any(c["type"] == "Ready" and c["status"] != "True" for c in n["status"]["conditions"])]
    return {"pods": rows, "hpa": hpa, "not_ready_nodes": nodes}


def srv_counts():
    """누적 카운터 절대값(없는 시리즈는 0): (API detections status=400 합, inference decode 단계 합)."""
    now = time.time()
    a = prom(19090, 'sum(http_request_duration_seconds_count{namespace="parking-guardian",route="/api/v1/device/detections",status="400"})', now)
    d = prom(19090, 'sum(pg_inference_stage_seconds_count{namespace="parking-guardian",stage="decode"})', now)
    f = lambda r: float(r[0]["value"][1]) if isinstance(r, list) and r else (0.0 if isinstance(r, list) else None)
    return f(a), f(d)


def n_req_expected(folder):
    n = 0
    try:
        with open(folder / "points.json", encoding="utf-8") as fh:
            for line in fh:
                if '"metric":"http_reqs"' in line and '"Point"' in line:
                    n += 1
    except OSError:
        pass
    return n


def pct(a, q):
    a = sorted(a)
    return a[min(len(a) - 1, int(len(a) * q))] if a else None


def run_phase(phase, run_dir, base_url, key, run_id):
    rate_x100, dur, target = PHASES[phase]
    folder = run_dir / phase
    folder.mkdir(parents=True)
    problems, notes = [], []

    pre_c = cluster_state()
    if any(v != (2, 2, 2) for v in pre_c["hpa"].values()) or pre_c["not_ready_nodes"] or \
            any(not r["ready"] or r["phase"] != "Running" for r in pre_c["pods"].values()):
        sys.exit(f"{phase}: 사전 상태 이상(HPA 2/2·Pod Ready 아님). 시작하지 않습니다: {pre_c['hpa']}")
    ready = sh(["curl", "-s", "-m", "10", "-o", os.devnull, "-w", "%{http_code}", f"{base_url}/api/v1/readyz"]).stdout
    if ready != "200":
        sys.exit(f"{phase}: readyz={ready}")
    pre_db, pre_s3, pre_wifi = snapshot(), s3_count(), wifi_state()

    # 서버 지표 기준점(누적 카운터) — port-forward
    pf = subprocess.Popen(["kubectl", "-n", "monitoring", "port-forward", "svc/monitoring-prometheus", "19090:9090"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    base_api, base_dec = srv_counts()

    monitor_csv = folder / "pc-monitor.csv"
    monitor = subprocess.Popen(["powershell", "-NoProfile", "-File", str(REPO / "loadtest/tools/monitor-pc.ps1"), "-Out", str(monitor_csv),
                                "-IntervalSec", "5"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(6)
    start = dt.datetime.now(dt.timezone.utc)
    env = dict(os.environ, DEVICE_KEY=key, PATH=os.environ["PATH"] + os.pathsep + K6_PATH)
    cmd = [K6_EXE, "run", "-e", f"BASE_URL={base_url}", "-e", f"PHASE={phase}", "-e", f"RATE_X100={rate_x100}", "-e", f"DUR={dur}",
           "-e", f"SIZE={SIZE}", "-e", f"RUN_ID={run_id}-{phase}", f"--summary-export={folder / 'summary.json'}",
           "--summary-trend-stats=avg,min,med,max,p(90),p(95),p(99)", f"--out=json={folder / 'points.json'}",
           str(REPO / "loadtest/k6/uprobe.js")]
    log(f"{phase} 시작: 목표 {target and round(target, 1)} Mbps, {rate_x100 / 100:.2f} req/s, {dur}초")
    aborted, hot = None, 0
    with open(folder / "k6.log", "w", encoding="utf-8") as klog:
        k6 = subprocess.Popen(cmd, env=env, stdout=klog, stderr=subprocess.STDOUT)
        while k6.poll() is None:
            time.sleep(5)
            try:
                last = next(csv.reader(monitor_csv.read_text(encoding="utf-8-sig").splitlines()[-1:]))
                k6cpu = float(last[1]) if last[1] not in ("", "k6_cpu_pct_of_all_cores") else 0.0
                total = float(last[2]) if last[2] not in ("", "total_cpu_pct") else 0.0
            except Exception:  # noqa: BLE001
                continue
            hot = hot + 1 if (k6cpu >= 70 or total >= 95) else 0
            if hot >= 2:
                aborted = f"PC CPU 포화(k6 {k6cpu}%, 전체 {total}%) 2회 연속"
                k6.terminate()
                break
            c = cluster_state() if int(time.time()) % 20 < 5 else None
            if c and (any(r["restarts"] for r in c["pods"].values()) or c["not_ready_nodes"] or
                      any(v != (2, 2, 2) for v in c["hpa"].values())):
                aborted = f"클러스터 이상 감지: {c['hpa']} 재시작/노드 확인"
                k6.terminate()
                break
        k6.wait()
    end = dt.datetime.now(dt.timezone.utc)
    time.sleep(2)
    monitor.terminate()
    post_wifi = wifi_state()
    log(f"{phase} k6 종료 rc={k6.returncode}")

    time.sleep(25)  # 스크레이프 지연
    post_db, post_s3, post_c = snapshot(), s3_count(), cluster_state()
    win = int((end - start).total_seconds()) + 30
    ns = 'namespace="parking-guardian"'
    qs = {
        "ai_client_by_outcome": f'sum by (outcome)(increase(pg_ai_client_requests_total{{{ns}}}[{win}s]))',
        "inference_by_outcome": f'sum by (outcome)(increase(pg_inference_requests_total{{{ns}}}[{win}s]))',
        "backend_cpu_max_cores_by_pod": f'max_over_time(sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},pod=~"backend-.*",container="backend"}}[30s]))[{win}s:15s])',
        "node_steal_fraction": f'avg by (instance)(rate(node_cpu_seconds_total{{mode="steal"}}[{win}s]))',
    }
    now = time.time()
    prom_res = {k: vals(prom(19090, q, now)) for k, q in qs.items()}
    api_d = dec_d = None
    for _ in range(18):  # 스크레이프 주기 때문에 최대 90초까지 기다린다
        a, d = srv_counts()
        if a is not None and d is not None and base_api is not None and base_dec is not None:
            api_d, dec_d = a - base_api, d - base_dec
        if api_d is not None and api_d >= n_req_expected(folder) and dec_d >= n_req_expected(folder):
            break
        time.sleep(5)
    prom_res["server_api_400_delta"], prom_res["server_decode_delta"] = api_d, dec_d
    pf.terminate()

    # ---- 분석 -----------------------------------------------------------------------------
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8")) if (folder / "summary.json").exists() else {}

    def mv(name):
        m = summary.get("metrics", {}).get(name, {})
        return m.get("values", m)

    sent_points, n_req, bad_status = [], 0, 0
    first_ts = None
    with open(folder / "points.json", encoding="utf-8") as fh:
        for line in fh:
            if '"Point"' not in line:
                continue
            o = json.loads(line)
            ts = dt.datetime.fromisoformat(o["data"]["time"].replace("Z", "+00:00")).timestamp()
            if o["metric"] == "data_sent":
                sent_points.append((ts, o["data"]["value"]))
            elif o["metric"] == "http_reqs":
                n_req += 1
                if o["data"]["tags"].get("status") != "400":
                    bad_status += 1
            first_ts = ts if first_ts is None else first_ts
    sent_total = sum(v for _, v in sent_points)
    sending = {k: mv("http_req_sending").get(k) for k in ("avg", "med", "p(95)", "p(99)", "max")}
    dropped = mv("dropped_iterations").get("count", 0)
    non400 = mv("probe_non_400").get("count", 0)
    got400 = mv("probe_400").get("count", 0)
    failed = mv("http_req_failed").get("passes", None)

    # 업로드율: 데이터 전송 점 시각 기준 10초 구간별 Mbps
    achieved_overall, low_streak, max_streak, buckets = None, 0, 0, []
    if sent_points and target:
        t0 = min(t for t, _ in sent_points)
        t1 = max(t for t, _ in sent_points)
        span = max(t1 - t0, 1.0)
        achieved_overall = sent_total * 8 / span / 1e6
        nb = int(span // 10)
        for i in range(nb):
            b = sum(v for t, v in sent_points if t0 + i * 10 <= t < t0 + (i + 1) * 10) * 8 / 10 / 1e6
            buckets.append(round(b, 2))
            low_streak = low_streak + 1 if b < target * 0.95 else 0
            max_streak = max(max_streak, low_streak)

    # PC
    rows = [r for r in csv.reader(monitor_csv.read_text(encoding="utf-8-sig").splitlines()) if r and not r[0].startswith("#")][1:]
    k6cpu = [float(r[1]) for r in rows if r[1]]
    tot = [float(r[2]) for r in rows]
    tx = [float(r[5]) for r in rows]
    pc = {"k6_cpu_pct_max": max(k6cpu, default=None), "total_cpu_pct_max": max(tot, default=None),
          "total_cpu_pct_avg": round(sum(tot) / len(tot), 1) if tot else None, "net_tx_kbps_avg": round(sum(tx) / len(tx)) if tx else None}

    # 판정
    if aborted:
        problems.append("중단: " + aborted)
    if k6.returncode not in (0, 99):
        problems.append(f"k6 비정상 종료 rc={k6.returncode}")
    if bad_status or non400:
        problems.append(f"400 이외 응답 {max(bad_status, non400)}건")
    if failed:
        notes.append(f"http_req_failed(4xx 포함) passes={failed} — 400 은 k6 기본으로 실패 표시되므로 참고만")
    if pre_db["violations"] != post_db["violations"] or pre_db["machines"] != post_db["machines"]:
        problems.append("안전 위반: 위반/기기 행 변화")
    for t in pre_db["counts"]:
        if pre_db["counts"][t] != post_db["counts"][t]:
            problems.append(f"안전 위반: {t} 행 수 {pre_db['counts'][t]} -> {post_db['counts'][t]}")
    if pre_s3 != post_s3:
        problems.append(f"안전 위반: S3 객체 수 {pre_s3} -> {post_s3}")
    for name, r in post_c["pods"].items():
        p0 = pre_c["pods"].get(name)
        if p0 is None or p0["uid"] != r["uid"] or r["restarts"] != p0["restarts"] or not r["ready"]:
            problems.append(f"Pod 변화/재시작/NotReady: {name}")
    if set(pre_c["pods"]) != set(post_c["pods"]):
        problems.append("Pod 집합 변화")
    if any(v != (2, 2, 2) for v in post_c["hpa"].values()) or post_c["not_ready_nodes"]:
        problems.append(f"HPA/노드 이상: {post_c['hpa']} {post_c['not_ready_nodes']}")
    if pc["k6_cpu_pct_max"] is not None and pc["k6_cpu_pct_max"] >= 70:
        problems.append(f"k6 CPU {pc['k6_cpu_pct_max']}% >= 70%")
    if pc["total_cpu_pct_max"] is not None and pc["total_cpu_pct_max"] >= 80:
        notes.append(f"PC 전체 CPU 최대 {pc['total_cpu_pct_max']}% (기준 80%; 평균 {pc['total_cpu_pct_avg']}%)")

    # 서버 수신 확인: 요청 수 == API 400 == decode
    api400_n, decode_n = api_d, dec_d
    if api400_n is None or abs(api400_n - n_req) > 0.5:
        problems.append(f"서버 API 400 증가({api400_n}) != k6 요청 수({n_req})")
    if decode_n is None or abs(decode_n - n_req) > 0.5:
        problems.append(f"inference decode 증가({decode_n}) != k6 요청 수({n_req}) — 본문 전량 수신 미확인")
    if dropped:
        problems.append(f"dropped_iterations={dropped}")
    conditional = False
    if phase in ("smoke20", "U1", "U2") and target and achieved_overall is not None:
        ratio = achieved_overall / target
        if phase == "U2":
            if ratio < 0.90:
                conditional = True
                notes.append(f"U2 달성율 {ratio:.1%} < 90% (조건부 통과)")
        else:
            if ratio < 0.95:
                problems.append(f"달성율 {ratio:.1%} < 95%")
            if max_streak >= 3 and phase == "U1":
                problems.append(f"목표 95% 미만이 30초 이상 지속(연속 {max_streak * 10}초)")
    if phase == "U1":
        if sending["p(95)"] is not None and sending["p(95)"] > 1000:
            problems.append(f"http_req_sending p95 {sending['p(95)']:.0f} ms > 1000")
        if sending["max"] is not None and sending["max"] > 3000:
            problems.append(f"http_req_sending max {sending['max']:.0f} ms > 3000")

    result = {"phase": phase, "start_utc": start.isoformat(timespec="seconds"), "end_utc": end.isoformat(timespec="seconds"),
              "ok": not problems, "conditional": conditional, "problems": problems, "notes": notes, "requests": n_req,
              "target_mbps": target, "achieved_mbps_overall": achieved_overall and round(achieved_overall, 2),
              "achieved_ratio": achieved_overall and target and round(achieved_overall / target, 3), "bucket10s_mbps": buckets,
              "http_req_sending_ms": {k: v and round(v, 1) for k, v in sending.items()}, "dropped_iterations": dropped,
              "probe_400": got400, "probe_non_400": non400, "data_sent_bytes": sent_total, "pc": pc,
              "wifi_before": pre_wifi, "wifi_after": post_wifi, "s3_objects": [pre_s3, post_s3],
              "db_counts": [pre_db["counts"], post_db["counts"]], "db_pg_activity": [pre_db["pg_stat_activity_by_state"], post_db["pg_stat_activity_by_state"]],
              "hpa_after": post_c["hpa"], "prometheus": prom_res}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "db-before.json").write_text(json.dumps(pre_db, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "db-after.json").write_text(json.dumps(post_db, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"{phase}: {'통과' if not problems else '실패'}{' (조건부)' if conditional else ''} 요청 {n_req} 달성 {result['achieved_mbps_overall']} Mbps"
        f" ({result['achieved_ratio']}) sending p95/max {result['http_req_sending_ms']['p(95)']}/{result['http_req_sending_ms']['max']} ms"
        f" 서버 400={api400_n} decode={decode_n} k6CPU max={pc['k6_cpu_pct_max']}%")
    for p in problems:
        log("  문제: " + p)
    for n in notes:
        log("  참고: " + n)
    return result


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = "--only" in sys.argv
    first = args[0] if args else "smoke1"
    host = sh(["kubectl", "-n", "parking-guardian", "get", "ingress", "-o", "jsonpath={.items[0].spec.rules[0].host}"]).stdout.strip()
    base_url = f"https://{host}"
    kr = sh(["kubectl", "-n", "parking-guardian", "exec", "deploy/backend", "-c", "backend", "--", "flask", "device-key", SERIAL])
    key = kr.stdout.strip().splitlines()[-1].strip() if kr.stdout.strip() else ""
    if kr.returncode or not key:
        sys.exit("장비 키 조회 실패")
    run_id = "uprobe-" + dt.datetime.now().strftime("%H%M%S")
    run_dir = RESULTS / f"{dt.date.today().isoformat()}-uprobe-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for phase in ORDER[ORDER.index(first):]:
        res = run_phase(phase, run_dir, base_url, key, run_id)
        results[phase] = res
        if not res["ok"]:
            log(f"{phase} 실패 — 중단합니다. 재시도하지 않습니다.")
            break
        if only:
            break
        if phase != ORDER[-1]:
            time.sleep(20)
    leaks = sum(1 for p in run_dir.rglob("*") if p.is_file() and key in p.read_text(encoding="utf-8", errors="ignore"))
    (run_dir / f"summary-{first}.json").write_text(json.dumps({k: {"ok": v["ok"], "conditional": v["conditional"], "problems": v["problems"]}
                                                                for k, v in results.items()} | {"device_key_leaks": leaks}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"장비 키가 든 결과 파일 수: {leaks}")
    log(f"결과 폴더: {run_dir}  (SHA256SUMS·읽기 전용은 분석 뒤 별도 처리)")
    sys.exit(0 if all(r["ok"] for r in results.values()) and leaks == 0 else 2)


if __name__ == "__main__":
    main()
