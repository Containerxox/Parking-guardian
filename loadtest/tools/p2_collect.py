"""Phase 2 측정 수집·계산 도구 (읽기 전용). 정의는 perf_metrics.py 머리말(사전 등록 대상).

  report  k6 전송 로그(P2SEND/P2META) + DB 의 detection_jobs / detection_results → 단계별 처리량·지연·성공률·실패·미완료,
          작업 상태별 건수, Worker 별 처리 건수, 대기열(시스템 안 작업 수) 추이, 시도별 원자료. JSON + CSV 로 저장.
  sqs     CloudWatch 의 SQS 지표(보이는/처리 중/지연 메시지, 가장 오래된 메시지 나이) 1분 시계열 → CSV. (본 큐 + DLQ)

DB 는 SELECT 만 쓴다(assert_select_only 가 모든 문장을 검사하고, SQLite 는 읽기 전용 모드로 연다). 값은 시간 창 + run tag 로 고른다.
출처(--source): sqlite:<파일>   로컬 모의 서버의 DB (시험용)
               json:<파일>     이미 뽑아 둔 행 (오프라인 재계산)
               kubectl         AWS 의 backend Pod 안에서 SELECT 실행(`kubectl exec -i deploy/backend … python -`, 스크립트 SHA-256 을 먼저 출력)
이 도구는 장비 키·비밀번호를 읽지도 쓰지도 않는다. --forbid-env 로 이름을 주면 그 환경변수 값이 출력 파일에 들어 있지 않은지 쓰기 직전에 검사한다.

Worker "처리 시작 시각": DB 에 저장되지 않는다(claim 때의 updated_at / lease_until 은 완료 때 덮어쓰이거나 지워진다). 그래서
  처리 시작 ≈ 판정 완료(result.created_at) - 추론 시간(result.elapsed_ms) 으로 근사하고 "근사"로 표시한다(S3 읽기·DB 쓰기 시간은 빠진다).
"""
import argparse
import bisect
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perf_metrics as pm  # noqa: E402

for _stream in (sys.stdout, sys.stderr):      # Windows 콘솔(cp949)에서도 한글·기호 출력이 깨지거나 예외가 나지 않게
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

APP_NS = "parking-guardian"

# ---------------------------------------------------------------------------------------------- SELECT 만
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|merge|vacuum|lock|do|set|reindex|refresh|comment|pragma|attach)\b", re.I)


def assert_select_only(sql):
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise ValueError("여러 문장은 허용하지 않는다")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise ValueError("SELECT 로 시작해야 한다")
    if FORBIDDEN.search(re.sub(r"'[^']*'", "''", s)):
        raise ValueError(f"읽기 전용이 아닌 키워드가 있다: {FORBIDDEN.search(s).group(0)}")
    return s


JOB_COLS = ("id, machine_id, event_id, session_id, seq, event, received_at, published_at, status, worker_id, claim_attempt, "
            "publish_attempts, result_id, last_error, updated_at")
RES_COLS = "id, job_id, machine_id, event_id, seq, verdict, applied, elapsed_ms, received_at, created_at"


def sql_jobs(machines=None):
    w = f" and machine_id in ({','.join(str(int(m)) for m in machines)})" if machines else ""
    return f"select {JOB_COLS} from detection_jobs where received_at >= {{LO}} and received_at <= {{HI}}{w} order by received_at"


def sql_results(machines=None):
    w = f" and machine_id in ({','.join(str(int(m)) for m in machines)})" if machines else ""
    return f"select {RES_COLS} from detection_results where received_at >= {{LO}} and received_at <= {{HI}}{w} order by created_at"


def render(sql, dialect):
    lo, hi = (":lo", ":hi") if dialect == "sqlite" else ("cast(:lo as timestamptz)", "cast(:hi as timestamptz)")
    return assert_select_only(sql.replace("{LO}", lo).replace("{HI}", hi))


# ---------------------------------------------------------------------------------------------- 출처
def iso_utc(epoch_s):
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat(timespec="microseconds")


def fetch_sqlite(path, lo, hi, machines):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        out = {}
        for key, sql in (("jobs", sql_jobs(machines)), ("results", sql_results(machines))):
            out[key] = [dict(r) for r in con.execute(render(sql, "sqlite"), {"lo": iso_utc(lo), "hi": iso_utc(hi)})]
        return out
    finally:
        con.close()


POD_SCRIPT = '''
import json, re, sys
from app import create_app
from app.extensions import db
from sqlalchemy import text
FORBIDDEN = re.compile(r"\\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|merge|vacuum|lock|do|set|reindex|refresh|comment|pragma|attach)\\b", re.I)
def only_select(sql):
    s = sql.strip().rstrip(";").strip()
    assert ";" not in s and re.match(r"^(select|with)\\b", s, re.I) and not FORBIDDEN.search(re.sub(r"'[^']*'", "''", s)), "SELECT 만 허용"
    return s
arg = json.loads(sys.argv[1])
app = create_app()
out = {}
with app.app_context():
    for key, sql in arg["queries"].items():
        sql = only_select(sql)
        params = {k: arg[k] for k in ("lo", "hi") if ":" + k in sql}
        rows = db.session.execute(text(sql), params).mappings().all()
        out[key] = [dict(r) for r in rows]
    db.session.rollback()
print(json.dumps(out, default=str))
'''


def fetch_kubectl(lo, hi, machines, runner=None):
    script = POD_SCRIPT.encode("utf-8")
    print("pod 스크립트 sha256:", hashlib.sha256(script).hexdigest(), flush=True)
    arg = json.dumps({"lo": iso_utc(lo), "hi": iso_utc(hi),
                      "queries": {"jobs": render(sql_jobs(machines), "pg"), "results": render(sql_results(machines), "pg")}})
    run = runner or default_runner
    out = run(["kubectl", "exec", "-i", "deploy/backend", "-n", APP_NS, "--", "python", "-", arg], input=script)
    return json.loads(out.strip().splitlines()[-1])


def default_runner(cmd, input=None, timeout=120):
    r = subprocess.run(cmd, input=input if isinstance(input, bytes) else (input.encode() if input else None),
                       capture_output=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{cmd[0]} 실패(종료 {r.returncode}): {r.stderr.decode('utf-8', 'replace')[-300:]}")
    return r.stdout.decode("utf-8", "replace")


def load_rows(source, lo, hi, machines, runner=None):
    if source.startswith("sqlite:"):
        return fetch_sqlite(source[7:], lo, hi, machines)
    if source.startswith("json:"):
        return json.loads(Path(source[5:]).read_text(encoding="utf-8"))
    if source == "kubectl":
        return fetch_kubectl(lo, hi, machines, runner)
    raise SystemExit("--source 는 sqlite:<파일> | json:<파일> | kubectl")


# ---------------------------------------------------------------------------------------------- k6 로그 파싱
def parse_sends(text):
    """k6 --log-output 파일에서 P2META 와 P2SEND 줄을 읽는다. raw 형식과 text 형식(level=info msg="…" 이스케이프) 모두 지원."""
    metas, sends = [], []
    for line in text.splitlines():
        body = line
        m = re.search(r'msg="((?:[^"\\]|\\.)*)"', line)
        if m:
            try:
                body = json.loads('"' + m.group(1) + '"')
            except ValueError:
                body = line
        for tag in ("P2META ", "P2SEND "):
            i = body.find(tag)
            if i < 0:
                continue
            try:
                obj = json.loads(body[i + len(tag):])
            except ValueError:
                continue
            (metas if tag == "P2META " else sends).append(obj)
            break
    return metas, sends


def ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def hexid(v):
    return str(v).replace("-", "").lower()


def run_key(hex_eid):
    """lib_p2.js 의 id 구조(RRRRRRRR DDDD 4 K SS 8000 CCCCCCCCCCCC)면 (run tag, 종류 'measure'|'warmup'), 아니면 None(다른 출처의 행)."""
    if len(hex_eid) == 32 and hex_eid[12] == "4" and hex_eid[13] in "eb" and hex_eid[16:20] == "8000":
        return hex_eid[:8], ("measure" if hex_eid[13] == "e" else "warmup")
    return None


def is_warmup(hex_eid):
    """lib_p2.js makeId: RRRRRRRR DDDD 4 K SS … → 16진수 문자열에서 12번째가 '4', 13번째가 종류. b = 워밍업."""
    return len(hex_eid) >= 14 and hex_eid[12] == "4" and hex_eid[13] == "b"


# ---------------------------------------------------------------------------------------------- 보고서
def parse_starts(text):
    """각 장비 VU 가 로그에 남긴 P2START (시나리오 실제 시작 시각). 없으면 빈 목록."""
    out = []
    for line in text.splitlines():
        m = re.search(r'msg="((?:[^"\\]|\\.)*)"', line)
        body = json.loads('"' + m.group(1) + '"') if m else line
        i = body.find("P2START ")
        if i >= 0:
            try:
                out.append(json.loads(body[i + 8:]))
            except ValueError:
                pass
    return out


def build_report(metas, sends, rows, eval_at, on_time_s=pm.ON_TIME_S, drain_wait=120.0, tag=None, starts=None):
    if not metas:
        raise SystemExit("P2META 줄이 없다: k6 로그(--log-output)가 맞는지 확인")
    meta = next((m for m in metas if m["tag"] == tag), None) if tag else metas[-1]
    if meta is None:
        raise SystemExit(f"tag {tag} 를 가진 P2META 가 없다")
    # 단계 창의 기준 시각: 시나리오 실제 시작(P2START 의 최솟값). 없으면 setup 이 끝난 시각(P2META.t0)
    t0 = (min(s["scenario_start"] for s in starts) if starts else meta["t0"]) / 1000.0
    steps = meta["steps"]
    prefix = meta["tag"]
    mine = [s for s in sends if hexid(s["eid"]).startswith(prefix)]
    per_eid = {}
    for s in mine:
        cur = per_eid.setdefault(hexid(s["eid"]), {"first": s, "attempts": 0, "ok": False, "statuses": []})
        cur["attempts"] += 1
        cur["statuses"].append(s["status"])
        cur["ok"] = cur["ok"] or s["kind"] in ("accepted", "duplicate", "ok200")
        if s["attempt"] == 0:
            cur["first"] = s
    mine_jobs = {hexid(j["event_id"]): j for j in rows["jobs"] if hexid(j["event_id"]).startswith(prefix)}
    warm = {e for e in mine_jobs if is_warmup(e)}                       # 워밍업 사진(id 종류 b): 시도·통계에서 빼고 개수만 센다
    jobs = {e: j for e, j in mine_jobs.items() if e not in warm}
    all_results = {hexid(r["event_id"]): r for r in rows["results"] if hexid(r["event_id"]).startswith(prefix)}
    results = {e: r for e, r in all_results.items() if e not in warm}
    # 워밍업 작업의 접수→판정 지연: Worker 가 0 에서 올라올 때의 콜드 스타트 관찰값(측정 통계와 섞지 않는다)
    warm_lat = [ts(all_results[e]["created_at"]) - ts(mine_jobs[e]["received_at"]) for e in warm if e in all_results]
    warmup = {"jobs": len(warm), "judged": len(warm_lat), "status": dict(Counter(mine_jobs[e]["status"] for e in warm)), "latency_s": pm.latency_stats(warm_lat)}
    last_send = max((s["ts"] for s in mine), default=meta["t0"]) / 1000.0
    eval_too_early = eval_at < last_send + drain_wait

    attempts, by_step = [], {st["name"]: [] for st in steps}
    despite = 0
    for eid, cur in per_eid.items():
        first = cur["first"]
        job = jobs.get(eid)
        res = results.get(eid)
        lat = None
        if job is not None and res is not None:
            lat = ts(res["created_at"]) - ts(job["received_at"])
        age = (eval_at - ts(job["received_at"])) if job is not None else None
        outcome, young = pm.classify_p2(cur["ok"] or job is not None, job, lat, age, on_time_s)
        despite += 1 if (job is not None and not cur["ok"]) else 0
        row = {"eid": eid, "dev": first["dev"], "step": first["step"], "seq": first["seq"], "send_ts": first["ts"] / 1000.0,
               "k6_status": first["status"], "k6_ms": first["ms"], "attempts": cur["attempts"], "outcome": outcome,
               "latency_s": lat, "status": job["status"] if job else None, "worker_id": job.get("worker_id") if job else None,
               "censored_young": young}
        attempts.append(row)
        by_step[first["step"]].append(row)

    # 처리량: 완료 시각이 단계 창 안에 있는 사진 수
    step_rows, in_window = [], 0
    done_times = sorted(ts(r["created_at"]) for r in results.values())
    for st in steps:
        a, b = t0 + st["start"], t0 + st["end"]
        n = bisect.bisect_left(done_times, b) - bisect.bisect_left(done_times, a)
        in_window += n
        step_rows.append(pm.summarize_step(st["name"], st["seconds"], by_step[st["name"]], n))
    after_end = len(done_times) - in_window - bisect.bisect_left(done_times, t0)

    # 단계 시간
    judged = [(jobs[e], results[e]) for e in results if e in jobs and jobs[e]["status"] in ("done", "stale")]
    def spread(vals):
        return pm.latency_stats(vals)
    stages = {
        "recv_to_publish_s": spread([ts(j["published_at"]) - ts(j["received_at"]) for j, _ in judged if j.get("published_at")]),
        "publish_to_result_s": spread([ts(r["created_at"]) - ts(j["published_at"]) for j, r in judged if j.get("published_at")]),
        "recv_to_result_s": spread([ts(r["created_at"]) - ts(j["received_at"]) for j, r in judged]),
        "inference_s": spread([r["elapsed_ms"] / 1000.0 for _, r in judged]),
        "queue_wait_plus_overhead_s(근사)": spread([ts(r["created_at"]) - ts(j["published_at"]) - r["elapsed_ms"] / 1000.0 for j, r in judged if j.get("published_at")]),
    }
    status_counts = Counter(j["status"] for j in jobs.values())
    workers = Counter(j.get("worker_id") or "(none)" for j, _ in judged)
    stale = sum(1 for j, _ in judged if j["status"] == "stale")

    # 대기열: 시스템 안에 있는 작업 수 (접수 후 아직 판정이 없는 것)
    recv = sorted(ts(j["received_at"]) for j in jobs.values())
    comp = sorted(ts(results[e]["created_at"]) for e in results if e in jobs)
    backlog = []
    end_t = max([t0 + meta["total"]] + comp + ([last_send] if mine else [])) if mine else t0 + meta["total"]
    t = t0
    while t <= end_t + 5:
        inside = bisect.bisect_right(recv, t) - bisect.bisect_right(comp, t)
        backlog.append({"t_s": round(t - t0, 1), "accepted_cum": bisect.bisect_right(recv, t), "completed_cum": bisect.bisect_right(comp, t), "in_system": inside})
        t += 5.0

    report = {
        "definition": {"on_time_s": on_time_s, "p99_min_n": pm.P99_MIN_N, "drain_wait_s": drain_wait, "see": "perf_metrics.py 머리말"},
        "run": {k: meta[k] for k in ("run_id", "tag", "scenario", "c", "scale", "ndev", "event_mode", "retry", "total")},
        "t0_epoch": t0, "t0_source": "P2START" if starts else "P2META", "start_delay_ms": max((s.get("start_delay_ms", 0) for s in (starts or [])), default=None),
        "eval_at_epoch": eval_at, "last_send_epoch": last_send, "eval_too_early": eval_too_early,
        "attempts": len(attempts), "warmup_jobs_excluded": len(warm), "warmup": warmup, "accepted_despite_client_failure": despite,
        "steps": step_rows, "overall": pm.overall(step_rows), "completed_after_last_step": max(0, after_end),
        "job_status_counts": dict(status_counts), "stale_judged": stale, "worker_counts": dict(workers), "stages": stages,
        "notes": ["처리 시작 시각은 DB 에 저장되지 않는다: 근사 = 판정 완료 - 추론 시간",
                  "분위수는 판정이 끝난 사진만으로 계산한다. completion_rate 와 같이 읽는다",
                  "eval_too_early 가 true 면 unfinished 가 부풀려져 있다(평가를 더 늦춰 다시 계산)"],
    }
    return report, attempts, backlog


# ---------------------------------------------------------------------------------------------- 저장
def check_secrets(blobs, names):
    for name in names:
        val = os.environ.get(name, "")
        for part in [p.strip() for p in val.split(",") if len(p.strip()) >= 6]:
            for text in blobs:
                if part in text:
                    raise SystemExit(f"중단: 환경변수 {name} 의 값이 출력에 들어 있다. 파일을 쓰지 않았다.")


def write_csv(path, rows):
    rows = list(rows)
    if not rows:
        Path(path).write_text("", encoding="utf-8")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def save_outputs(out_dir, report, attempts, backlog, forbid_env=()):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report_text = json.dumps(report, indent=1, ensure_ascii=False, default=str)
    attempts_text = json.dumps(attempts, default=str)
    check_secrets([report_text, attempts_text], forbid_env)
    (out / "p2-report.json").write_text(report_text + "\n", encoding="utf-8")
    write_csv(out / "p2-steps.csv", pm.flatten_csv_rows(report["steps"]))
    write_csv(out / "p2-attempts.csv", attempts)
    write_csv(out / "p2-backlog.csv", backlog)
    write_csv(out / "p2-workers.csv", [{"worker_id": k, "judged": v} for k, v in sorted(report["worker_counts"].items())])
    return out


# ---------------------------------------------------------------------------------------------- SQS (CloudWatch)
SQS_METRICS = (("ApproximateNumberOfMessagesVisible", "visible"), ("ApproximateNumberOfMessagesNotVisible", "not_visible"),
               ("ApproximateNumberOfMessagesDelayed", "delayed"), ("ApproximateAgeOfOldestMessage", "oldest_age_s"))


def sqs_series(queue_name, start_epoch, end_epoch, runner=None, region="ap-northeast-2"):
    run = runner or default_runner
    series = {}
    for metric, label in SQS_METRICS:
        out = run(["aws", "cloudwatch", "get-metric-statistics", "--namespace", "AWS/SQS", "--metric-name", metric, "--dimensions",
                   f"Name=QueueName,Value={queue_name}", "--start-time", iso_utc(start_epoch), "--end-time", iso_utc(end_epoch), "--period", "60",
                   "--statistics", "Maximum", "--region", region, "--output", "json"])
        for p in json.loads(out)["Datapoints"]:
            series.setdefault(p["Timestamp"], {"timestamp": p["Timestamp"], "queue": queue_name})[label] = p["Maximum"]
    return [series[k] for k in sorted(series, key=lambda x: ts(x))]


def summarize_runs(jobs, now):
    """DB 의 작업 행을 run tag 로 묶어 회차별 현황(상태별 건수, 시간 범위, 미완료 나이)을 만든다. 우리 형식이 아닌 행은 '(other)' 한 묶음."""
    groups = {}
    for j in jobs:
        eid = hexid(j["event_id"])
        k = run_key(eid)
        key = (k[0] if k else "(other)", int(j["machine_id"]))
        g = groups.setdefault(key, {"run_tag": key[0], "machine_id": key[1], "jobs": 0, "warmup_jobs": 0, "status": Counter(), "first": None, "last": None, "oldest_unfinished": None})
        g["jobs"] += 1
        g["warmup_jobs"] += 1 if k and k[1] == "warmup" else 0
        g["status"][j["status"]] += 1
        t = ts(j["received_at"])
        g["first"] = t if g["first"] is None else min(g["first"], t)
        g["last"] = t if g["last"] is None else max(g["last"], t)
        if j["status"] in ("pending_publish", "queued", "processing"):
            g["oldest_unfinished"] = t if g["oldest_unfinished"] is None else min(g["oldest_unfinished"], t)
    out = []
    for g in sorted(groups.values(), key=lambda x: (x["first"], x["machine_id"])):
        out.append({"run_tag": g["run_tag"], "machine_id": g["machine_id"], "jobs": g["jobs"], "warmup_jobs": g["warmup_jobs"], "status": dict(g["status"]),
                    "first_received": iso_utc(g["first"]), "last_received": iso_utc(g["last"]),
                    "unfinished": sum(v for k, v in g["status"].items() if k in ("pending_publish", "queued", "processing")),
                    "oldest_unfinished_age_s": None if g["oldest_unfinished"] is None else round(now - g["oldest_unfinished"], 1)})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("runs", help="DB 에 남은 회차(run tag)별 작업 수·상태·미완료 현황 (읽기 전용)")
    u.add_argument("--source", required=True); u.add_argument("--since", help="이 시각 이후 접수분(epoch 또는 ISO). 기본 24시간 전"); u.add_argument("--machines", default="")
    r = sub.add_parser("report")
    r.add_argument("--sends", required=True); r.add_argument("--source", required=True); r.add_argument("--out", required=True)
    r.add_argument("--eval-at", help="평가 시점(epoch 초 또는 ISO). 기본: 지금"); r.add_argument("--drain-wait", type=float, default=120.0)
    r.add_argument("--on-time", type=float, default=pm.ON_TIME_S); r.add_argument("--machines", default="")
    r.add_argument("--tag"); r.add_argument("--forbid-env", default="DEVICE_KEYS,ADMIN_PASSWORD")
    q = sub.add_parser("sqs")
    q.add_argument("--queue", required=True); q.add_argument("--start", required=True); q.add_argument("--end", required=True); q.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "runs":
        now = datetime.now(timezone.utc).timestamp()
        lo = ts(a.since) if a.since else now - 86400
        rows = load_rows(a.source, lo, now + 60, [int(x) for x in a.machines.split(",") if x.strip()])
        table = summarize_runs(rows["jobs"], now)
        print(json.dumps(table, indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "sqs":
        rows = sqs_series(a.queue, ts(a.start), ts(a.end))
        write_csv(a.out, rows)
        print(f"{len(rows)}행 저장: {a.out}")
        return 0
    text = Path(a.sends).read_text(encoding="utf-8", errors="replace")
    metas, sends = parse_sends(text)
    meta = next((m for m in metas if m["tag"] == a.tag), None) if a.tag else (metas[-1] if metas else None)
    if meta is None:
        raise SystemExit("P2META 를 찾지 못했다")
    machines = [int(x) for x in a.machines.split(",") if x.strip()]
    lo = meta["t0"] / 1000.0 - 600     # 접수 시각은 전송 시각과 같다. 앞뒤 여유만 둔다
    hi = max([s["ts"] for s in sends] + [meta["t0"]]) / 1000.0 + 600
    eval_at = ts(a.eval_at) if a.eval_at else datetime.now(timezone.utc).timestamp()
    rows = load_rows(a.source, lo, hi, machines)
    report, attempts, backlog = build_report(metas, sends, rows, eval_at, a.on_time, a.drain_wait, a.tag, parse_starts(text))
    out = save_outputs(a.out, report, attempts, backlog, [x for x in a.forbid_env.split(",") if x])
    o = report["overall"]
    print(f"저장: {out}  시도 {report['attempts']}, on_time {o['on_time_rate']}, completion {o['completion_rate']}, "
          f"request_fail {o['request_fail_rate']}, unfinished {o['unfinished']}, eval_too_early={report['eval_too_early']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
