"""S0 한 회차를 실행하고 기록한다 (사전 등록 고정 v1 의 S0: 일반 API 5 req/s + readyz 1 req/s, 5분, 사진 0).

    python loadtest/tools/s0_run.py <회차 번호>          예: python loadtest/tools/s0_run.py 1

하는 일: 사전 점검 → 회차 전 스냅샷(DB) → PC 감시 시작 → k6 실행 → 회차 후 스냅샷 → Prometheus 쿼리 → 무효 판정 →
conditions.md, SHA256SUMS 작성 → 읽기 전용. **무효·중단이면 비정상 종료 코드로 끝나고 스스로 다시 실행하지 않는다.**

인증정보: 환경변수 ADMIN_PASSWORD 또는 파일 ~/.pg-s0.env 의 `ADMIN_PASSWORD=...` 한 줄. 값은 출력·기록하지 않고(명령줄에도 넣지 않는다),
결과 파일에 값이 들어갔는지 마지막에 검사한다(개수만 보고).
장비 키는 쓰지 않는다. 클러스터와 DB 는 읽기만 한다(kubectl get / 앱 컨텍스트 SELECT). 쓰기는 k6 의 로그인뿐이다.
"""
import csv
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
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
RESULTS = REPO / "docs" / "measurements" / "phase1" / "results"
ADMIN_EMAIL = "kim@a.com"
K6_PATH = r"C:\Program Files\k6"
K6_EXE = K6_PATH + r"\k6.exe"
DURATION_S = 300
# 사전 등록 3절 통과/무효 기준
P95_LIMIT_MS = 500.0
OK_RATE_MIN = 0.99
K6_CPU_LIMIT = 70.0

SNAPSHOT = r'''
import json
from app import create_app
from app.extensions import db
from sqlalchemy import text
app = create_app()
app.app_context().push()
def rows(sql):
    return [[str(c) for c in r] for r in db.session.execute(text(sql)).all()]
out = {
  "violations": rows("select id,status,coalesce(end_reason,''),detection_count,last_seen_at from violations order by id"),
  "machines": rows("select id,last_detection_at,last_heartbeat_at,pending_violation_count,clear_count from machines order by id"),
  "counts": {t: rows("select count(*) from " + t)[0][0] for t in ("violations","notifications","violation_history","refresh_tokens","machines","admins")},
  "pg_stat_activity_by_state": rows("select coalesce(state,'(none)'),count(*) from pg_stat_activity where datname=current_database() group by 1 order by 1"),
}
print("JSON:" + json.dumps(out))
'''


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", **kw)


def log(msg):
    print(f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


def load_password():
    pw = os.environ.get("ADMIN_PASSWORD")
    if pw:
        return pw
    f = pathlib.Path.home() / ".pg-s0.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("ADMIN_PASSWORD="):
                return line.split("=", 1)[1].strip().strip('"')
    sys.exit("ADMIN_PASSWORD 가 없습니다 (환경변수 또는 ~/.pg-s0.env). 값은 대화에 붙여넣지 마세요.")


def kubectl_json(*args):
    r = sh(["kubectl", *args, "-o", "json"])
    if r.returncode:
        sys.exit("kubectl 실패: " + r.stderr[:200])
    return json.loads(r.stdout)


def snapshot():
    r = sh(["kubectl", "-n", "parking-guardian", "exec", "-i", "deploy/backend", "-c", "backend", "--", "python", "-"],
           input=SNAPSHOT)
    for line in r.stdout.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    sys.exit("DB 스냅샷 실패: " + (r.stderr or r.stdout)[-300:])


def prom(port, expr, at):
    url = f"http://127.0.0.1:{port}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": at})
    try:
        return json.load(urllib.request.urlopen(url, timeout=30))["data"]["result"]
    except Exception as exc:  # noqa: BLE001
        return f"ERR {exc}"


def vals(result):
    if isinstance(result, str):
        return result
    out = {}
    for item in result:
        label = ",".join(f"{k}={v}" for k, v in item["metric"].items() if k in ("pod", "route", "instance", "horizontalpodautoscaler", "status"))
        out[label or "_"] = round(float(item["value"][1]), 4)
    return out


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def metric_values(summary, name):
    m = summary.get("metrics", {}).get(name, {})
    return m.get("values", m)


def main():
    run_n = sys.argv[1] if len(sys.argv) > 1 else "1"
    password = load_password()
    run_id = f"s0-{run_n}"
    folder = RESULTS / f"{dt.date.today().isoformat()}-{run_id}"
    if folder.exists():
        sys.exit(f"{folder} 가 이미 있습니다. 덮어쓰지 않습니다.")
    folder.mkdir(parents=True)
    env = dict(os.environ, ADMIN_PASSWORD=password, PATH=os.environ["PATH"] + os.pathsep + K6_PATH,
               PYTHONIOENCODING="utf-8")

    # ---- 0. 사전 점검 (읽기) -------------------------------------------------------------------
    host = sh(["kubectl", "-n", "parking-guardian", "get", "ingress", "-o",
               "jsonpath={.items[0].spec.rules[0].host}"]).stdout.strip()
    base_url = f"https://{host}"
    ready = sh(["curl", "-s", "-m", "10", "-o", os.devnull, "-w", "%{http_code}", f"{base_url}/api/v1/readyz"]).stdout
    pods = kubectl_json("-n", "parking-guardian", "get", "pods")["items"]
    bad = [p["metadata"]["name"] for p in pods if p["status"]["phase"] not in ("Running", "Succeeded")]
    k6_pods = sh(["kubectl", "-n", "loadtest", "get", "pods", "--no-headers"]).stdout.strip()
    hpa = kubectl_json("-n", "parking-guardian", "get", "hpa")["items"]
    hpa_state = {h["metadata"]["name"]: h["status"].get("currentReplicas") for h in hpa}
    log(f"readyz={ready} 비정상 Pod={bad} 남은 k6 Pod={'있음' if k6_pods else '없음'} HPA 현재 복제={hpa_state}")
    if ready != "200" or bad or k6_pods or any(v != 2 for v in hpa_state.values()):
        sys.exit("사전 점검 실패: 시작하지 않습니다 (원인 확인 후 다시).")

    from p0b_run import wifi_state
    wifi_before = wifi_state()
    pre = snapshot()
    (folder / "db-before.json").write_text(json.dumps(pre, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"회차 전: refresh_tokens={pre['counts']['refresh_tokens']} violations={pre['counts']['violations']}")

    # ---- 1. PC 감시 + k6 ------------------------------------------------------------------------
    monitor_csv = folder / "pc-monitor.csv"
    monitor = subprocess.Popen(["powershell", "-NoProfile", "-File", str(REPO / "loadtest/tools/monitor-pc.ps1"),
                                "-Out", str(monitor_csv), "-IntervalSec", "5"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(6)
    start = dt.datetime.now(dt.timezone.utc)
    k6_cmd = [K6_EXE, "run", "-e", "SCENARIO=S0", "-e", f"BASE_URL={base_url}", "-e", f"ADMIN_EMAIL={ADMIN_EMAIL}",
              "-e", f"RUN_ID={run_id}", f"--summary-export={folder / 'summary.json'}",
              "--summary-trend-stats=avg,min,med,max,p(90),p(95),p(99)", f"--out=json={folder / 'points.json'}",
              str(REPO / "loadtest/k6/scenarios.js")]
    log(f"k6 시작 ({run_id}, {DURATION_S}초) {start:%H:%M:%S}Z")
    with open(folder / "k6.log", "w", encoding="utf-8") as klog:
        k6 = subprocess.Popen(k6_cmd, env=env, stdout=klog, stderr=subprocess.STDOUT)
        aborted = None
        hot = 0
        while k6.poll() is None:
            time.sleep(10)
            try:
                last = list(csv.reader(monitor_csv.read_text(encoding="utf-8-sig").splitlines()[-1:]))[0]
                k6cpu = float(last[1]) if last[1] not in ("", "k6_cpu_pct_of_all_cores") else 0.0
                total = float(last[2]) if last[2] not in ("", "total_cpu_pct") else 0.0
            except Exception:  # noqa: BLE001
                continue
            hot = hot + 1 if (k6cpu >= K6_CPU_LIMIT or total >= 95) else 0
            if hot >= 2:
                aborted = f"PC CPU 포화(k6 {k6cpu}%, 전체 {total}%) 2회 연속"
                k6.terminate()
                break
        k6.wait()
    end = dt.datetime.now(dt.timezone.utc)
    time.sleep(2)
    monitor.terminate()
    log(f"k6 종료 rc={k6.returncode} {end:%H:%M:%S}Z")

    # ---- 2. 회차 후 스냅샷, Prometheus (스크레이프 지연 25초 대기) --------------------------------
    wifi_after = wifi_state()
    (folder / "wifi.json").write_text(json.dumps({"before": wifi_before, "after": wifi_after}, ensure_ascii=False, indent=1), encoding="utf-8")
    time.sleep(25)
    post = snapshot()
    (folder / "db-after.json").write_text(json.dumps(post, ensure_ascii=False, indent=1), encoding="utf-8")
    pf = subprocess.Popen(["kubectl", "-n", "monitoring", "port-forward", "svc/monitoring-prometheus", "19090:9090"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    at = time.time()
    win = int((end - start).total_seconds()) + 40
    ns = 'namespace="parking-guardian"'
    api_routes = 'route=~"/api/v1/(violations|dashboard/summary|notifications)|/readyz"'
    queries = {
        "server_p50_by_route_s": f'histogram_quantile(0.5, sum by (le,route)(increase(http_request_duration_seconds_bucket{{{ns},{api_routes}}}[{win}s])))',
        "server_p95_by_route_s": f'histogram_quantile(0.95, sum by (le,route)(increase(http_request_duration_seconds_bucket{{{ns},{api_routes}}}[{win}s])))',
        "server_p99_by_route_s": f'histogram_quantile(0.99, sum by (le,route)(increase(http_request_duration_seconds_bucket{{{ns},{api_routes}}}[{win}s])))',
        "server_requests_by_route": f'sum by (route)(increase(http_request_duration_seconds_count{{{ns},{api_routes}}}[{win}s]))',
        "server_5xx_total": f'sum(increase(http_request_duration_seconds_count{{{ns},status=~"5.."}}[{win}s]))',
        "backend_cpu_avg_cores_by_pod": f'sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},pod=~"backend-.*",container="backend"}}[{win}s]))',
        "backend_cpu_max_cores_by_pod": f'max_over_time(sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},pod=~"backend-.*",container="backend"}}[30s]))[{win}s:15s])',
        "backend_memory_max_mib_by_pod": f'max_over_time(container_memory_working_set_bytes{{{ns},pod=~"backend-.*",container="backend"}}[{win}s]) / 1048576',
        "backend_in_flight_max": f'max_over_time(sum(http_requests_in_flight{{{ns}}})[{win}s:15s])',
        "inference_cpu_avg_cores_by_pod_idle_baseline": f'sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},pod=~"inference-.*",container="inference"}}[{win}s]))',
        "hpa_replicas_min": f'min_over_time(kube_horizontalpodautoscaler_status_current_replicas{{{ns}}}[{win}s])',
        "hpa_replicas_max": f'max_over_time(kube_horizontalpodautoscaler_status_current_replicas{{{ns}}}[{win}s])',
        "node_busy_fraction": f'1 - avg by (instance)(rate(node_cpu_seconds_total{{mode="idle"}}[{win}s]))',
        "node_steal_fraction": f'avg by (instance)(rate(node_cpu_seconds_total{{mode="steal"}}[{win}s]))',
    }
    pjson = {name: vals(prom(19090, expr, at)) for name, expr in queries.items()}
    pf.terminate()
    (folder / "prometheus.json").write_text(json.dumps({"window_s": win, "query_time": at, "results": pjson},
                                                       ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 3. 분석과 무효 판정 --------------------------------------------------------------------
    verdict = {"invalid_reasons": [], "notes": []}
    if aborted:
        verdict["invalid_reasons"].append("중단: " + aborted)
    if k6.returncode not in (0, 99):  # 99 = 임계값 실패(기록은 정상)
        verdict["invalid_reasons"].append(f"k6 비정상 종료 rc={k6.returncode}")
    summary = {}
    try:
        summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        verdict["invalid_reasons"].append("summary.json 없음")

    def trend(name):
        v = metric_values(summary, name)
        return {k: (round(v[k], 2) if isinstance(v.get(k), (int, float)) else None) for k in ("avg", "med", "p(90)", "p(95)", "p(99)", "max", "min")}

    api = trend("api_ms")
    readyz = trend("readyz_ms")
    api_ok = metric_values(summary, "api_ok")
    ok_rate = api_ok.get("rate", api_ok.get("value"))
    dropped = metric_values(summary, "dropped_iterations").get("count", 0)
    if dropped and dropped > 0:
        verdict["invalid_reasons"].append(f"dropped_iterations={dropped}")

    # PC 감시 요약
    rows = [r for r in csv.reader(monitor_csv.read_text(encoding="utf-8-sig").splitlines()) if r and not r[0].startswith("#")][1:]
    header_line = monitor_csv.read_text(encoding="utf-8-sig").splitlines()[0]
    k6cpu = [float(r[1]) for r in rows if r[1] not in ("",)]
    totcpu = [float(r[2]) for r in rows]
    freemem = [float(r[3]) for r in rows]
    rx = [float(r[4]) for r in rows]
    tx = [float(r[5]) for r in rows]
    pc = {"adapter": header_line, "samples": len(rows), "k6_cpu_pct_max": max(k6cpu, default=None),
          "k6_cpu_pct_avg": round(statistics.mean(k6cpu), 1) if k6cpu else None, "total_cpu_pct_max": max(totcpu, default=None),
          "total_cpu_pct_avg": round(statistics.mean(totcpu), 1) if totcpu else None, "free_mem_mb_min": min(freemem, default=None),
          "net_rx_kbps_avg": round(statistics.mean(rx)) if rx else None, "net_tx_kbps_avg": round(statistics.mean(tx)) if tx else None,
          "net_rx_kbps_max": max(rx, default=None), "net_tx_kbps_max": max(tx, default=None)}
    if pc["k6_cpu_pct_max"] is not None and pc["k6_cpu_pct_max"] >= K6_CPU_LIMIT:
        verdict["invalid_reasons"].append(f"k6 CPU {pc['k6_cpu_pct_max']}% >= {K6_CPU_LIMIT}%")

    # 요청별 원본 점에서 경로별 클라이언트 분위수와 로그인 횟수, readyz 실패
    by_route, logins, readyz_bad, status_non200 = {}, 0, 0, 0
    with open(folder / "points.json", encoding="utf-8") as fh:
        for line in fh:
            if '"Point"' not in line:
                continue
            o = json.loads(line)
            tags = o["data"].get("tags", {})
            if o["metric"] == "http_req_duration" and tags.get("kind") == "api":
                by_route.setdefault(tags.get("route", "?"), []).append(o["data"]["value"])
            elif o["metric"] == "http_reqs":
                if tags.get("kind") == "login":
                    logins += 1
                if tags.get("kind") in ("api", "readyz") and tags.get("status") != "200":
                    status_non200 += 1
                    if tags.get("kind") == "readyz":
                        readyz_bad += 1
    client_by_route = {r: {"n": len(v), "p50": round(pct(sorted(v), .5), 1), "p95": round(pct(sorted(v), .95), 1),
                           "p99": round(pct(sorted(v), .99), 1), "max": round(max(v), 1)} for r, v in by_route.items()}
    if readyz_bad:
        verdict["notes"].append(f"readyz 200 아님 {readyz_bad}회")
    if status_non200:
        verdict["notes"].append(f"200 아닌 응답 {status_non200}건 (api/readyz)")

    # 안전 점검: 기존 위반 1~4, 기기 1·3, 장비 요청
    safety = {"violations_unchanged": pre["violations"] == post["violations"],
              "machines_unchanged": pre["machines"] == post["machines"],
              "counts_before": pre["counts"], "counts_after": post["counts"],
              "refresh_tokens_delta": int(post["counts"]["refresh_tokens"]) - int(pre["counts"]["refresh_tokens"]),
              "logins_in_points": logins}
    if not (safety["violations_unchanged"] and safety["machines_unchanged"]):
        verdict["invalid_reasons"].append("위반/기기 상태가 바뀜 (안전 위반) — 즉시 보고")
    for t in ("violations", "notifications", "violation_history", "machines", "admins"):
        if pre["counts"][t] != post["counts"][t]:
            verdict["notes"].append(f"{t} 행 수 변화: {pre['counts'][t]} -> {post['counts'][t]}")

    passed = (api.get("p(95)") is not None and api["p(95)"] < P95_LIMIT_MS and ok_rate is not None and ok_rate > OK_RATE_MIN)
    result = {"run": run_id, "start_utc": start.isoformat(timespec="seconds"), "end_utc": end.isoformat(timespec="seconds"),
              "valid": not verdict["invalid_reasons"], "invalid_reasons": verdict["invalid_reasons"], "notes": verdict["notes"],
              "pass_criteria": {"api_p95_ms_lt_500_and_ok_rate_gt_99pct": bool(passed)},
              "k6_api_ms": api, "k6_api_ok_rate": ok_rate, "k6_readyz_ms": readyz, "dropped_iterations": dropped,
              "client_by_route_ms": client_by_route, "pc": pc, "safety": safety, "prometheus": pjson}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 4. conditions.md, 인증정보 검사, 해시, 읽기 전용 -----------------------------------------
    images = sorted({c["image"].rsplit("/", 1)[-1] for p in pods for c in p["spec"]["containers"] if "parking-guardian" in c["image"]})
    nodes = sorted({p["spec"].get("nodeName", "?")[3:15] + ":" + p["metadata"]["name"][:18] for p in pods
                    if p["metadata"]["name"].startswith(("backend", "inference", "frontend"))})
    k6v = sh([K6_EXE, "version"]).stdout.strip()
    md = f"""# {run_id} 조건과 결과 — {dt.date.today().isoformat()}

- 상태: 측정 기록. **로컬 보관, Git 에 올리지 않는다.**
- 판정: **{'유효' if result['valid'] else '무효'}** {('— ' + '; '.join(verdict['invalid_reasons'])) if verdict['invalid_reasons'] else ''}
- 통과 기준(p95 < 500 ms, 성공률 > 99%): **{'충족' if passed else '미충족'}**
- 시간: {result['start_utc']} ~ {result['end_utc']} (UTC)

## 조건
- 시나리오 S0: 일반 API 5 req/s + readyz 1 req/s, {DURATION_S}초, 사진·장비 요청 0. 계정 COMPANY_ADMIN(A회사). 대상 backend 2 Pod, 이미지 {', '.join(images)}.
- 부하 발생기: 이 PC (**Wi-Fi 사용 — 사용자 허용, S0 에 한함**). {header_line.lstrip('# ')}. {k6v.splitlines()[0] if k6v else ''}
- 배치: {', '.join(nodes)}
- 스크립트 변경: `scenarios.js` S0 장비 요청 제거(사전 등록 고정 `9ffe18c` 이후, 사용자 승인 2026-10-31).
- 자격 증명: 환경변수/로컬 파일로 전달, 명령줄·로그·결과에 미포함(아래 검사).

## 결과 (k6, ms)
| 지표 | avg | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| api_ms (전체) | {api['avg']} | {api['med']} | {api['p(95)']} | {api['p(99)']} | {api['max']} |
| readyz_ms | {readyz['avg']} | {readyz['med']} | {readyz['p(95)']} | {readyz['p(99)']} | {readyz['max']} |
- 성공률 api_ok: {ok_rate}, dropped_iterations: {dropped}, 200 아닌 응답: {status_non200}

경로별(클라이언트): {json.dumps(client_by_route, ensure_ascii=False)}

## PC / 네트워크
{json.dumps(pc, ensure_ascii=False)}

## 서버(Prometheus) / DB
- 요약: result.json 의 prometheus. DB 연결 상태: 전 {pre['pg_stat_activity_by_state']} 후 {post['pg_stat_activity_by_state']} (조회 연결 1개 포함)
- refresh_tokens: {pre['counts']['refresh_tokens']} -> {post['counts']['refresh_tokens']} (증가 {safety['refresh_tokens_delta']}), k6 로그인 {logins}회
- 기존 위반 1~4 불변: {safety['violations_unchanged']}, 기기 행 불변: {safety['machines_unchanged']}
- 다른 행 수 변화: {verdict['notes'] or '없음'}
"""
    (folder / "conditions.md").write_text(md, encoding="utf-8")
    leaks = sum(1 for p in folder.iterdir() if p.is_file() and password in p.read_text(encoding="utf-8", errors="ignore"))
    (folder / "conditions.md").write_text(md + f"\n## 인증정보 검사\n결과 파일 중 비밀번호 문자열이 든 파일 수: {leaks}\n", encoding="utf-8")
    sums = []
    for p in sorted(folder.iterdir()):
        if p.name != "SHA256SUMS":
            sums.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()} *{p.name}")
    (folder / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    for p in folder.iterdir():
        os.chmod(p, 0o444)

    log(f"판정: {'유효' if result['valid'] else '무효'} / 통과 기준 {'충족' if passed else '미충족'} / 비밀번호 포함 파일 {leaks}")
    log(f"api p50/p95/p99 = {api['med']}/{api['p(95)']}/{api['p(99)']} ms, ok={ok_rate}, dropped={dropped}, "
        f"k6 CPU max={pc['k6_cpu_pct_max']}%, refresh_tokens +{safety['refresh_tokens_delta']}")
    log(f"결과 폴더: {folder}")
    sys.exit(0 if (result["valid"] and leaks == 0) else 2)


if __name__ == "__main__":
    main()
