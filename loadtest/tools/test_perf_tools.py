"""부하 테스트 도구의 단위 시험 (AWS·클러스터·네트워크 없음). 실행: backend\\venv 의 python -m pytest loadtest/tools -q
대상: perf_metrics, p2_collect, p1_points, p2_watch, p3(가짜 클러스터). k6 + 모의 서버 통합 시험은 test_p2_integration.py.
"""
import json
import os
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import p1_points  # noqa: E402
import p2_collect as pc  # noqa: E402
import p2_watch as pw  # noqa: E402
import perf_metrics as pm  # noqa: E402

T0 = 1_000_000.0


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="microseconds")


# ============================================================================================ perf_metrics
def test_percentile_linear_interpolation_matches_known_values():
    assert pm.percentile([], 50) is None
    assert pm.percentile([5], 99) == 5.0
    xs = [1, 2, 3, 4]
    assert pm.percentile(xs, 50) == 2.5
    assert pm.percentile(xs, 0) == 1 and pm.percentile(xs, 100) == 4
    assert pm.percentile([2.0, 9.99, 12.0], 95) == pytest.approx(11.799)
    assert pm.percentile(list(range(1, 101)), 99) == pytest.approx(99.01)
    assert pm.percentile([3, 1, 2], 50) == 2.0                       # 정렬을 알아서 한다


def test_latency_stats_flags_unreliable_p99():
    assert pm.latency_stats([])["n"] == 0
    small = pm.latency_stats([1.0] * 299)
    assert small["p99_reliable"] is False and small["n"] == 299
    assert pm.latency_stats([1.0] * 300)["p99_reliable"] is True


def test_classify_p2_covers_every_outcome():
    job = lambda st: {"status": st}  # noqa: E731
    assert pm.classify_p2(False, None, None, None) == ("request_failed", False)
    assert pm.classify_p2(True, job("done"), 2.0, 100) == ("on_time", False)
    assert pm.classify_p2(True, job("done"), 10.0, 100) == ("on_time", False)       # 경계: 10.0 s 는 제시간
    assert pm.classify_p2(True, job("done"), 10.01, 100) == ("late", False)
    assert pm.classify_p2(True, job("stale"), 3.0, 100) == ("on_time", False)       # stale = 판정은 있다
    assert pm.classify_p2(True, job("rejected"), None, 100) == ("processing_failed", False)
    assert pm.classify_p2(True, job("dead_lettered"), None, 100) == ("processing_failed", False)
    assert pm.classify_p2(True, job("queued"), None, 100) == ("unfinished", False)
    assert pm.classify_p2(True, job("processing"), None, 5) == ("unfinished", True)  # 접수 5 s 전: censored_young
    assert pm.classify_p2(True, job("pending_publish"), None, 10) == ("unfinished", False)


def test_classify_p1():
    assert pm.classify_p1(200, 1.0) == ("on_time", False)
    assert pm.classify_p1(201, 10.0) == ("on_time", False)
    assert pm.classify_p1(200, 11.0) == ("late", False)
    for status in (503, 502, 500, 400, 401, 0):
        assert pm.classify_p1(status, 0.2)[0] == "request_failed"


def test_summarize_step_keeps_failures_and_unfinished_in_the_denominator():
    items = ([{"outcome": "on_time", "latency_s": 1.0}] * 6 + [{"outcome": "late", "latency_s": 12.0}] +
             [{"outcome": "request_failed"}] * 2 + [{"outcome": "unfinished", "censored_young": True}] +
             [{"outcome": "processing_failed"}])
    row = pm.summarize_step("s", 10, items, 7)
    assert row["offered"] == 11 and row["offered_per_s"] == pytest.approx(1.1)
    assert row["on_time_rate"] == pytest.approx(6 / 11)
    assert row["completion_rate"] == pytest.approx(7 / 11)
    assert row["request_fail_rate"] == pytest.approx(2 / 11)
    assert row["processing_fail_rate"] == pytest.approx(1 / 9)               # 분모 = 접수된 것 (11 - 2)
    assert row["censored_young"] == 1 and row["throughput_per_s"] == pytest.approx(0.7)
    assert row["latency_s"]["n"] == 7                                       # 분위수는 판정이 끝난 것만
    empty = pm.summarize_step("e", 10, [], 0)
    assert empty["on_time_rate"] is None and empty["offered"] == 0


# ============================================================================================ SELECT 전용 가드
@pytest.mark.parametrize("sql", ["select 1", "SELECT id from detection_jobs where status = 'update'", "with x as (select 1) select * from x",
                                 "select 1;", pc.sql_jobs([7, 8]).replace("{LO}", ":lo").replace("{HI}", ":hi")])
def test_assert_select_only_accepts_reads(sql):
    pc.assert_select_only(sql)


@pytest.mark.parametrize("sql", ["insert into t values (1)", "update machines set status = 'x'", "delete from detection_jobs", "drop table x",
                                 "select 1; delete from x", "select * from x for update", "truncate detection_jobs", "alter table x add y int",
                                 "create table z(a int)", "select 1 /* */ ; select 2", "vacuum", "set role admin", "copy x to stdout", "pragma writable_schema=1"])
def test_assert_select_only_rejects_writes(sql):
    with pytest.raises(ValueError):
        pc.assert_select_only(sql)


def test_every_query_the_tools_send_is_select_only():
    for dialect in ("sqlite", "pg"):
        pc.render(pc.sql_jobs([1, 2]), dialect)
        pc.render(pc.sql_results(None), dialect)
    for sql in pw.DB_QUERIES.values():
        pc.assert_select_only(sql)
    with pytest.raises(ValueError):
        pc.sql_jobs(["1; drop table x"])                                     # 장비 번호는 int 로만 들어간다


def test_pod_script_embeds_the_same_guard_and_sends_only_select():
    assert "only_select" in pc.POD_SCRIPT and "insert" in pc.POD_SCRIPT       # 허용 목록이 아니라 금지 목록 + SELECT 시작 검사
    ns = {}
    guard_src = pc.POD_SCRIPT.split("arg = json.loads")[0]
    guard_src = guard_src.replace("from app import create_app\nfrom app.extensions import db\nfrom sqlalchemy import text\n", "")
    exec(guard_src, ns)                                                       # noqa: S102 - 시험용: 가드 함수만 꺼내 실행
    ns["only_select"]("select 1")
    for bad in ("delete from x", "select 1; select 2", "update x set a=1"):
        with pytest.raises(AssertionError):
            ns["only_select"](bad)


# ============================================================================================ p2_collect 보고서
def build_fixture():
    tag = "aaaaaaaa"
    eid = lambda n: f"{tag}-0000-4e00-8000-{n:012x}"  # noqa: E731
    meta = {"run_id": "t", "tag": tag, "t0": int(T0 * 1000), "scenario": "S1", "c": 3.0, "scale": 1, "ndev": 3, "event_mode": "refresh", "retry": 0,
            "total": 20, "steps": [{"name": "s1", "seconds": 10, "start": 0, "end": 10, "index": 0, "rate": 0.5},
                                   {"name": "s2", "seconds": 10, "start": 10, "end": 20, "index": 1, "rate": 0.5}]}
    def send(n, step, i, status, kind, ts_s, attempt=0):
        return {"dev": n % 3, "step": step, "step_i": i, "eid": eid(n), "seq": n, "event": "refresh", "attempt": attempt, "ts": int(ts_s * 1000),
                "status": status, "kind": kind, "ms": 40, "planned_ms": int(ts_s * 1000)}
    sends = [send(1, "s1", 0, 202, "accepted", T0 + 1), send(2, "s1", 0, 202, "accepted", T0 + 2), send(3, "s1", 0, 202, "accepted", T0 + 3),
             send(4, "s1", 0, 503, "http5xx", T0 + 4), send(5, "s1", 0, 202, "accepted", T0 + 5),
             send(6, "s2", 1, 202, "accepted", T0 + 11), send(7, "s2", 1, 202, "accepted", T0 + 95), send(8, "s2", 1, 202, "accepted", T0 + 12),
             send(9, "s2", 1, 0, "other", T0 + 13), send(10, "s2", 1, 202, "accepted", T0 + 14),
             {**send(99, "s2", 1, 202, "accepted", T0 + 15), "eid": "bbbbbbbb-0000-4e00-8000-000000000001"}]      # 다른 실행의 전송: 무시돼야 한다
    def job(n, status, recv, **kw):
        return {"id": f"j{n}", "machine_id": 7, "event_id": eid(n).replace("-", ""), "session_id": "s", "seq": n, "event": "refresh", "received_at": iso(recv),
                "published_at": iso(recv + 0.05), "status": status, "worker_id": kw.pop("worker", None), "claim_attempt": 1, "publish_attempts": 1,
                "result_id": None, "last_error": None, "updated_at": iso(recv), **kw}
    def res(n, recv, lat, applied=1):
        return {"id": n, "job_id": f"j{n}", "machine_id": 7, "event_id": eid(n), "seq": n, "verdict": "permitted", "applied": applied, "elapsed_ms": 700,
                "received_at": iso(recv), "created_at": iso(recv + lat)}
    jobs = [job(1, "done", T0 + 1, worker="w1"), job(2, "done", T0 + 2, worker="w2"), job(3, "done", T0 + 3, worker="w1"),
            job(5, "rejected", T0 + 5), job(6, "queued", T0 + 11), job(7, "processing", T0 + 95), job(8, "stale", T0 + 12, worker="w2"),
            job(9, "done", T0 + 13, worker="w1"), job(10, "dead_lettered", T0 + 14),
            {**job(98, "done", T0 + 1), "event_id": "bbbbbbbb00004e0080000000000000f"}]
    results = [res(1, T0 + 1, 2.0), res(2, T0 + 2, 10.0), res(3, T0 + 3, 5.0), res(8, T0 + 12, 3.0, applied=0), res(9, T0 + 13, 6.0)]
    # 완료 시각: e1 T0+3 (s1), e2 T0+12 (s2), e3 T0+8 (s1), e8 T0+15 (s2), e9 T0+19 (s2)
    return meta, sends, {"jobs": jobs, "results": results}, eid


def test_build_report_counts_every_outcome_exactly():
    meta, sends, rows, eid = build_fixture()
    # e2 를 late 로 만든다: 지연 12 s
    rows["results"][1]["created_at"] = iso(T0 + 2 + 12.0)
    report, attempts, backlog = pc.build_report([meta], sends, rows, eval_at=T0 + 100, on_time_s=10.0, drain_wait=5.0)
    s1, s2 = report["steps"]
    assert (s1["offered"], s1["on_time"], s1["late"], s1["request_failed"], s1["processing_failed"], s1["unfinished"]) == (5, 2, 1, 1, 1, 0)
    assert (s2["offered"], s2["on_time"], s2["late"], s2["request_failed"], s2["processing_failed"], s2["unfinished"]) == (5, 2, 0, 0, 1, 2)
    assert s1["on_time_rate"] == pytest.approx(0.4) and s1["completion_rate"] == pytest.approx(0.6) and s1["request_fail_rate"] == pytest.approx(0.2)
    assert s1["processing_fail_rate"] == pytest.approx(0.25)
    assert s2["censored_young"] == 1                                              # e7: 평가 5 s 전에 접수
    assert report["attempts"] == 10                                              # 다른 실행(bbbbbbbb)의 전송 제외
    assert report["accepted_despite_client_failure"] == 1                        # e9: k6 는 시간 초과로 봤지만 서버는 접수·판정
    o = report["overall"]
    assert o["offered"] == 10 and o["on_time"] == 4 and o["late"] == 1 and o["request_failed"] == 1 and o["processing_failed"] == 2 and o["unfinished"] == 2
    assert report["job_status_counts"] == {"done": 4, "rejected": 1, "queued": 1, "processing": 1, "stale": 1, "dead_lettered": 1}
    assert report["stale_judged"] == 1 and report["worker_counts"] == {"w1": 3, "w2": 2}
    # 분위수: s1 판정 3건 지연 [2, 12, 5]
    lat = s1["latency_s"]
    assert lat["n"] == 3 and lat["p50"] == pytest.approx(5.0) and lat["p95"] == pytest.approx(11.3) and lat["p99_reliable"] is False
    assert report["eval_too_early"] is False
    assert backlog[-1]["accepted_cum"] == 9 and all(b["in_system"] >= 0 for b in backlog)
    assert max(b["in_system"] for b in backlog) >= 3


def test_throughput_counts_completions_by_completion_time_not_arrival():
    meta, sends, rows, _ = build_fixture()
    report, _, _ = pc.build_report([meta], sends, rows, eval_at=T0 + 100, drain_wait=5.0)
    # 완료 시각: e1 T0+3, e3 T0+8 (s1) / e2 T0+12, e8 T0+15, e9 T0+19 (s2)
    assert report["steps"][0]["completed_in_window"] == 2 and report["steps"][0]["throughput_per_s"] == pytest.approx(0.2)
    assert report["steps"][1]["completed_in_window"] == 3 and report["steps"][1]["throughput_per_s"] == pytest.approx(0.3)


def test_step_windows_use_the_real_scenario_start_when_available():
    meta, sends, rows, _ = build_fixture()
    starts = [{"dev": 0, "scenario_start": int((T0 + 4) * 1000), "start_delay_ms": 40}, {"dev": 1, "scenario_start": int((T0 + 4.1) * 1000), "start_delay_ms": 30}]
    rep, _, _ = pc.build_report([meta], sends, rows, eval_at=T0 + 100, drain_wait=5.0, starts=starts)
    assert rep["t0_source"] == "P2START" and rep["t0_epoch"] == pytest.approx(T0 + 4) and rep["start_delay_ms"] == 40
    base, _, _ = pc.build_report([meta], sends, rows, eval_at=T0 + 100, drain_wait=5.0)
    assert base["t0_source"] == "P2META" and base["t0_epoch"] == pytest.approx(T0)
    # 창이 4 s 밀리면 완료 시각 T0+3(e1)은 창 밖이 되고, T0+8(e3)·T0+12(e2)가 첫 단계 [T0+4, T0+14), T0+15(e8)·T0+19(e9)가 둘째 단계가 된다
    assert base["steps"][0]["completed_in_window"] == 2 and base["steps"][1]["completed_in_window"] == 3
    assert rep["steps"][0]["completed_in_window"] == 2 and rep["steps"][1]["completed_in_window"] == 2
    text = ('level=info msg="P2START {\\"dev\\":0,\\"scenario_start\\":5,\\"start_delay_ms\\":1}"\n'
            'P2START {"dev":1,"scenario_start":6}\n' 'noise\n')
    assert [x["dev"] for x in pc.parse_starts(text)] == [0, 1]


def test_eval_too_early_flag_and_unfinished_not_hidden():
    meta, sends, rows, _ = build_fixture()
    report, _, _ = pc.build_report([meta], sends, rows, eval_at=T0 + 96, drain_wait=120.0)
    assert report["eval_too_early"] is True
    assert report["overall"]["unfinished"] == 2                                    # 평가가 일러도 unfinished 는 on_time 이 아니다
    assert report["overall"]["on_time_rate"] == pytest.approx(0.5)      # e2 의 지연이 정확히 10.0 s 라 경계값으로 제시간


def test_request_failed_when_no_job_even_if_client_retried():
    meta, sends, rows, eid = build_fixture()
    sends.append({**sends[3], "attempt": 1, "status": 503, "ts": sends[3]["ts"] + 2000})       # e4 재시도도 실패
    report, attempts, _ = pc.build_report([meta], sends, rows, eval_at=T0 + 100, drain_wait=5.0)
    e4 = [a for a in attempts if a["eid"] == eid(4).replace("-", "")][0]
    assert e4["attempts"] == 2 and e4["outcome"] == "request_failed"
    assert report["attempts"] == 10                                                # 재시도는 시도를 늘리지 않는다


def test_parse_sends_raw_and_text_log_formats():
    meta, sends, _, _ = build_fixture()
    raw = f"P2META {json.dumps(meta)}\n" + "\n".join(f"P2SEND {json.dumps(s)}" for s in sends[:2]) + "\nsome other log line\n"
    metas, got = pc.parse_sends(raw)
    assert len(metas) == 1 and len(got) == 2 and got[0]["eid"] == sends[0]["eid"]
    esc = json.dumps(sends[0]).replace('"', '\\"')
    text = f'time="2026-11-04T10:00:00Z" level=info msg="P2SEND {esc}" source=console\n'
    _, got2 = pc.parse_sends(text)
    assert len(got2) == 1 and got2[0]["seq"] == sends[0]["seq"]


def test_outputs_json_csv_and_secret_guard(tmp_path, monkeypatch):
    meta, sends, rows, _ = build_fixture()
    report, attempts, backlog = pc.build_report([meta], sends, rows, eval_at=T0 + 100, drain_wait=5.0)
    out = pc.save_outputs(tmp_path / "o", report, attempts, backlog, forbid_env=("DEVICE_KEYS",))
    data = json.loads((out / "p2-report.json").read_text(encoding="utf-8"))
    assert data["overall"]["offered"] == 10
    import csv as _csv
    steps = list(_csv.DictReader(open(out / "p2-steps.csv", encoding="utf-8")))
    assert [r["step"] for r in steps] == ["s1", "s2"] and "lat_p95" in steps[0] and "on_time_rate" in steps[0]
    assert len(list(_csv.DictReader(open(out / "p2-attempts.csv", encoding="utf-8")))) == 10
    assert (out / "p2-backlog.csv").exists() and (out / "p2-workers.csv").exists()
    monkeypatch.setenv("DEVICE_KEYS", "w1,SECRETKEY-123456")
    report["leak"] = "x SECRETKEY-123456 y"
    with pytest.raises(SystemExit):
        pc.save_outputs(tmp_path / "o2", report, attempts, backlog, forbid_env=("DEVICE_KEYS",))
    assert not (tmp_path / "o2").exists() or not (tmp_path / "o2" / "p2-report.json").exists()    # 쓰기 전에 중단


def test_kubectl_source_runs_only_select_via_fixed_script():
    calls = []
    def fake(cmd, input=None):
        calls.append((cmd, input))
        return json.dumps({"jobs": [], "results": []}) + "\n"
    rows = pc.load_rows("kubectl", T0, T0 + 60, [7, 8], runner=fake)
    assert rows == {"jobs": [], "results": []}
    cmd, script = calls[0]
    assert cmd[:6] == ["kubectl", "exec", "-i", "deploy/backend", "-n", "parking-guardian"] and script == pc.POD_SCRIPT.encode("utf-8")
    arg = json.loads(cmd[-1])
    assert set(arg["queries"]) == {"jobs", "results"}
    for sql in arg["queries"].values():
        pc.assert_select_only(sql)
        assert "cast(:lo as timestamptz)" in sql
    assert "machine_id in (7,8)" in arg["queries"]["jobs"]


def test_sqlite_source_is_opened_read_only_and_unchanged(tmp_path):
    import sqlite3
    db = tmp_path / "x.sqlite"
    con = sqlite3.connect(db)
    con.executescript("create table detection_jobs (id text, machine_id int, event_id text, session_id text, seq int, event text, received_at text, published_at text, "
                      "status text, worker_id text, claim_attempt int, publish_attempts int, result_id int, last_error text, updated_at text);"
                      "create table detection_results (id int, job_id text, machine_id int, event_id text, seq int, verdict text, applied int, elapsed_ms int, received_at text, created_at text);")
    con.execute("insert into detection_jobs values ('j',7,'abc','s',1,'arrived',?, null,'queued',null,0,0,null,null,null)", (iso(T0),))
    con.commit(); con.close()
    before = db.read_bytes()
    rows = pc.fetch_sqlite(str(db), T0 - 10, T0 + 10, [7])
    assert len(rows["jobs"]) == 1 and rows["results"] == []
    assert db.read_bytes() == before
    assert "mode=ro" in Path(pc.__file__).read_text(encoding="utf-8")
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("insert into detection_jobs (id) values ('x')")


def test_sqs_series_merges_metrics_per_minute():
    def fake(cmd, input=None):
        metric = cmd[cmd.index("--metric-name") + 1]
        val = {"ApproximateNumberOfMessagesVisible": 5, "ApproximateNumberOfMessagesNotVisible": 2, "ApproximateNumberOfMessagesDelayed": 0, "ApproximateAgeOfOldestMessage": 33}[metric]
        return json.dumps({"Datapoints": [{"Timestamp": "2026-11-04T10:01:00+00:00", "Maximum": val}, {"Timestamp": "2026-11-04T10:00:00+00:00", "Maximum": val - 1}]})
    rows = pc.sqs_series("q.fifo", T0, T0 + 120, runner=fake)
    assert len(rows) == 2 and rows[0]["visible"] == 4 and rows[1]["oldest_age_s"] == 33 and rows[1]["queue"] == "q.fifo"
    assert all(set(r) >= {"visible", "not_visible", "delayed", "oldest_age_s"} for r in rows)


# ============================================================================================ Phase 1 k6 점 → 같은 지표
def test_p1_points_report_and_compare(tmp_path):
    lines = []
    def point(step, status, ms, kind="photo", phase=None):
        tags = {"kind": kind, "step": step, "status": str(status)}
        if phase:
            tags["phase"] = phase
        lines.append(json.dumps({"type": "Point", "metric": "http_req_duration", "data": {"time": "2026-11-04T10:00:00Z", "value": ms, "tags": tags}}))
    for _ in range(8):
        point("0.2C", 200, 900)
    point("0.2C", 200, 11500)           # late
    point("0.2C", 503, 20)              # request_failed
    point("0.2C", 200, 700, kind="api")           # 제외(사진 아님)
    point("0.2C", 200, 700, phase="warmup")       # 제외(워밍업)
    lines.append(json.dumps({"type": "Metric", "metric": "http_req_duration"}))
    pts = tmp_path / "points.json"
    pts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rep = p1_points.build_report(p1_points.read_points(pts), p1_points.parse_steps("0.2C:10"))
    row = rep["steps"][0]
    assert row["offered"] == 10 and row["on_time"] == 8 and row["late"] == 1 and row["request_failed"] == 1
    assert row["on_time_rate"] == pytest.approx(0.8) and row["request_fail_rate"] == pytest.approx(0.1) and row["throughput_per_s"] == pytest.approx(0.9)
    assert row["latency_s"]["p50"] == pytest.approx(0.9)
    assert row["processing_fail_rate"] == 0.0 and row["unfinished"] == 0
    meta, sends, rows, _ = build_fixture()
    p2rep, _, _ = pc.build_report([meta], sends, rows, eval_at=T0 + 100, drain_wait=5.0)
    p2rep["steps"][0]["step"] = "0.2C"
    cmp_rows = p1_points.compare(rep, p2rep)
    first = [r for r in cmp_rows if r["step"] == "0.2C"][0]
    assert first["p1_on_time_rate"] == pytest.approx(0.8) and first["p2_on_time_rate"] == pytest.approx(0.6)
    assert any(r["step"] == "s2" and r["p1_on_time_rate"] is None for r in cmp_rows)      # 한쪽에만 있는 단계는 빈 칸


# ============================================================================================ p2_watch 중단 규칙
def mkpod(name, app, phase="Running", ready=True, restarts=0, deleting=False):
    return {"name": name, "app": app, "phase": phase, "node": "n1", "ready": ready, "restarts": restarts, "created": None, "deleting": deleting, "unschedulable": phase == "Pending"}


def sample(ts, workers=1, pending_total=0, last_result_age=5, **kw):
    pods = [mkpod("backend-1", "backend"), mkpod("backend-2", "backend"), mkpod("relay-1", "relay"), mkpod("frontend-1", "frontend")]
    pods += [mkpod(f"worker-{i}", "worker") for i in range(workers)]
    base = {"ts": ts, "pods": kw.pop("pods", pods), "keda": {"ready": "True", "active": "True", "paused": None, "min": 1, "max": 3},
            "apps": {a: ("Synced", "Healthy") for a in ("root", "parking-guardian", "keda")}, "sqs": {"visible": 0, "not_visible": 0, "delayed": 0},
            "dlq": {"visible": 0, "not_visible": 0, "delayed": 0}, "sqs_age_s": 0.0, "secret": {"last_rotated": 111.0, "next_rotation": ts + 99999, "rotation_enabled": True},
            "db": {"pending_total": pending_total, "oldest_pending_publish_age_s": 0, "last_result_ts": ts - last_result_age}}
    base.update(kw)
    return base


def codes(alerts, level=None):
    return {a["code"] for a in alerts if level is None or a["level"] == level}


def test_healthy_sample_has_no_alerts():
    w = pw.Watcher(baseline_rotated=111.0)
    assert w.step(sample(1000)) == []


def test_overload_backlog_alone_is_not_an_abort():
    w = pw.Watcher(baseline_rotated=111.0)
    s = sample(1000, pending_total=300, last_result_age=2, sqs={"visible": 280, "not_visible": 20, "delayed": 0}, sqs_age_s=200.0)
    out = w.step(s)
    assert codes(out, "ABORT") == set() and "SQS_AGE" in codes(out, "WARN")


def test_worker_stall_aborts_only_after_threshold():
    w = pw.Watcher(pw.Config(stall_after_s=60), baseline_rotated=111.0)
    assert codes(w.step(sample(1000, pending_total=5, last_result_age=30)), "ABORT") == set()
    assert "WORKER_STALL" in codes(w.step(sample(1040, pending_total=5, last_result_age=70)), "ABORT")


def test_worker_idle_without_work_is_fine():
    w = pw.Watcher(baseline_rotated=111.0)
    assert w.step(sample(1000, pending_total=0, last_result_age=500)) == []


def test_keda_no_scaleup_when_work_waits_and_no_worker():
    w = pw.Watcher(pw.Config(scaleup_wait_s=90), baseline_rotated=111.0)
    s = lambda t: sample(t, workers=0, pending_total=4, last_result_age=500)  # noqa: E731
    assert codes(w.step(s(1000)), "ABORT") == set()
    assert codes(w.step(s(1060)), "ABORT") == set()
    assert "KEDA_NO_SCALEUP" in codes(w.step(s(1100)), "ABORT")
    w2 = pw.Watcher(pw.Config(scaleup_wait_s=90), baseline_rotated=111.0)
    w2.step(s(1000))
    assert codes(w2.step(sample(1050, workers=1, pending_total=4, last_result_age=1)), "ABORT") == set()    # 올라오면 해소


def test_dlq_message_aborts():
    w = pw.Watcher(baseline_rotated=111.0)
    assert "DLQ_MESSAGE" in codes(w.step(sample(1000, dlq={"visible": 1, "not_visible": 0, "delayed": 0})), "ABORT")


def test_pending_pod_aborts_after_hold_time():
    w = pw.Watcher(pw.Config(pending_pod_s=60), baseline_rotated=111.0)
    pods = lambda: sample(0)["pods"] + [mkpod("worker-9", "worker", phase="Pending", ready=False)]  # noqa: E731
    assert codes(w.step(sample(1000, pods=pods())), "ABORT") == set()
    assert codes(w.step(sample(1030, pods=pods())), "ABORT") == set()
    assert "POD_PENDING" in codes(w.step(sample(1070, pods=pods())), "ABORT")
    assert codes(w.step(sample(1080)), "ABORT") == set()                       # 해소되면 조건 초기화


def test_restart_increase_aborts_and_baseline_is_first_sample():
    w = pw.Watcher(baseline_rotated=111.0)
    assert w.step(sample(1000)) == []
    pods = sample(0)["pods"]
    pods[0] = mkpod("backend-1", "backend", restarts=1)
    assert "POD_RESTART" in codes(w.step(sample(1015, pods=pods)), "ABORT")


def test_argo_outofsync_and_degraded():
    w = pw.Watcher(baseline_rotated=111.0)
    out = w.step(sample(1000, apps={"root": ("Synced", "Healthy"), "parking-guardian": ("OutOfSync", "Healthy")}))
    assert "ARGO_OUTOFSYNC" in codes(out, "ABORT")
    w2 = pw.Watcher(pw.Config(allow_outofsync=("parking-guardian",)), baseline_rotated=111.0)
    assert w2.step(sample(1000, apps={"root": ("Synced", "Healthy"), "parking-guardian": ("OutOfSync", "Healthy")})) == []
    assert "ARGO_UNHEALTHY" in codes(w2.step(sample(1015, apps={"parking-guardian": ("OutOfSync", "Degraded")})), "ABORT")


def test_keda_not_ready_and_pause_mismatch():
    w = pw.Watcher(pw.Config(expect_workers=2), baseline_rotated=111.0)
    out = w.step(sample(1000, keda={"ready": "False", "active": "False", "paused": "1", "min": 1, "max": 3}))
    assert "KEDA_NOT_READY" in codes(out, "ABORT") and "KEDA_PAUSE_MISMATCH" in codes(out, "WARN")
    w2 = pw.Watcher(pw.Config(expect_workers=2), baseline_rotated=111.0)
    assert w2.step(sample(1000, keda={"ready": "True", "active": "True", "paused": "2", "min": 1, "max": 3})) == []


def test_relay_stall_and_rds_rotation():
    w = pw.Watcher(baseline_rotated=111.0)
    s = sample(1000)
    s["db"]["oldest_pending_publish_age_s"] = 45
    assert "RELAY_STALL" in codes(w.step(s), "ABORT")
    w2 = pw.Watcher(baseline_rotated=111.0)
    assert "RDS_ROTATED" in codes(w2.step(sample(1000, secret={"last_rotated": 222.0, "next_rotation": 9e9, "rotation_enabled": True})), "ABORT")


def test_sqs_age_abort_threshold_and_source_errors():
    w = pw.Watcher(baseline_rotated=111.0)
    assert "SQS_OLD_MESSAGE" in codes(w.step(sample(1000, sqs_age_s=901.0)), "ABORT")
    w2 = pw.Watcher(pw.Config(source_error_abort=3), baseline_rotated=111.0)
    e = lambda t: sample(t, errors=["db: Timeout"])  # noqa: E731
    assert codes(w2.step(e(1000)), "ABORT") == set() and codes(w2.step(e(1015)), "ABORT") == set()
    assert "SOURCE_ERROR" in codes(w2.step(e(1030)), "ABORT")
    assert w2.step(sample(1045)) == [] and w2.errors == 0


def test_preflight_rotation_window():
    soon = {"rotation_enabled": True, "next_rotation": 1000 + 2 * 3600}
    assert pw.preflight_rotation(soon, 1000, window_h=4)
    assert not pw.preflight_rotation(soon, 1000, window_h=1)
    assert not pw.preflight_rotation({"rotation_enabled": False, "next_rotation": 1001}, 1000, window_h=4)


def node(name, cpu="1930m"):
    return {"metadata": {"name": name}, "status": {"allocatable": {"cpu": cpu}}}


def kpod(name, node_name, cpu_m, phase="Running"):
    return {"metadata": {"name": name}, "spec": {"nodeName": node_name, "containers": [{"resources": {"requests": {"cpu": f"{cpu_m}m"}}}]}, "status": {"phase": phase}}


def test_cpu_slots_match_the_documented_numbers():
    nodes = json.dumps({"items": [node("a"), node("b"), node("c")]})
    coexist = json.dumps({"items": [kpod("inf1", "a", 1000), kpod("o1", "a", 740), kpod("inf2", "b", 1000), kpod("o2", "b", 655), kpod("o3", "c", 840),
                                    kpod("done", "c", 500, phase="Succeeded")]})
    r = pw.cpu_slots(nodes, coexist)
    assert r["free_m"] == {"a": 190, "b": 275, "c": 1090} and r["slots"] == 1
    comparison = json.dumps({"items": [kpod("o1", "a", 740), kpod("o2", "b", 655), kpod("o3", "c", 840)]})
    r2 = pw.cpu_slots(nodes, comparison)
    assert r2["free_m"] == {"a": 1190, "b": 1275, "c": 1090} and r2["slots"] == 3


def test_scale_events_from_timeline():
    def sm(ts, n, ready, active):
        s = sample(ts, workers=n)
        for p in s["pods"]:
            if p["app"] == "worker":
                p["ready"] = ready >= 1
        s["pods"] = [p for p in s["pods"]]
        s["keda"]["active"] = active
        return s
    ev = pw.scale_events([sm(0, 0, 0, "False"), sm(15, 0, 0, "True"), sm(30, 1, 0, "True"), sm(45, 1, 1, "True"), sm(400, 0, 0, "False")])
    kinds = [(e["kind"], e["from"], e["to"]) for e in ev]
    assert ("keda_active", "False", "True") in kinds and ("worker_replicas", 0, 1) in kinds and ("worker_ready", 0, 1) in kinds
    assert ("worker_replicas", 1, 0) in kinds and ("keda_active", "True", "False") in kinds


def test_parsers_read_kubectl_and_aws_json():
    pods = pw.parse_pods(json.dumps({"items": [{"metadata": {"name": "worker-x", "labels": {"app.kubernetes.io/name": "worker"}, "creationTimestamp": "t"},
                                               "spec": {"nodeName": "n"}, "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "reason": "Unschedulable"}]}}]}))
    assert pods[0]["app"] == "worker" and pods[0]["unschedulable"] and pods[0]["ready"] is False
    so = pw.parse_scaledobject(json.dumps({"metadata": {"annotations": {"autoscaling.keda.sh/paused-replicas": "2"}}, "spec": {"minReplicaCount": 1, "maxReplicaCount": 3},
                                           "status": {"conditions": [{"type": "Ready", "status": "True"}, {"type": "Active", "status": "False"}]}}))
    assert so == {"ready": "True", "active": "False", "paused": "2", "min": 1, "max": 3}
    assert pw.parse_sqs_attrs(json.dumps({"Attributes": {"ApproximateNumberOfMessages": "3", "ApproximateNumberOfMessagesNotVisible": "1"}})) == {"visible": 3, "not_visible": 1, "delayed": 0}
    sec = pw.parse_secret(json.dumps({"LastRotatedDate": "2026-11-04T04:23:00+00:00", "NextRotationDate": "2026-11-11T23:59:00+00:00", "RotationEnabled": True}))
    assert sec["rotation_enabled"] is True and sec["next_rotation"] > sec["last_rotated"]
    assert pw.parse_apps(json.dumps({"items": [{"metadata": {"name": "root"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}]})) == {"root": ("Synced", "Healthy")}


def test_collect_sample_uses_only_read_only_commands_and_never_reads_secret_values():
    calls = []
    def inner(cmd, input=None):
        calls.append(list(cmd))
        if cmd[:3] == ["kubectl", "get", "pods"]:
            return json.dumps({"items": []})
        if cmd[:3] == ["kubectl", "get", "scaledobject"]:
            return json.dumps({"metadata": {}, "spec": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}})
        if cmd[:3] == ["kubectl", "get", "app"]:
            return json.dumps({"items": []})
        if cmd[:2] == ["aws", "sqs"]:
            return json.dumps({"Attributes": {}})
        if cmd[:2] == ["aws", "cloudwatch"]:
            return json.dumps({"Datapoints": []})
        if cmd[:2] == ["aws", "secretsmanager"]:
            return json.dumps({"LastRotatedDate": "2026-11-04T04:23:00+00:00", "RotationEnabled": True})
        if cmd[:3] == ["kubectl", "top", "pods"]:
            return "backend-1   3m   100Mi\n"
        if cmd[:2] == ["kubectl", "exec"]:
            return json.dumps({"pending": [], "last": [{"last_result": None}]}) + "\n"
        raise AssertionError(cmd)
    cfg = pw.Config()
    cfg.queue_url, cfg.dlq_url, cfg.secret_id, cfg.queue_name = "https://sqs/q.fifo", "https://sqs/dlq.fifo", "rds-secret-id", "q.fifo"
    s = pw.collect_sample(pw.readonly_runner(inner), cfg, now=T0)
    assert "errors" not in s and s["db"]["pending_total"] == 0
    flat = [" ".join(c) for c in calls]
    assert not any("get-secret-value" in c for c in flat)
    assert any("describe-secret" in c for c in flat)


@pytest.mark.parametrize("cmd", [["kubectl", "scale", "deploy/worker", "--replicas=0"], ["kubectl", "patch", "app", "root"], ["kubectl", "delete", "pod", "x"],
                                 ["kubectl", "apply", "-f", "x.yaml"], ["kubectl", "annotate", "scaledobject", "worker", "a=b"], ["kubectl", "rollout", "restart", "deploy/x"],
                                 ["aws", "sqs", "send-message"], ["aws", "sqs", "purge-queue"], ["aws", "sqs", "delete-message"],
                                 ["aws", "secretsmanager", "get-secret-value"], ["aws", "secretsmanager", "rotate-secret"], ["aws", "secretsmanager", "list-secrets"],
                                 ["kubectl", "get", "secret", "-n", "parking-guardian", "-o", "yaml"], ["kubectl", "get", "secrets", "-A"], ["kubectl", "logs", "deploy/backend"], ["aws", "rds", "stop-db-instance"],
                                 ["aws", "ec2", "terminate-instances"], ["terraform", "apply"], ["psql"]])
def test_readonly_runner_blocks_writes(cmd):
    run = pw.readonly_runner(lambda c, input=None: "{}")
    with pytest.raises(PermissionError):
        run(cmd)


def test_readonly_runner_allows_reads_and_only_the_fixed_pod_script():
    run = pw.readonly_runner(lambda c, input=None: "{}")
    for cmd in (["kubectl", "get", "pods", "-o", "json"], ["aws", "sqs", "get-queue-attributes", "--queue-url", "u"], ["aws", "cloudwatch", "get-metric-statistics"],
                ["aws", "secretsmanager", "describe-secret", "--secret-id", "s"]):
        assert run(cmd) == "{}"
    assert run(["kubectl", "exec", "-i", "deploy/backend", "-n", "parking-guardian", "--", "python", "-", "{}"], input=pc.POD_SCRIPT.encode()) == "{}"
    with pytest.raises(PermissionError):
        run(["kubectl", "exec", "-i", "deploy/backend", "--", "python", "-", "{}"], input=b"import os; os.system('rm -rf /')")
    with pytest.raises(PermissionError):
        run(["kubectl", "exec", "deploy/backend", "--", "python", "-c", "print(1)"])


def test_watch_source_has_no_write_verbs_as_command_literals():
    import ast
    tree = ast.parse(Path(pw.__file__).read_text(encoding="utf-8"))
    seen = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) and node.elts[0].value in ("kubectl", "aws"):
            consts = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            seen.append(consts)
            if consts[0] == "kubectl":
                assert consts[1] in ("get", "exec", "top"), consts
                assert consts[2] not in ("secret", "secrets"), consts
            else:
                assert (consts[1], consts[2]) in pw.AWS_ALLOWED, consts
    assert len(seen) >= 8                                                       # 실제로 명령을 만든다는 것도 확인
    src = Path(pw.__file__).read_text(encoding="utf-8")
    for bad in ("get-secret-value", "send-message", "purge-queue", "delete-message", "rotate-secret", "terminate", "scale", "annotate"):
        assert not re.search("[\"']" + bad + "[\"']", src), bad


def test_collect_and_watch_never_touch_device_keys_or_passwords():
    for mod in (pc, pw, p1_points, pm):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", src, flags=re.S)
        assert not re.search(r"os\.environ(\.get)?\(?\[?[\"'](DEVICE_KEYS|ADMIN_PASSWORD|X-Device-Key)", code)
        assert "X-Device-Key" not in code


# ============================================================================================ p3 (가짜 클러스터)
import p3  # noqa: E402

AUTO = {"prune": True, "selfHeal": True}


def mkapp(name, auto=True, sync="Synced", health="Healthy"):
    spec = {"project": "default", "source": {"path": name}}
    if auto:
        spec["syncPolicy"] = {"automated": dict(AUTO)}
    else:
        spec["syncPolicy"] = {}
    return {"metadata": {"name": name, "uid": f"uid-{name}", "managedFields": []}, "spec": spec,
            "status": {"sync": {"status": sync, "revision": "abcdef1234"}, "health": {"status": health}}}


def mkpod2(name, app, uid=None, phase="Running", ready=True, restarts=0, node="n1", job=False):
    labels = {"app.kubernetes.io/name": app}
    return {"metadata": {"name": name, "uid": uid or f"uid-{name}", "labels": labels}, "spec": {"nodeName": node},
            "status": {"phase": phase, "containerStatuses": [{"ready": ready, "restartCount": restarts}]}}


class FakeCluster:
    """p3.py 가 쓰는 kubectl 동사(get/patch/annotate/apply)만 흉내 낸다. 변경 호출은 calls 에 남는다."""

    def __init__(self, with_keda=True):
        names = ["root", "parking-guardian", "platform", "monitoring", "monitoring-config"] + (["keda"] if with_keda else [])
        self.apps = {n: mkapp(n) for n in names}
        self.hpa = {h: {"metadata": {"name": h}, "spec": {"minReplicas": 2, "maxReplicas": 3}, "status": {"currentReplicas": 2, "desiredReplicas": 2}} for h in ("backend", "inference")}
        tmpl = lambda n: {"template": {"spec": {"containers": [{"name": n, "image": f"img-{n}"}]}}}  # noqa: E731
        self.deploy = {d: {"metadata": {"name": d}, "spec": {"replicas": r, **tmpl(d)}} for d, r in (("backend", 2), ("inference", 2), ("frontend", 1), ("relay", 1), ("worker", 0))}
        self.pods = ([mkpod2(f"backend-{i}", "backend", node=f"n{i}") for i in (1, 2)] + [mkpod2(f"inference-{i}", "inference", node=f"n{i}") for i in (1, 2)] +
                     [mkpod2("frontend-1", "frontend"), mkpod2("relay-1", "relay"), mkpod2("backend-migrate-x", "backend-migrate", phase="Succeeded", ready=False)])
        self.so = {"worker": {"metadata": {"name": "worker", "annotations": {}}, "spec": {"minReplicaCount": 0, "maxReplicaCount": 1},
                              "status": {"conditions": [{"type": "Ready", "status": "True"}, {"type": "Active", "status": "False"}]}}}
        self.calls = []

    # --- 시나리오 조작 ---
    def make_comparison(self):
        for d in ("inference",):
            self.deploy.pop(d, None); self.hpa.pop(d, None)
        self.pods = [p for p in self.pods if p["metadata"]["labels"]["app.kubernetes.io/name"] != "inference"]
        self.so["worker"]["spec"].update(minReplicaCount=1, maxReplicaCount=3)
        self.set_workers(1)

    def set_workers(self, n):
        self.pods = [p for p in self.pods if p["metadata"]["labels"]["app.kubernetes.io/name"] != "worker"]
        self.pods += [mkpod2(f"worker-{i}", "worker") for i in range(n)]
        self.deploy["worker"]["spec"]["replicas"] = n

    # --- kubectl ---
    def kube(self, *args, data=None):
        self.calls.append(args)
        a = list(args)
        ns = None
        if a[0] == "-n":
            ns, a = a[1], a[2:]
        verb = a[0]
        if verb == "get":
            kind = a[1]
            name = a[2] if len(a) > 2 and not a[2].startswith("-") else None
            if kind == "endpointslices":
                svc = a[a.index("-l") + 1].split("=")[1]
                ready = len([p for p in self.pods if p["metadata"]["labels"]["app.kubernetes.io/name"] == svc and p["status"]["phase"] == "Running"])
                return json.dumps({"items": [{"endpoints": [{"conditions": {"ready": True}}] * ready}]})
            store = {"app": self.apps, "hpa": self.hpa, "deploy": self.deploy, "scaledobject": self.so}.get(kind)
            if kind == "pods":
                return json.dumps({"items": self.pods})
            if name:
                if name not in store:
                    raise RuntimeError(f'kubectl get {kind} {name}\nError from server (NotFound): {kind} "{name}" not found')
                return json.dumps(store[name])
            return json.dumps({"items": list(store.values())})
        if verb == "patch":
            kind, name = a[1], a[2]
            body = json.loads(a[a.index("-p") + 1])
            if kind == "app":
                sp = self.apps[name]["spec"].setdefault("syncPolicy", {})
                if "automated" in body["spec"]["syncPolicy"]:
                    v = body["spec"]["syncPolicy"]["automated"]
                    sp.pop("automated", None) if v is None else sp.__setitem__("automated", v)
            elif kind == "hpa":
                self.hpa[name]["spec"].update(body["spec"])
            return ""
        if verb == "annotate":
            key = a[3]
            ann = self.so["worker"]["metadata"].setdefault("annotations", {})
            if key.endswith("-"):
                ann.pop(key[:-1], None)
                self.set_workers(self.so["worker"]["spec"]["minReplicaCount"])
            else:
                k, v = key.split("=", 1)
                ann[k] = v
                self.set_workers(int(v))                                  # KEDA 가 paused-replicas 를 따른다
            return ""
        if verb == "apply":
            for n in ("root", "parking-guardian"):
                self.apps[n]["spec"].setdefault("syncPolicy", {})["automated"] = dict(AUTO)
            for h in self.hpa.values():                                     # Argo 가 HPA 를 Git 의 값(2/3)으로 되돌린다
                h["spec"].update(minReplicas=2, maxReplicas=3)
            return "application.argoproj.io/root configured"
        raise AssertionError(args)

    def mutations(self):
        return [c for c in self.calls if (c[2] if c[0] == "-n" else c[0]) in ("patch", "annotate", "apply")]


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    fc = FakeCluster()
    clock = {"t": 1000.0}
    monkeypatch.setattr(p3, "kube", fc.kube)
    monkeypatch.setattr(p3, "time", types.SimpleNamespace(sleep=lambda s: clock.__setitem__("t", clock["t"] + s), time=lambda: clock["t"]))
    monkeypatch.setattr(p3, "EKS_DOCS", tmp_path / "snaps")
    monkeypatch.setattr(p3, "alb_backend_healthy", lambda: 2)
    return fc


def run_check(fn, *a, **k):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = fn(*a, **k)
    return ok, buf.getvalue()


def test_p3_backup_of_the_original_exists_and_is_unchanged_in_content():
    orig = (HERE / "p3.py.bak-before-p2").read_text(encoding="utf-8")
    assert "APPS = [" in orig and "Application 5개" in orig                    # 원본(5개 하드코딩)이 그대로 있다
    assert "p2-pause" not in orig


def test_p3_phase1_baseline_works_with_six_applications(cluster):
    ok, out = run_check(p3.check, "baseline")
    assert ok, out
    assert "Application 6개 모두 Synced/Healthy" in out


def test_p3_old_five_app_snapshot_still_restores_and_reports_new_app_as_info(cluster, tmp_path):
    snap = tmp_path / "old-snap"
    snap.mkdir()
    st = p3.read_state(["root", "parking-guardian", "platform", "monitoring", "monitoring-config"])
    for a, o in st["apps"].items():
        (snap / f"app-{a}.json").write_text(json.dumps(p3.strip(o)), encoding="utf-8")
    for h, o in st["hpa"].items():
        (snap / f"hpa-{h}.json").write_text(json.dumps(p3.strip(o)), encoding="utf-8")
    for d, o in st["deploy"].items():
        (snap / f"deploy-{d}.json").write_text(json.dumps(p3.strip(o)), encoding="utf-8")
    (snap / "pods.json").write_text(json.dumps(p3.pod_rows(st["pods"])), encoding="utf-8")
    ok, out = run_check(p3.check, "restored", snap)
    assert ok, out
    assert "INFO" in out and "keda" in out and "Application 5개 모두 Synced/Healthy" in out


def test_p3_snapshot_records_apps_and_phase2_objects(cluster, tmp_path):
    out = p3.snapshot(tmp_path / "s")
    names = {f.name for f in out.iterdir()}
    assert {"app-keda.json", "app-root.json", "deploy-worker.json", "deploy-relay.json", "scaledobject-worker.json", "pods.json", "meta.json", "SHA256SUMS"} <= names
    assert "hpa-keda-hpa-worker.json" not in names                            # 이 가짜 클러스터에는 없다: 건너뛰고 예외 없음
    assert p3.snap_apps(out) == sorted(cluster.apps)


def test_p3_pause_and_restore_roundtrip_on_current_layout(cluster):
    assert p3.pause(True) == 0
    assert all(cluster.apps[a]["spec"]["syncPolicy"].get("automated") is None for a in ("root", "parking-guardian"))
    assert cluster.apps["keda"]["spec"]["syncPolicy"]["automated"] == AUTO               # 나머지 앱은 그대로
    assert all((cluster.hpa[h]["spec"]["minReplicas"], cluster.hpa[h]["spec"]["maxReplicas"]) == (2, 2) for h in ("backend", "inference"))
    snap = sorted(p3.EKS_DOCS.iterdir())[-1]
    assert p3.restore(snap, True, False, 0) == 0
    assert cluster.apps["root"]["spec"]["syncPolicy"]["automated"] == AUTO


def test_p3_phase1_check_reports_missing_objects_as_fail_not_exception(cluster):
    cluster.make_comparison()
    ok, out = run_check(p3.check, "baseline")
    assert ok is False and "FAIL" in out and "inference" in out


def test_p3_dry_runs_change_nothing(cluster, tmp_path):
    snap = p3.snapshot(tmp_path / "s")
    n0 = len(cluster.mutations())
    assert p3.pause(False) == 0
    assert p3.restore(snap, False, False, 0) == 0
    cluster.make_comparison()
    assert p3.p2_pause(False) == 0
    assert p3.p2_workers(2, False, False) == 0
    assert p3.p2_restore("comparison", False, 0) == 0
    assert len(cluster.mutations()) == n0


def test_p2_status_baseline_comparison_fixed_are_distinguished(cluster):
    assert run_check(p3.check_p2, "p2-baseline")[0] is True
    assert run_check(p3.check_p2, "p2-comparison")[0] is False                   # 아직 공존 구성
    cluster.make_comparison()
    assert run_check(p3.check_p2, "p2-comparison")[0] is True
    ok, out = run_check(p3.check_p2, "p2-baseline")
    assert ok is False and "inference" in out
    assert run_check(p3.check_p2, "p2-fixed")[0] is False                        # 자동 동기화가 아직 켜져 있다


def test_p2_pause_refuses_when_not_in_comparison_mode(cluster):
    n0 = len(cluster.mutations())
    assert p3.p2_pause(True) == 2
    assert len(cluster.mutations()) == n0


def test_p2_pause_then_workers_then_restore(cluster):
    cluster.make_comparison()
    assert p3.p2_pause(True) == 0
    assert cluster.apps["root"]["spec"]["syncPolicy"].get("automated") is None
    assert (cluster.hpa["backend"]["spec"]["minReplicas"], cluster.hpa["backend"]["spec"]["maxReplicas"]) == (2, 2)
    assert "inference" not in cluster.hpa                                       # inference 는 건드릴 수 없다(없다)
    for n in (2, 3, 1):
        assert p3.p2_workers(n, True, False) == 0
        assert cluster.so["worker"]["metadata"]["annotations"]["autoscaling.keda.sh/paused-replicas"] == str(n)
        assert cluster.deploy["worker"]["spec"]["replicas"] == n
        assert run_check(p3.check_p2, "p2-fixed", n)[0] is True
        assert run_check(p3.check_p2, "p2-fixed", n + 1 if n < 3 else 1)[0] is False   # 다른 N 은 FAIL
    assert p3.p2_workers(0, True, True) == 0
    assert "autoscaling.keda.sh/paused-replicas" not in cluster.so["worker"]["metadata"]["annotations"]
    assert p3.p2_restore("comparison", True, 0) == 0
    assert cluster.apps["root"]["spec"]["syncPolicy"]["automated"] == AUTO
    assert (cluster.hpa["backend"]["spec"]["minReplicas"], cluster.hpa["backend"]["spec"]["maxReplicas"]) == (2, 3)


def test_p2_workers_guards(cluster):
    cluster.make_comparison()
    n0 = len(cluster.mutations())
    assert p3.p2_workers(2, True, False) == 2                                   # 자동 동기화가 켜져 있으면 거부
    p3.p2_pause(True)
    n1 = len(cluster.mutations())
    assert p3.p2_workers(4, True, False) == 2                                   # N 은 3 이하
    assert p3.p2_workers(0, True, False) == 2
    assert len(cluster.mutations()) == n1 and n1 > n0


def test_p2_restore_removes_leftover_pause_and_reports_failure_when_baseline_not_reached(cluster):
    cluster.make_comparison()
    p3.p2_pause(True)
    p3.p2_workers(2, True, False)
    assert p3.p2_restore("baseline", True, 0) == 4                              # Git 은 아직 비교 구성: baseline 이 아니다 -> 실패 보고
    assert "autoscaling.keda.sh/paused-replicas" not in cluster.so["worker"]["metadata"]["annotations"]
    assert cluster.apps["root"]["spec"]["syncPolicy"]["automated"] == AUTO       # 그래도 자동 동기화는 돌려놓았다


def test_p3_only_uses_expected_mutating_kubectl_verbs():
    src = Path(p3.__file__).read_text(encoding="utf-8")
    verbs = set(re.findall(r'kube\(\s*(?:"-n",\s*\w+,\s*)?"(\w+)"', src))
    assert verbs <= {"get", "patch", "annotate", "apply"}, verbs               # scale/delete/rollout 등은 없다
