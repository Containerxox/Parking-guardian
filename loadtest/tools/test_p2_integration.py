"""k6 + 로컬 모의 서버 통합 시험 (127.0.0.1 만 사용, AWS·클러스터·실제 DB 접속 없음). 장비 키·비밀번호는 모두 시험용 가짜 값이다.
실행: backend\\venv 의 python -m pytest loadtest/tools/test_p2_integration.py -q   (k6 가 PATH 에 있어야 한다. 약 3~5분)
"""
import contextlib
import csv
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
K6DIR = HERE.parent / "k6"
K6 = shutil.which("k6")
sys.path.insert(0, str(HERE))
import p1_points  # noqa: E402
import p2_collect as pc  # noqa: E402

pytestmark = pytest.mark.skipif(K6 is None, reason="k6 가 없다")
KEYS = "CANARYKEY-aaaaaa,CANARYKEY-bbbbbb,CANARYKEY-cccccc"
KEYMAP = "CANARYKEY-aaaaaa:7,CANARYKEY-bbbbbb:8,CANARYKEY-cccccc:9"
ADMIN_PW = "CANARYPW-zzzzzz9"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def mock(tmp_path, *extra):
    port, db = free_port(), tmp_path / "mock.sqlite"
    proc = subprocess.Popen([sys.executable, str(HERE / "p2_mock_server.py"), "--port", str(port), "--db", str(db), "--keys", KEYMAP, *map(str, extra)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/api/v1/readyz", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("모의 서버가 뜨지 않았다")

        def stats():
            return json.loads(urllib.request.urlopen(base + "/_mock/stats", timeout=5).read())
        yield types_ns(base=base, db=db, stats=stats)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def k6(script, envs, tmp_path, name="run", extra=(), raw=True, timeout=240):
    log = tmp_path / f"{name}.log"
    cmd = [K6, "run", "--quiet", f"--log-output=file={log}"] + (["--log-format=raw"] if raw else []) + list(extra)
    for k, v in envs.items():
        cmd += ["-e", f"{k}={v}"]
    r = subprocess.run(cmd + [script], cwd=K6DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r, (log.read_text(encoding="utf-8", errors="replace") if log.exists() else "")


def p2env(m, tmp_path, **kw):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    env = {"BASE_URL": m.base, "DEVICE_KEYS": KEYS, "API_RATE": 0, "P2_SCENARIO": "SMOKE", "RUN_ID": "itest", "OUT_DIR": str(out)}
    env.update(kw)
    return env


def dropped_of(log):
    """k6 가 목표 시각을 못 지켜 건너뛴 슬롯 수(P2DROP). PC 가 바쁘면 생긴다 - 그 회차는 '무효'지만 기능 단언은 그대로 성립해야 한다."""
    return sum(1 for line in log.splitlines() if "P2DROP " in line)


def assert_valid_or_only_dropped_slots(r, log):
    """종료 코드 0(유효) 이거나, 실패한 임계값이 p2_dropped_slots 하나뿐이다(= PC 부하로 인한 무효 판정, 도구가 의도대로 알린 것).
    다른 임계값(4xx, 연결 실패, 중복 id 등)이 실패했다면 진짜 결함이므로 시험을 실패시킨다."""
    if r.returncode == 0:
        return
    assert r.returncode == 99 and dropped_of(log) > 0, r.stdout[-400:] + r.stderr[-400:]
    # 임계값 실패가 dropped_slots 때문인지: k6 요약 한 줄(p2: ... 4xx=N 5xx=N other=N dropped_slots=N)에서 4xx, other 가 0 인지도 본다
    line = r.stdout.strip().splitlines()[-1]
    assert " 4xx=0 " in line + " " and " other=0 " in line + " ", line


def sends_of(log):
    metas, sends = pc.parse_sends(log)
    return metas, sends


def wait_drained(db, expect, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n = con.execute("select count(*) from detection_jobs where status in ('done','stale')").fetchone()[0]
        con.close()
        if n >= expect:
            return n
        time.sleep(0.3)
    return n


# ----------------------------------------------------------------------------------------------------------------
def test_k6_unit_tests_of_the_pure_library():
    r = subprocess.run([K6, "run", "--quiet", "test_lib_p2.js"], cwd=K6DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]
    assert "checks_failed......: 0.00%" in r.stdout


def test_per_device_serialization_order_pacing_and_unique_ids(tmp_path):
    with mock(tmp_path, "--latency-ms", 100, "--jitter-ms", 200, "--workers", 2, "--service-ms", 50) as m:
        r, log = k6("scenarios_p2.js", p2env(m, tmp_path, STEPS="a:6:10,b:3:5"), tmp_path)
        assert_valid_or_only_dropped_slots(r, log)
        metas, sends = sends_of(log)
        stats = m.stats()
    dropped = dropped_of(log)
    meta = metas[0]
    # 1) 장비별 직렬화: 서버가 본 장비별 최대 동시 요청 수 1, 그러나 장비들끼리는 겹친다(전체 직렬이 아니다)
    assert stats["max_inflight_per_device"] == {"7": 1, "8": 1, "9": 1}, stats["max_inflight_per_device"]
    assert stats["max_global_inflight"] >= 2
    # 2) 장비별 순서: 서버 도착 seq 가 1,2,3… 그대로, 순서 위반 0
    assert stats["order_violations"] == []
    for dev, seqs in stats["arrival_seqs"].items():
        assert seqs == list(range(1, len(seqs) + 1)), (dev, seqs[:10])
    # 3) 개수: 계획된 슬롯은 장비당 (6/3)*10 + (3/3)*5 = 25, 합 75 (±3). PC 가 바빠 건너뛴 슬롯(P2DROP)만큼만 덜 보낸다
    assert abs(len(sends) + dropped - 75) <= 3, (len(sends), dropped)
    assert stats["accepted"] == len(sends) == sum(len(v) for v in stats["arrival_seqs"].values())
    assert all(s["status"] == 202 and s["attempt"] == 0 for s in sends)
    # 4) 고유 event_id: 전부 유일, run tag 로 시작, 단계 번호가 id 에 들어 있음, session_id 와 구분
    eids = [s["eid"] for s in sends]
    assert len(set(eids)) == len(eids) and all(e.startswith(meta["tag"]) for e in eids)
    assert {e.split("-")[2] for e in eids} == {"4e00", "4e01"}
    # 5) 도착률(계약): (a) 계획 간격은 정확하다: 장비 0 의 단계 a 는 1/(6/3)=0.5 s, 단계 b 는 1.0 s
    for dev in (0, 1, 2):
        for step, gap in (("a", 500), ("b", 1000)):
            planned = sorted(s["planned_ms"] for s in sends if s["dev"] == dev and s["step"] == step)
            gaps = {b - a for a, b in zip(planned, planned[1:])}
            assert gaps and max(gaps) - min(gaps) <= 2 and abs(next(iter(gaps)) - gap) <= 2, (dev, step, gaps)
    # (b) 실제 전송은 계획보다 빠르지 않고, 늦어도 LATE_TOL_MS(1000 ms)+여유 안이다. 더 늦은 슬롯은 건너뛰고 P2DROP 으로 센다
    lateness = [s["ts"] - s["planned_ms"] for s in sends]
    assert min(lateness) >= -5 and max(lateness) <= 1000 + 50, (min(lateness), max(lateness))
    # (c) 건너뛴 슬롯이 없었다면(= 유효 회차) 실제 평균 간격도 계획과 같다
    if dropped == 0:
        d0a = sorted(s["ts"] for s in sends if s["dev"] == 0 and s["step"] == "a")
        gaps0 = [(b - a) / 1000 for a, b in zip(d0a, d0a[1:])]
        assert 0.45 <= sum(gaps0) / len(gaps0) <= 0.55, gaps0
    # 6) 첫 사진은 arrived, 이후 refresh. 실패·중복 기록은 0
    first = {s["dev"]: s for s in sends if s["seq"] == 1}
    assert all(f["event"] == "arrived" for f in first.values()) and all(s["event"] == "refresh" for s in sends if s["seq"] > 1)
    assert stats["duplicates"] == 0 and stats["bad_requests"] == 0
    assert (tmp_path / "out" / "summary.json").exists()
    starts = pc.parse_starts(log)
    assert sorted(x["dev"] for x in starts) == [0, 1, 2] and all(x["start_delay_ms"] >= 0 for x in starts)
    assert max(x["scenario_start"] for x in starts) - min(x["scenario_start"] for x in starts) < 500     # 모든 장비 시나리오가 같은 시각에 시작


def test_naive_arrival_rate_model_does_not_preserve_device_order(tmp_path):
    """대조 실험: 같은 모의 서버에서 arrival-rate 실행기는 장비별 순서·직렬화를 보장하지 못한다."""
    with mock(tmp_path, "--latency-ms", 50, "--jitter-ms", 600) as m:
        r, _ = k6("order_probe.js", {"BASE_URL": m.base, "DEVICE_KEYS": KEYS, "RATE": 8, "SECONDS": 12}, tmp_path, "probe")
        assert r.returncode == 0, r.stderr[-500:]
        time.sleep(1.5)
        stats = m.stats()
    assert any(v > 1 for v in stats["max_inflight_per_device"].values()), stats["max_inflight_per_device"]
    assert len(stats["order_violations"]) > 0, "순진한 방식에서도 순서가 지켜졌다면 이 시험이 증거가 못 된다"


def test_http_errors_are_recorded_without_reusing_ids_when_retry_is_off(tmp_path):
    with mock(tmp_path, "--fail-rate", 0.4, "--seed", 3, "--workers", 2) as m:
        r, log = k6("scenarios_p2.js", p2env(m, tmp_path, STEPS="a:6:10", RETRY=0), tmp_path)
        _, sends = sends_of(log)
        stats = m.stats()
        con = sqlite3.connect(f"file:{m.db}?mode=ro", uri=True)
        n_jobs = con.execute("select count(*) from detection_jobs").fetchone()[0]
        con.close()
    assert_valid_or_only_dropped_slots(r, log)                                      # 5xx 만으로는 무효가 아니다(기록한다)
    kinds = {s["kind"] for s in sends}
    assert {"accepted", "http5xx"} <= kinds
    assert all(s["attempt"] == 0 for s in sends) and len({s["eid"] for s in sends}) == len(sends)
    assert n_jobs == sum(1 for s in sends if s["kind"] == "accepted") == stats["accepted"]       # 실패한 요청은 작업을 만들지 않았다
    per_dev = {}
    for s in sends:
        per_dev.setdefault(s["dev"], []).append(s["seq"])
    assert all(v == list(range(1, len(v) + 1)) for v in per_dev.values())            # 실패해도 seq 는 건너뛰지 않고 이어진다(재전송 없음)
    summary = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    assert summary["metrics"]["p2_http5xx"]["values"]["count"] == sum(1 for s in sends if s["kind"] == "http5xx")


def test_lost_response_is_retried_with_the_same_ids_and_becomes_a_duplicate(tmp_path):
    with mock(tmp_path, "--lost-response-rate", 0.5, "--seed", 5, "--workers", 2) as m:
        r, log = k6("scenarios_p2.js", p2env(m, tmp_path, STEPS="a:3:12", RETRY=2), tmp_path)
        _, sends = sends_of(log)
        stats = m.stats()
        con = sqlite3.connect(f"file:{m.db}?mode=ro", uri=True)
        rows = con.execute("select event_id, count(*) from detection_jobs group by event_id having count(*) > 1").fetchall()
        n_jobs = con.execute("select count(*) from detection_jobs").fetchone()[0]
        con.close()
    assert_valid_or_only_dropped_slots(r, log)
    retried = [s for s in sends if s["attempt"] > 0]
    assert retried, "응답 유실이 한 번도 없었다 - 시험이 의미가 없다"
    firsts = {s["eid"]: s for s in sends if s["attempt"] == 0}
    assert all(s["eid"] in firsts and s["seq"] == firsts[s["eid"]]["seq"] for s in retried)       # 같은 event_id, 같은 seq
    assert {"duplicate"} <= {s["kind"] for s in retried}
    assert stats["duplicates"] == sum(1 for s in sends if s["kind"] == "duplicate")
    assert rows == [] and n_jobs == len(firsts)                                        # DB 에는 사진당 작업 1건
    assert len(firsts) == len({s["eid"] for s in sends})


def test_missed_target_rate_marks_the_run_invalid(tmp_path):
    with mock(tmp_path, "--latency-ms", 700) as m:
        r, log = k6("scenarios_p2.js", p2env(m, tmp_path, STEPS="a:9:8"), tmp_path)
        _, sends = sends_of(log)
    assert r.returncode != 0                                                         # 임계값 p2_dropped_slots count==0 실패
    assert "P2DROP" in log
    summary = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    assert summary["metrics"]["p2_dropped_slots"]["values"]["count"] > 0
    assert len(sends) < 9 * 8                                                        # 건너뛴 슬롯만큼 덜 보냈다
    assert all(s["attempt"] == 0 for s in sends)


def test_rejected_device_key_stops_before_any_load(tmp_path):
    with mock(tmp_path) as m:
        r, log = k6("scenarios_p2.js", p2env(m, tmp_path, DEVICE_KEYS="bad-key-1,bad-key-2", STEPS="a:2:5"), tmp_path)
        assert m.stats()["accepted"] == 0
    assert r.returncode != 0 and "거절" in (r.stderr + r.stdout + log)


def test_violation_photos_need_an_explicit_opt_in(tmp_path):
    with mock(tmp_path) as m:
        r, log = k6("scenarios_p2.js", p2env(m, tmp_path, STEPS="a:2:5", VIOLATION_RATIO=0.5), tmp_path)
        assert m.stats()["accepted"] == 0
    assert r.returncode != 0 and "ALLOW_VIOLATIONS" in (r.stderr + r.stdout + log)


def test_end_to_end_pipeline_report_and_no_secret_leak(tmp_path):
    out = tmp_path / "report"
    with mock(tmp_path, "--workers", 2, "--service-ms", 100) as m:
        envs = p2env(m, tmp_path, STEPS="a:6:8", API_RATE=2, ADMIN_EMAIL="admin@example.invalid", ADMIN_PASSWORD=ADMIN_PW, WARMUP_PHOTOS=1, WARMUP_WAIT_S=2)
        r, log = k6("scenarios_p2.js", envs, tmp_path, raw=False)           # k6 기본(text) 로그 형식도 읽을 수 있어야 한다
        assert_valid_or_only_dropped_slots(r, log)
        metas, sends = sends_of(log)
        assert metas and len(sends) + dropped_of(log) >= 40 and len(sends) >= 10
        wait_drained(m.db, len(sends) + 3)                                   # 측정 사진 + 워밍업 3장
        env = dict(os.environ, DEVICE_KEYS=KEYS, ADMIN_PASSWORD=ADMIN_PW)
        logfile = tmp_path / "sends.log"
        logfile.write_text(log, encoding="utf-8")
        cp = subprocess.run([sys.executable, str(HERE / "p2_collect.py"), "report", "--sends", str(logfile), "--source", f"sqlite:{m.db}", "--out", str(out),
                             "--drain-wait", "0", "--machines", "7,8,9"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    rep = json.loads((out / "p2-report.json").read_text(encoding="utf-8"))
    o = rep["overall"]
    assert rep["attempts"] == len(sends) and o["offered"] == len(sends)                  # 워밍업 사진은 시도에 들어가지 않는다
    assert o["request_failed"] == 0 and o["processing_failed"] == 0 and o["unfinished"] == 0
    assert o["on_time_rate"] == 1.0 and o["completion_rate"] == 1.0
    lat = rep["steps"][0]["latency_s"]
    assert lat["n"] == len(sends) and lat["p50"] >= 0.09 and lat["p95"] < 10.0 and lat["p99_reliable"] is False    # 하한=서비스 시간 100 ms, 상한=10 s 계약
    assert sum(rep["worker_counts"].values()) == len(sends) and rep["warmup_jobs_excluded"] == 3          # 워밍업 3장은 통계에서 빠진다
    assert rep["warmup"]["jobs"] == 3 and rep["warmup"]["judged"] == 3 and rep["warmup"]["latency_s"]["n"] == 3   # 워밍업 지연은 따로 보고(콜드 스타트 관찰값)
    assert set(rep["worker_counts"]) <= {"mock-worker-0", "mock-worker-1"}
    assert rep["job_status_counts"].get("done", 0) + rep["job_status_counts"].get("stale", 0) == len(sends)
    stages = rep["stages"]
    assert stages["inference_s"]["p50"] == pytest.approx(0.1) and stages["recv_to_publish_s"]["p50"] == pytest.approx(0.005, abs=0.002)
    for name in ("p2-report.json", "p2-steps.csv", "p2-attempts.csv", "p2-backlog.csv", "p2-workers.csv"):
        assert (out / name).exists() and (out / name).stat().st_size > 0
    with open(out / "p2-attempts.csv", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == len(sends)
    # 비밀 정보: 장비 키·관리자 비밀번호는 k6 출력, 로그, 요약, 수집 결과, 수집 도구 출력 어디에도 없어야 한다
    blobs = [r.stdout, r.stderr, log, cp.stdout, cp.stderr, (tmp_path / "out" / "summary.json").read_text(encoding="utf-8")]
    blobs += [p.read_text(encoding="utf-8") for p in out.iterdir()]
    for secret in ("CANARYKEY-", ADMIN_PW):
        assert not any(secret in b for b in blobs), secret


def test_phase1_script_still_works_and_phase1_report_matches_phase2_step_names(tmp_path):
    """회귀: 수정하지 않은 기존 scenarios.js(Phase 1)가 그대로 돌고, p1_points 가 같은 단계 이름의 보고서를 만든다."""
    with mock(tmp_path, "--p1-service-ms", 30, "--workers", 2, "--service-ms", 30) as m:
        base = {"BASE_URL": m.base, "ADMIN_EMAIL": "admin@example.invalid", "ADMIN_PASSWORD": ADMIN_PW}
        s0, _ = k6("scenarios.js", {**base, "SCENARIO": "S0", "SCALE": 0.02}, tmp_path, "s0")
        assert s0.returncode == 0, s0.stdout[-800:] + s0.stderr[-800:]
        points = tmp_path / "points.json"
        s1, _ = k6("scenarios.js", {**base, "SCENARIO": "S1", "C": 3, "SCALE": 0.02, "DEVICE_KEYS": KEYS, "RUN_ID": "p1reg"}, tmp_path, "s1", extra=[f"--out", f"json={points}"])
        assert s1.returncode == 0, s1.stdout[-800:] + s1.stderr[-800:]
        # Phase 2 의 S1 (같은 C, 같은 SCALE)
        p2 = p2env(m, tmp_path, P2_SCENARIO="S1", C=3, SCALE=0.02, WARMUP_PHOTOS=0)
        r2, log2 = k6("scenarios_p2.js", p2, tmp_path, "p2s1")
        assert r2.returncode == 0, r2.stdout[-800:] + r2.stderr[-800:]
        metas, sends = sends_of(log2)
        wait_drained(m.db, len(sends))
        lo, hi = metas[0]["t0"] / 1000 - 60, metas[0]["t0"] / 1000 + 200
        rows = pc.fetch_sqlite(str(m.db), lo, hi, [7, 8, 9])
    assert [st["name"] for st in metas[0]["steps"]] == ["0.2C", "0.5C", "0.8C", "1.0C", "1.2C", "1.5C"]
    p1 = p1_points.build_report(p1_points.read_points(points), p1_points.parse_steps("", 0.02))
    assert [r["step"] for r in p1["steps"]] == ["0.2C", "0.5C", "0.8C", "1.0C", "1.2C", "1.5C"]
    assert p1["overall"]["offered"] > 0 and p1["overall"]["request_failed"] == 0 and p1["overall"]["on_time_rate"] == 1.0
    p2rep, _, _ = pc.build_report(metas, sends, rows, eval_at=time.time(), drain_wait=0, starts=pc.parse_starts(log2))
    assert p2rep['t0_source'] == 'P2START'
    assert [r["step"] for r in p2rep["steps"]] == [r["step"] for r in p1["steps"]]
    table = p1_points.compare(p1, p2rep)
    assert len(table) == 6 and all(r["p1_offered_per_s"] is not None and r["p2_offered_per_s"] is not None for r in table)
    # 같은 C 에서 양쪽의 시도 수(= 도착률)가 같은 규모: 1.5C 단계 기대값 4.5/s × 5 s ≈ 22
    last = table[-1]
    assert last["p1_offered_per_s"] == pytest.approx(4.5, rel=0.3) and last["p2_offered_per_s"] == pytest.approx(4.5, rel=0.3)
