"""개선된 관찰자(p2_sampler.py, p2_watch.py, p2_phases.py)의 시험: 병렬 수집, 시간 초과, 부분 실패, 낡은 값, ABORT 감지와 보류, 시각 정확성, 호출량, 읽기 전용 실행기,
cAdvisor 해석, 대기열 에피소드, Worker 단계, 옛 타임라인(실제 스모크 기록)과의 호환, 비밀 정보. 클라우드·클러스터 접속 없음(conftest 가 막는다)."""
import json
import os
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
import sys  # noqa: E402

sys.path.insert(0, str(HERE))
import p2_collect as pc  # noqa: E402
import p2_phases as ph  # noqa: E402
import p2_sampler as sp  # noqa: E402
import p2_smoke_check as sc  # noqa: E402
import p2_watch as pw  # noqa: E402

SMOKE_DIR = HERE.parents[1] / "docs" / "measurements" / "phase2" / "results" / "20261104-smoke10-1"
T0 = 1_000_000.0


# ============================================================================================ 공통 가짜 실행기
def pods_json(extra=None):
    def pod(name, app, **kw):
        return {"metadata": {"name": name, "labels": {"app.kubernetes.io/name": app}, "creationTimestamp": kw.get("created", "2026-11-04T10:00:00Z")},
                "spec": {"nodeName": kw.get("node", "node-a")},
                "status": {"phase": "Running", "conditions": kw.get("conds", []), "containerStatuses": [{"ready": True, "restartCount": 0, "state": {"running": {"startedAt": kw.get("started", "2026-11-04T10:00:05Z")}}}],
                           "initContainerStatuses": kw.get("init", [])}}
    items = [pod("backend-1", "backend"), pod("relay-1", "relay")] + (extra or [])
    return json.dumps({"items": items})


class FakeCluster:
    """명령을 해석해 가짜 응답을 준다. delay: 호출마다 걸리는 시간, fail: 이 문자열을 포함한 명령은 오류, calls: 호출 기록(시작·끝 시각)."""

    def __init__(self, delay=0.0, fail=(), pods=None):
        self.delay, self.fail, self.calls, self.pods = delay, tuple(fail), [], pods or pods_json()

    def __call__(self, cmd, input=None, timeout=None):
        t0 = time.time()
        time.sleep(self.delay)
        key = " ".join(cmd[:3])
        try:
            for f in self.fail:
                if f in " ".join(cmd):
                    raise TimeoutError(f"시간 초과: {f}")
            if cmd[:3] == ["kubectl", "get", "pods"]:
                return self.pods
            if cmd[:3] == ["kubectl", "get", "scaledobject"]:
                return json.dumps({"metadata": {}, "spec": {"minReplicaCount": 0, "maxReplicaCount": 1}, "status": {"conditions": [{"type": "Ready", "status": "True"}, {"type": "Active", "status": "False"}]}})
            if cmd[:3] == ["kubectl", "get", "app"]:
                return json.dumps({"items": [{"metadata": {"name": "root"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}]})
            if cmd[:3] == ["kubectl", "top", "pods"]:
                return "backend-1   3m   100Mi\n"
            if cmd[:2] == ["aws", "sqs"]:
                return json.dumps({"Attributes": {"ApproximateNumberOfMessages": "0"}})
            if cmd[:2] == ["aws", "cloudwatch"]:
                return json.dumps({"Datapoints": [{"Timestamp": "2026-11-04T10:00:00Z", "Maximum": 7.0}]})
            if cmd[:2] == ["aws", "secretsmanager"]:
                return json.dumps({"LastRotatedDate": "2026-11-04T04:23:00+00:00", "RotationEnabled": True})
            if cmd[:2] == ["kubectl", "exec"]:
                return json.dumps({"pending": [], "last": [{"last_result": None}], "recent": [{"n": 0, "published": 0, "max_publish_delay_s": None}]}) + "\n"
            raise AssertionError(cmd)
        finally:
            self.calls.append((key, t0, time.time()))


def cfg_with(**kw):
    c = pw.Config(**kw)
    c.queue_url, c.dlq_url, c.secret_id, c.queue_name = "https://sqs/q.fifo", "https://sqs/dlq.fifo", "rds-secret", "q.fifo"
    return c


# ============================================================================================ 1. 병렬 수집 · 시각 기록
def test_collect_sample_runs_sources_in_parallel_and_records_each_call():
    fc = FakeCluster(delay=0.25)
    run = pw.readonly_runner(fc)
    t0 = time.time()
    s = pw.collect_sample(run, cfg_with())
    wall = time.time() - t0
    assert len(fc.calls) == 9                                              # 호출 수는 그대로(pods, keda, apps, top, sqs, sqs_age, dlq, secret, db)
    assert wall < 1.0, f"병렬이면 약 0.25 s, 순차면 약 2.3 s: {wall:.2f}"
    assert max(e for _, _, e in fc.calls) - min(b for _, b, _ in fc.calls) < 1.0       # 호출 구간이 서로 겹쳤다
    assert set(s["sources"]) == {"pods", "keda", "apps", "top", "sqs", "sqs_age", "dlq", "secret", "db"}
    for name, info in s["sources"].items():
        assert info["ok"] and info["t_end"] >= info["t_start"] and info["ms"] >= 240, name       # 소스마다 시작·완료 시각과 소요
    assert "errors" not in s and s["degraded"] is False and s["sqs_age_s"] == 7.0 and s["db"]["pending_total"] == 0


def test_timeout_of_one_source_is_a_partial_failure_not_a_lost_sample():
    fc = FakeCluster(delay=0.0, fail=("exec",))
    s = pw.collect_sample(pw.readonly_runner(fc), cfg_with())
    assert "db" not in s and "pods" in s and "sqs" in s and "keda" in s                  # 실패한 소스만 빠진다
    assert s["sources"]["db"]["ok"] is False and "TimeoutError" in s["sources"]["db"]["error"]
    assert any(e.startswith("db:") for e in s["errors"]) and len(s["errors"]) == 1


def test_runner_receives_the_per_call_timeout():
    seen = []
    def inner(cmd, input=None, timeout=None):
        seen.append(timeout)
        return FakeCluster()(cmd, input)
    pw.collect_sample(pw.readonly_runner(inner), cfg_with(call_timeout_s=7, exec_timeout_s=33))
    assert 7 in seen and 33 in seen and None not in seen                                  # 모든 외부 호출에 제한 시간이 있다


def test_db_read_time_is_the_midpoint_of_the_call_and_ages_use_it():
    clock = iter([100.0, 104.0])
    rows = {"pending": [{"status": "queued", "n": 3, "oldest": pc.iso_utc(90.0)}], "last": [{"last_result": pc.iso_utc(95.0)}], "recent": [{"n": 3, "published": 3, "max_publish_delay_s": "0.31"}]}
    db = pw._db_sample(lambda cmd, input=None, timeout=None: json.dumps(rows) + "\n", pw.Config(), lambda: next(clock))
    assert db["read_t"] == 102.0 and db["oldest_age_s"] == {"queued": 12.0} and db["pending_total"] == 3
    assert db["last_result_ts"] == 95.0 and db["recent"]["max_publish_delay_s"] == pytest.approx(0.31)      # Relay 발행 지연


# ============================================================================================ 2. 고정 주기 틱 · 폴러 · 낡은 값
class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_ticker_keeps_schedule_without_drift_and_reports_missed_ticks():
    c = Clock()
    tk = sp.FixedRateTicker(10, c, c.sleep, start=1000.0)
    first = tk.wait()
    assert (first["index"], first["scheduled"], first["late_s"], first["missed"]) == (0, 1000.0, 0.0, 0)
    c.t += 4                                                                  # 처리 4 s: 일정 안
    second = tk.wait()
    assert (second["scheduled"], second["late_s"], second["missed"]) == (1010.0, 0.0, 0) and c.t == 1010.0
    c.t += 35                                                                 # 처리 35 s: 틱 2개를 통째로 놓침
    third = tk.wait()
    assert third["missed"] == 2 and third["late_s"] == pytest.approx(5.0) and third["scheduled"] == 1040.0       # 1020·1030 틱을 놓치고 1040 틱이 5 s 늦게 실행
    fourth = tk.wait()
    assert fourth["scheduled"] == 1050.0 and fourth["missed"] == 0 and c.t == 1050.0               # 밀리지 않고 원래 격자(10 s 배수)로 돌아온다


def test_poller_records_start_end_and_keeps_last_good_value_on_failure():
    c = Clock(500.0)
    state = {"n": 0}
    def fn():
        c.t += 2.0
        state["n"] += 1
        if state["n"] == 2:
            raise TimeoutError("느림")
        return {"v": state["n"]}
    p = sp.Poller("db", fn, 30, "db", c)
    p.poll_once()
    a = p.snapshot()
    assert (a["t_start"], a["t_end"], a["ms"], a["ok"], a["value"]) == (500.0, 502.0, 2000, True, {"v": 1})
    p.poll_once()
    b = p.snapshot()
    assert b["ok"] is False and "TimeoutError" in b["error"] and b["value"] == {"v": 1} and b["ok_t_end"] == 502.0 and b["consecutive_failures"] == 1
    p.poll_once()
    assert p.snapshot()["consecutive_failures"] == 0 and p.snapshot()["value"] == {"v": 3}


def test_build_sample_drops_stale_values_and_marks_the_sample_degraded():
    c = Clock(1000.0)
    ok = sp.Poller("pods", lambda: [{"name": "x"}], 15, "pods", c)
    db = sp.Poller("db", lambda: {"pending_total": 0}, 30, "db", c)
    ok.poll_once(); db.poll_once()
    fresh = sp.build_sample([ok, db], 1005.0, 15)
    assert fresh["pods"] and fresh["db"] and fresh["degraded"] is False and "errors" not in fresh
    stale = sp.build_sample([ok, db], 1500.0, 15)                              # 값을 읽은 지 500 s
    assert "pods" not in stale and "db" not in stale and stale["degraded"] is True                            # 낡은 값은 판단에 쓰지 않는다
    assert stale["sources"]["db"]["stale"] is True and stale["sources"]["db"]["age_s"] == 500.0 and len(stale["errors"]) == 2
    late = sp.build_sample([ok, db], 1005.0, 15, tick={"index": 3, "scheduled": 1000.0, "late_s": 40.0, "missed": 2})
    assert late["degraded"] is True and late["tick"]["missed"] == 2                                             # 틱이 2 interval 넘게 늦은 표본
    never = sp.build_sample([sp.Poller("pods", lambda: 1, 15, "pods", c)], 1005.0, 15)
    assert never["degraded"] is True and "pods" not in never                                                    # 한 번도 못 읽음


# ============================================================================================ 3. Watcher: 지연으로 인한 거짓 ABORT 방지, 실제 ABORT 유지
def good_pods(worker_ready=1):
    base = [{"name": "backend-1", "app": "backend", "phase": "Running", "node": "n", "ready": True, "restarts": 0, "created": None, "deleting": False, "unschedulable": False}]
    return base + [{"name": f"worker-{i}", "app": "worker", "phase": "Running", "node": "n", "ready": True, "restarts": 0, "created": None, "deleting": False, "unschedulable": False}
                   for i in range(worker_ready)]


def samp(ts, db=None, **kw):
    s = {"ts": ts, "pods": good_pods(), "keda": {"ready": "True", "active": "True", "paused": None, "min": 0, "max": 1}, "apps": {"root": ("Synced", "Healthy")},
         "sqs": {"visible": 0, "not_visible": 0, "delayed": 0}, "dlq": {"visible": 0, "not_visible": 0, "delayed": 0}, "sources": {n: {"ok": True, "stale": False, "t_end": ts} for n in ("pods", "db", "top")},
         "degraded": False, "tick": {"index": 0, "scheduled": ts, "late_s": 0.0, "missed": 0}}
    s["db"] = db if db is not None else {"pending_total": 4, "by_status": {"queued": 4}, "last_result_ts": ts - 90, "read_t": ts, "oldest_pending_publish_age_s": 0}
    s.update(kw)
    return s


def codes(alerts, level=None):
    return {a["code"] for a in alerts if level is None or a["level"] == level}


def test_stall_aborts_on_a_fresh_sample_but_is_held_back_on_a_degraded_one():
    assert "WORKER_STALL" in codes(pw.Watcher().step(samp(1000)), "ABORT")
    deg = pw.Watcher().step(samp(1000, degraded=True))
    assert "WORKER_STALL" not in codes(deg, "ABORT") and "WORKER_STALL" in codes(deg, "WARN")
    assert "ABORT 보류" in [a for a in deg if a["code"] == "WORKER_STALL"][0]["msg"]


def test_state_facts_still_abort_when_the_sample_is_degraded():
    s = samp(1000, degraded=True, dlq={"visible": 1, "not_visible": 0, "delayed": 0}, keda={"ready": "False", "active": "False", "paused": None, "min": 0, "max": 1})
    out = pw.Watcher().step(s)
    assert {"DLQ_MESSAGE", "KEDA_NOT_READY"} <= codes(out, "ABORT")


def test_stall_uses_the_db_read_time_not_the_sample_start_time():
    s = samp(1000, db={"pending_total": 2, "by_status": {"queued": 2}, "last_result_ts": 960.0, "read_t": 1001.0, "oldest_pending_publish_age_s": 0})
    assert "WORKER_STALL" not in codes(pw.Watcher().step(s), "ABORT")                  # 마지막 판정 41 s 전 (< 60 s)
    s2 = samp(1000, db={"pending_total": 2, "by_status": {"queued": 2}, "last_result_ts": 960.0, "read_t": 1021.0, "oldest_pending_publish_age_s": 0})
    assert "WORKER_STALL" in codes(pw.Watcher().step(s2), "ABORT")                     # 실제로 DB 를 읽은 시각 기준 61 s


def test_missing_or_stale_db_never_produces_stall_or_no_scaleup():
    w = pw.Watcher()
    w.step(samp(1000))
    s = samp(1015, sqs={"visible": 5, "not_visible": 0, "delayed": 0})
    del s["db"]                                                                       # DB 값이 낡아 표본에서 빠졌다
    s["sources"]["db"] = {"ok": False, "stale": True, "error": "TimeoutError"}
    out = w.step(s)
    assert not (codes(out, "ABORT") & {"WORKER_STALL", "KEDA_NO_SCALEUP"})
    s2 = samp(1200, sqs={"visible": 5, "not_visible": 0, "delayed": 0}, pods=good_pods(0))
    del s2["db"]
    assert not (codes(pw.Watcher().step(s2), "ABORT") & {"KEDA_NO_SCALEUP"})


def test_critical_sources_abort_after_consecutive_failures_others_only_warn():
    w = pw.Watcher(pw.Config(source_error_abort=3))
    def bad(ts, name):
        s = samp(ts, db={"pending_total": 0, "by_status": {}, "last_result_ts": ts, "read_t": ts, "oldest_pending_publish_age_s": 0})
        s["sources"][name] = {"ok": False, "stale": True, "error": "TimeoutError"}
        return s
    assert "SOURCE_ERROR" in codes(w.step(bad(1000, "db")), "WARN") and "SOURCE_ERROR" in codes(w.step(bad(1015, "db")), "WARN")
    assert "SOURCE_ERROR" in codes(w.step(bad(1030, "db")), "ABORT")                  # 핵심 소스 연속 3회
    w2 = pw.Watcher(pw.Config(source_error_abort=3))
    for i in range(6):
        assert "SOURCE_ERROR" not in codes(w2.step(bad(1000 + 15 * i, "top")), "ABORT")      # top 은 경고만
    assert w2.step(samp(2000, db={"pending_total": 0, "by_status": {}, "last_result_ts": 2000, "read_t": 2000, "oldest_pending_publish_age_s": 0}))[0:0] == []
    assert w2.fails["top"] == 0                                                                # 회복되면 카운터가 0 으로


def test_late_and_missed_ticks_are_recorded_as_warnings():
    s = samp(1000, db={"pending_total": 0, "by_status": {}, "last_result_ts": 1000, "read_t": 1000, "oldest_pending_publish_age_s": 0})
    s["tick"] = {"index": 5, "scheduled": 940.0, "late_s": 60.0, "missed": 3}
    assert "SAMPLE_MISSED" in codes(pw.Watcher().step(s), "WARN")
    s["tick"]["missed"] = 0
    assert "SAMPLE_LATE" in codes(pw.Watcher().step(s), "WARN")


# ============================================================================================ 4. 루프: ABORT 감지, 파일 형식, 호환
def test_run_loop_writes_jsonl_and_abort_flag_and_stops_new_work(tmp_path):
    c = Clock(1000.0)
    out = tmp_path / "timeline.jsonl"
    seq = []
    def samples_fn(tick):
        i = tick["index"]
        pending = 0 if i < 2 else 4                                        # 틱 2부터 일이 있는데 판정이 안 나온다
        ts = c.t
        s = samp(ts, db={"pending_total": pending, "by_status": {"queued": pending}, "last_result_ts": 1000.0 + (0 if i < 2 else 0), "read_t": ts, "oldest_pending_publish_age_s": 0})
        s["tick"] = tick
        seq.append(i)
        return s
    printed = []
    aborted, alerts = pw.run_loop(samples_fn, pw.Watcher(pw.Config(stall_after_s=60)), 15, 600, out, clock=c, sleep=c.sleep, printer=lambda *a, **k: printed.append(a[0]))
    assert aborted is True and "WORKER_STALL" in codes(alerts, "ABORT")
    assert (tmp_path / "ABORT.flag").exists() and json.loads((tmp_path / "ABORT.flag").read_text(encoding="utf-8"))
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(seq) == 5 and lines[-1]["alerts"] and all("build_ms" in x["tick"] for x in lines)           # 마지막 판정 후 60 s 가 된 틱 4 에서 감지하고 멈춘다
    assert [x["tick"]["index"] for x in lines] == list(range(5)) and not lines[-2]["alerts"]
    assert any("ABORT" in p for p in printed)


def test_new_sample_format_is_json_serialisable_and_old_consumers_still_work(tmp_path):
    s = pw.collect_sample(pw.readonly_runner(FakeCluster()), cfg_with())
    s["alerts"] = []
    line = json.dumps(s, default=str, ensure_ascii=False)
    back = json.loads(line)
    f = tmp_path / "t.jsonl"
    f.write_text(line + "\n" + line + "\n", encoding="utf-8")
    assert back["pods"][0]["name"] and "sources" in back and isinstance(back["db"], dict)
    assert pw.scale_events([back, back]) == []                              # 옛 소비자(scale_events)가 새 표본을 그대로 읽는다
    assert "backend" in pw.summarize_resources([back])
    assert pw.main(["summarize", "--out", str(f)]) == 0 and pw.main(["resources", "--out", str(f)]) == 0


@pytest.mark.skipif(not (SMOKE_DIR / "timeline.jsonl").exists(), reason="스모크 기록 파일이 없다")
def test_real_smoke_timeline_still_loads_in_every_consumer():
    samples = [json.loads(x) for x in (SMOKE_DIR / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    ev = pw.scale_events(samples)
    assert [e["kind"] for e in ev if e["kind"] == "worker_replicas"] == ["worker_replicas", "worker_replicas"]       # 0->1, 1->0
    cad = pw.cadence_stats(samples)
    assert cad["gap_s"]["p50"] > 30 and cad["sources"] == {}                      # 옛 기록: 설정 15초인데 간격 40초대 (이번 개선의 근거)
    bl = pw.backlog_episodes(samples)
    assert bl["episodes"] and bl["episodes"][0]["max_pending"] == 3 and bl["episodes"][0]["kind"] in ("short", "long")      # CloudWatch 가 놓친 워밍업 3건 적체
    assert pw.summarize_resources(samples)["worker"]["cpu_m_max"] <= 5            # kubectl top 이 버스트를 놓친 증거
    rows = ph.worker_phases(samples)
    assert rows and "pod_created" in rows[0]["phases"] and "k8s_ready" in rows[0]["missing"] + list(rows[0]["phases"])
    report = SMOKE_DIR / "p2-report.json"
    attempts = SMOKE_DIR / "p2-attempts.csv"
    if report.exists() and attempts.exists():
        res = sc.evaluate_smoke(json.loads(report.read_text(encoding="utf-8")), sc.read_csv(attempts), ev, sc.read_csv(SMOKE_DIR / "sqs-main.csv"), sc.read_csv(SMOKE_DIR / "sqs-dlq.csv"), 10, 3, True)
        assert sc.verdict(res) == 0


# ============================================================================================ 5. 호출량 상한
def test_aws_call_volume_is_bounded():
    c = cfg_with()
    ps = pw.build_pollers(pw.readonly_runner(FakeCluster()), pw.AwsViaRunner(lambda *a, **k: ""), c)
    per_min = {p.name: 60.0 / p.period_s for p in ps}
    aws = per_min["sqs"] + per_min["dlq"] + per_min["sqs_age"] + per_min["secret"]
    assert aws == pytest.approx(9.2) and per_min["sqs_age"] == 1.0 and per_min["secret"] == pytest.approx(0.2)        # SQS 8 + CloudWatch 1 + Secrets 0.2
    assert per_min["db"] == 2.0                                                                                       # kubectl exec 는 분당 2회(backend Pod 부하 최소화)
    assert sum(v for k, v in per_min.items() if k not in ("sqs", "dlq", "sqs_age", "secret", "db")) <= 16


# ============================================================================================ 6. 읽기 전용 AWS 래퍼 · 실행기
class FakeSqs:
    def __init__(self):
        self.calls = []

    def get_queue_attributes(self, **kw):
        self.calls.append(kw)
        return {"Attributes": {"ApproximateNumberOfMessages": "2", "ApproximateNumberOfMessagesNotVisible": "1"}}

    def delete_message(self, **kw):                                         # 래퍼가 절대 노출하면 안 되는 쓰기
        raise AssertionError("쓰기 호출")


class FakeCw:
    def get_metric_statistics(self, **kw):
        from datetime import datetime, timezone
        return {"Datapoints": [{"Timestamp": datetime.fromtimestamp(200, tz=timezone.utc), "Maximum": 5.0}, {"Timestamp": datetime.fromtimestamp(100, tz=timezone.utc), "Maximum": 1.0}]}


class FakeSm:
    def describe_secret(self, **kw):
        from datetime import datetime, timezone
        return {"LastRotatedDate": datetime.fromtimestamp(50, tz=timezone.utc), "RotationEnabled": True}

    def get_secret_value(self, **kw):
        raise AssertionError("비밀 값 읽기")


def test_readonly_aws_exposes_only_three_read_methods():
    sqs = FakeSqs()
    a = sp.ReadOnlyAws(clients={"sqs": sqs, "cloudwatch": FakeCw(), "secretsmanager": FakeSm()})
    public = {n for n in dir(a) if not n.startswith("_")} - {"METHODS"}
    assert public == set(sp.ReadOnlyAws.METHODS)
    assert a.sqs_attrs("https://q") == {"visible": 2, "not_visible": 1, "delayed": 0} and sqs.calls[0]["AttributeNames"] == ["All"]
    assert [p["value"] for p in a.metric_points("AWS/SQS", "X", {"QueueName": "q"}, 0, 300)] == [1.0, 5.0]               # 시각순 정렬
    assert a.secret_dates("id") == {"last_rotated": 50.0, "next_rotation": None, "rotation_enabled": True}
    for forbidden in ("delete_message", "send_message", "purge_queue", "get_secret_value", "put_metric_data", "rotate_secret"):
        assert not hasattr(a, forbidden)


def test_readonly_runner_new_rules_and_timeout_passthrough():
    seen = []
    run = pw.readonly_runner(lambda cmd, **kw: seen.append(kw) or "ok")
    assert run(["kubectl", "logs", "worker-6cb8b4b69b-khndk", "-n", "parking-guardian", "--timestamps", "--tail=100"], timeout=9) == "ok" and seen[-1] == {"timeout": 9}
    assert run(["kubectl", "logs", "-n", "keda", "deploy/keda-operator", "--since=30m", "--timestamps"]) == "ok" and seen[-1] == {}
    assert run(["kubectl", "get", "--raw", "/api/v1/nodes/ip-10-0-7-63.ap-northeast-2.compute.internal/proxy/metrics/cadvisor"]) == "ok"
    for bad in (["kubectl", "logs", "backend-55b9ff54d7-fdkfr", "-n", "parking-guardian"], ["kubectl", "logs", "worker-abc", "-n", "kube-system"],
                ["kubectl", "logs", "-n", "keda", "deploy/keda-operator", "-f"][:3], ["kubectl", "get", "--raw", "/api/v1/secrets"],
                ["kubectl", "get", "--raw", "/api/v1/nodes/n/proxy/exec"], ["kubectl", "logs", "-n", "argocd", "deploy/argocd-server"]):
        with pytest.raises(PermissionError):
            run(bad)


# ============================================================================================ 7. cAdvisor · CPU
CADVISOR = '''# HELP container_cpu_usage_seconds_total Cumulative cpu time consumed in seconds.
# TYPE container_cpu_usage_seconds_total counter
container_cpu_usage_seconds_total{container="worker",cpu="total",id="/a",image="img",name="n",namespace="parking-guardian",pod="worker-abc"} 12.5 1730000000000
container_cpu_usage_seconds_total{container="",cpu="total",id="/pod",namespace="parking-guardian",pod="worker-abc"} 13.0 1730000000000
container_cpu_usage_seconds_total{container="POD",cpu="total",id="/p",namespace="parking-guardian",pod="worker-abc"} 0.1 1730000000000
container_cpu_usage_seconds_total{container="inference",cpu="total",namespace="parking-guardian",pod="inference-1"} 100.25 1730000000500
container_cpu_usage_seconds_total{container="coredns",cpu="total",namespace="kube-system",pod="coredns-1"} 9.0 1730000000000
container_memory_working_set_bytes{container="worker",namespace="parking-guardian",pod="worker-abc"} 943718400 1730000000000
other_metric{container="worker",namespace="parking-guardian",pod="worker-abc"} 1
'''


def test_parse_cadvisor_selects_only_container_counters_of_the_namespace():
    p = sp.parse_cadvisor(CADVISOR)
    assert set(p) == {"worker-abc", "inference-1"}
    assert p["worker-abc"]["cpu_s"] == 12.5 and p["worker-abc"]["containers"] == {"worker": 12.5} and p["worker-abc"]["mem_bytes"] == {"worker": 943718400.0}
    assert p["inference-1"]["cpu_s"] == 100.25 and p["inference-1"]["ts_ms"] == 1730000000500


def snap(t, **pods):
    return {"t": t, "pods": {k: {"cpu_s": v, "containers": {"c": v}, "mem_bytes": {"c": 1048576.0 * 800}} for k, v in pods.items()}}


def test_cpu_rates_from_counters_catch_bursts_that_top_would_miss():
    snaps = [snap(0, **{"inference-1": 100.0}), snap(30, **{"inference-1": 130.0}), snap(60, **{"inference-1": 175.0})]
    out = sp.cpu_rates(snaps, {"inference-1": "inference"}, requests={"inference": 1000})["inference"]
    assert out["cpu_s_total"] == 75.0 and out["avg_m"] == 1250.0 and out["interval_peak_m"] == 1500.0
    assert out["over_request_intervals"] == 1 and out["peak_over_request"] == 1.5 and out["avg_over_request"] == 1.25 and out["resolution_s_median"] == 30.0
    assert out["mem_mi_max"] == 800.0


def test_cpu_rates_count_a_new_pod_from_zero_and_skip_counter_resets():
    snaps = [snap(0, **{"worker-new": 0}), snap(30, **{"worker-new": 10.0}), snap(60, **{"worker-new": 40.0})]
    snaps[0]["pods"].pop("worker-new")                                     # 첫 스냅샷에는 아직 없었다(생성 20, 첫 관찰 30)
    out = sp.cpu_rates(snaps, {"worker-new": "worker"}, {"worker-new": 20.0}, window_start=0.0, requests={"worker": 1000})["worker"]
    assert out["cpu_s_total"] == 40.0 and out["observed_s"] == 40.0 and out["avg_m"] == 1000.0           # 생성 후 40 s 동안 40 CPU 초
    reset = sp.cpu_rates([snap(0, **{"inference-1": 500.0}), snap(30, **{"inference-1": 5.0}), snap(60, **{"inference-1": 35.0})], {"inference-1": "inference"}, requests={"inference": 1000})["inference"]
    assert reset["cpu_s_total"] == 30.0 and reset["intervals"] == 1                                      # 재시작(카운터 감소) 구간은 버린다


def test_summarize_cadvisor_reads_snapshots_from_the_timeline():
    pods = [{"name": "inference-1", "app": "inference", "created": "1969-12-31T23:58:00Z"}]            # 윈도 시작(0) 전에 있던 Pod
    samples = [{"ts": t, "pods": pods, "cadvisor": snap(t, **{"inference-1": 100.0 + t})} for t in (0, 30, 30, 60)]       # 같은 스냅샷 시각(30)은 한 번만
    out = pw.summarize_cadvisor(samples)
    assert out["inference"]["avg_m"] == 1000.0 and out["inference"]["intervals"] == 2 and "누적 카운터" in out["inference"]["source"]
    assert pw.summarize_cadvisor([{"ts": 1, "pods": pods}]) == {}


# ============================================================================================ 8. 대기열 에피소드 · 간격 통계
def dbs(times_pending, last=None, ready=True):
    out = []
    for i, (t, n) in enumerate(times_pending):
        out.append({"ts": t, "db": {"pending_total": n, "read_t": t, "last_result_ts": (last(i) if last else t - 5)}, "pods": [{"name": "w", "app": "worker", "ready": ready}]})
    return out


def test_short_backlog_is_separated_from_long_and_stalled_ones():
    short = pw.backlog_episodes(dbs([(0, 0), (15, 0), (30, 3), (45, 3), (60, 0), (75, 0)]))
    e = short["episodes"][0]
    assert short["resolution_s_median"] == 15.0 and e["kind"] == "short" and e["duration_min_s"] == 15.0 and e["duration_max_s"] == 45.0 and e["max_pending"] == 3
    assert "1분" in e["cloudwatch_note"]
    long_ = pw.backlog_episodes(dbs([(0, 0)] + [(15 * i, 5) for i in range(1, 9)] + [(135, 0)]))["episodes"][0]
    assert long_["kind"] == "long" and long_["duration_max_s"] == 135.0
    stalled = pw.backlog_episodes(dbs([(0, 0)] + [(15 * i, 5) for i in range(1, 9)], last=lambda i: 1.0))["episodes"][0]
    assert stalled["kind"] == "stalled" and stalled["progressed"] is False
    open_ = pw.backlog_episodes(dbs([(0, 0), (15, 2), (30, 2)]))["episodes"][0]
    assert open_["kind"] == "open" and open_["end_seen_ts"] is None
    assert pw.backlog_episodes(dbs([(0, 0), (15, 0)]))["episodes"] == []


def test_cadence_stats_counts_each_call_once_and_reports_failures_and_missed_ticks():
    samples = []
    for i in range(4):
        samples.append({"ts": 100.0 + 15 * i, "tick": {"missed": 1 if i == 3 else 0, "late_s": 0.5 * i}, "degraded": i == 3,
                        "sources": {"db": {"ok": True, "ms": 6000, "t_end": 90.0 if i < 2 else 120.0}, "top": {"ok": i != 1, "ms": 700 + i, "t_end": 100.0 + 15 * i}}})
    c = pw.cadence_stats(samples)
    assert c["gap_s"]["p50"] == 15.0 and c["missed_ticks"] == 1 and c["degraded_samples"] == 1 and c["late_s_max"] == 1.5
    assert c["sources"]["db"]["calls"] == 2 and c["sources"]["top"]["calls"] == 4 and c["sources"]["top"]["failed"] == 1       # 같은 호출 결과는 틱마다 반복되지만 한 번만


# ============================================================================================ 9. Worker 로그 수집과 단계 분석
def test_worker_logs_keep_only_relevant_lines_drop_secrets_and_stop_after_ready():
    text = ("2026-11-04T10:45:52.100Z loading models\n2026-11-04T10:45:53.000Z password=abc loading token\n2026-11-04T10:45:58.000Z unrelated line\n"
            "2026-11-04T10:46:01.500Z preload done load_seconds=6.307 ready\n")
    calls = []
    def runner(cmd, input=None, timeout=None):
        calls.append(cmd)
        return text
    pods = [{"name": "worker-abc", "app": "worker", "deleting": False, "ready": True, "started_at": "x"}, {"name": "backend-1", "app": "backend", "deleting": False, "ready": True}]
    wl = pw.WorkerLogs(runner, lambda: pods, pw.Config())
    out = wl()
    assert out["worker-abc"]["complete"] is True and len(calls) == 1 and "backend-1" not in out
    lines = out["worker-abc"]["lines"]
    assert all("password" not in x.lower() and "token" not in x.lower() for x in lines) and any("load_seconds" in x for x in lines)
    wl(); assert len(calls) == 1                                           # 완료되면 더 부르지 않는다
    failing = pw.WorkerLogs(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")), lambda: pods, pw.Config())
    for _ in range(20):
        failing()
    assert failing.state["worker-abc"]["tries"] == 12                      # 시도 횟수 상한


KEDA_LINES = ("2026-11-04T10:45:46Z\tINFO\tscaleexecutor\tSuccessfully updated ScaleTarget\t{scaledobject.Name: worker, scaledObject.Namespace: parking-guardian, "
              "scaleTarget.Name: worker, Original Replicas Count: 0, New Replicas Count: 1}\n"
              "2026-11-04T10:51:01Z\tINFO\tscaleexecutor\tSuccessfully set ScaleTarget replicas count to ScaledObject minReplicaCount\t{scaledobject.Name: worker}\n"
              "2026-11-04T10:51:02Z\tINFO\tother\tsomething else\n")


def test_parse_keda_log_extracts_decisions():
    d = pw.parse_keda_log(KEDA_LINES)
    assert len(d) == 2 and d[0]["ts"] == pc.ts("2026-11-04T10:45:46Z") and (d[0]["from"], d[0]["to"]) == (0, 1) and d[1]["to"] is None


def worker_timeline():
    t = lambda x: pc.iso_utc(x).replace("+00:00", "Z")  # noqa: E731
    pod = {"name": "worker-abc", "app": "worker", "phase": "Running", "node": "n", "ready": True, "restarts": 0, "deleting": False, "unschedulable": False,
           "created": t(1000.0), "scheduled_at": t(1000.5), "init_done_at": t(1005.0), "started_at": t(1006.0), "ready_at": t(1006.2)}
    logs = {"worker-abc": {"lines": [f"{t(1006.5)} start", f"{t(1015.2)} preload done load_seconds=6.3 ready"], "complete": True, "tries": 3}}
    return [{"ts": 1004.0, "pods": [dict(pod, ready=False, started_at=None, ready_at=None, init_done_at=None)]}, {"ts": 1020.0, "pods": [pod], "worker_logs": logs}]


def test_worker_phases_separate_k8s_ready_from_model_loaded_and_label_sources():
    report = {"workers_detail": {"worker-abc-xyz": {"first_result": 1016.5, "first_elapsed_ms": 700, "jobs": 3}}}
    decisions = [{"ts": 994.0, "from": 0, "to": 1}, {"ts": 1300.0, "from": 1, "to": 0}]
    r = ph.worker_phases(worker_timeline(), report, decisions)[0]
    p = r["phases"]
    assert p["keda_decision"]["source"] == "observed(KEDA 로그)" and p["keda_decision"]["t"] == 994.0
    assert p["k8s_ready"]["t"] == pytest.approx(1006.2) and p["model_loaded"]["source"] == "observed(로그)" and p["model_loaded"]["t"] == pytest.approx(1015.2)
    assert r["k8s_ready_vs_model_loaded_s"] == pytest.approx(9.0, abs=0.01)                    # Ready 와 모델 준비 사이(스모크에서 약 9초)
    assert r["cold_start_total_s"] == pytest.approx(22.5) and p["first_result"]["source"] == "observed(DB)" and r["missing"] == []
    # 로그에서 못 찾으면 추정이고 그렇다고 표시한다
    tl = worker_timeline()
    tl[1].pop("worker_logs")
    est = ph.worker_phases(tl, report, decisions)[0]["phases"]["model_loaded"]
    assert est["source"].startswith("estimated") and est["t"] == pytest.approx(1016.5 - 0.7)
    # 아무것도 못 보면 빠진 단계로 남는다
    bare = ph.worker_phases([{"ts": 5.0, "pods": [{"name": "worker-q", "app": "worker", "created": None}]}])[0]
    assert "keda_decision" in bare["missing"] and bare["phases"]["pod_created"]["source"].startswith("estimated")


def test_collect_report_exposes_per_worker_first_result_for_the_phase_tool():
    from test_perf_tools import T0 as TT, build_fixture
    meta, sends, rows, _ = build_fixture()
    rep, _, _ = pc.build_report([meta], sends, rows, eval_at=TT + 100, drain_wait=5.0)
    wd = rep["workers_detail"]
    assert set(wd) == {"w1", "w2"} and wd["w1"]["jobs"] == 3 and wd["w1"]["first_result"] < wd["w1"]["last_result"] and wd["w1"]["first_elapsed_ms"] == 700


# ============================================================================================ 10. 비밀 정보
def test_samples_and_timeline_never_contain_environment_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVICE_KEYS", "CANARYKEY-observer-111111")
    monkeypatch.setenv("SUPER_PASSWORD", "CANARYPW-observer-222222")
    s = pw.collect_sample(pw.readonly_runner(FakeCluster()), cfg_with())
    blob = json.dumps(s, default=str, ensure_ascii=False)
    assert "CANARY" not in blob and not any(v in blob for v in (os.environ["DEVICE_KEYS"], os.environ["SUPER_PASSWORD"]))
    import ast
    for name in ("p2_watch.py", "p2_sampler.py", "p2_phases.py"):
        tree = ast.parse((HERE / name).read_text(encoding="utf-8"))
        assert not any(isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv") for n in ast.walk(tree)), name       # 환경변수를 읽지 않는다
        code_strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 200]     # 긴 것은 문서 문자열
        assert not any(x in ("DEVICE_KEYS", "SUPER_PASSWORD", "ADMIN_PASSWORD", "X-Device-Key") for x in code_strings), name


def test_threaded_pollers_and_loop_produce_regular_samples_with_real_threads(tmp_path):
    """실제 스레드·실제 시계로 짧게 돌려 본다: 틱 간격이 규칙적이고, 느린 소스(db)는 더 드물게 읽히며, 모든 표본에 소스별 시각이 있다."""
    fc = FakeCluster(delay=0.02)
    cfg = cfg_with(fast_period_s=0.2, db_period_s=0.6, cloudwatch_period_s=0.6, secret_period_s=5, top_period_s=0.2, apps_period_s=0.4)
    pollers = pw.build_pollers(pw.readonly_runner(fc), pw.AwsViaRunner(pw.readonly_runner(fc)), cfg)
    for p in pollers:
        p.start()
    try:
        time.sleep(0.5)
        w = pw.Watcher(cfg)
        out = tmp_path / "t.jsonl"
        aborted, _ = pw.run_loop(lambda tick: sp.build_sample(pollers, time.time(), 0.3, None, pw.CRITICAL_SOURCES, tick), w, 0.3, 1.8, out)
    finally:
        for p in pollers:
            p.stop()
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert aborted is False and len(rows) >= 5
    gaps = [b["ts"] - a["ts"] for a, b in zip(rows, rows[1:])]
    assert all(0.2 <= g <= 0.5 for g in gaps), gaps                                  # 고정 주기 0.3 s (스레드 스케줄링 여유 포함)
    db_calls = {r["sources"]["db"]["t_end"] for r in rows}
    top_calls = {r["sources"]["top"]["t_end"] for r in rows}
    assert len(db_calls) < len(top_calls) <= len(rows)                               # 느린 소스는 더 드물게 읽는다
    assert all(r["sources"][n]["ms"] >= 15 for r in rows for n in ("pods", "db")) and all("tick" in r and "build_ms" in r["tick"] for r in rows)
    assert not any(a["level"] == "ABORT" for r in rows for a in r["alerts"])
