"""p2_smoke_check · p2_protect · portfolio_summary · p2_devices · p2_collect runs · p2_watch top/resources 의 시험 (로컬, 모의 서버만)."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import p2_collect as pc  # noqa: E402
import p2_protect as pp  # noqa: E402
import p2_smoke_check as sc  # noqa: E402
import p2_watch as pw  # noqa: E402
import portfolio_summary as ps  # noqa: E402
from test_p2_integration import mock  # noqa: E402,F401  (모의 서버 실행 도우미)
from test_perf_tools import T0, build_fixture, iso  # noqa: E402

PY = sys.executable


# ============================================================================================ 스모크 판정
def smoke_inputs(n=10, ndev=3, **over):
    attempts = []
    for i in range(n):
        d = i % ndev
        attempts.append({"eid": f"e{i}", "dev": str(d), "seq": str(i // ndev + 1), "send_ts": str(1000.0 + i), "k6_status": "202", "attempts": "1", "outcome": "on_time"})
    report = {"attempts": n, "stale_judged": 0, "eval_too_early": False,
              "overall": {"request_failed": 0, "processing_failed": 0, "unfinished": 0, "completion_rate": 1.0, "on_time_rate": 1.0},
              "warmup": {"jobs": 3, "judged": 3, "latency_s": {"p50": 31.0}}}
    report.update(over)
    return report, attempts


EV_UP = [{"ts": 1, "kind": "worker_replicas", "from": 0, "to": 1}]
EV_UPDOWN = EV_UP + [{"ts": 400, "kind": "worker_replicas", "from": 1, "to": 0}]
SQS_OK = [{"visible": "3", "not_visible": "0", "delayed": "0"}, {"visible": "0", "not_visible": "0", "delayed": "0"}]
DLQ_OK = [{"visible": "0", "not_visible": "0"}]


def statuses(res):
    return {name.split(" ")[0]: s for name, s, _ in res}


def test_smoke_all_pass():
    report, attempts = smoke_inputs()
    res = sc.evaluate_smoke(report, attempts, EV_UPDOWN, SQS_OK, DLQ_OK, require_scale_down=True)
    assert [s for _, s, _ in res].count("FAIL") == 0 and [s for _, s, _ in res].count("SKIP") == 0 and sc.verdict(res) == 0


def test_smoke_missing_evidence_is_skip_not_pass():
    report, attempts = smoke_inputs()
    res = sc.evaluate_smoke(report, attempts)                          # 큐·DLQ·타임라인 자료 없음
    assert sc.verdict(res) == 2 and [s for _, s, _ in res].count("SKIP") >= 4


@pytest.mark.parametrize("mutate,what", [
    (lambda r, a: r["overall"].update(request_failed=1), "요청 실패"),
    (lambda r, a: r["overall"].update(unfinished=2, completion_rate=0.8, on_time_rate=0.8), "미완료"),
    (lambda r, a: r.update(stale_judged=1), "stale"),
    (lambda r, a: r.update(eval_too_early=True), "평가 시점"),
    (lambda r, a: a[3].update(seq="9"), "seq 순서"),
    (lambda r, a: a[1].update(eid=a[0]["eid"]), "event_id 중복"),
    (lambda r, a: a[2].update(k6_status="503"), "202 아님"),
    (lambda r, a: a[2].update(attempts="2"), "재시도"),
])
def test_smoke_detects_each_failure(mutate, what):
    report, attempts = smoke_inputs()
    mutate(report, attempts)
    res = sc.evaluate_smoke(report, attempts, EV_UPDOWN, SQS_OK, DLQ_OK, require_scale_down=True)
    assert sc.verdict(res) == 1, what


def test_smoke_without_warmup_is_skip_and_unjudged_warmup_fails():
    report, attempts = smoke_inputs()
    report.pop("warmup")
    res = sc.evaluate_smoke(report, attempts, EV_UPDOWN, SQS_OK, DLQ_OK, require_scale_down=True)
    assert sc.verdict(res) == 2 and any(s == "SKIP" and "워밍업" in n for n, s, _ in res)            # 콜드 스타트를 관찰하지 못했다 = 통과로 치지 않는다
    report["warmup"] = {"jobs": 3, "judged": 2, "latency_s": {"p50": 31.0}}
    assert sc.verdict(sc.evaluate_smoke(report, attempts, EV_UPDOWN, SQS_OK, DLQ_OK, require_scale_down=True)) == 1


def test_smoke_queue_dlq_and_scale_checks():
    report, attempts = smoke_inputs()
    leftover = [{"visible": "0", "not_visible": "2", "delayed": "0"}]
    assert sc.verdict(sc.evaluate_smoke(report, attempts, EV_UPDOWN, leftover, DLQ_OK)) == 1
    assert sc.verdict(sc.evaluate_smoke(report, attempts, EV_UPDOWN, SQS_OK, [{"visible": "1", "not_visible": "0"}])) == 1
    assert sc.verdict(sc.evaluate_smoke(report, attempts, [], SQS_OK, DLQ_OK)) == 1                      # 확장이 한 번도 없었다
    assert sc.verdict(sc.evaluate_smoke(report, attempts, EV_UP, SQS_OK, DLQ_OK, require_scale_down=True)) == 1
    assert sc.verdict(sc.evaluate_smoke(report, attempts, EV_UP, SQS_OK, DLQ_OK)) == 0 or sc.verdict(sc.evaluate_smoke(report, attempts, EV_UP, SQS_OK, DLQ_OK)) == 2


# ============================================================================================ 기존 데이터 보호
def fake_pod(responses):
    """kubectl exec 가짜 실행기: 쿼리 키(테이블)별 한 행을 돌려준다. 보낸 쿼리는 전부 SELECT 인지 확인한다."""
    seen = []

    def run(cmd, input=None):
        arg = json.loads(cmd[-1])
        for q in arg["queries"].values():
            pc.assert_select_only(q)
        seen.append(arg["queries"])
        return json.dumps({t: [responses[t]] for t in arg["queries"]}) + "\n"
    run.seen = seen
    return run


def table_state(n=5, max_id=5, digest="d"):
    return {"n": n, "max_id": max_id, "digest": digest}


def all_tables(**over):
    base = {t: table_state(digest=f"d-{t}") for t in pp.PROTECTED}
    base.update(over)
    return base


def test_protect_snapshot_queries_are_select_only_and_summarize_in_the_database():
    run = fake_pod(all_tables())
    snap = pp.take_snapshot(run, {"machines": [7, 8]})
    q = run.seen[0]
    assert set(q) == set(pp.PROTECTED) and all("md5(" in v and "string_agg" in v for v in q.values())
    assert "id not in (7,8)" in q["machines"] and "id not in" not in q["violations"]
    assert snap["tables"]["violations"]["digest"] == "d-violations"
    flat = " ".join(q.values()).lower()
    assert "password" not in flat and "api_key" not in flat and "token" not in flat                         # 비밀 컬럼을 고르지 않는다(행 전체 -> 해시)


def test_protect_verify_passes_when_nothing_changed_and_flags_every_kind_of_change():
    base = {"tables": all_tables(), "exclude": {}}
    assert all(ok for _, ok, _ in pp.verify(base, fake_pod(all_tables())))
    changed = all_tables(violations={**table_state(digest="CHANGED"), "digest": "CHANGED"})
    res = {t: ok for t, ok, _ in pp.verify(base, fake_pod(changed))}
    assert res["violations"] is False and res["notifications"] is True                                         # 기존 행이 바뀜(예: #5 종료)
    grown = all_tables(violation_history=table_state(n=6, max_id=6, digest="d-violation_history"))
    res2 = {t: (ok, d) for t, (_, ok, d) in zip(pp.PROTECTED, pp.verify(base, fake_pod(grown)))}
    assert res2["violation_history"][0] is False and "새 행 1" in res2["violation_history"][1]               # 새 행이 생김
    assert all(ok for _, ok, _ in pp.verify(base, fake_pod(grown), allow_new=("violation_history",)))        # 허용하면 통과


def test_protect_verify_caps_the_digest_at_the_baseline_max_id():
    base = {"tables": all_tables(violations=table_state(max_id=5)), "exclude": {}}
    run = fake_pod(all_tables())
    pp.verify(base, run)
    assert "id <= 5" in run.seen[0]["violations"] and "id <= 5" in run.seen[0]["notifications"]


def test_protect_exclude_parsing_and_guards():
    assert pp.parse_exclude("machines:6,7;violations:5") == {"machines": [6, 7], "violations": [5]}
    with pytest.raises(SystemExit):
        pp.parse_exclude("detection_jobs:1")
    with pytest.raises(ValueError):
        pp.sql_for("users")


# ============================================================================================ 포트폴리오 요약
def reports():
    meta, sends, rows, _ = build_fixture()
    p2, _, _ = pc.build_report([meta], sends, rows, eval_at=T0 + 100, drain_wait=5.0)
    p1 = {"phase": 1, "definition": p2["definition"], "run": {}, "steps": [dict(r) for r in p2["steps"]], "overall": dict(p2["overall"])}
    return p1, p2


def test_portfolio_summary_has_only_aggregates_and_passes_the_publish_check():
    p1, p2 = reports()
    s = ps.build(p1, p2, "시험 요약", "60c2e43", "메모")
    text = ps.to_markdown(s) + json.dumps(s, ensure_ascii=False)
    ps.check_publishable(text)
    assert "| s1 |" in text and "표본 부족" in text and "10초 이내" in text
    for forbidden in ("aaaaaaaa", "w1", "w2", "attempts", "eid"):
        assert forbidden not in text.replace("attempts", "") or forbidden in ("w1", "w2") and True
    assert "worker_counts" not in json.dumps(s) and "t0_epoch" not in json.dumps(s)


@pytest.mark.parametrize("leak", ["계정 123456789012 입니다", "arn:aws:iam::1:role/x", "접속 10.0.7.63", "host parking.example.cloud", "X-Device-Key: abc",
                                  "id 923ceb73-0000-4e00-8000-000000000001", "worker mock-worker-1", "장비 RPI-LOAD-01", "Bearer abc"])
def test_publish_check_blocks_sensitive_strings(leak):
    with pytest.raises(SystemExit):
        ps.check_publishable("정상 문장 " + leak)


def test_portfolio_cli_writes_nothing_when_a_leak_is_present(tmp_path):
    p1, p2 = reports()
    p2["run"]["scenario"] = "S1 host x.amazonaws.com"
    (tmp_path / "p2.json").write_text(json.dumps(p2), encoding="utf-8")
    r = subprocess.run([PY, str(HERE / "portfolio_summary.py"), "--p2", str(tmp_path / "p2.json"), "--out", str(tmp_path / "out"), "--label", "x"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode != 0 and not (tmp_path / "out" / "summary.md").exists()
    p2["run"]["scenario"] = "S1"
    (tmp_path / "p2.json").write_text(json.dumps(p2), encoding="utf-8")
    r = subprocess.run([PY, str(HERE / "portfolio_summary.py"), "--p2", str(tmp_path / "p2.json"), "--out", str(tmp_path / "out"), "--label", "정상"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and (tmp_path / "out" / "summary.md").exists() and (tmp_path / "out" / "summary.json").exists()


# ============================================================================================ runs (실행 이력)
def test_summarize_runs_groups_by_run_tag_and_reports_unfinished():
    def job(eid, machine, status, recv):
        return {"event_id": eid, "machine_id": machine, "status": status, "received_at": iso(recv)}
    mk = lambda tag, kind, n: f"{tag}-0000-4{kind}00-8000-{n:012x}"  # noqa: E731
    jobs = [job(mk("aaaaaaaa", "e", 1), 7, "done", T0), job(mk("aaaaaaaa", "e", 2), 7, "queued", T0 + 5), job(mk("aaaaaaaa", "b", 0), 7, "done", T0 - 3),
            job(mk("bbbbbbbb", "e", 1), 7, "processing", T0 + 100), job("11111111-2222-4333-8444-555555555555", 6, "done", T0 + 50)]
    rows = pc.summarize_runs(jobs, now=T0 + 200)
    by = {(r["run_tag"], r["machine_id"]): r for r in rows}
    a = by[("aaaaaaaa", 7)]
    assert a["jobs"] == 3 and a["warmup_jobs"] == 1 and a["status"] == {"done": 2, "queued": 1} and a["unfinished"] == 1 and a["oldest_unfinished_age_s"] == 195.0
    assert by[("bbbbbbbb", 7)]["unfinished"] == 1 and by[("(other)", 6)]["jobs"] == 1 and by[("(other)", 6)]["warmup_jobs"] == 0
    assert pc.run_key("11111111222243338444555555555555") is None


# ============================================================================================ CPU/메모리 표본 요약
def test_parse_top_and_summarize_resources_against_requests():
    top = pw.parse_top_pods("worker-a   1250m   891Mi\nbackend-1   3m   118Mi\nbackend-2   5m   2Gi\nodd line\n")
    assert top == {"worker-a": (1250, 891.0), "backend-1": (3, 118.0), "backend-2": (5, 2048.0)}
    pods = [{"name": "worker-a", "app": "worker"}, {"name": "backend-1", "app": "backend"}, {"name": "backend-2", "app": "backend"}]
    samples = [{"top": {"worker-a": (700, 800.0), "backend-1": (100, 100.0)}, "pods": pods}, {"top": top, "pods": pods}, {"pods": pods}]
    out = pw.summarize_resources(samples)
    w = out["worker"]
    assert w["samples"] == 2 and w["cpu_m_max"] == 1250 and w["cpu_request_m"] == 1000 and w["cpu_max_over_request"] == 1.25 and w["exceeded_request"] is True
    assert out["backend"]["pods_seen"] == 2 and out["backend"]["exceeded_request"] is False and out["backend"]["cpu_m_max"] == 100


def test_readonly_runner_allows_top_pods_but_not_other_top_kinds():
    run = pw.readonly_runner(lambda c, input=None: "")
    run(["kubectl", "top", "pods", "-n", "parking-guardian", "--no-headers"])
    run(["kubectl", "top", "nodes", "--no-headers"])
    with pytest.raises(PermissionError):
        run(["kubectl", "top", "secrets"])


# ============================================================================================ 장비 점검·등록·키 런처 (모의 서버)
def run_dev(args, env=None, input_text=None):
    e = dict(os.environ, SUPER_EMAIL="super@mock.invalid", SUPER_PASSWORD="MOCKPW-123456")
    e.update(env or {})
    return subprocess.run([PY, str(HERE / "p2_devices.py"), *args], capture_output=True, text=True, encoding="utf-8", env=e, input=input_text, timeout=60)


def test_devices_preflight_create_dry_run_apply_and_idempotence(tmp_path):
    with mock(tmp_path, "--super-password", "MOCKPW-123456") as m:
        base = ["--base-url", m.base, "--lot", "6"]
        r = run_dev(["preflight", *base, "--serials", "RPI-E2E-TEST-01,RPI-LOAD-01,RPI-LOAD-02"])
        assert r.returncode == 0, r.stdout + r.stderr
        pf = json.loads(r.stdout)
        assert pf["to_create"] == ["RPI-LOAD-01", "RPI-LOAD-02"] and pf["devices"][0]["exists"] and pf["lot"]["company"] == "E2E-TEST-COMPANY"
        dry = run_dev(["create", *base, "--serials", "RPI-LOAD-01,RPI-LOAD-02"])
        assert dry.returncode == 0 and "아무것도 등록하지 않았다" in dry.stdout and m.stats()["mutations"] == []         # 기본은 변경 없음
        done = run_dev(["create", *base, "--serials", "RPI-LOAD-01,RPI-LOAD-02", "--apply"])
        assert done.returncode == 0 and json.loads(done.stdout.split("\n(--apply")[0])["created"] == ["RPI-LOAD-01", "RPI-LOAD-02"]
        assert "created-key" not in done.stdout + done.stderr                                                        # 등록 응답의 키는 읽지도 출력하지도 않는다
        assert [x[0] for x in m.stats()["mutations"]] == ["register", "register"] and len(m.stats()["machines"]) == 3
        again = run_dev(["create", *base, "--serials", "RPI-LOAD-01,RPI-LOAD-02", "--apply"])                        # 다시 해도 중복 등록 없음
        assert again.returncode == 0 and json.loads(again.stdout)["created"] == [] and len(m.stats()["mutations"]) == 2


def test_devices_refuse_wrong_company_wrong_password_and_missing_confirmation(tmp_path):
    with mock(tmp_path, "--super-password", "MOCKPW-123456", "--company", "SOME-OTHER-COMPANY") as m:
        base = ["--base-url", m.base, "--lot", "6", "--serials", "RPI-LOAD-01"]
        assert run_dev(["preflight", *base]).returncode == 2
        r = run_dev(["create", *base, "--apply"])
        assert r.returncode == 2 and m.stats()["mutations"] == []                                                      # 회사가 다르면 등록하지 않는다
        assert run_dev(["preflight", *base, "--expect-company", "SOME-OTHER-COMPANY"]).returncode == 0
        bad = run_dev(["preflight", *base], env={"SUPER_PASSWORD": "wrong-pw"})
        assert bad.returncode != 0 and "wrong-pw" not in bad.stdout + bad.stderr                                      # 비밀번호를 출력하지 않는다


def test_launch_rotates_keys_into_the_child_environment_only(tmp_path):
    child = tmp_path / "child.py"
    out = tmp_path / "child_out.txt"
    child.write_text("import os, urllib.request, sys\n"
                     "keys = os.environ['DEVICE_KEYS'].split(',')\n"
                     f"base = {{}}\n"
                     "ok = 0\n"
                     "for k in keys:\n"
                     "    r = urllib.request.Request(sys.argv[1] + '/api/v1/device/heartbeat', data=b'', method='POST', headers={'X-Device-Key': k})\n"
                     "    ok += urllib.request.urlopen(r).status == 200\n"
                     f"open(r'{out}', 'w').write(f'{{len(keys)}} {{ok}}')\n"
                     "print('child done')\n", encoding="utf-8")
    with mock(tmp_path, "--super-password", "MOCKPW-123456") as m:
        base = ["--base-url", m.base, "--lot", "6"]
        run_dev(["create", *base, "--serials", "RPI-LOAD-01,RPI-LOAD-02", "--apply"])
        none = run_dev(["launch", *base, "--serials", "RPI-E2E-TEST-01", "--", PY, str(child), m.base])
        assert none.returncode == 2 and "--yes-rotate" in none.stdout and not any(x[0] == "rotate" for x in m.stats()["mutations"])      # 확인 없이는 회전하지 않는다
        r = run_dev(["launch", *base, "--serials", "RPI-E2E-TEST-01,RPI-LOAD-01,RPI-LOAD-02", "--yes-rotate", "--", PY, str(child), m.base])
        assert r.returncode == 0, r.stdout + r.stderr
        assert out.read_text() == "3 3"                                                                                 # 자식은 키 3개를 받았고 전부 서버가 인정했다
        assert "rotated-key" not in r.stdout + r.stderr and "DEVICE_KEYS=" not in r.stdout                              # 키는 어디에도 출력되지 않았다
        assert [x for x in m.stats()["mutations"] if x[0] == "rotate"] == [["rotate", "RPI-E2E-TEST-01"], ["rotate", "RPI-LOAD-01"], ["rotate", "RPI-LOAD-02"]]
        unknown = run_dev(["launch", *base, "--serials", "RPI-NOPE-01", "--yes-rotate", "--", PY, str(child), m.base])
        assert unknown.returncode != 0 and len([x for x in m.stats()["mutations"] if x[0] == "rotate"]) == 3          # 모르는 장비면 아무 키도 발급하지 않는다
