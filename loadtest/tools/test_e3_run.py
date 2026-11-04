"""e3_run.py(B0 수정판) 시험. 실제 kubectl·aws·k6 를 실행하지 않는다(conftest 가 막는다): 가짜 kubectl 응답과 임시 폴더만 쓴다.
확인: 하드코딩 제거, 스냅샷 검증, k6 노드 선택, inference/backend 2개·Worker 0·HPA<=2 즉시 중단, k6 CPU 경고 vs 무효, 새 결과 폴더만 사용,
기존 사전 점검(P3 고정·엔드포인트 2·남은 k6 Pod)과의 공존, --dry-run 이 쓰기 명령을 막는지."""
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e3_run as e3  # noqa: E402


# ============================================================================================ 가짜 JSON 만들기
def pod(name, app, node, phase="Running", ready=True, cpu="1", restarts=0, oom=False, ns="parking-guardian"):
    cs = {"name": app, "restartCount": restarts, "state": {"running": {}}}
    if oom:
        cs["lastState"] = {"terminated": {"reason": "OOMKilled"}}
    return {"metadata": {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/name": app}},
            "spec": {"nodeName": node, "containers": [{"name": app, "resources": {"requests": {"cpu": cpu}}}]},
            "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}], "containerStatuses": [cs]}}


def node(name, ready=True, cordon=False, cpu="1930m", ip="10.0.0.1"):
    return {"metadata": {"name": name}, "spec": {"unschedulable": True} if cordon else {},
            "status": {"allocatable": {"cpu": cpu}, "addresses": [{"type": "InternalIP", "address": ip}],
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def hpa(name, mn=2, mx=2, cur=2, des=2, limited=None):
    conds = [{"type": "ScalingLimited", "status": "True", "reason": limited}] if limited else []
    return {"metadata": {"name": name}, "spec": {"minReplicas": mn, "maxReplicas": mx}, "status": {"currentReplicas": cur, "desiredReplicas": des, "conditions": conds}}


def items(*xs):
    return {"items": list(xs)}


def live_cluster():
    """현재 클러스터와 같은 모양(11-04 점검): inference 2(node-19, node-23), backend 2(node-19, node-7), relay·frontend(node-23)."""
    nodes = items(node("n19", ip="10.0.19.1"), node("n23", ip="10.0.23.1"), node("n7", ip="10.0.7.1"))
    pods = items(pod("inference-a", "inference", "n19"), pod("inference-b", "inference", "n23"),
                 pod("backend-a", "backend", "n19", cpu="200m"), pod("backend-b", "backend", "n7", cpu="200m"),
                 pod("relay-a", "relay", "n23", cpu="50m"), pod("frontend-a", "frontend", "n23", cpu="50m"),
                 pod("cw-n19", "cloudwatch", "n19", cpu="190m", ns="amazon-cloudwatch"), pod("cw-n23", "cloudwatch", "n23", cpu="190m", ns="amazon-cloudwatch"),
                 pod("cw-n7", "cloudwatch", "n7", cpu="390m", ns="amazon-cloudwatch"))
    return nodes, pods


GOOD_PODS = items(pod("inference-a", "inference", "n19"), pod("inference-b", "inference", "n23"),
                  pod("backend-a", "backend", "n19", cpu="200m"), pod("backend-b", "backend", "n7", cpu="200m"),
                  pod("relay-a", "relay", "n23", cpu="50m"), pod("frontend-a", "frontend", "n23", cpu="50m"))
GOOD_HPAS = items(hpa("backend"), hpa("inference"))
WORKER0 = {"spec": {"replicas": 0}, "status": {}}


# ============================================================================================ 1. 하드코딩 제거
def test_no_old_cluster_hardcoding_left():
    src = (HERE / "e3_run.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "ip-10-0-20-149" not in code and "20261031-222457" not in code
    assert not hasattr(e3, "NODE_A") and not hasattr(e3, "SNAP")
    old = (HERE / "e3_run.py.bak-before-b0")
    if old.exists():
        assert "NODE_A" in old.read_text(encoding="utf-8")               # 백업은 원본 그대로 남아 있다


def test_cli_requires_snapshot_and_exposes_dry_run():
    with pytest.raises(SystemExit):
        e3.parse_args(["1", "3"])
    a = e3.parse_args(["1", "3", "--snap", "x", "--dry-run", "--node", "n7"])
    assert (a.first, a.last, a.snap, a.dry_run, a.node, a.prefix) == (1, 3, "x", True, "n7", "b0")


# ============================================================================================ 2. 스냅샷 검증
def make_snapshot(tmp, taken=None, tamper=False, drop=None):
    d = tmp / "snap"
    d.mkdir()
    files = {"pods.json": "{}", "app-root.json": "{}", "app-parking-guardian.json": "{}",
             "meta.json": json.dumps({"taken_at": (taken or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")})}
    sums = []
    for n, t in files.items():
        (d / n).write_bytes(t.encode())
        sums.append(f"{hashlib.sha256(t.encode()).hexdigest()} *{n}")
    (d / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode())
    if tamper:
        (d / "pods.json").write_bytes(b'{"x":1}')
    if drop:
        (d / drop).unlink()
    return d


def test_snapshot_verification(tmp_path):
    ok, why = e3.verify_snapshot(make_snapshot(tmp_path))
    assert ok and not why
    for i, kw in enumerate(({"tamper": True}, {"drop": "meta.json"}, {"taken": dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)})):
        sub = tmp_path / f"c{i}"
        sub.mkdir()
        ok, why = e3.verify_snapshot(make_snapshot(sub, **kw))
        assert not ok and why
    assert e3.verify_snapshot(tmp_path / "없음")[0] is False


# ============================================================================================ 3. k6 노드 선택
def test_pick_node_chooses_node_without_inference_and_enough_cpu():
    nodes, pods = live_cluster()
    name, ip, free, table = e3.pick_node(nodes, pods)
    assert name == "n7" and ip == "10.0.7.1" and free == 1930 - 200 - 390
    assert {t["node"] for t in table if t["eligible"]} == {"n7"}
    assert all("inference" in " ".join(t["why"]) for t in table if t["node"] in ("n19", "n23"))


def test_pick_node_rejects_unsuitable_nodes_and_overrides():
    nodes, pods = live_cluster()
    nodes["items"][2] = node("n7", cordon=True)
    with pytest.raises(RuntimeError, match="k6 를 둘 노드가 없다"):
        e3.pick_node(nodes, pods)
    nodes["items"][2] = node("n7", ready=False)
    with pytest.raises(RuntimeError):
        e3.pick_node(nodes, pods)
    nodes["items"][2] = node("n7", cpu="2000m")
    pods["items"].append(pod("big", "x", "n7", cpu="1950m"))             # 여유 50m < 100m
    with pytest.raises(RuntimeError):
        e3.pick_node(nodes, pods)
    nodes, pods = live_cluster()
    assert e3.pick_node(nodes, pods, override="n7")[0] == "n7"
    with pytest.raises(RuntimeError, match="inference Pod 가 있음"):
        e3.pick_node(nodes, pods, override="n19")                         # 지정해도 inference 노드는 안 된다
    with pytest.raises(RuntimeError, match="클러스터에 없다"):
        e3.pick_node(nodes, pods, override="옛노드")


def test_pick_node_ignores_finished_pods_and_counts_pending_inference():
    nodes, pods = live_cluster()
    pods["items"].append(pod("old-job", "x", "n7", phase="Succeeded", cpu="1500m"))      # 끝난 Pod 의 요청은 세지 않는다
    assert e3.pick_node(nodes, pods)[0] == "n7"
    pods["items"].append(pod("inference-c", "inference", "n7", phase="Pending", ready=False))   # 세 번째 inference 가 뜨면 그 노드는 제외
    with pytest.raises(RuntimeError):
        e3.pick_node(nodes, pods)


# ============================================================================================ 4. 고정 상태 검증
def guard(pods=GOOD_PODS, hpas=GOOD_HPAS, worker=WORKER0, base=None):
    return e3.evaluate_guard(pods, hpas, worker, base)


def test_guard_passes_on_fixed_state():
    g = guard()
    assert g["ok"] and not g["abort_hpa"] and not g["fails"]
    assert g["obs"]["inference"] == 2 and g["obs"]["backend"] == 2 and g["obs"]["worker_pods"] == 0


@pytest.mark.parametrize("mutate,expect", [
    (lambda p, h, w: p["items"].append(pod("inference-c", "inference", "n7")), "inference Pod 정확히 2개"),
    (lambda p, h, w: p["items"].pop(0), "inference Pod 정확히 2개"),
    (lambda p, h, w: p["items"].append(pod("backend-c", "backend", "n23")), "backend Pod 정확히 2개"),
    (lambda p, h, w: p["items"].append(pod("worker-x", "worker", "n7")), "Worker Pod 없음"),
    (lambda p, h, w: w["spec"].update(replicas=1), "worker Deployment replicas 0"),
    (lambda p, h, w: p["items"].append(pod("x", "frontend", "n7", phase="Pending", ready=False)), "Pending Pod 없음"),
    (lambda p, h, w: h["items"][1]["spec"].update(minReplicas=2, maxReplicas=3), "HPA inference min=max=2"),
    (lambda p, h, w: p["items"].__setitem__(0, pod("inference-a", "inference", "n19", ready=False)), "inference Pod 정확히 2개"),
])
def test_guard_fails_on_each_violation(mutate, expect):
    import copy
    p, h, w = copy.deepcopy(GOOD_PODS), copy.deepcopy(GOOD_HPAS), copy.deepcopy(WORKER0)
    mutate(p, h, w)
    g = guard(p, h, w)
    assert not g["ok"] and any(expect in f for f in g["fails"]), g["fails"]


@pytest.mark.parametrize("cur,des", [(3, 2), (2, 3), (3, 3), (4, 2)])
def test_guard_hpa_over_two_is_immediate_abort(cur, des):
    h = items(hpa("backend"), hpa("inference", mn=2, mx=3, cur=cur, des=des, limited="TooManyReplicas"))
    g = guard(hpas=h)
    assert not g["ok"] and g["abort_hpa"] and "inference" in g["abort_hpa"][0]
    assert g["obs"]["hpa"]["inference"]["desired"] == des and g["obs"]["hpa"]["inference"]["scaling_limited"] == "True"


def test_guard_backend_hpa_over_two_also_aborts():
    assert guard(hpas=items(hpa("backend", cur=3, des=3), hpa("inference"))).get("abort_hpa")


def test_guard_detects_restart_growth_and_oom():
    base = e3.restart_baseline_of(GOOD_PODS)
    import copy
    p = copy.deepcopy(GOOD_PODS)
    p["items"][0]["status"]["containerStatuses"][0]["restartCount"] = 1
    g = guard(p, base=base)
    assert not g["ok"] and any("재시작" in f for f in g["fails"])
    p = copy.deepcopy(GOOD_PODS)
    p["items"][2] = pod("backend-a", "backend", "n19", cpu="200m", oom=True)
    g = guard(p, base=base)
    assert not g["ok"] and any("OOMKilled" in f for f in g["fails"]) and g["obs"]["oom"]
    assert guard(base=base)["ok"]                                          # 변화 없으면 통과


def test_guard_without_worker_deployment_still_checks_pods():
    assert guard(worker=None)["ok"]
    p = items(*GOOD_PODS["items"], pod("worker-x", "worker", "n7"))
    assert not guard(p, worker=None)["ok"]


def test_k6_pod_in_loadtest_namespace_does_not_trip_the_parking_guardian_guard():
    # 가드는 parking-guardian 네임스페이스의 `get pods` 결과만 본다: k6 Pod(loadtest)는 거기에 없다
    assert guard(items(*GOOD_PODS["items"]))["ok"]


# ============================================================================================ 5. k6 CPU: 경고 vs 무효
def test_k6_below_threshold_is_ok_and_unknown_is_warning():
    assert e3.assess_k6(0.3, 0.2, None, 0.5, {"a": 1.0, "b": 1.0})["level"] == "ok"
    r = e3.assess_k6(None, None, None, None, {})
    assert r["level"] == "warn" and r["warnings"]


def test_k6_high_cpu_alone_is_only_a_warning():
    r = e3.assess_k6(0.82, 0.6, 0.0, 0.55, {"a": 0.99, "b": 0.98})
    assert r["level"] == "warn" and not r["evidence"] and "0.82" in r["warnings"][0]
    assert e3.assess_k6(0.7, 0.5, None, None, {"a": 1.0, "b": 1.0})["level"] == "warn"      # 경계값 0.7 은 경고 대상, 증거 없음/확인 불가 → 무효 아님


@pytest.mark.parametrize("kw,needle", [
    ({"k6_throttle": 0.08}, "throttling"),
    ({"k6_node_busy": 0.93}, "노드 사용률"),
    ({"inference_cpu_avg": {"a": 0.99, "b": 0.55}}, "inference 평균 CPU"),
])
def test_k6_high_cpu_with_confirmed_bottleneck_is_invalid(kw, needle):
    args = {"k6_cpu_max": 0.9, "k6_cpu_avg": 0.7, "k6_throttle": 0.0, "k6_node_busy": 0.5, "inference_cpu_avg": {"a": 1.0, "b": 1.0}}
    args.update(kw)
    r = e3.assess_k6(args["k6_cpu_max"], args["k6_cpu_avg"], args["k6_throttle"], args["k6_node_busy"], args["inference_cpu_avg"])
    assert r["level"] == "invalid" and any(needle in x for x in r["evidence"])


def test_k6_low_cpu_never_invalid_even_if_other_signals_bad():
    assert e3.assess_k6(0.4, 0.3, 0.5, 0.99, {"a": 0.1, "b": 0.1})["level"] == "ok"        # 병목 판정은 0.7 이상일 때만


# ============================================================================================ 6. 새 결과 폴더만
def test_result_folder_is_new_and_existing_results_are_never_touched(tmp_path, monkeypatch):
    monkeypatch.setattr(e3, "RESULTS", tmp_path)
    old = tmp_path / "2026-10-31-e3n2-3"
    old.mkdir()
    (old / "result.json").write_text("원본", encoding="utf-8")
    day = dt.date(2026, 11, 5)
    f = e3.new_result_folder("b0", 1, day)
    assert f.name == "2026-11-05-b0-1" and not f.exists()
    f.mkdir()
    with pytest.raises(FileExistsError):
        e3.new_result_folder("b0", 1, day)
    assert (old / "result.json").read_text(encoding="utf-8") == "원본"
    s1 = e3.unique_summary_path("b0", day)
    s1.write_text("[]", encoding="utf-8")
    s2 = e3.unique_summary_path("b0", day)
    assert s1 != s2 and s1.read_text(encoding="utf-8") == "[]"            # 요약 파일도 덮어쓰지 않는다
    assert "e3n2" not in f.name


# ============================================================================================ 7. k6 Pod 정의
def test_pod_yaml_uses_chosen_node_and_has_no_cpu_limit():
    d = yaml.safe_load(e3.POD_YAML.format(name="b0-1", node="n7", vus=4, duration="3m"))
    assert d["metadata"]["namespace"] == "loadtest" and d["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "n7"}
    c = d["spec"]["containers"][0]
    assert "cpu" not in c["resources"]["limits"] and c["resources"]["requests"]["cpu"] == "100m"      # CPU limit 없음 → throttling 없음
    assert "--no-connection-reuse" in c["command"] and d["spec"]["restartPolicy"] == "Never"
    envs = {e["name"]: e["value"] for e in c["env"]}
    assert envs["VUS"] == "4" and envs["DURATION"] == "3m" and envs["INFERENCE_URL"].endswith(":8000") and envs["RUN_ID"] == "b0-1"


def test_k6_queries_cover_cpu_throttling_and_memory():
    q = e3.k6_queries("b0-1", 180, 30)
    assert set(q) == {"k6_cpu_avg_cores", "k6_cpu_max_cores", "k6_throttle_ratio", "k6_memory_max_mib"}
    assert 'pod="b0-1"' in q["k6_cpu_avg_cores"] and "cfs_throttled_periods_total" in q["k6_throttle_ratio"]
    assert "cfs_throttled" in e3.inference_throttle_query(180, 30)


# ============================================================================================ 8. --dry-run 은 쓰기를 막는다
@pytest.mark.parametrize("args", [("create", "namespace", "x"), ("-n", "loadtest", "delete", "pod", "p"), ("apply", "-f", "-"), ("-n", "a", "patch", "hpa", "x"),
                                  ("-n", "loadtest", "create", "configmap", "k6-scripts"), ("scale", "deploy/x"), ("delete", "namespace", "loadtest")])
def test_dry_run_blocks_every_write_verb(monkeypatch, args):
    called = []
    monkeypatch.setattr(e3.subprocess, "run", lambda *a, **k: called.append(a) or None)
    monkeypatch.setattr(e3, "DRY_RUN", True)
    with pytest.raises(RuntimeError, match="읽기 전용"):
        e3.kube(*args)
    assert not called                                                      # kubectl 프로세스 자체가 실행되지 않았다


def test_dry_run_allows_reads_and_verb_detection(monkeypatch):
    assert e3.verb_of(("-n", "parking-guardian", "get", "pods")) == "get"
    assert e3.verb_of(("get", "pods", "-A")) == "get" and e3.verb_of(("-n", "loadtest", "-o", "json", "delete")) == "delete"
    seen = []

    class R:
        returncode, stdout, stderr = 0, "{}", ""
    monkeypatch.setattr(e3.subprocess, "run", lambda cmd, **k: seen.append(cmd) or R())
    monkeypatch.setattr(e3, "DRY_RUN", True)
    assert e3.kube("-n", "parking-guardian", "get", "pods", "-o", "json") == "{}"
    assert seen and seen[0][:2] == ["kubectl", "-n"]


class FakeKube:
    """run_one / preflight 시험용 가짜 kubectl. 호출을 기록한다."""

    def __init__(self, pods=GOOD_PODS, hpas=GOOD_HPAS, worker=WORKER0, endpoints=2, leftovers="", pod_phase="Running", k6_pod=None):
        import copy
        self.pods, self.hpas, self.worker = copy.deepcopy(pods), copy.deepcopy(hpas), copy.deepcopy(worker)
        self.endpoints, self.leftovers, self.pod_phase, self.k6_pod = endpoints, leftovers, pod_phase, k6_pod
        self.calls = []
        self.nodes, self.all_pods = live_cluster()

    def __call__(self, *a, data=None, check=True):
        self.calls.append(a)
        joined = " ".join(a)
        if "endpointslices" in joined:
            return json.dumps({"items": [{"endpoints": [{"addresses": [f"10.0.0.{i}"], "conditions": {"ready": True}} for i in range(self.endpoints)]}]})
        if joined.startswith("-n loadtest get pods"):
            return self.leftovers
        if joined.startswith("get nodes"):
            return json.dumps(self.nodes)
        if joined.startswith("get pods -A"):
            return json.dumps(self.all_pods)
        if joined.startswith("-n parking-guardian get pods -o wide"):
            return "\n".join(f"{p['metadata']['name']} 1/1 Running 0 1h" for p in self.pods["items"]) + "\n"
        if joined.startswith("-n parking-guardian get pods"):
            return json.dumps(self.pods)
        if joined.startswith("-n parking-guardian get hpa"):
            return json.dumps(self.hpas)
        if joined.startswith("-n parking-guardian get deploy worker"):
            return json.dumps(self.worker)
        if joined.startswith("-n parking-guardian get deploy"):
            return json.dumps({"items": [{"metadata": {"name": "inference"}, "spec": {"template": {"spec": {"containers": [{"image": "x/inference:60c2e43"}]}}}}]})
        if "jsonpath={.status.phase}" in joined:
            return self.pod_phase
        if joined.startswith("-n loadtest get pod ") and a[-1] == "json":
            return json.dumps(self.k6_pod) if self.k6_pod else "{}"
        return ""

    def verbs(self):
        return [e3.verb_of(c) for c in self.calls]


def patch_env(monkeypatch, fk, tmp_path, p3=(True, [])):
    monkeypatch.setattr(e3, "kube", fk)
    monkeypatch.setattr(e3, "p3_fixed", lambda snap: p3)
    monkeypatch.setattr(e3, "RESULTS", tmp_path)
    monkeypatch.setattr(e3.time, "sleep", lambda s: None)
    monkeypatch.setattr(e3, "prom", lambda *a, **k: "ERR 시험")
    monkeypatch.setattr(e3.subprocess, "Popen", lambda *a, **k: type("P", (), {"terminate": lambda self: None})())
    return {"snap": tmp_path / "snap", "prefix": "b0", "node_override": None}


# ============================================================================================ 9. 기존 사전 점검과의 공존
def test_preflight_passes_with_all_old_and_new_conditions(monkeypatch, tmp_path):
    fk = FakeKube()
    ctx = patch_env(monkeypatch, fk, tmp_path)
    ok, reasons, g = e3.preflight(ctx)
    assert ok and not reasons and ctx["node"] == "n7" and ctx["node_ip"] == "10.0.7.1"


@pytest.mark.parametrize("kw,p3,needle", [
    ({}, (False, ["FAIL  root 자동 동기화 꺼짐"]), "P3 고정 검사 FAIL"),
    ({"endpoints": 3}, (True, []), "엔드포인트 ready=3"),
    ({"endpoints": 1}, (True, []), "엔드포인트 ready=1"),
    ({"leftovers": "b0-1  1/1 Running"}, (True, []), "남은 k6 Pod"),
    ({"worker": {"spec": {"replicas": 1}, "status": {"replicas": 1}}}, (True, []), "고정 상태 검증 FAIL"),
    ({"hpas": items(hpa("backend"), hpa("inference", mn=2, mx=3, cur=3, des=3))}, (True, []), "고정 상태 검증 FAIL"),
])
def test_preflight_reports_each_conflict_without_creating_anything(monkeypatch, tmp_path, kw, p3, needle):
    fk = FakeKube(**kw)
    ctx = patch_env(monkeypatch, fk, tmp_path, p3)
    ok, reasons, _ = e3.preflight(ctx)
    assert not ok and any(needle in r for r in reasons), reasons
    assert set(fk.verbs()) <= {"get"}                                      # 사전 점검은 읽기만 한다


def test_preflight_fails_when_no_node_can_host_k6(monkeypatch, tmp_path):
    fk = FakeKube()
    fk.nodes["items"][2] = node("n7", cordon=True)
    ctx = patch_env(monkeypatch, fk, tmp_path)
    ok, reasons, _ = e3.preflight(ctx)
    assert not ok and any("k6 노드 선택 실패" in r for r in reasons)


# ============================================================================================ 10. 실행 중 즉시 중단 / OOM / 종료
def test_run_aborts_immediately_when_hpa_exceeds_two(monkeypatch, tmp_path):
    fk = FakeKube()
    ctx = patch_env(monkeypatch, fk, tmp_path)
    state = {"n": 0}
    base_call = fk.__call__

    def scripted(*a, data=None, check=True):
        if " ".join(a).startswith("-n parking-guardian get hpa"):
            state["n"] += 1
            if state["n"] >= 2:                                           # 사전 점검(1회) 뒤 첫 감시 때 HPA 가 3 을 요구
                fk.hpas = items(hpa("backend"), hpa("inference", mn=2, mx=3, cur=2, des=3))
        return base_call(*a, data=data, check=check)
    monkeypatch.setattr(e3, "kube", scripted)
    res, good = e3.run_one(ctx, 1)
    assert not good and not res["valid"]
    assert any("HPA current/desired > 2" in r for r in res["invalid_reasons"])
    deleted = [c for c in fk.calls if e3.verb_of(c) == "delete" and "pod" in c]
    assert deleted and "b0-1" in deleted[0]                               # k6 Pod 를 즉시 삭제했다
    assert [c for c in fk.calls if e3.verb_of(c) == "create"] and (tmp_path / list(p.name for p in tmp_path.iterdir() if p.name.endswith("-b0-1"))[0] / "result.json").exists()


def test_run_aborts_when_worker_appears(monkeypatch, tmp_path):
    fk = FakeKube()
    ctx = patch_env(monkeypatch, fk, tmp_path)
    base_call = fk.__call__
    state = {"n": 0}

    def scripted(*a, data=None, check=True):
        if " ".join(a).startswith("-n parking-guardian get pods") and "wide" not in a:
            state["n"] += 1
            if state["n"] >= 3:
                fk.pods = items(*GOOD_PODS["items"], pod("worker-zz", "worker", "n7"))
        return base_call(*a, data=data, check=check)
    monkeypatch.setattr(e3, "kube", scripted)
    res, good = e3.run_one(ctx, 1)
    assert not good and any("Worker Pod 없음" in r for r in res["invalid_reasons"])


def test_run_records_k6_oom_and_marks_invalid(monkeypatch, tmp_path):
    k6 = {"metadata": {"name": "b0-1"}, "status": {"phase": "Failed", "containerStatuses": [{"name": "k6", "restartCount": 0,
                                                              "state": {"terminated": {"reason": "OOMKilled", "finishedAt": "2026-11-05T01:02:03Z"}}}]}}
    fk = FakeKube(pod_phase="Failed", k6_pod=k6)
    ctx = patch_env(monkeypatch, fk, tmp_path)
    res, good = e3.run_one(ctx, 1)
    assert not good and any("OOMKilled" in r for r in res["invalid_reasons"]) and res["k6"]["oom"]
    assert res["warnings"]                                                 # Prometheus 를 못 읽으면 병목 판정 불가를 경고로 남긴다
    assert set(res["k6"]) >= {"cpu_avg_cores", "cpu_max_cores", "throttle_ratio", "memory_max_mib", "node_busy", "assessment"}
    assert set(res["guard"]) >= {"hpa_max_seen", "worker_pods_seen"}


def test_run_without_abort_writes_complete_new_result_files(monkeypatch, tmp_path):
    fk = FakeKube(pod_phase="Succeeded")
    ctx = patch_env(monkeypatch, fk, tmp_path)
    res, good = e3.run_one(ctx, 1)
    folder = next(p for p in tmp_path.iterdir() if p.name.endswith("-b0-1"))
    assert {p.name for p in folder.iterdir()} >= {"result.json", "conditions.md", "k6.log", "prometheus.json", "guard.jsonl", "Pods-after.txt", "SHA256SUMS"}
    assert "k6 요약을 읽지 못함" in res["invalid_reasons"]                 # 빈 로그 → 무효(파싱 실패). 중단(abort)은 아니다
    assert not any("중단" in r for r in res["invalid_reasons"])
    saved = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    assert saved["k6"]["assessment"] == "warn" and saved["image_tags"]["inference"] == "60c2e43"


def test_second_run_with_same_number_refuses_to_overwrite(monkeypatch, tmp_path):
    fk = FakeKube(pod_phase="Succeeded")
    ctx = patch_env(monkeypatch, fk, tmp_path)
    e3.run_one(ctx, 1)
    before = {p.name: p.read_bytes() for p in next(p for p in tmp_path.iterdir() if p.name.endswith("-b0-1")).iterdir()}
    with pytest.raises(FileExistsError):
        e3.run_one(ctx, 1)
    after = {p.name: p.read_bytes() for p in next(p for p in tmp_path.iterdir() if p.name.endswith("-b0-1")).iterdir()}
    assert before == after


def test_preflight_failure_in_run_creates_no_k6_pod(monkeypatch, tmp_path):
    fk = FakeKube(endpoints=1)
    ctx = patch_env(monkeypatch, fk, tmp_path)
    res, good = e3.run_one(ctx, 1)
    assert not good and "사전 점검 실패" in res["aborted"]
    assert "create" not in fk.verbs()
    assert (next(tmp_path.iterdir()) / "ABORTED.txt").exists()


# ============================================================================================ 11. dry-run 전체
def test_dry_run_main_prints_plan_and_creates_nothing(monkeypatch, tmp_path, capsys):
    snap = make_snapshot(tmp_path)
    fk = FakeKube()
    monkeypatch.setattr(e3, "kube", fk)                                    # kube 가 가짜라 DRY_RUN 가드는 시험 8에서 따로 확인
    monkeypatch.setattr(e3, "p3_fixed", lambda s: (False, ["FAIL x"]))
    monkeypatch.setattr(e3, "RESULTS", tmp_path / "results")
    rc = e3.main(["1", "3", "--snap", str(snap), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "k6 노드: n7" in out and "미생성" in out and "아무것도 만들지 않았다" in out
    assert set(fk.verbs()) <= {"get"} and not (tmp_path / "results").exists()
    monkeypatch.setattr(e3, "DRY_RUN", False)


def test_main_refuses_stale_or_missing_snapshot_before_any_write(monkeypatch, tmp_path, capsys):
    fk = FakeKube()
    monkeypatch.setattr(e3, "kube", fk)
    rc = e3.main(["1", "1", "--snap", str(tmp_path / "없음")])
    assert rc == 2 and not fk.calls
