"""e3_run.py W1b 시험(2026-11-04): 재질의(W1b-1), S0 스냅샷 검증(W1b-2), throttling 해석(W1b-3), A/B/C 등급·시도 제한, ConfigMap 용량, 토큰 캐시.
실제 kubectl·aws·k6·Prometheus 는 쓰지 않는다(conftest 가 막는다): 가짜 kubectl·가짜 Prometheus 응답과 임시 폴더만 쓴다."""
import copy
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e3_run as e3  # noqa: E402
import test_e3_run as base  # noqa: E402  (픽스처 재사용: FakeKube, live_cluster, make_s0 ...)
from test_e3_run import FakeKube, items, hpa, pod, make_s0, patch_env  # noqa: E402

K6_LOG = """
     inference_done...............: 575 3.107/s
     inference_busy...............: 106 0.57/s
     inference_failed.............: 0   0/s
     inference_unavailable........: 0   0/s
     { phase:measure }...: avg=1.11s min=0.8s med=1.1s max=2.1s p(90)=2.0s p(95)=2.02s p(99)=2.08s
     inference_elapsed_ms.: avg=616ms min=500ms med=610ms max=740ms p(90)=700ms p(95)=720ms p(99)=736ms
     vus..........................: 4   min=4  max=4
"""
FINISH = {"metadata": {"name": "b0-1", "uid": "uid-k6"}, "status": {"phase": "Succeeded", "containerStatuses": [
    {"name": "k6", "restartCount": 0, "state": {"terminated": {"reason": "Completed", "finishedAt": "2026-11-05T01:02:03Z"}}}]}}


class RunKube(FakeKube):
    """Running 을 N번 돌려준 뒤 Succeeded, 종료 뒤에는 k6 로그를 돌려주는 가짜 kubectl."""

    def __init__(self, running_polls=7, log=K6_LOG, **kw):
        super().__init__(**kw)
        self.running, self.log = running_polls, log
        self.k6_pod = FINISH

    def __call__(self, *a, data=None, check=True):
        j = " ".join(a)
        if "jsonpath={.status.phase}" in j:
            self.calls.append(a)
            if self.running > 0:
                self.running -= 1
                return "Running"
            return "Succeeded"
        if j.startswith("-n loadtest logs"):
            self.calls.append(a)
            return self.log
        return super().__call__(*a, data=data, check=check)


def fake_prom(cpu=(0.99, 0.98), k6_cpu=0.30, node_busy=0.4, record=None, fail=()):
    """식의 내용으로 응답을 고른다. cpu=None 이면 inference CPU 시리즈 없음, fail 에 든 부분 문자열이 식에 있으면 조회 오류."""
    def prom(port, expr, at):
        if record is not None:
            record.append((expr, at))
        if any(f in expr for f in fail):
            return "ERR 조회 오류 시험"
        v = lambda x: [at, str(x)]  # noqa: E731
        if 'container="k6"' in expr:
            if "cfs" in expr:
                return []
            return [{"metric": {}, "value": v(k6_cpu if "rate" in expr else 120)}]
        if "rate(container_cpu_usage_seconds_total" in expr and 'container="inference"' in expr and "max_over_time" not in expr:
            return [] if cpu is None else [{"metric": {"pod": "inference-a"}, "value": v(cpu[0])}, {"metric": {"pod": "inference-b"}, "value": v(cpu[1])}]
        if expr.startswith("1 - avg by (instance)"):
            return [{"metric": {"instance": "10.0.7.1:9100"}, "value": v(node_busy)}]
        if "pg_inference_requests_total" in expr:
            return [{"metric": {"pod": "inference-a", "outcome": "ok"}, "value": v(300)}, {"metric": {"pod": "inference-b", "outcome": "ok"}, "value": v(275)}]
        return []
    return prom


def run_clean(monkeypatch, tmp_path, prom, **kw):
    fk = RunKube(**kw)
    ctx = patch_env(monkeypatch, fk, tmp_path)
    monkeypatch.setattr(e3, "prom", prom)
    res, good = e3.run_one(ctx, 1)
    folder = next(p for p in tmp_path.iterdir() if p.name.endswith("-b0-1"))
    return res, good, folder, fk


# ============================================================================================ W1b-1: 저장과 재질의
def test_initial_run_saves_everything_needed_to_requery(monkeypatch, tmp_path):
    rec = []
    res, good, folder, fk = run_clean(monkeypatch, tmp_path, fake_prom(record=rec))
    pr = json.loads((folder / "prometheus.json").read_text(encoding="utf-8"))
    assert pr["origin"] == "initial" and pr["window_s"] == 180 and pr["eval_time"] and pr["eval_time_utc"] and pr["finished_ts"] and pr["finished_utc"]
    assert pr["run"]["start_utc"] and pr["run"]["end_utc"] and pr["run"]["k6_pod"]["name"] == "b0-1" and pr["run"]["k6_pod"]["uid"] == "uid-k6"
    assert [p["name"] for p in pr["run"]["inference_pods"]] == ["inference-a", "inference-b"]
    assert set(pr["queries"]) == set(pr["raw"]) == set(pr["status"]) and "cpu_avg_cores_by_pod" in pr["queries"] and 'container="k6"' in pr["queries"]["k6_cpu_avg_cores"]
    assert all(e in pr["queries"].values() for e, _ in rec)                    # 실제로 보낸 식 = 저장된 식
    assert pr["status"]["cpu_avg_cores_by_pod"] == {"state": "ok", "series": 2}
    assert pr["status"]["k6_throttle_ratio"]["state"] == "empty" and "시리즈 0건" in pr["status"]["k6_throttle_ratio"]["reason"]
    assert "k6_throttle_ratio" in pr["missing"] and "cpu_avg_cores_by_pod" not in pr["missing"]
    assert all(at == pr["eval_time"] for _, at in rec)                         # 모든 질의가 같은 평가 시각
    assert res["grade"]["level"] == "A" and res["prometheus_missing"] == sorted(pr["missing"])
    for f in ("prometheus.json", "result.json", "SHA256SUMS", "guard.jsonl"):
        assert (folder / f).exists()


def test_initial_status_distinguishes_error_empty_ok(monkeypatch, tmp_path):
    res, _, folder, _ = run_clean(monkeypatch, tmp_path, fake_prom(fail=("node_cpu_seconds_total",)))
    st = json.loads((folder / "prometheus.json").read_text(encoding="utf-8"))["status"]
    assert st["node_busy_fraction"]["state"] == "error" and "조회 오류" in st["node_busy_fraction"]["reason"]
    assert st["cpu_avg_cores_by_pod"]["state"] == "ok" and st["waiting_max_by_pod"]["state"] == "empty"


def requery_with(monkeypatch, folder, prom):
    monkeypatch.setattr(e3, "prom", prom)
    monkeypatch.setattr(e3.subprocess, "Popen", lambda *a, **k: type("P", (), {"terminate": lambda self: None})())
    monkeypatch.setattr(e3.time, "sleep", lambda s: None)
    return e3.requery_folder(folder)


def b_grade_run(monkeypatch, tmp_path):
    res, good, folder, _ = run_clean(monkeypatch, tmp_path, fake_prom(cpu=None))
    assert res["grade"]["level"] == "B" and res["grade"]["category"] == "insufficient_evidence"
    return folder


def test_requery_upgrades_b_to_a_only_with_real_cpu_evidence_and_never_overwrites(monkeypatch, tmp_path):
    folder = b_grade_run(monkeypatch, tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    pr = json.loads((folder / "prometheus.json").read_text(encoding="utf-8"))
    rec = []
    out = requery_with(monkeypatch, folder, fake_prom(cpu=(0.97, 0.95), record=rec))
    up = out["upgrade"]
    assert up["upgraded"] and up["original_grade"] == "B" and up["new_grade"] == "A"
    assert up["cpu_by_pod_requery"] == {"inference-a": 0.97, "inference-b": 0.95}
    assert up["evidence"]["requery_file"] == "prometheus-requery-1.json" and up["evidence"]["requery_sha256"] == e3.sha256_file(folder / "prometheus-requery-1.json")
    assert all(at == pr["eval_time"] and expr in pr["queries"].values() for expr, at in rec)         # 같은 식·같은 평가 시각
    rq = json.loads((folder / "prometheus-requery-1.json").read_text(encoding="utf-8"))
    assert rq["origin"] == "requery" and rq["requery_of"] == "prometheus.json" and rq["requery_of_sha256"] == e3.sha256_file(folder / "prometheus.json")
    after = {p.name: p.read_bytes() for p in folder.iterdir() if p.name in before}
    assert after == before                                                        # 원본(prometheus.json, result.json, SHA256SUMS ...)은 그대로
    assert json.loads((folder / "result.json").read_text(encoding="utf-8"))["grade"]["level"] == "B"     # 최초 결과의 등급은 바꾸지 않는다
    for n in ("prometheus-requery-1.json", "grade-upgrade-1.json", "requery-1.sha256"):
        assert (folder / n).exists()
    first = {n: (folder / n).read_bytes() for n in ("prometheus-requery-1.json", "grade-upgrade-1.json", "requery-1.sha256")}
    out2 = requery_with(monkeypatch, folder, fake_prom(cpu=(0.10, 0.10)))         # 두 번째 재질의는 새 번호로, 첫 번째 파일을 덮어쓰지 않는다
    assert out2["requery_file"] == "prometheus-requery-2.json" and not out2["upgrade"]["upgraded"]
    assert {n: (folder / n).read_bytes() for n in first} == first                 # 1번 증거 파일은 바이트 단위로 그대로


@pytest.mark.parametrize("prom_kw", [dict(cpu=None), dict(cpu=None, fail=("rate(container_cpu_usage_seconds_total",))])
def test_requery_keeps_b_when_cpu_still_missing_and_never_fills_values(monkeypatch, tmp_path, prom_kw):
    folder = b_grade_run(monkeypatch, tmp_path)
    out = requery_with(monkeypatch, folder, fake_prom(**prom_kw))
    up = out["upgrade"]
    assert not up["upgraded"] and up["new_grade"] == "B" and "추정하지 않음" in up["reason"]
    assert up["cpu_by_pod_requery"] == {} and "evidence" not in up


def test_requery_partial_cpu_one_pod_is_not_enough(monkeypatch, tmp_path):
    folder = b_grade_run(monkeypatch, tmp_path)

    def one_pod(port, expr, at):
        r = fake_prom(cpu=(0.99, 0.99))(port, expr, at)
        if "rate(container_cpu_usage_seconds_total" in expr and 'container="inference"' in expr and "max_over_time" not in expr and r:
            return r[:1]
        return r
    up = requery_with(monkeypatch, folder, one_pod)["upgrade"]
    assert not up["upgraded"] and up["new_grade"] == "B"


def test_requery_does_not_upgrade_a_c_or_not_saturated_runs(monkeypatch, tmp_path):
    res, _, folder, _ = run_clean(monkeypatch, tmp_path, fake_prom(cpu=(0.6, 0.55)))
    assert res["grade"]["level"] == "B" and res["grade"]["category"] == "not_saturated"
    up = requery_with(monkeypatch, folder, fake_prom(cpu=(0.99, 0.99)))["upgrade"]
    assert not up["upgraded"] and "등급 상승 대상이 아니다" in up["reason"]               # 포화 미달은 재질의로 바꿀 수 없다(값은 이미 알려져 있다)


def test_requery_cli_is_wired(monkeypatch, tmp_path, capsys):
    folder = b_grade_run(monkeypatch, tmp_path)
    monkeypatch.setattr(e3, "enable_token_cache", lambda: None)
    monkeypatch.setattr(e3, "prom", fake_prom(cpu=(0.99, 0.99)))
    rc = e3.main(["--requery", str(folder)])
    assert rc == 0 and "등급 B -> A" in capsys.readouterr().out
    assert e3.parse_args(["--requery", "x"]).requery == "x"


# ============================================================================================ W1b-2: S0 스냅샷 검증
HEAD = lambda: "ad00d85"  # noqa: E731


def test_s0_snapshot_accepted(tmp_path):
    ok, bad, warns = e3.verify_baseline_snapshot(make_s0(tmp_path), config_head_fn=HEAD)
    assert ok and not bad and not warns


def test_s1_fixed_snapshot_is_rejected_with_clear_reason(tmp_path):
    ok, bad, _ = e3.verify_baseline_snapshot(make_s0(tmp_path, root_auto=False, child_auto=False, hpa=(2, 2)), config_head_fn=HEAD)
    assert not ok
    msg = " ".join(bad)
    assert "변경 전 baseline(S0) 스냅샷이 아니다" in msg and "S1" in msg and "HPA backend min/max=2/2" in msg and "자동 동기화가 꺼져 있다" in msg


@pytest.mark.parametrize("kw,needle", [
    (dict(root_auto=False), "Application root 의 자동 동기화가 꺼져"),
    (dict(child_auto=False), "Application parking-guardian 의 자동 동기화가 꺼져"),
    (dict(hpa=(2, 2)), "HPA inference min/max=2/2"),
    (dict(hpa=(1, 3)), "HPA backend min/max=1/3"),
    (dict(tamper=True), "해시 불일치"),
    (dict(drop="hpa-inference.json"), "필수 리소스 파일 없음: hpa-inference.json"),
    (dict(drop="deploy-frontend.json"), "필수 리소스 파일 없음"),
    (dict(no_meta_key="config_repo_head"), "복원 메타데이터 없음: meta.json.config_repo_head"),
    (dict(no_meta_key="taken_at"), "복원 메타데이터 없음: meta.json.taken_at"),
    (dict(taken=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)), "시간 전 것이다"),
    (dict(head="deadbee"), "config 리포 HEAD 불일치"),
])
def test_s0_snapshot_rejections(tmp_path, kw, needle):
    ok, bad, _ = e3.verify_baseline_snapshot(make_s0(tmp_path, **kw), config_head_fn=HEAD)
    assert not ok and any(needle in b for b in bad), bad


def test_s0_config_head_unreadable_is_rejected_and_p2_files_only_warn(tmp_path):
    ok, bad, _ = e3.verify_baseline_snapshot(make_s0(tmp_path), config_head_fn=lambda: None)
    assert not ok and any("확인 불가" in b for b in bad)
    ok, bad, warns = e3.verify_baseline_snapshot(make_s0(tmp_path, p2=False, name="s0b"), config_head_fn=HEAD)
    assert ok and len(warns) == 4                                                   # Phase 2 객체 파일이 없어도 거부하지 않고 경고만


def test_snapshot_file_names_match_what_p3_writes_and_p3_does_not_use_the_validator():
    import p3
    names = {f"app-{a}.json" for a in ("root", "parking-guardian")} | {f"hpa-{h}.json" for h in p3.HPAS} | {f"deploy-{d}.json" for d in p3.PODS_EXPECT}
    names |= {"pods.json", "meta.json"}
    assert set(e3.CORE_SNAPSHOT_FILES) == names
    assert set(e3.P2_SNAPSHOT_FILES) == {f"{k}-{n}.json" for k, n in p3.P2_OBJECTS}
    src = (HERE / "p3.py").read_text(encoding="utf-8")
    assert "e3_run" not in src and "verify_baseline_snapshot" not in src            # p3.py pause/restore 는 이 검증과 독립이다
    assert e3.EXPECTED_HPA_ORIGINAL == (2, 3)


def test_main_and_dry_run_reject_s1_before_any_write(monkeypatch, tmp_path, capsys):
    s1 = make_s0(tmp_path, root_auto=False, child_auto=False, hpa=(2, 2), name="s1")
    fk = FakeKube()
    monkeypatch.setattr(e3, "kube", fk)
    monkeypatch.setattr(e3, "enable_token_cache", lambda: None)
    monkeypatch.setattr(e3, "read_config_head", lambda: "ad00d85")
    assert e3.main(["1", "3", "--snap", str(s1)]) == 2
    assert "S0" in capsys.readouterr().out and not fk.calls
    monkeypatch.setattr(e3, "p3_fixed", lambda s: (True, []))
    monkeypatch.setattr(e3, "DRY_RUN", False)
    assert e3.main(["1", "3", "--snap", str(s1), "--dry-run"]) == 2                  # dry-run 도 S1 을 FAIL 로 보고
    monkeypatch.setattr(e3, "DRY_RUN", False)


# ============================================================================================ W1b-3: throttling 해석
def test_classify_throttle_four_states():
    c = e3.classify_throttle
    assert c(False, "empty", None)["state"] == "no_limit_no_metric" and "경합이 없다는 뜻은 아니다" in c(False, "empty", None)["note"]
    assert c(True, "empty", None)["state"] == "limit_metric_missing"
    assert c(False, "error", None)["state"] == c(True, "error", None)["state"] == "query_error"
    assert c(True, "ok", 0.08)["state"] == "observed" and c(True, "ok", 0.0)["state"] == "none_observed"
    assert c(False, "ok", None)["state"] == "no_limit_no_metric"                      # 시리즈는 있는데 값이 NaN 이면 지표 없음과 같다


def test_assess_k6_throttle_state_changes_warnings_but_never_proves_absence_of_bottleneck():
    hi = dict(k6_cpu_max=0.8, k6_cpu_avg=0.6, k6_node_busy=0.5, inference_cpu_avg={"a": 1.0, "b": 1.0})
    a = e3.assess_k6(hi["k6_cpu_max"], hi["k6_cpu_avg"], None, hi["k6_node_busy"], hi["inference_cpu_avg"], throttle_state="no_limit_no_metric")
    assert a["level"] == "warn" and not any("throttling" in w for w in a["warnings"]) and a["notes"] and "증명이 아니다" in a["caveat"]
    for st, needle in (("limit_metric_missing", "limit 이 있는데"), ("query_error", "조회 오류"), (None, "확인 불가")):
        w = e3.assess_k6(hi["k6_cpu_max"], hi["k6_cpu_avg"], None, hi["k6_node_busy"], hi["inference_cpu_avg"], throttle_state=st)
        assert any(needle in x for x in w["warnings"]), (st, w["warnings"])
    inv = e3.assess_k6(0.8, 0.6, 0.07, 0.5, hi["inference_cpu_avg"], throttle_state="observed")
    assert inv["level"] == "invalid" and any("throttling" in x for x in inv["evidence"])
    low = e3.assess_k6(0.3, 0.2, None, 0.5, hi["inference_cpu_avg"], throttle_state="no_limit_no_metric")
    assert low["level"] == "ok" and "증명이 아니다" in low["caveat"]                    # 'ok' 도 병목 없음의 증명이 아니라고 항상 적는다


def test_run_records_throttle_state_and_no_false_error(monkeypatch, tmp_path):
    res, _, folder, _ = run_clean(monkeypatch, tmp_path, fake_prom())
    assert res["throttling"]["k6"]["state"] == "no_limit_no_metric" and res["throttling"]["inference"]["state"] == "no_limit_no_metric"
    assert not any("throttling" in w for w in res["warnings"])
    assert "경합이 없다는 뜻은 아니다" in res["throttling"]["k6"]["note"] and res["grade"]["caveat"]


def test_run_marks_throttle_query_error_as_warning_when_k6_busy(monkeypatch, tmp_path):
    res, _, _, _ = run_clean(monkeypatch, tmp_path, fake_prom(k6_cpu=0.85, fail=("cfs_throttled_periods_total",)))
    assert res["throttling"]["k6"]["state"] == "query_error" and any("조회 오류" in w for w in res["k6"]["evidence"] + res["warnings"])
    assert res["grade"]["level"] == "A"                                                  # 오류만으로는 무효가 아니다(증거 아님)


# ============================================================================================ A/B/C 등급
def gctx(**kw):
    c = {"aborted": None, "guard_end_fails": [], "restarts": [], "oom": [], "parsed": True, "failed": 0, "unavailable": 0, "guard_samples": 12,
         "guard_max_gap_s": 18.0, "vus_max": 4, "vus_target": 4, "expected_pods": ["inference-a", "inference-b"]}
    c.update(kw)
    return c


OKK6 = {"level": "ok", "warnings": [], "evidence": [], "notes": [], "caveat": ""}
CPU2 = {"inference-a": 0.99, "inference-b": 0.97}


def test_grade_a():
    g = e3.compute_grade(gctx(), CPU2, OKK6)
    assert g["level"] == "A" and g["category"] == "ok" and "최대 처리량의 증거가 아니다" in g["reasons"][0]


@pytest.mark.parametrize("kw,cat", [
    (dict(aborted="HPA current/desired > 2 로 즉시 중단"), "safety_stop"), (dict(guard_end_fails=["Worker Pod 없음"]), "safety_stop"),
    (dict(restarts=["x 재시작"]), "safety_stop"), (dict(oom=["a/b"]), "safety_stop"),
    (dict(parsed=False), "measurement_invalid"), (dict(failed=1), "measurement_invalid"), (dict(unavailable=2), "measurement_invalid"),
    (dict(guard_samples=4), "measurement_invalid"), (dict(guard_max_gap_s=31), "measurement_invalid"), (dict(guard_max_gap_s=None), "measurement_invalid"),
    (dict(vus_max=3), "measurement_invalid"),
])
def test_grade_c(kw, cat):
    g = e3.compute_grade(gctx(**kw), CPU2, OKK6)
    assert g["level"] == "C" and g["category"] == cat


def test_grade_c_when_k6_bottleneck_confirmed():
    inv = {"level": "invalid", "warnings": [], "evidence": ["k6 노드 사용률 93%"], "notes": [], "caveat": ""}
    assert e3.compute_grade(gctx(), CPU2, inv)["level"] == "C"


def test_d3_vus_unknown_is_b_load_unverified_and_vus_below_target_is_c():
    g = e3.compute_grade(gctx(vus_max=None), CPU2, OKK6)
    assert g["level"] == "B" and g["category"] == "load_unverified" and "추정하지 않는다" in g["reasons"][0]
    assert e3.compute_grade(gctx(vus_max=3), CPU2, OKK6)["level"] == "C"
    assert e3.compute_grade(gctx(vus_max=4), CPU2, OKK6)["level"] == "A" and e3.compute_grade(gctx(vus_max=5), CPU2, OKK6)["level"] == "A"
    # VUS 미확인과 CPU 미확인이 겹치면 범주는 load_unverified(재질의로 못 바꿈), 사유는 둘 다 적는다
    both = e3.compute_grade(gctx(vus_max=None), None, OKK6)
    assert both["category"] == "load_unverified" and len(both["reasons"]) == 2
    # 포화 미달과 VUS 미확인이 겹쳐도 B, 범주는 우선순위가 높은 load_unverified
    assert e3.compute_grade(gctx(vus_max=None), {"inference-a": 0.5, "inference-b": 0.5}, OKK6)["category"] == "load_unverified"
    # C 사유가 있으면 VUS 미확인은 B 로 가리지 못한다
    assert e3.compute_grade(gctx(vus_max=None, failed=1), CPU2, OKK6)["level"] == "C"


def test_d6_guard_sample_and_gap_boundaries():
    assert e3.compute_grade(gctx(guard_samples=5), CPU2, OKK6)["level"] == "A"
    c = e3.compute_grade(gctx(guard_samples=4), CPU2, OKK6)
    assert c["level"] == "C" and "표본 4개 < 5" in " ".join(c["reasons"])
    assert e3.compute_grade(gctx(guard_max_gap_s=30.0), CPU2, OKK6)["level"] == "A"
    c = e3.compute_grade(gctx(guard_max_gap_s=30.01), CPU2, OKK6)
    assert c["level"] == "C" and "공백" in " ".join(c["reasons"])
    assert e3.MIN_GUARD_SAMPLES == 5 and e3.MAX_GUARD_GAP_S == 30


def test_max_gap_counts_start_samples_and_end():
    assert e3.max_gap([10, 20, 35], 0, 40) == 15
    assert e3.max_gap([], 0, 40) == 40
    assert e3.max_gap([5], 0, 100) == 95                                                # 끝쪽 공백도 센다
    assert e3.max_gap([30, 10], 0, 30) == 20 and e3.max_gap([10, 20, 30], 0, 30) == 10    # 순서와 상관없이 정렬해서 계산


@pytest.mark.parametrize("cpu", [None, {}, {"inference-a": 0.99}, {"inference-a": 0.99, "inference-x": 0.99}, {"inference-a": 0.99, "inference-b": None}])
def test_grade_b_when_cpu_unknown_or_incomplete(cpu):
    g = e3.compute_grade(gctx(), cpu, OKK6)
    assert g["level"] == "B" and g["category"] == "insufficient_evidence" and "추정하지 않는다" in g["reasons"][0]


def test_grade_b_not_saturated_and_boundary():
    assert e3.compute_grade(gctx(), {"inference-a": 0.99, "inference-b": 0.79}, OKK6)["category"] == "not_saturated"
    assert e3.compute_grade(gctx(), {"inference-a": 0.8, "inference-b": 0.8}, OKK6)["level"] == "A"                  # 경계 0.8 은 포화로 본다


def test_busy_503_share_does_not_change_grade():
    # 사전 등록 v1: 실패율 기준은 inference_failed/unavailable 뿐. busy 503(대기열 상한에 의한 정상 거절)은 등급에 영향이 없다 (이전 C 측정도 15.6~19.8 %)
    assert e3.compute_grade(gctx(), CPU2, OKK6)["level"] == "A"


# ============================================================================================ 시도 제한(A 3회 / 최대 5회)
def R(level, cat="ok", name="x", tp=3.1):
    return {"run": name, "throughput_ok_per_s": tp, "grade": {"level": level, "category": cat}}


def test_next_action_rules():
    assert e3.next_action([R("A")])[0]
    assert not e3.next_action([R("A"), R("A"), R("A")])[0]
    assert e3.next_action([R("A"), R("B", "insufficient_evidence"), R("A")])[0]
    assert not e3.next_action([R("A"), R("A"), R("C", "safety_stop")])[0]                     # 안전 중단은 즉시
    assert not e3.next_action([{"run": "b0-1", "valid": False, "aborted": "사전 점검 실패"}])[0]
    assert not e3.next_action([R("B")] * 5)[0] and "소진" in e3.next_action([R("B")] * 5)[1]
    assert e3.next_action([R("B")] * 4)[0]
    assert not e3.next_action([R("A"), R("C", "measurement_invalid"), R("C", "measurement_invalid"), R("C", "measurement_invalid")])[0]
    assert e3.next_action([R("C", "measurement_invalid"), R("C", "measurement_invalid"), R("A")])[0]


def test_summary_confirms_c_only_with_three_a_runs_and_uses_a_runs_median():
    rs = [R("A", name="r1", tp=3.10), R("B", "insufficient_evidence", name="r2", tp=9.9), R("A", name="r3", tp=3.20), R("C", "measurement_invalid", name="r4", tp=1.0), R("A", name="r5", tp=3.15)]
    s = e3.summarize_attempts(rs)
    assert s["status"] == "C 확정" and s["C_median_ok_per_s"] == 3.15 and s["C_range"] == [3.10, 3.20] and s["C_basis_runs"] == ["r1", "r3", "r5"]
    assert s["grades"] == {"A": 3, "B": 1, "C": 1} and "B 등급은 C 산정에 쓰지 않는다" in s["note"] and "0.8" in s["note"]
    s2 = e3.summarize_attempts(rs[:3])
    assert s2["status"] == "C 미확정" and "A 등급 2회 < 3회" in s2["reason"] and "C_median_ok_per_s" not in s2


class LoopRun:
    """main 루프 시험용: run_one 대신 미리 정한 결과를 하나씩 돌려준다."""

    def __init__(self, seq):
        self.seq, self.n = seq, 0

    def __call__(self, ctx, n):
        r = self.seq[self.n]
        self.n += 1
        return r, r.get("grade", {}).get("level") != "C"


def run_main(monkeypatch, tmp_path, seq, args=("1", "9")):
    snap = make_s0(tmp_path)
    fk = FakeKube()
    monkeypatch.setattr(e3, "kube", fk)
    monkeypatch.setattr(e3, "enable_token_cache", lambda: None)
    monkeypatch.setattr(e3, "read_config_head", lambda: "ad00d85")
    monkeypatch.setattr(e3, "RESULTS", tmp_path / "results")
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(e3.time, "sleep", lambda s: None)
    lr = LoopRun(seq)
    monkeypatch.setattr(e3, "run_one", lr)
    rc = e3.main([*args, "--snap", str(snap)])
    summ = json.loads(next((tmp_path / "results").glob("*-summary*.json")).read_text(encoding="utf-8"))
    return rc, lr.n, summ, fk


def test_main_stops_after_three_a_runs(monkeypatch, tmp_path):
    rc, n, summ, fk = run_main(monkeypatch, tmp_path, [R("A", name=f"r{i}") for i in range(9)])
    assert rc == 0 and n == 3 and summ["summary"]["status"] == "C 확정"
    assert any(e3.verb_of(c) == "delete" and "loadtest" in c for c in fk.calls)


def test_main_stops_at_five_attempts_even_with_b_runs(monkeypatch, tmp_path):
    rc, n, summ, _ = run_main(monkeypatch, tmp_path, [R("B", "insufficient_evidence", name=f"r{i}") for i in range(9)])
    assert n == 5 and summ["summary"]["status"] == "C 미확정"


def test_main_stops_immediately_on_safety_stop(monkeypatch, tmp_path):
    rc, n, summ, _ = run_main(monkeypatch, tmp_path, [R("A", name="r1"), R("C", "safety_stop", name="r2"), R("A", name="r3")])
    assert n == 2 and summ["summary"]["a_runs"] == ["r1"]


def test_summary_file_is_never_overwritten(monkeypatch, tmp_path):
    rc, n, summ, _ = run_main(monkeypatch, tmp_path, [R("A", name=f"r{i}") for i in range(9)])
    first = next((tmp_path / "results").glob("*-summary*.json"))
    first_bytes = first.read_bytes()
    seq = [R("A", name=f"q{i}") for i in range(9)]
    monkeypatch.setattr(e3, "run_one", LoopRun(seq))
    e3.main(["1", "9", "--snap", str(make_s0(tmp_path, name="s0c"))])
    assert first.read_bytes() == first_bytes and len(list((tmp_path / "results").glob("*-summary*.json"))) == 2


# ============================================================================================ ConfigMap 용량
def test_configmap_budget_for_real_files_and_limits():
    b = e3.configmap_budget(e3.k6_configmap_files())
    assert b["ok"] and 0 < b["raw"] < b["worst_case"] < b["limit"] and b["raw_pct"] < 80 and b["worst_pct"] < 100
    assert set(b["parts"]) == {"lib.js", "inference.js", "sample_a.jpg", "sample_b.jpg"} and b["warn"] == (b["worst_pct"] > 90)
    # 중복 없음: 같은 내용의 키가 둘이 아니다
    assert len(set(e3.k6_configmap_files().values())) == 4


def test_configmap_budget_detects_oversize(tmp_path):
    big = tmp_path / "big.jpg"
    big.write_bytes(os.urandom(800_000))
    files = dict(e3.k6_configmap_files())
    files["third.jpg"] = big                                                             # 사진을 하나 더 넣으면 이렇게 된다
    b = e3.configmap_budget(files)
    assert not b["ok"] and b["worst_case"] > b["limit"] and b["warn"]


def test_main_aborts_before_any_write_when_configmap_too_big(monkeypatch, tmp_path, capsys):
    snap = make_s0(tmp_path)
    fk = FakeKube()
    monkeypatch.setattr(e3, "kube", fk)
    monkeypatch.setattr(e3, "enable_token_cache", lambda: None)
    monkeypatch.setattr(e3, "read_config_head", lambda: "ad00d85")
    monkeypatch.setattr(e3, "configmap_budget", lambda files: {"ok": False, "raw": 1, "worst_case": 2_000_000, "limit": e3.CM_LIMIT, "warn": True, "worst_pct": 190, "raw_pct": 100})
    assert e3.main(["1", "3", "--snap", str(snap)]) == 2
    assert "ConfigMap 크기 초과 위험" in capsys.readouterr().out and not fk.calls


def test_configmap_creation_failure_cleans_namespace_without_creating_pod(monkeypatch, tmp_path, capsys):
    snap = make_s0(tmp_path)
    fk = FakeKube()

    def failing(*a, data=None, check=True):
        fk.calls.append(a)
        if "configmap" in a and e3.verb_of(a) == "create":
            raise RuntimeError("kubectl create configmap: Request entity too large: limit is 3145728")
        return ""
    monkeypatch.setattr(e3, "kube", failing)
    monkeypatch.setattr(e3, "enable_token_cache", lambda: None)
    monkeypatch.setattr(e3, "read_config_head", lambda: "ad00d85")
    monkeypatch.setattr(e3, "RESULTS", tmp_path / "results")
    assert e3.main(["1", "3", "--snap", str(snap)]) == 3
    verbs = [(e3.verb_of(c), "namespace" in c or "ns" in c) for c in fk.calls]
    assert ("delete", True) in verbs and not any(v == "create" and "-f" in c for v, c in zip([e3.verb_of(c) for c in fk.calls], fk.calls))
    assert "ConfigMap 생성 실패" in capsys.readouterr().out and not (tmp_path / "results").exists()


# ============================================================================================ 토큰 캐시(감시 주기)
def test_token_cache_sets_and_restores_kubeconfig_and_closes(monkeypatch, tmp_path):
    class KA:
        path = str(tmp_path / "kc.json")
        closed = 0
        refreshed = 0

        def kubeconfig(self):
            KA.refreshed += 1
            return self.path

        def close(self):
            KA.closed += 1
    ka = KA()
    seen = {}

    def enable():
        seen["old"] = os.environ.get("KUBECONFIG")
        os.environ["KUBECONFIG"] = ka.path
        return ka
    monkeypatch.setenv("KUBECONFIG", "원래값")
    monkeypatch.setattr(e3, "enable_token_cache", enable)
    snap = make_s0(tmp_path)
    monkeypatch.setattr(e3, "read_config_head", lambda: "ad00d85")
    monkeypatch.setattr(e3, "kube", FakeKube())
    monkeypatch.setattr(e3, "RESULTS", tmp_path / "results")
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(e3, "run_one", LoopRun([R("A", name="r1"), R("A", name="r2"), R("A", name="r3")]))
    monkeypatch.setattr(e3.time, "sleep", lambda s: None)
    e3.main(["1", "3", "--snap", str(snap)])
    assert KA.closed == 1 and os.environ["KUBECONFIG"] == "원래값"                  # 종료 시 임시 kubeconfig 를 지우고 환경변수를 되돌린다


def test_loop_interval_and_guard_cadence_constants():
    assert e3.LOOP_SECONDS <= 5 and e3.MAX_GUARD_GAP_S >= 2 * e3.LOOP_SECONDS and e3.MIN_GUARD_SAMPLES >= 5
    assert (e3.TARGET_A, e3.MAX_ATTEMPTS) == (3, 5)


def test_no_token_or_secret_values_in_result_files(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_TOKEN_FOR_TEST", "CANARYTOKEN-w1b-123")
    res, _, folder, _ = run_clean(monkeypatch, tmp_path, fake_prom())
    blob = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in folder.iterdir())
    assert "CANARYTOKEN" not in blob and "k8s-aws-v1" not in blob
    src = (HERE / "e3_run.py").read_text(encoding="utf-8")
    assert "get-secret-value" not in src and "kubectl\", \"get\", \"secret" not in src


def test_guard_error_tolerance_two_then_abort(monkeypatch, tmp_path):
    fk = RunKube(running_polls=50)
    ctx = patch_env(monkeypatch, fk, tmp_path)
    monkeypatch.setattr(e3, "prom", fake_prom())
    base_call = fk.__call__
    state = {"n": 0}

    def flaky(*a, data=None, check=True):
        if " ".join(a).startswith("-n parking-guardian get hpa"):
            state["n"] += 1
            if state["n"] >= 2:                                                          # 사전 점검 뒤부터 계속 조회 실패
                raise RuntimeError("kubectl get hpa: connection refused")
        return base_call(*a, data=data, check=check)
    monkeypatch.setattr(e3, "kube", flaky)
    res, good = e3.run_one(ctx, 1)
    assert not good and res["grade"]["category"] == "safety_stop" and any("감시 불능" in r for r in res["invalid_reasons"])


# ============================================================================================ D3/D4 실제 run_one 경로
def test_run_without_vus_line_in_log_is_b_load_unverified_and_not_upgradable(monkeypatch, tmp_path):
    log = "\n".join(l for l in K6_LOG.splitlines() if not l.strip().startswith("vus"))
    res, good, folder, _ = run_clean(monkeypatch, tmp_path, fake_prom(cpu=None), log=log)
    assert res["grade"]["level"] == "B" and res["grade"]["category"] == "load_unverified" and good
    assert res["vus"] == {"target": 4, "max_observed": None}
    up = requery_with(monkeypatch, folder, fake_prom(cpu=(0.99, 0.99)))["upgrade"]           # CPU 를 얻어도 VUS 미확인은 재질의로 못 올린다
    assert not up["upgraded"] and up["new_grade"] == "B" and "등급 상승 대상이 아니다" in up["reason"]


def test_run_with_vus_below_four_is_c(monkeypatch, tmp_path):
    log = K6_LOG.replace("min=4  max=4", "min=2  max=3")
    res, good, _, _ = run_clean(monkeypatch, tmp_path, fake_prom(), log=log)
    assert res["grade"]["level"] == "C" and not good and any("부하 미달" in r for r in res["invalid_reasons"])


def test_run_with_too_few_guard_samples_is_c(monkeypatch, tmp_path):
    res, good, _, _ = run_clean(monkeypatch, tmp_path, fake_prom(), running_polls=2)
    assert res["grade"]["level"] == "C" and any("표본" in r for r in res["invalid_reasons"])


BUSY_HEAVY = K6_LOG.replace("inference_busy...............: 106 0.57/s", "inference_busy...............: 400 2.17/s")


def test_d4_busy_503_is_recorded_not_invalid_and_never_counted_as_throughput(monkeypatch, tmp_path):
    res, good, folder, _ = run_clean(monkeypatch, tmp_path, fake_prom(), log=BUSY_HEAVY)
    assert res["grade"]["level"] == "A" and good                                          # busy 가 많아도 자동 무효 아님
    tp = res["throughput"]
    assert tp["success_ok"] == 575 and tp["success_per_s"] == 3.107
    assert tp["busy_503"] == 400 and tp["busy_503_per_s"] == 2.17 and tp["busy_503_ratio"] == round(400 / 975, 4)
    assert tp["attempted"] == 975 and tp["attempted_per_s"] == round(3.107 + 2.17, 3)
    assert res["throughput_ok_per_s"] == 3.107 != tp["attempted_per_s"]                  # C 산정값은 성공 처리량(시도율이 아님)
    assert tp["success_per_s_over_measure_window"] == round(575 / 180, 3)                 # 측정 구간 평균(3.194)은 참고, k6 값(3.107)이 더 보수적
    assert tp["success_per_s"] <= tp["success_per_s_over_measure_window"]
    assert "busy 503·시도율은 처리량이 아니다" in tp["basis"]
    assert "시도 합계 975건" in (folder / "conditions.md").read_text(encoding="utf-8")
    s = e3.summarize_attempts([dict(res, run="r1"), dict(res, run="r2"), dict(res, run="r3")])
    assert s["status"] == "C 확정" and s["C_median_ok_per_s"] == 3.107                 # 요약의 C 는 성공 처리량 중앙값


def test_d4_server_side_cross_check_warns_when_totals_disagree(monkeypatch, tmp_path):
    def prom_big(port, expr, at):
        r = fake_prom()(port, expr, at)
        if "pg_inference_requests_total" in expr:
            return [{"metric": {"pod": "inference-a", "outcome": "ok"}, "value": [at, "400"]}, {"metric": {"pod": "inference-b", "outcome": "ok"}, "value": [at, "400"]}]
        return r
    res, _, _, _ = run_clean(monkeypatch, tmp_path, prom_big)
    assert res["throughput"]["server_ok_per_s"] == round(800 / 180, 3) and any("서버 집계" in w for w in res["warnings"])
    assert res["grade"]["level"] == "A"                                                   # 경고일 뿐 등급은 바뀌지 않는다


def test_throughput_breakdown_handles_missing_values():
    tp = e3.throughput_breakdown(None, None, None, None, None, None, None, None, {}, 180)
    assert tp["attempted"] == 0 and tp["busy_503_ratio"] is None and tp["server_ok_per_s"] is None and tp["success_per_s_over_measure_window"] is None


def test_existing_result_folders_are_never_touched_by_a_new_run(monkeypatch, tmp_path):
    old = tmp_path / "2026-10-31-e3n2-3"
    old.mkdir()
    (old / "result.json").write_text("원본", encoding="utf-8")
    snap_before = (old / "result.json").read_bytes()
    run_clean(monkeypatch, tmp_path, fake_prom())
    assert (old / "result.json").read_bytes() == snap_before and [p.name for p in old.iterdir()] == ["result.json"]
