"""S1 한 회차 (인자로 S5 를 주면 S5-a: normal 2분 → inference 0개 3분 → 복구 3분, 사진 0.5C 유지. 인자로 S3 를 주면 S3: 사진 1.2C 10분 + 회복 3분, 일반 API 5 req/s 병행. 같은 코드·같은 안전 검사)(사진 0.2C→0.5C→0.8C→1.0C→1.2C→1.5C, 각 3분 = 18분 + 일반 API 5 req/s + readyz 1 req/s)를 실행하고 기록한다.

    python loadtest/tools/s1_run.py <회차> [S1|S3]   예: python loadtest/tools/s1_run.py 1   /   python loadtest/tools/s1_run.py 1 S3
    S0-A 회차는 기존 `python loadtest/tools/s0_run.py A<회차>` 를 쓴다(같은 조건, P3 고정 상태).

전제(실행 전 확인): P3 적용(HPA 2/2, Argo 자동 동기화 중단), P4 통과, 비정상 Pod 0. 이 스크립트가 사전 점검으로 HPA 2/2·readyz 200·Pod Ready·남은 k6 Pod 없음을 확인한다.
인증정보: ADMIN_PASSWORD(환경변수 또는 ~/.pg-s0.env). 장비 키는 backend Pod 의 `flask device-key` 로 만들어 환경변수로만 k6 에 넘기고 기록하지 않는다.
사진: sample_a 100%(허가), 장비 3개(기기 2·4·5). C = 3.107 장/초 (E3 N=2 중앙값, results/2026-10-31-e3n2-SUMMARY.md).
안전 검사: 위반 1~4 불변, 기기 1·3 행 불변, violations/notifications/violation_history 행 수 불변(기기 2·4·5 행은 정상 갱신), Pod 재시작·Pending·노드 NotReady·HPA 변화 = 즉시 중단.
무효: dropped_iterations > 0, photo_http_other > 0, k6 CPU >= 70%. 502 등 ALB 오류는 무효 사유가 아니라 이상 징후로 시각·종류·단계를 기록한다(사전 등록 변경 기록 2026-11-01).
중단·무효면 비정상 종료 코드로 끝나고 스스로 다시 실행하지 않는다.
"""
import atexit
import collections
import csv
import datetime as dt
import hashlib
import json
import os
import pathlib
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from s0_run import ADMIN_EMAIL, K6_CPU_LIMIT, K6_EXE, K6_PATH, load_password, log, metric_values, pct, prom, sh  # noqa: E402
from uprobe_run import cluster_state, s3_count, snapshot  # noqa: E402
from p0b_run import wifi_state  # noqa: E402
from s5_guard import clear_fault as guard_clear_fault, fault_env as inf_fault_env, ready_endpoints as inf_ready_endpoints, restore as guard_restore, spec_replicas as inf_replicas  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
RESULTS = REPO / "docs" / "measurements" / "phase1" / "results"
C = 3.107


def vals(result):
    """s0_run.vals 와 같지만 outcome·status·route 라벨을 모두 남긴다(busy/bulkhead_full 구분용)."""
    if isinstance(result, str):
        return result
    out = {}
    for item in result:
        label = ",".join(f"{k}={v}" for k, v in item["metric"].items() if k in ("pod", "route", "instance", "outcome", "status"))
        out[label or "_"] = round(float(item["value"][1]), 4)
    return out
SERIALS = ["RPI-A-PANGYO-01", "RPI-B-HQ-01", "RPI-B-LOGIS-01"]  # 기기 2·4·5
STEPS = [("0.2C", 0.2), ("0.5C", 0.5), ("0.8C", 0.8), ("1.0C", 1.0), ("1.2C", 1.2), ("1.5C", 1.5)]
STEP_S = 180
STEPS_BY = {"S1": STEPS, "S3": [("1.2C", 1.2), ("recovery", 0.0)],
            "S5": [("normal", 0.5), ("inject", 0.5), ("recover", 0.5)],
            "S5C": [("normal", 0.5), ("inject", 0.5), ("recover", 0.5)],  # S5-c: backend Pod 1개 삭제, k6 는 같은 S5 프로필
            "S5B": [("normal", 0.5), ("inject", 0.5), ("recover", 0.5)]}  # S5-b: inference 지연 주입(INFERENCE_FAULT_DELAY_MS)
TOTAL_MIN = {"S1": 18, "S3": 13, "S5": 8, "S5C": 8, "S5B": 8}
FAULT_DELAY_MS = int(os.environ.get("S5B_DELAY_MS", "3000"))  # S5-b 의 Pod 당 추론 지연(ms). 슬롯을 잡은 채 sleep 한다
REPLACE_READY_LIMIT_S = 180  # S5-c: 삭제 뒤 교체 Pod·ALB Healthy 2개가 이 시간 안에 안 돌아오면 부하를 멈추고 보고
# S5-a: k6 첫 일반 API 요청 시각(= scenarios.js 의 t0) 기준 120초 뒤 inference 를 0 으로, 180초 뒤 2 로.
# 독립 감시자(s5_guard)는 주입 시작 + 180 + 90초에 복구를 보장한다.
INJECT_AT_S, INJECT_S, GUARD_MARGIN_S, RECOVERY_WAIT_S = 120, 180, 90, 300
GRACE_S = 40  # setup(로그인·워밍업) 시간. 단계는 setup 이 끝난 시각부터 센다.



def recent_api(points_path, window_s=60):
    """points.json 끝부분에서 최근 window_s 초의 일반 API(kind=api) 요청 수와 200 응답 수를 센다(읽기 전용)."""
    try:
        size = os.path.getsize(points_path)
        with open(points_path, "rb") as fh:
            fh.seek(max(0, size - 3_000_000))
            lines = fh.read().decode("utf-8", errors="ignore").splitlines()[1:]
    except OSError:
        return 0, 0
    pts = []
    for line in lines:
        if '"metric":"http_reqs"' in line and '"kind":"api"' in line:
            try:
                o = json.loads(line)
                pts.append((dt.datetime.fromisoformat(o["data"]["time"]).timestamp(), o["data"]["tags"].get("status")))
            except Exception:  # noqa: BLE001
                continue
    if not pts:
        return 0, 0
    newest = max(t for t, _ in pts)
    win = [st for t, st in pts if t >= newest - window_s]
    return len(win), sum(1 for st in win if st == "200")


_TG = {}


def backend_tg_arn():
    if "arn" not in _TG:
        r = sh(["aws", "elbv2", "describe-target-groups", "--query",
                "TargetGroups[?contains(TargetGroupName,`backend`)].TargetGroupArn", "--output", "text"])
        _TG["arn"] = r.stdout.strip()
    return _TG["arn"]


def alb_targets():
    """ALB backend 대상 그룹의 대상별 상태. 실패하면 None."""
    r = sh(["aws", "elbv2", "describe-target-health", "--target-group-arn", backend_tg_arn(), "--output", "json"])
    if r.returncode:
        return None
    return [{"ip": d["Target"]["Id"], "state": d["TargetHealth"]["State"], "reason": d["TargetHealth"].get("Reason")}
            for d in json.loads(r.stdout)["TargetHealthDescriptions"]]


def backend_pods_now():
    r = sh(["kubectl", "-n", "parking-guardian", "get", "pods", "-l", "app.kubernetes.io/name=backend", "-o", "json"])
    out = []
    if r.returncode:
        return out
    for p_ in json.loads(r.stdout)["items"]:
        cs = p_["status"].get("containerStatuses", [])
        out.append({"name": p_["metadata"]["name"], "ip": p_["status"].get("podIP"), "node": p_["spec"].get("nodeName"),
                    "phase": p_["status"].get("phase"), "terminating": bool(p_["metadata"].get("deletionTimestamp")),
                    "ready": bool(cs) and all(c.get("ready") for c in cs),
                    "restarts": sum(c.get("restartCount", 0) for c in cs), "created": p_["metadata"]["creationTimestamp"],
                    "ready_at": next((c["lastTransitionTime"] for c in p_["status"].get("conditions", [])
                                      if c["type"] == "Ready" and c["status"] == "True"), None)})
    return out


def first_api_time(points_path):
    """k6 가 일반 API 를 처음 보낸 시각(epoch). scenarios.js 의 t0(setup 이 끝난 시각)과 거의 같다. 아직 없으면 None."""
    try:
        with open(points_path, encoding="utf-8") as fh:
            for line in fh:
                if '"metric":"http_reqs"' in line and '"kind":"api"' in line:
                    return dt.datetime.fromisoformat(json.loads(line)["data"]["time"]).timestamp()
    except OSError:
        pass
    return None


def backend_ready_endpoints():
    r = sh(["kubectl", "-n", "parking-guardian", "get", "endpointslices", "-l", "kubernetes.io/service-name=backend", "-o", "json"])
    if r.returncode:
        return None
    return sum(1 for sl in json.loads(r.stdout)["items"] for e in (sl.get("endpoints") or []) if e.get("conditions", {}).get("ready"))


def main():
    global STEPS
    run_n = sys.argv[1] if len(sys.argv) > 1 else "1"
    scen = sys.argv[2].upper() if len(sys.argv) > 2 else "S1"
    if scen not in STEPS_BY:
        sys.exit("시나리오는 S1, S3, S5, S5B 또는 S5C")
    STEPS = STEPS_BY[scen]
    password = load_password()
    run_id = f"{scen.lower()}-{run_n}"
    folder = RESULTS / f"{dt.date.today().isoformat()}-{run_id}"
    if folder.exists():
        sys.exit(f"{folder} 가 이미 있습니다. 덮어쓰지 않습니다.")
    keys = []
    for s in SERIALS:
        r = sh(["kubectl", "-n", "parking-guardian", "exec", "deploy/backend", "-c", "backend", "--", "flask", "device-key", s])
        k = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
        if r.returncode or not k:
            sys.exit(f"장비 키 조회 실패: {s}")
        keys.append(k)
    folder.mkdir(parents=True)
    env = dict(os.environ, ADMIN_PASSWORD=password, DEVICE_KEYS=",".join(keys), PATH=os.environ["PATH"] + os.pathsep + K6_PATH,
               PYTHONIOENCODING="utf-8")

    # ---- 0. 사전 점검 ----------------------------------------------------------------------
    host = sh(["kubectl", "-n", "parking-guardian", "get", "ingress", "-o", "jsonpath={.items[0].spec.rules[0].host}"]).stdout.strip()
    base_url = f"https://{host}"
    ready = sh(["curl", "-s", "-m", "10", "-o", os.devnull, "-w", "%{http_code}", f"{base_url}/api/v1/readyz"]).stdout
    pre_c = cluster_state()
    k6_pods = sh(["kubectl", "-n", "loadtest", "get", "pods", "--no-headers"]).stdout.strip()
    bad = [n for n, r in pre_c["pods"].items() if not r["ready"] or r["phase"] != "Running"]
    hpa_ok = all(v == (2, 2, 2) for v in pre_c["hpa"].values())
    log(f"readyz={ready} 비정상 Pod={bad} HPA={pre_c['hpa']} 남은 k6 Pod={'있음' if k6_pods else '없음'}")
    if ready != "200" or bad or not hpa_ok or k6_pods or pre_c["not_ready_nodes"]:
        sys.exit("사전 점검 실패: 시작하지 않습니다.")
    wifi_before = wifi_state()
    if scen == "S5C":
        t_pre = alb_targets()
        if not t_pre or sum(1 for x in t_pre if x["state"] == "healthy") != 2:
            sys.exit("S5-c 사전 점검 실패: ALB backend 대상 Healthy 2개가 아님: " + str(t_pre))
    pre_db, pre_s3 = snapshot(), s3_count()
    (folder / "db-before.json").write_text(json.dumps(pre_db, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 1. PC 감시 + k6 -------------------------------------------------------------------
    monitor_csv = folder / "pc-monitor.csv"
    monitor = subprocess.Popen(["powershell", "-NoProfile", "-File", str(REPO / "loadtest/tools/monitor-pc.ps1"), "-Out", str(monitor_csv),
                                "-IntervalSec", "5"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(6)
    start = dt.datetime.now(dt.timezone.utc)
    k6_scen = "S5" if scen in ("S5C", "S5B") else scen
    cmd = [K6_EXE, "run", "-e", f"SCENARIO={k6_scen}", "-e", f"C={C}", "-e", f"BASE_URL={base_url}", "-e", f"ADMIN_EMAIL={ADMIN_EMAIL}",
           "-e", f"RUN_ID={run_id}", f"--summary-export={folder / 'summary.json'}", "--summary-trend-stats=avg,min,med,max,p(90),p(95),p(99)",
           f"--out=json={folder / 'points.json'}", str(REPO / "loadtest/k6/scenarios.js")]
    log(f"k6 시작 ({run_id}, 약 {TOTAL_MIN[scen]}분 + 준비) {start:%H:%M:%S}Z")
    aborted, hot, last_check = None, 0, 0.0
    notready_events = []
    no_ep, api_bad, be_nr = 0, 0, 0
    inj = {"t0": None, "inject_at": None, "recover_at": None, "inject_start": None, "recover_start": None,
           "recovered_at": None, "guard": None, "scale0_rc": None, "pre": None, "note": [], "victim": None, "last_rec_check": 0.0, "on_ready": None, "fault_steps": []}
    timeline, sampling = [], threading.Event()

    def sampler():
        while not sampling.is_set():
            try:
                hpa = sh(["kubectl", "-n", "parking-guardian", "get", "hpa", "inference", "-o", "json"])
                h = json.loads(hpa.stdout) if hpa.returncode == 0 else {}
                cond = {c["type"]: c["status"] + "/" + c.get("reason", "") for c in h.get("status", {}).get("conditions", [])}
                timeline.append({"t": round(time.time(), 1), "inf_spec": inf_replicas(), "inf_ready_ep": inf_ready_endpoints(),
                                 "be_ready_ep": backend_ready_endpoints(), "inf_hpa_current": h.get("status", {}).get("currentReplicas"),
                                 "inf_hpa_active": cond.get("ScalingActive"), "fault_env": inf_fault_env()})
            except Exception as exc:  # noqa: BLE001
                timeline.append({"t": round(time.time(), 1), "error": str(exc)[:80]})
            sampling.wait(5)

    def do_scale(n):
        r = sh(["kubectl", "-n", "parking-guardian", "scale", "deploy/inference", "--replicas=" + str(n)])
        return r.returncode

    def start_injection():
        inj["pre"] = {"replicas": inf_replicas(), "ready_endpoints": inf_ready_endpoints()}
        deadline = time.time() + INJECT_S + GUARD_MARGIN_S
        inj["guard"] = subprocess.Popen(
            [sys.executable, str(pathlib.Path(__file__).parent / "s5_guard.py"), "--deadline-epoch", str(deadline),
             "--log", str(folder / "guard.log")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: 실행기가 죽어도 감시자는 산다
        atexit.register(lambda: guard_restore(False))  # 실행기가 예외로 끝나도 복구
        inj["scale0_rc"] = do_scale(0)
        inj["inject_start"] = time.time()
        log("[S5] inference -> 0 (rc=" + str(inj["scale0_rc"]) + "), 사전 상태 " + str(inj["pre"]) + "; 감시자 pid=" + str(inj["guard"].pid))

    def do_recover():
        rc = do_scale(2)
        inj["recover_start"] = time.time()
        log("[S5] inference -> 2 (rc=" + str(rc) + ") 주입 " + str(round(inj["recover_start"] - inj["inject_start"])) + "초")

    def sampler_c():
        while not sampling.is_set():
            try:
                pods_ = [{k: p_[k] for k in ("name", "ip", "ready", "terminating", "phase")} for p_ in backend_pods_now()]
                timeline.append({"t": round(time.time(), 1), "pods": pods_, "be_ready_ep": backend_ready_endpoints(), "alb": alb_targets()})
            except Exception as exc:  # noqa: BLE001
                timeline.append({"t": round(time.time(), 1), "error": str(exc)[:80]})
            sampling.wait(5)

    def start_injection_c():
        live = [p_ for p_ in backend_pods_now() if p_["ready"] and not p_["terminating"]]
        victim = sorted(live, key=lambda p_: p_["name"])[0]
        inj["victim"] = victim
        inj["pre"] = {"pods": backend_pods_now(), "alb": alb_targets(), "ready_endpoints": backend_ready_endpoints()}
        r = sh(["kubectl", "-n", "parking-guardian", "delete", "pod", victim["name"], "--wait=false"])
        inj["scale0_rc"] = r.returncode
        inj["inject_start"] = time.time()
        log("[S5-c] backend Pod 삭제 " + victim["name"] + " (ip=" + str(victim["ip"]) + ", rc=" + str(r.returncode) + "), 사전 ALB " +
            str([x["state"] for x in (inj["pre"]["alb"] or [])]))

    def check_recovered_c():
        """교체 Pod 포함 backend Ready Pod 2개 + Ready Endpoint 2개 + ALB Healthy 2개(삭제한 대상은 healthy 아님)."""
        pods_ = backend_pods_now()
        live = [p_ for p_ in pods_ if not p_["terminating"]]
        ready = [p_ for p_ in live if p_["ready"]]
        healthy = [x for x in (alb_targets() or []) if x["state"] == "healthy"]
        return len(live) == 2 and len(ready) == 2 and len(healthy) == 2 and backend_ready_endpoints() == 2 \
            and inj["victim"]["name"] not in [p_["name"] for p_ in live]

    def fault_switch(on):
        """scale 0 → set env(제거) → scale 2. 롤링 업데이트는 쓰지 않는다(노드 CPU 여유 < inference 1 CPU 라 surge Pod 가 못 뜬다)."""
        steps, rc = [], 0
        value = ("INFERENCE_FAULT_DELAY_MS=" + str(FAULT_DELAY_MS)) if on else "INFERENCE_FAULT_DELAY_MS-"
        for args in (["scale", "deploy/inference", "--replicas=0"], ["set", "env", "deploy/inference", value],
                     ["scale", "deploy/inference", "--replicas=2"]):
            r = sh(["kubectl", "-n", "parking-guardian"] + args)
            steps.append({"t": round(time.time(), 1), "cmd": " ".join(args)[:70], "rc": r.returncode})
            if r.returncode:
                rc = r.returncode
                break
        return rc, steps

    def fault_ready(expect):
        return inf_replicas() == 2 and inf_ready_endpoints() == 2 and inf_fault_env() == expect

    def start_fault_on():
        inj["pre"] = {"replicas": inf_replicas(), "ready_endpoints": inf_ready_endpoints(), "fault_env": inf_fault_env()}
        deadline = time.time() + INJECT_S + GUARD_MARGIN_S
        inj["guard"] = subprocess.Popen(
            [sys.executable, str(pathlib.Path(__file__).parent / "s5_guard.py"), "--clear-fault", "--deadline-epoch", str(deadline),
             "--log", str(folder / "guard.log")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x00000008 | 0x00000200)
        atexit.register(lambda: guard_clear_fault(False))
        inj["inject_start"] = time.time()
        inj["scale0_rc"], steps = fault_switch(True)
        inj["fault_steps"].append(["on", steps])
        log("[S5-b] 지연 " + str(FAULT_DELAY_MS) + " ms ON 전환 시작 (rc=" + str(inj["scale0_rc"]) + "), 사전 " + str(inj["pre"]) + "; 감시자 pid=" + str(inj["guard"].pid))

    def start_fault_off():
        inj["recover_start"] = time.time()
        rc, steps = fault_switch(False)
        inj["fault_steps"].append(["off", steps])
        log("[S5-b] 지연 OFF 전환 시작 (rc=" + str(rc) + "), ON 유지 " + str(round(inj["recover_start"] - inj["inject_start"])) + "초")

    if scen in ("S5", "S5B"):
        threading.Thread(target=sampler, daemon=True).start()
    if scen == "S5C":
        threading.Thread(target=sampler_c, daemon=True).start()
    with open(folder / "k6.log", "w", encoding="utf-8") as klog:
        k6 = subprocess.Popen(cmd, env=env, stdout=klog, stderr=subprocess.STDOUT)
        while k6.poll() is None:
            time.sleep(1 if scen in ("S5", "S5B", "S5C") else 5)
            try:
                last = next(csv.reader(monitor_csv.read_text(encoding="utf-8-sig").splitlines()[-1:]))
                k6cpu = float(last[1]) if last[1] not in ("", "k6_cpu_pct_of_all_cores") else 0.0
                total = float(last[2]) if last[2] not in ("", "total_cpu_pct") else 0.0
                hot = hot + 1 if (k6cpu >= K6_CPU_LIMIT or total >= 95) else 0
            except Exception:  # noqa: BLE001
                pass
            if hot >= (10 if scen in ("S5", "S5B", "S5C") else 2):  # S5 는 1초 루프이고 감시 CSV 는 5초마다 갱신된다
                aborted = "PC CPU 포화 2회 연속"
            if time.time() - last_check >= 20 and not aborted:
                last_check = time.time()
                c = cluster_state()
                s5 = scen in ("S5", "S5B")
                s5c = scen == "S5C"
                skip = "inference" if s5 else ("backend" if s5c else None)  # S5: inference, S5-c: backend Pod 변화는 의도된 것
                cp = {n: r for n, r in c["pods"].items() if not (skip and n.startswith(skip))}
                pp = {n: r for n, r in pre_c["pods"].items() if not (skip and n.startswith(skip))}
                nr = [n for n, r in cp.items() if not r["ready"] and r["phase"] == "Running"]
                if nr:
                    notready_events.append({"t_utc": dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), "pods": nr})
                # 보완 안전 중단(2026-11-01): 정상 backend Endpoint 가 모두 사라지거나, 관리자 핵심 API 가 지속 실패하면 중단.
                # S3 에서 Pod NotReady 자체는 관측만 한다. S5 는 backend 가 영향받으면 안 되므로 backend NotReady 2회 연속도 중단.
                ep = backend_ready_endpoints()
                no_ep = no_ep + 1 if ep == 0 else 0
                be_nr = be_nr + 1 if (s5 and any(n.startswith("backend") for n in nr)) else 0
                n_api, ok_api = recent_api(folder / "points.json")
                api_bad = api_bad + 1 if (n_api >= 100 and ok_api / n_api < 0.5) else 0
                if s5:
                    hpa_bad = c["hpa"]["backend"] != (2, 2, 2) or tuple(c["hpa"]["inference"][:2]) != (2, 2)
                else:
                    hpa_bad = any(v != (2, 2, 2) for v in c["hpa"].values())
                if no_ep >= 2:
                    aborted = "정상 backend Endpoint 0개가 40초 이상 지속"
                elif api_bad >= 3:
                    aborted = "관리자 핵심 API 성공률 < 50% 가 1분 창으로 3회 연속(최근 " + str(ok_api) + "/" + str(n_api) + ")"
                elif be_nr >= 2:
                    aborted = "S5: backend Pod 가 NotReady (inference 장애가 backend 로 번짐 — 의도하지 않은 장애)"
                if any(r["restarts"] for r in cp.values()) or c["not_ready_nodes"] or hpa_bad \
                        or any(r["phase"] == "Pending" for r in cp.values()) or set(cp) != set(pp):
                    aborted = "클러스터 이상(재시작/Pending/노드/HPA/Pod 집합, S5 면 inference 제외): hpa=" + str(c["hpa"])
                if s5 and inj["inject_start"] is None and any(r["restarts"] for n, r in c["pods"].items() if n.startswith("inference")):
                    aborted = "S5: 주입 전 inference Pod 재시작"
                if s5c:
                    bk = {n: r for n, r in c["pods"].items() if n.startswith("backend")}
                    if any(r["restarts"] for r in bk.values()):
                        aborted = "S5-c: backend Pod 재시작(의도하지 않은 장애)"
                    elif len(bk) > 3:
                        aborted = "S5-c: backend Pod 가 3개를 넘음"
            if scen == "S5" and not aborted:
                if inj["t0"] is None:
                    inj["t0"] = first_api_time(folder / "points.json")
                    if inj["t0"]:
                        inj["inject_at"], inj["recover_at"] = inj["t0"] + INJECT_AT_S, inj["t0"] + INJECT_AT_S + INJECT_S
                        log("[S5] t0 확정: " + str(INJECT_AT_S) + "초 뒤 주입, " + str(INJECT_S) + "초 유지")
                elif inj["inject_start"] is None and time.time() >= inj["inject_at"]:
                    start_injection()
                    if inj["scale0_rc"]:
                        aborted = "S5: scale 0 명령 실패"
                elif inj["inject_start"] and inj["recover_start"] is None and time.time() >= inj["recover_at"]:
                    do_recover()
                elif inj["inject_start"] and inj["recover_start"] is None and time.time() > inj["inject_start"] + INJECT_S + 60:
                    do_recover()  # 안전 상한: 어떤 이유로든 주입이 길어지면 강제 복구
                elif inj["recover_start"] and inj["recovered_at"] is None:
                    if inf_replicas() == 2 and inf_ready_endpoints() == 2:  # 실제 복구 시각(k6 종료와 무관)
                        inj["recovered_at"] = time.time()
                        log("[S5] inference Ready Endpoint 2개 복구 확인: scale 2 이후 " + str(round(inj["recovered_at"] - inj["recover_start"], 1)) + "초")
            if scen == "S5B" and not aborted:
                if inj["t0"] is None:
                    inj["t0"] = first_api_time(folder / "points.json")
                    if inj["t0"]:
                        inj["inject_at"], inj["recover_at"] = inj["t0"] + INJECT_AT_S, inj["t0"] + INJECT_AT_S + INJECT_S
                        log("[S5-b] t0 확정: " + str(INJECT_AT_S) + "초 뒤 지연 ON, " + str(INJECT_S) + "초 뒤 OFF")
                elif inj["inject_start"] is None and time.time() >= inj["inject_at"]:
                    start_fault_on()
                    if inj["scale0_rc"]:
                        aborted = "S5-b: 지연 ON 전환 명령 실패"
                elif inj["inject_start"] and inj["on_ready"] is None and inj["recover_start"] is None:
                    if time.time() - inj["inject_start"] > 120:
                        aborted = "S5-b: 지연 ON 전환이 120초 안에 완료되지 않음(수동 확인: s5_guard.py --status)"
                    elif time.time() - inj["last_rec_check"] >= 2:
                        inj["last_rec_check"] = time.time()
                        if fault_ready(str(FAULT_DELAY_MS)):
                            inj["on_ready"] = time.time()
                            log("[S5-b] 지연 ON 완료(Ready Endpoint 2, 설정 확인): 시작 후 " + str(round(inj["on_ready"] - inj["inject_start"], 1)) + "초")
                elif inj["recover_start"] is None and time.time() >= inj["recover_at"]:
                    start_fault_off()
                elif inj["recover_start"] and inj["recovered_at"] is None:
                    if time.time() - inj["recover_start"] > 120:
                        aborted = "S5-b: 지연 OFF 전환이 120초 안에 완료되지 않음(수동 확인: s5_guard.py --status)"
                    elif time.time() - inj["last_rec_check"] >= 2:
                        inj["last_rec_check"] = time.time()
                        if fault_ready(""):
                            inj["recovered_at"] = time.time()
                            log("[S5-b] 지연 OFF 완료(Ready Endpoint 2, 설정 없음): OFF 시작 후 " + str(round(inj["recovered_at"] - inj["recover_start"], 1)) + "초")
            if scen == "S5C" and not aborted:
                if inj["t0"] is None:
                    inj["t0"] = first_api_time(folder / "points.json")
                    if inj["t0"]:
                        inj["inject_at"] = inj["t0"] + INJECT_AT_S
                        log("[S5-c] t0 확정: " + str(INJECT_AT_S) + "초 뒤 backend Pod 1개 삭제")
                elif inj["inject_start"] is None and time.time() >= inj["inject_at"]:
                    start_injection_c()
                    if inj["scale0_rc"]:
                        aborted = "S5-c: Pod 삭제 명령 실패"
                elif inj["inject_start"] and inj["recovered_at"] is None:
                    if time.time() - inj["inject_start"] > REPLACE_READY_LIMIT_S:
                        aborted = "S5-c: 교체 Pod·ALB Healthy 2개가 " + str(REPLACE_READY_LIMIT_S) + "초 안에 복구되지 않음(수동 확인: s5_guard.py --status)"
                    elif time.time() - inj["last_rec_check"] >= 2:
                        inj["last_rec_check"] = time.time()
                        if check_recovered_c():
                            inj["recovered_at"] = time.time()
                            log("[S5-c] 교체 Pod Ready·Endpoint 2·ALB Healthy 2 복구: 삭제 후 " + str(round(inj["recovered_at"] - inj["inject_start"], 1)) + "초")
            if aborted:
                log("자동 중단: " + aborted)
                k6.terminate()
                break
        k6.wait()
    if scen == "S5":  # 성공·실패·중단 어느 경우든 복구를 보장한다
        if inj["inject_start"] and inj["recover_start"] is None:
            do_recover()
        t_wait = time.time()
        while inj["inject_start"] and time.time() - t_wait < RECOVERY_WAIT_S:
            if inf_replicas() == 2 and inf_ready_endpoints() == 2:
                inj["recovered_at"] = time.time()
                break
            time.sleep(3)
        if inj["inject_start"] and inj["recovered_at"] is None:
            inj["note"].append("복구 미확인: guard_restore 시도")
            guard_restore(False, folder / "guard.log", wait_s=120)
            if inf_replicas() == 2 and inf_ready_endpoints() == 2:
                inj["recovered_at"] = time.time()
        sampling.set()
        if inj["recovered_at"] and inj["recover_start"]:
            log("[S5] 복구 확인: scale 2 이후 " + str(round(inj["recovered_at"] - inj["recover_start"], 1)) + "초")
    if scen == "S5B":  # 성공·실패·중단 어느 경우든 지연 설정을 제거하고 2 Pod 로 되돌린다
        if inj["inject_start"] and inj["recover_start"] is None:
            start_fault_off()
        t_wait = time.time()
        while inj["inject_start"] and inj["recovered_at"] is None and time.time() - t_wait < RECOVERY_WAIT_S:
            if fault_ready(""):
                inj["recovered_at"] = time.time()
                break
            time.sleep(3)
        if inj["inject_start"] and inj["recovered_at"] is None:
            inj["note"].append("OFF 미확인: guard clear_fault 시도")
            guard_clear_fault(False, folder / "guard.log", wait_s=180)
            if fault_ready(""):
                inj["recovered_at"] = time.time()
        sampling.set()
    if scen == "S5C":
        t_wait = time.time()
        while inj["inject_start"] and inj["recovered_at"] is None and time.time() - t_wait < REPLACE_READY_LIMIT_S:
            if check_recovered_c():
                inj["recovered_at"] = time.time()
                break
            time.sleep(3)
        sampling.set()
    end = dt.datetime.now(dt.timezone.utc)
    time.sleep(2)
    monitor.terminate()
    log(f"k6 종료 rc={k6.returncode} {end:%H:%M:%S}Z")

    # ---- 2. 회차 후 -------------------------------------------------------------------------
    wifi_after = wifi_state()
    (folder / "wifi.json").write_text(json.dumps({"before": wifi_before, "after": wifi_after}, ensure_ascii=False, indent=1), encoding="utf-8")
    time.sleep(25)
    post_db, post_s3, post_c = snapshot(), s3_count(), cluster_state()
    (folder / "db-after.json").write_text(json.dumps(post_db, ensure_ascii=False, indent=1), encoding="utf-8")
    pf = subprocess.Popen(["kubectl", "-n", "monitoring", "port-forward", "svc/monitoring-prometheus", "19090:9090"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    at = time.time()
    win = int((end - start).total_seconds()) + 40
    ns = 'namespace="parking-guardian"'
    q = {
        "ai_client_by_outcome": f'sum by (outcome)(increase(pg_ai_client_requests_total{{{ns}}}[{win}s]))',
        "inference_by_outcome": f'sum by (outcome)(increase(pg_inference_requests_total{{{ns}}}[{win}s]))',
        "server_5xx_by_status_route": f'sum by (status,route)(increase(http_request_duration_seconds_count{{{ns},status=~"5.."}}[{win}s]))',
        "server_detections_by_status": f'sum by (status)(increase(http_request_duration_seconds_count{{{ns},route="/api/v1/device/detections"}}[{win}s]))',
        "server_detections_p95_s": f'histogram_quantile(0.95, sum by (le)(increase(http_request_duration_seconds_bucket{{{ns},route="/api/v1/device/detections"}}[{win}s])))',
        "server_api_p95_by_route_s": f'histogram_quantile(0.95, sum by (le,route)(increase(http_request_duration_seconds_bucket{{{ns},route=~"/api/v1/(violations|dashboard/summary|notifications)"}}[{win}s])))',
        "backend_cpu_max_cores_by_pod": f'max_over_time(sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},pod=~"backend-.*",container="backend"}}[30s]))[{win}s:15s])',
        "inference_cpu_max_cores_by_pod": f'max_over_time(sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},pod=~"inference-.*",container="inference"}}[30s]))[{win}s:15s])',
        "backend_memory_max_mib_by_pod": f'max_over_time(container_memory_working_set_bytes{{{ns},pod=~"backend-.*",container="backend"}}[{win}s]) / 1048576',
        "inference_memory_max_mib_by_pod": f'max_over_time(container_memory_working_set_bytes{{{ns},pod=~"inference-.*",container="inference"}}[{win}s]) / 1048576',
        "backend_in_flight_max": f'max_over_time(sum(http_requests_in_flight{{{ns}}})[{win}s:15s])',
        "node_busy_max_fraction": f'max_over_time((1 - avg by (instance)(rate(node_cpu_seconds_total{{mode="idle"}}[30s])))[{win}s:15s])',
        "node_steal_fraction": f'avg by (instance)(rate(node_cpu_seconds_total{{mode="steal"}}[{win}s]))',
        "restarts": f'sum by (pod)(kube_pod_container_status_restarts_total{{{ns}}})',
    }
    pjson = {n: vals(prom(19090, e, at)) for n, e in q.items()}
    pf.terminate()
    (folder / "prometheus.json").write_text(json.dumps({"window_s": win, "results": pjson}, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 3. 분석 ----------------------------------------------------------------------------
    inv, notes = [], []
    if aborted:
        inv.append("중단: " + aborted)
    if k6.returncode not in (0, 99):
        inv.append(f"k6 비정상 종료 rc={k6.returncode}")
    try:
        summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        summary, _ = {}, inv.append("summary.json 없음")
    dropped = metric_values(summary, "dropped_iterations").get("count", 0)
    other = metric_values(summary, "photo_http_other").get("count", 0)
    if dropped:
        inv.append(f"dropped_iterations={dropped}")
    if other:
        inv.append(f"photo_http_other={other} (401/400/500/연결 실패)")

    per = {s: {"photo": [], "send": [], "api": [], "api_ok": [0, 0], "photo_status": {}, "photo_n": 0} for s, _ in STEPS}
    per["other"] = {"photo": [], "send": [], "api": [], "api_ok": [0, 0], "photo_status": {}, "photo_n": 0}
    anomalies, logins = [], 0
    photo_st = []
    photo_pts, api_pts = [], []  # (epoch, ms, status) 분 단위 표용
    with open(folder / "points.json", encoding="utf-8") as fh:
        for line in fh:
            if '"Point"' not in line:
                continue
            o = json.loads(line)
            t = o["data"].get("tags", {})
            step = t.get("step") if t.get("step") in per else "other"
            m, v = o["metric"], o["data"]["value"]
            if m == "http_req_duration" and t.get("kind") == "photo":
                per[step]["photo"].append(v)
                photo_pts.append((dt.datetime.fromisoformat(o["data"]["time"]).timestamp(), v))
            elif m == "http_req_sending" and t.get("kind") == "photo":
                per[step]["send"].append(v)
            elif m == "http_req_duration" and t.get("kind") == "api":
                per[step]["api"].append(v)
                api_pts.append((dt.datetime.fromisoformat(o["data"]["time"]).timestamp(), v))
            elif m == "http_reqs":
                st, kind = t.get("status"), t.get("kind")
                if kind == "login":
                    logins += 1
                if kind == "photo":
                    photo_st.append((dt.datetime.fromisoformat(o["data"]["time"]).timestamp(), st))
                    per[step]["photo_n"] += 1
                    per[step]["photo_status"][st] = per[step]["photo_status"].get(st, 0) + 1
                if kind == "api":
                    per[step]["api_ok"][0 if st == "200" else 1] += 1
                if t.get("phase") != "warmup" and st and ((st.startswith("5") and st != "503") or st == "0"):
                    anomalies.append({"time": o["data"]["time"], "kind": kind, "step": step, "status": st, "route": t.get("route")})
    table = {}
    for s, _ in STEPS:
        d = per[s]
        n = d["photo_n"]
        ok = d["photo_status"].get("200", 0) + d["photo_status"].get("201", 0)
        a_ok, a_bad = d["api_ok"]
        table[s] = {"photo_n": n, "photo_ok_rate": round(ok / n, 4) if n else None, "photo_status": d["photo_status"],
                    "photo_ms": {"p50": pct(sorted(d["photo"]), .5), "p95": pct(sorted(d["photo"]), .95), "p99": pct(sorted(d["photo"]), .99),
                                 "max": max(d["photo"], default=None)},
                    "photo_sending_ms": {"p95": pct(sorted(d["send"]), .95), "max": max(d["send"], default=None)},
                    "api_n": a_ok + a_bad, "api_ok_rate": round(a_ok / (a_ok + a_bad), 4) if (a_ok + a_bad) else None,
                    "api_ms": {"p50": pct(sorted(d["api"]), .5), "p95": pct(sorted(d["api"]), .95), "p99": pct(sorted(d["api"]), .99),
                               "max": max(d["api"], default=None)}}
        table[s] = json.loads(json.dumps(table[s], default=lambda x: x), parse_float=lambda x: round(float(x), 4))
    if anomalies:
        notes.append(f"5xx(503 제외)/연결 실패 {len(anomalies)}건: " + json.dumps(anomalies, ensure_ascii=False))

    # 분 단위 표 (첫 사진 요청 시각 기준 분). S3 의 지속 과부하·회복 해석용
    by_minute = {}
    if photo_st:
        t0m = min(t for t, _ in photo_st)
        bins = {}
        for t, st_ in photo_st:
            b = bins.setdefault(int((t - t0m) // 60), {"n": 0, "ok": 0, "503": 0, "other": 0, "ms": [], "api": []})
            b["n"] += 1
            b["ok" if st_ in ("200", "201") else ("503" if st_ == "503" else "other")] += 1
        for t, v in photo_pts:
            bins.setdefault(int((t - t0m) // 60), {"n": 0, "ok": 0, "503": 0, "other": 0, "ms": [], "api": []})["ms"].append(v)
        for t, v in api_pts:
            bins.setdefault(int((t - t0m) // 60), {"n": 0, "ok": 0, "503": 0, "other": 0, "ms": [], "api": []})["api"].append(v)
        for k in sorted(bins):
            b = bins[k]
            by_minute[k] = {"photo_n": b["n"], "ok_rate": round(b["ok"] / b["n"], 3) if b["n"] else None, "503": b["503"], "other": b["other"],
                            "photo_p50": pct(sorted(b["ms"]), .5), "photo_p95": pct(sorted(b["ms"]), .95),
                            "api_p95": pct(sorted(b["api"]), .95), "api_n": len(b["api"])}
    # 회복 관찰용 시계열 (30초 간격): inference CPU, backend 동시 처리, busy+bulkhead 거절률, 노드 CPU
    pf2 = subprocess.Popen(["kubectl", "-n", "monitoring", "port-forward", "svc/monitoring-prometheus", "19092:9090"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    rng = {}
    qr = {"inference_cpu_cores_by_pod": f'sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},pod=~"inference-.*",container="inference"}}[30s]))',
          "backend_in_flight": f'sum(http_requests_in_flight{{{ns}}})',
          "ai_reject_per_s": f'sum(rate(pg_ai_client_requests_total{{{ns},outcome=~"busy|bulkhead_full"}}[30s]))',
          "ai_ok_per_s": f'sum(rate(pg_ai_client_requests_total{{{ns},outcome="ok"}}[30s]))',
          "node_busy_by_instance": '1 - avg by (instance)(rate(node_cpu_seconds_total{mode="idle"}[30s]))'}
    for name_, expr_ in qr.items():
        try:
            u = "http://127.0.0.1:19092/api/v1/query_range?" + urllib.parse.urlencode(
                {"query": expr_, "start": start.timestamp(), "end": end.timestamp() + 60, "step": 30})
            res = json.load(urllib.request.urlopen(u, timeout=30))["data"]["result"]
            rng[name_] = {(",".join(f"{k}={v}" for k, v in r["metric"].items() if k in ("pod", "instance")) or "_"):
                          [[int(float(t) - start.timestamp()), round(float(v), 3)] for t, v in r["values"]] for r in res}
        except Exception as exc:  # noqa: BLE001
            rng[name_] = f"ERR {exc}"
    pf2.terminate()
    (folder / "prometheus-range.json").write_text(json.dumps({"t0": "k6 start (s)", "step_s": 30, "series": rng}, ensure_ascii=False), encoding="utf-8")

    s5_block = None
    if scen == "S5":
        ta, tb = inj["inject_start"], inj["recover_start"]
        ok200 = sorted(t for t, st_ in photo_st if st_ in ("200", "201"))
        last_ok_before = max((t for t in ok200 if ta and t < ta), default=None)
        first_ok_after = min((t for t in ok200 if tb and t >= tb), default=None)
        inwin = [st_ for t, st_ in photo_st if ta and tb and ta <= t < tb]
        api_in = sorted(v for t, v in api_pts if ta and tb and ta <= t < tb)

        def iso(x):
            return x and dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime("%H:%M:%S")

        guard_log = (folder / "guard.log").read_text(encoding="utf-8") if (folder / "guard.log").exists() else None
        s5_block = {"method": "kubectl scale deploy/inference --replicas=0 -> 2 (P3: HPA 2/2 고정, Argo 자동 동기화 중단)", "pre": inj["pre"],
                    "inject_start_utc": iso(ta), "recover_start_utc": iso(tb), "recovered_at_utc": iso(inj["recovered_at"]),
                    "inject_seconds": tb and ta and round(tb - ta, 1),
                    "recovery_seconds_after_scale2": inj["recovered_at"] and tb and round(inj["recovered_at"] - tb, 1),
                    "last_photo_200_before_inject_utc": iso(last_ok_before), "first_photo_200_after_recover_utc": iso(first_ok_after),
                    "photo_unavailable_seconds": first_ok_after and ta and round(first_ok_after - ta, 1),
                    "photo_status_in_inject_window": dict(collections.Counter(inwin)),
                    "admin_api_in_inject_window": {"n": len(api_in), "p50": pct(api_in, .5), "p95": pct(api_in, .95), "p99": pct(api_in, .99)},
                    "notes": inj["note"], "guard_log": guard_log}
        (folder / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    if scen == "S5B":
        t_on, t_on_rdy, t_off, t_off_rdy = inj["inject_start"], inj["on_ready"], inj["recover_start"], inj["recovered_at"]
        t_end = end.timestamp()
        t_first = min((t for t, _ in photo_st), default=None)
        status_at = dict(photo_st)

        def iso(x):
            return x and dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime("%H:%M:%S")

        def p3_(a):
            return {"p50": pct(a, .5), "p95": pct(a, .95), "p99": pct(a, .99)} if a else None

        def window_stats(a, b):
            if not (a and b and b > a):
                return None
            sts = [x for t, x in photo_st if a <= t < b]
            n = len(sts)
            ok = sum(1 for x in sts if x in ("200", "201"))
            api_w = sorted(v for t, v in api_pts if a <= t < b)
            api_bad_w = [x for x in anomalies if x["kind"] == "api" and a <= dt.datetime.fromisoformat(x["time"]).timestamp() < b]
            return {"seconds": round(b - a, 1), "photo_n": n, "photo_status": dict(collections.Counter(sts)), "photo_ok_rate": round(ok / n, 4) if n else None,
                    "photo_ms_all": p3_(sorted(v for t, v in photo_pts if a <= t < b)),
                    "photo_ms_200": p3_(sorted(v for t, v in photo_pts if a <= t < b and status_at.get(t) == "200")),
                    "photo_ms_503": p3_(sorted(v for t, v in photo_pts if a <= t < b and status_at.get(t) == "503")),
                    "admin_api": {"n": len(api_w), **(p3_(api_w) or {}), "max": api_w[-1] if api_w else None, "errors_5xx_or_conn": len(api_bad_w)}}

        slow_a = t_on_rdy + 10 if t_on_rdy else None
        wins = {"baseline": window_stats(t_first and t_first + 10, t_on), "transition_on": window_stats(t_on, t_on_rdy),
                "slow_steady": window_stats(slow_a, t_off), "transition_off": window_stats(t_off, t_off_rdy), "after": window_stats(t_off_rdy, t_end)}
        # 회복: OFF 완료 뒤 15초 구간별 사진 p95 가 기준선 p95 의 1.3배 이하이고 성공률 97% 이상인 첫 구간
        base95 = (wins["baseline"] or {}).get("photo_ms_all", {}) and wins["baseline"]["photo_ms_all"]["p95"]
        rec_s = None
        if t_off_rdy and base95:
            for k in range(0, int(max(0, t_end - t_off_rdy) // 15)):
                w = window_stats(t_off_rdy + 15 * k, t_off_rdy + 15 * (k + 1))
                if w and w["photo_n"] and w["photo_ok_rate"] >= 0.97 and w["photo_ms_all"] and w["photo_ms_all"]["p95"] <= 1.3 * base95:
                    rec_s = 15 * k
                    break
        prom_delta = None
        if slow_a and t_off:
            pf3 = subprocess.Popen(["kubectl", "-n", "monitoring", "port-forward", "svc/monitoring-prometheus", "19093:9090"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)

            def snap(at):
                o = {}
                for key, expr in {"ai_outcome": "sum by (outcome)(pg_ai_client_requests_total{" + ns + "})",
                                  "ai_retries": "sum by (reason)(pg_ai_client_retries_total{" + ns + "})",
                                  "inference_rejected_busy": "sum(pg_inference_rejected_total{" + ns + "})"}.items():
                    res = prom(19093, expr, at)
                    o[key] = {",".join(k + "=" + v for k, v in r["metric"].items()) or "_": float(r["value"][1]) for r in res} if isinstance(res, list) else {}
                return o

            a_, b_ = snap(slow_a), snap(t_off)
            win_s = int(t_off - slow_a)
            prom_delta = {key: {k: round(b_[key].get(k, 0) - a_[key].get(k, 0), 1) for k in set(a_[key]) | set(b_[key])} for key in a_}
            for key, expr in {"ai_in_flight_max": "max_over_time(sum(pg_ai_client_in_flight{" + ns + "})[" + str(win_s) + "s:5s])",
                              "inference_waiting_max": "max_over_time(max(pg_inference_waiting{" + ns + "})[" + str(win_s) + "s:5s])",
                              "ai_duration_p95_s": "histogram_quantile(0.95, sum by (le)(increase(pg_ai_client_duration_seconds_bucket{" + ns + "}[" + str(win_s) + "s])))"}.items():
                res = prom(19093, expr, t_off)
                prom_delta[key] = float(res[0]["value"][1]) if isinstance(res, list) and res else None
            pf3.terminate()
        s5_block = {"method": "INFERENCE_FAULT_DELAY_MS=" + str(FAULT_DELAY_MS) + " 임시 설정: scale 0 → set env → scale 2 (ON), 반대로 OFF. 롤링 없음 (P3: HPA 2/2, Argo 자동 동기화 중단)",
                    "delay_ms": FAULT_DELAY_MS, "pre": inj["pre"], "on_start_utc": iso(t_on), "on_ready_utc": iso(t_on_rdy), "off_start_utc": iso(t_off),
                    "off_ready_utc": iso(t_off_rdy), "on_transition_s": t_on_rdy and t_on and round(t_on_rdy - t_on, 1),
                    "off_transition_s": t_off_rdy and t_off and round(t_off_rdy - t_off, 1), "windows": wins,
                    "recovery_to_baseline_s_after_off_ready": rec_s, "prometheus_delta_slow_window_approx": prom_delta,
                    "command_steps": inj["fault_steps"], "notes": inj["note"],
                    "guard_log": (folder / "guard.log").read_text(encoding="utf-8") if (folder / "guard.log").exists() else None}
        (folder / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    if scen == "S5C":
        ta, tr = inj["inject_start"], inj["recovered_at"]
        end_w = ((tr or (ta + 120)) if ta else None)
        post_pods = backend_pods_now()
        pre_names = {q["name"] for q in ((inj["pre"] or {}).get("pods") or [])}
        new_p = next((q for q in post_pods if q["name"] not in pre_names), None)

        def t2e(x):
            return dt.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp() if x else None

        def iso(x):
            return x and dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime("%H:%M:%S")

        def in_w(t):
            return bool(ta and end_w and ta <= t <= end_w)

        photo_w = [st_ for t, st_ in photo_st if in_w(t)]
        photo_ms_w = sorted(v for t, v in photo_pts if in_w(t))
        api_w = sorted(v for t, v in api_pts if in_w(t))
        api_fail_w = [a for a in anomalies if a["kind"] == "api" and in_w(dt.datetime.fromisoformat(a["time"]).timestamp())]
        tl_ok = [x for x in timeline if "error" not in x and ta and x["t"] >= ta]

        def healthy_ct(x):
            return sum(1 for y in (x.get("alb") or []) if y["state"] == "healthy")

        alb_back = next((x["t"] for x in tl_ok if healthy_ct(x) == 2 and x.get("be_ready_ep") == 2), None)
        v = inj["victim"] or {}
        s5_block = {"method": "kubectl delete pod <backend 1개> --wait=false (graceful 종료; P3: HPA 2/2 고정, Argo 자동 동기화 중단)",
                    "deleted_pod": {k: v.get(k) for k in ("name", "ip", "node")}, "replacement_pod": new_p and {k: new_p.get(k) for k in ("name", "ip", "node", "restarts")},
                    "pre": inj["pre"], "delete_utc": iso(ta), "recovered_at_utc": iso(tr),
                    "delete_to_all_recovered_s": tr and ta and round(tr - ta, 1),
                    "delete_to_replacement_created_s": new_p and ta and round(t2e(new_p["created"]) - ta, 1),
                    "delete_to_replacement_ready_s": new_p and ta and new_p["ready_at"] and round(t2e(new_p["ready_at"]) - ta, 1),
                    "replacement_create_to_ready_s": new_p and new_p["ready_at"] and round(t2e(new_p["ready_at"]) - t2e(new_p["created"]), 1),
                    "alb_healthy2_and_endpoint2_first_sample_s_after_delete": alb_back and ta and round(alb_back - ta, 1),
                    "min_backend_ready_endpoints": min((x.get("be_ready_ep") for x in tl_ok if x.get("be_ready_ep") is not None), default=None),
                    "min_alb_healthy": min((healthy_ct(x) for x in tl_ok if x.get("alb") is not None), default=None),
                    "window_s": end_w and ta and round(end_w - ta, 1),
                    "photo_status_in_window": dict(collections.Counter(photo_w)),
                    "photo_ms_in_window": {"n": len(photo_ms_w), "p50": pct(photo_ms_w, .5), "p95": pct(photo_ms_w, .95), "p99": pct(photo_ms_w, .99)},
                    "admin_api_in_window": {"n": len(api_w), "p50": pct(api_w, .5), "p95": pct(api_w, .95), "p99": pct(api_w, .99), "max": api_w[-1] if api_w else None},
                    "admin_api_5xx_or_conn_errors_in_window": api_fail_w, "notes": inj["note"]}
        (folder / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    rows = [r for r in csv.reader(monitor_csv.read_text(encoding="utf-8-sig").splitlines()) if r and not r[0].startswith("#")][1:]
    k6cpu = [float(r[1]) for r in rows if r[1]]
    tot = [float(r[2]) for r in rows]
    tx = [float(r[5]) for r in rows]
    pc = {"k6_cpu_pct_max": max(k6cpu, default=None), "total_cpu_pct_max": max(tot, default=None),
          "total_cpu_pct_avg": round(statistics.mean(tot), 1) if tot else None, "net_tx_kbps_avg": round(statistics.mean(tx)) if tx else None,
          "net_tx_kbps_max": max(tx, default=None)}
    if pc["k6_cpu_pct_max"] is not None and pc["k6_cpu_pct_max"] >= K6_CPU_LIMIT:
        inv.append(f"k6 CPU {pc['k6_cpu_pct_max']}% >= {K6_CPU_LIMIT}%")

    safety = {"violations_unchanged": pre_db["violations"] == post_db["violations"],
              "machines_1_3_unchanged": [m for m in pre_db["machines"] if m[0] in ("1", "3")] == [m for m in post_db["machines"] if m[0] in ("1", "3")],
              "s3_objects": [pre_s3, post_s3], "counts_before": pre_db["counts"], "counts_after": post_db["counts"],
              "pods_restarts_after": {n: r["restarts"] for n, r in post_c["pods"].items()}, "hpa_after": post_c["hpa"]}
    if not (safety["violations_unchanged"] and safety["machines_1_3_unchanged"]):
        inv.append("안전 위반: 위반 1~4 또는 기기 1·3 행 변화 — 즉시 보고")
    for t in ("violations", "notifications", "violation_history"):
        if pre_db["counts"][t] != post_db["counts"][t]:
            inv.append(f"안전 위반: {t} 행 수 {pre_db['counts'][t]} -> {post_db['counts'][t]}")
    if pre_s3 != post_s3:
        inv.append(f"안전 위반: S3 객체 수 {pre_s3} -> {post_s3}")
    if scen == "S5":
        non_inf = {n: r for n, r in post_c["pods"].items() if not n.startswith("inference")}
        if any(r["restarts"] for r in non_inf.values()) or post_c["not_ready_nodes"] or tuple(post_c["hpa"]["backend"]) != (2, 2, 2):
            inv.append("S5: backend/frontend 재시작 또는 노드/HPA 이상")
        if inj["inject_start"] is None:
            inv.append("S5: 장애 주입이 실행되지 않음")
        elif inj["recovered_at"] is None:
            inv.append("S5: inference 복구 미확인 — 즉시 수동 확인 필요(python loadtest/tools/s5_guard.py --now)")
        inf_pods = {n: r for n, r in post_c["pods"].items() if n.startswith("inference")}
        if len(inf_pods) != 2 or not all(r["ready"] for r in inf_pods.values()):
            inv.append("S5: 종료 후 inference Ready Pod 2개가 아님")
    elif scen == "S5B":
        non_inf = {n: r for n, r in post_c["pods"].items() if not n.startswith("inference")}
        if any(r["restarts"] for r in non_inf.values()) or post_c["not_ready_nodes"] or tuple(post_c["hpa"]["backend"]) != (2, 2, 2):
            inv.append("S5-b: backend/frontend 재시작 또는 노드/HPA 이상")
        if inj["inject_start"] is None:
            inv.append("S5-b: 지연 주입이 실행되지 않음")
        elif inj["on_ready"] is None:
            inv.append("S5-b: 지연 ON 완료가 확인되지 않음(지연이 적용된 구간 없음)")
        if inj["inject_start"] and (inj["recovered_at"] is None or inf_fault_env() != ""):
            inv.append("S5-b: 지연 설정 제거·복구 미확인 — 즉시 수동 확인 필요(python loadtest/tools/s5_guard.py --clear-fault --now)")
        inf_pods = {n: r for n, r in post_c["pods"].items() if n.startswith("inference")}
        if len(inf_pods) != 2 or not all(r["ready"] for r in inf_pods.values()):
            inv.append("S5-b: 종료 후 inference Ready Pod 2개가 아님")
    elif scen == "S5C":
        be = {n: r for n, r in post_c["pods"].items() if n.startswith("backend")}
        non_be = {n: r for n, r in post_c["pods"].items() if not n.startswith("backend")}
        if any(r["restarts"] for r in non_be.values()) or post_c["not_ready_nodes"] or any(tuple(v) != (2, 2, 2) for v in post_c["hpa"].values()):
            inv.append("S5-c: inference/frontend 재시작 또는 노드/HPA 이상")
        if inj["inject_start"] is None:
            inv.append("S5-c: Pod 삭제가 실행되지 않음")
        elif inj["recovered_at"] is None:
            inv.append("S5-c: 교체 Pod/ALB Healthy 복구 미확인 — 즉시 수동 확인 필요(python loadtest/tools/s5_guard.py --status)")
        if len(be) != 2 or not all(r["ready"] for r in be.values()) or any(r["restarts"] for r in be.values()):
            inv.append("S5-c: 종료 후 backend Ready Pod 2개(재시작 0)가 아님")
    elif any(r["restarts"] for r in post_c["pods"].values()) or post_c["not_ready_nodes"] or any(v != (2, 2, 2) for v in post_c["hpa"].values()):
        inv.append("Pod 재시작/노드/HPA 이상")

    result = {"run": run_id, "C": C, "start_utc": start.isoformat(timespec="seconds"), "end_utc": end.isoformat(timespec="seconds"),
              "valid": not inv, "invalid_reasons": inv, "notes": notes, "dropped_iterations": dropped, "photo_http_other": other,
              "photo_http_503_total": metric_values(summary, "photo_http_503").get("count", 0), "by_step": table, "by_minute": by_minute, "s5": s5_block, "notready_events": notready_events, "pc": pc, "safety": safety,
              "logins": logins, "anomalies": anomalies, "prometheus": pjson}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    images = sorted({c["image"].rsplit("/", 1)[-1] for p in json.loads(sh(["kubectl", "-n", "parking-guardian", "get", "pods", "-o", "json"]).stdout)["items"]
                     for c in p["spec"]["containers"] if "parking-guardian" in c["image"]})
    lines = [f"# {run_id} 조건과 결과 — {dt.date.today().isoformat()}", "",
             "- 로컬 보관, Git 에 올리지 않는다.", f"- 판정: **{'유효' if result['valid'] else '무효'}** {'; '.join(inv)}",
             f"- 시간: {result['start_utc']} ~ {result['end_utc']} (UTC), 이미지 {', '.join(images)}, C={C}",
             "- 조건: P3 고정(backend·inference HPA 2/2, Argo 자동 동기화 중단), sample_a 100%, 기기 2·4·5, Wi-Fi(PC 직접), 계정 COMPANY_ADMIN",
             f"- PC: {json.dumps(pc, ensure_ascii=False)}", f"- dropped_iterations={dropped}, photo_http_other={other}, 503 합계={result['photo_http_503_total']}",
             f"- 이상 징후: {notes or '없음'}", "", "## 단계별", "| 단계 | 사진 n | 성공률 | 200/503 | 사진 p50/p95/p99 ms | 일반 API p95 ms | API 성공률 |", "|---|---|---|---|---|---|---|"]
    for s, _ in STEPS:
        t = table[s]
        lines.append(f"| {s} | {t['photo_n']} | {t['photo_ok_rate']} | {t['photo_status']} | {t['photo_ms']['p50']}/{t['photo_ms']['p95']}/{t['photo_ms']['p99']} | "
                     f"{t['api_ms']['p95']} | {t['api_ok_rate']} |")
    lines += ["", "## 서버(Prometheus)", "```", json.dumps(pjson, ensure_ascii=False, indent=1), "```", "",
              f"## 안전 검사", f"```", json.dumps(safety, ensure_ascii=False, indent=1), "```"]
    (folder / "conditions.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    secrets = [password] + keys
    leaks = sum(1 for p in folder.iterdir() if p.is_file() and any(sx in p.read_text(encoding="utf-8", errors="ignore") for sx in secrets))
    (folder / "conditions.md").write_text("\n".join(lines) + f"\n\n## 인증정보 검사\n비밀번호·장비 키 문자열이 든 파일 수: {leaks}\n", encoding="utf-8")
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()} *{p.name}" for p in sorted(folder.iterdir()) if p.name != "SHA256SUMS"]
    (folder / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode())
    for p in folder.iterdir():
        os.chmod(p, 0o444)
    log(f"판정: {'유효' if result['valid'] else '무효'} / 인증정보 포함 파일 {leaks} / 이상 징후 {len(anomalies)}건")
    for s, _ in STEPS:
        t = table[s]
        log(f"  {s}: n={t['photo_n']} ok={t['photo_ok_rate']} p95={t['photo_ms']['p95']} ms / API p95={t['api_ms']['p95']} ms ok={t['api_ok_rate']}")
    log(f"결과 폴더: {folder}")
    sys.exit(0 if (result["valid"] and leaks == 0) else 2)


if __name__ == "__main__":
    main()
