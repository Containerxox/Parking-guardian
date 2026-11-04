"""관찰자 CPU 부하 최적화(2026-11-04) 시험: 토큰 캐시(KubeAuth), 작은 노드 지표 경로, 소스별 주기, DB 끄기.
실제 kubectl/aws 는 실행하지 않는다(conftest 가 막는다) - 가짜 실행기만 쓴다."""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import p2_sampler as sp  # noqa: E402
import p2_watch as pw  # noqa: E402

SECRET_TOKEN = "k8s-aws-v1.CANARYTOKEN-zzz-123"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeKube:
    """kubectl config view / aws eks get-token 만 흉내 낸다. 호출을 센다."""

    def __init__(self, ttl=900, exec_cmd="aws", fail_token=False):
        self.calls, self.ttl, self.exec_cmd, self.fail_token = [], ttl, exec_cmd, fail_token

    def __call__(self, cmd, input=None, timeout=None):
        self.calls.append(list(cmd))
        if cmd[:3] == ["kubectl", "config", "view"]:
            return json.dumps({"clusters": [{"cluster": {"server": "https://x.eks.amazonaws.com", "certificate-authority-data": "Q0E="}}],
                               "users": [{"user": {"exec": {"command": self.exec_cmd, "args": ["--region", "r", "eks", "get-token", "--cluster-name", "c"]}}}]})
        if "get-token" in cmd:
            if self.fail_token:
                raise RuntimeError("get-token 실패")
            return json.dumps({"status": {"token": SECRET_TOKEN, "expirationTimestamp": iso(time.time() + self.ttl)}})
        return "ok"

    def n(self, word):
        return sum(1 for c in self.calls if word in c)


def test_token_cache_runs_get_token_once_for_many_kubectl_calls():
    fk = FakeKube()
    ka = pw.KubeAuth.try_create(fk)
    try:
        run = ka.wrap(fk)
        for _ in range(30):
            run(["kubectl", "get", "pods", "-n", "parking-guardian"])
        assert fk.n("get-token") == 1                                   # 30번 불러도 토큰은 한 번만 받는다 (원래 방식은 호출마다 1번)
        sent = [c for c in fk.calls if c[:2] == ["kubectl", "--kubeconfig"]]
        assert len(sent) == 30 and all(c[3] == "get" for c in sent)     # 명령 본문은 그대로, --kubeconfig 만 앞에 붙는다
        assert all(os.path.samefile(c[2], ka.path) for c in sent)
        doc = json.loads(Path(ka.path).read_text(encoding="utf-8"))
        assert doc["users"][0]["user"]["token"] == SECRET_TOKEN and doc["clusters"][0]["cluster"]["server"].startswith("https://")
    finally:
        ka.close()


def test_token_cache_non_kubectl_commands_untouched_and_dir_removed():
    fk = FakeKube()
    ka = pw.KubeAuth.try_create(fk)
    d = ka.dir
    run = ka.wrap(fk)
    run(["aws", "sqs", "get-queue-attributes"])
    assert fk.calls[-1] == ["aws", "sqs", "get-queue-attributes"]
    ka.close()
    assert not os.path.exists(d)                                         # 종료하면 토큰 파일이 지워진다


def test_token_refreshed_before_expiry_and_old_token_kept_if_refresh_fails():
    fk = FakeKube(ttl=100)                                               # 만료 3분 전 기준보다 짧다 → 다음 호출에서 새로 받는다
    ka = pw.KubeAuth.try_create(fk)
    try:
        assert ka.refreshes == 1
        ka.kubeconfig()
        assert ka.refreshes == 2
        fk.fail_token = True
        before = Path(ka.path).read_text(encoding="utf-8")
        ka.kubeconfig()                                                  # 갱신 실패 → 예외 없이 옛 파일 유지(아직 유효)
        assert Path(ka.path).read_text(encoding="utf-8") == before
    finally:
        ka.close()


def test_token_cache_falls_back_when_not_aws_exec_or_setup_fails():
    assert pw.KubeAuth.try_create(FakeKube(exec_cmd="some-other-plugin")) is None   # 모르는 인증 방식은 건드리지 않는다
    assert pw.KubeAuth.try_create(FakeKube(fail_token=True)) is None                  # 토큰을 못 받으면 원래 방식으로 계속


def test_token_never_appears_in_sample_output_or_errors():
    fk = FakeKube()
    ka = pw.KubeAuth.try_create(fk)
    try:
        blob = json.dumps([c for c in fk.calls], ensure_ascii=False)
        assert SECRET_TOKEN not in blob                                  # 명령 인수(프로세스 목록에 보이는 부분)에 토큰이 없다 - 파일에만 있다
    finally:
        ka.close()
    import ast
    tree = ast.parse((HERE / "p2_watch.py").read_text(encoding="utf-8"))
    for n in ast.walk(tree):                                             # print/로그로 토큰 변수를 내보내지 않는다
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print":
            assert "token" not in ast.dump(n).lower().replace("토큰", ""), ast.dump(n)[:120]


def test_readonly_runner_allows_resource_metrics_path_only_for_known_endpoints():
    run = pw.readonly_runner(lambda cmd, **kw: "{}")
    for ep in ("resource", "cadvisor"):
        assert run(["kubectl", "get", "--raw", f"/api/v1/nodes/ip-10-0-1-2.ap-northeast-2.compute.internal/proxy/metrics/{ep}"]) == "{}"
    for bad in ("/api/v1/nodes/n/proxy/metrics", "/api/v1/nodes/n/proxy/logs/", "/api/v1/nodes/n/proxy/configz", "/api/v1/secrets", "/api/v1/nodes/n/proxy/metrics/resource/../x"):
        with pytest.raises(PermissionError):
            run(["kubectl", "get", "--raw", bad])


RESOURCE_TEXT = """# HELP container_cpu_usage_seconds_total Cumulative cpu time consumed by the container in core-seconds
# TYPE container_cpu_usage_seconds_total counter
container_cpu_usage_seconds_total{container="backend",namespace="parking-guardian",pod="backend-55b9ff54d7-l87lr"} 280.72732 1793792671281
container_cpu_usage_seconds_total{container="inference",namespace="parking-guardian",pod="inference-c86bdd469-6cf9v"} 26.09029 1793792667390
container_cpu_usage_seconds_total{container="coredns",namespace="kube-system",pod="coredns-1"} 9.5 1793792667390
container_memory_working_set_bytes{container="backend",namespace="parking-guardian",pod="backend-55b9ff54d7-l87lr"} 1.31780608e+08 1793792671281
container_memory_working_set_bytes{container="inference",namespace="parking-guardian",pod="inference-c86bdd469-6cf9v"} 8.2860032e+08 1793792667390
pod_cpu_usage_seconds_total{namespace="parking-guardian",pod="backend-55b9ff54d7-l87lr"} 281.0 1793792671281
node_cpu_usage_seconds_total 5000.5 1793792671281
"""


def pods_for(node="ip-10-0-1-2"):
    return [{"name": "backend-55b9ff54d7-l87lr", "app": "backend", "node": node}, {"name": "inference-c86bdd469-6cf9v", "app": "inference", "node": node}]


def test_resource_endpoint_parses_to_same_structure_as_cadvisor_and_uses_small_path():
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        return RESOURCE_TEXT
    cfg = pw.Config(metrics_endpoint="resource")
    out = pw.cadvisor_source(runner, lambda: pods_for(), cfg, time.time)()
    assert seen[0][-1].endswith("/proxy/metrics/resource") and out["endpoint"] == "resource"
    assert set(out["pods"]) == {"backend-55b9ff54d7-l87lr", "inference-c86bdd469-6cf9v"}      # kube-system·pod 합계·노드 합계 제외
    b = out["pods"]["backend-55b9ff54d7-l87lr"]
    assert b["cpu_s"] == pytest.approx(280.72732) and b["mem_bytes"]["backend"] == pytest.approx(131780608) and b["ts_ms"] == 1793792671281
    # cpu_rates 가 이 구조를 그대로 받는다(누적 카운터 → 구간 평균)
    nxt = json.loads(json.dumps(out))
    nxt["pods"]["backend-55b9ff54d7-l87lr"]["cpu_s"] += 3.0
    r = sp.cpu_rates([{"t": 0, "pods": out["pods"]}, {"t": 30, "pods": nxt["pods"]}], {"backend-55b9ff54d7-l87lr": "backend"})
    assert r["backend"]["avg_m"] == pytest.approx(100.0, rel=0.01)


def test_resource_endpoint_empty_for_node_with_watched_pods_is_a_failure_not_silent_zero():
    cfg = pw.Config(metrics_endpoint="resource")
    with pytest.raises(RuntimeError, match="cadvisor"):
        pw.cadvisor_source(lambda cmd, **kw: "# nothing\n", lambda: pods_for(), cfg, time.time)()
    assert pw.cadvisor_source(lambda cmd, **kw: "# nothing\n", lambda: pods_for(), pw.Config(metrics_endpoint="cadvisor"), time.time)()["pods"] == {}


def test_pollers_use_per_source_periods_and_db_can_be_off():
    cfg = pw.Config()
    cfg.queue_url = cfg.dlq_url = "u"
    cfg.queue_name = "q"
    cfg.secret_id = "s"
    cfg.cadvisor_period_s = 30
    noop = lambda *a, **k: "{}"       # noqa: E731
    periods = {p.name: p.period_s for p in pw.build_pollers(noop, pw.AwsViaRunner(noop), cfg)}
    assert periods["pods"] == periods["keda"] == periods["sqs"] == periods["dlq"] == 15
    assert periods["apps"] == 60 and periods["top"] == 30 and periods["db"] == 30 and periods["cadvisor"] == 30
    assert periods["apps"] * 2 >= 120                                                      # 낡음 한도(2×주기)가 15 s 틱보다 충분히 크다
    cfg.db_period_s = 0
    assert "db" not in {p.name for p in pw.build_pollers(noop, pw.AwsViaRunner(noop), cfg)}


def test_default_cli_keeps_tick_15_and_token_cache_on():
    import argparse
    src = (HERE / "p2_watch.py").read_text(encoding="utf-8")
    assert '"--interval", type=float, default=15.0' in src and '"--no-token-cache"' in src and 'default="resource"' in src
    assert argparse  # (CLI 문자열 검사용)


def test_poller_start_delay_staggers_first_read_but_keeps_period():
    clock_calls = []
    pl = sp.Poller("x", lambda: clock_calls.append(time.time()) or 1, 0.2, "x")
    t0 = time.time()
    pl.start(delay=0.5)
    time.sleep(0.3)
    assert pl.snapshot() is None                       # 지연 중에는 아직 읽지 않았다
    time.sleep(0.6)
    pl.stop()
    assert clock_calls and clock_calls[0] - t0 >= 0.49
    assert len(clock_calls) >= 2 and 0.15 <= clock_calls[1] - clock_calls[0] <= 0.35      # 시작 뒤 주기는 그대로


def test_readonly_aws_warm_creates_three_clients_without_network_calls():
    made = []

    class Fake(sp.ReadOnlyAws):
        def _client(self, service):
            made.append(service)
            return object()
    Fake()._warm()
    assert made == ["sqs", "cloudwatch", "secretsmanager"]


# ------------------------------------------------------------------------------------------ KubeAuth 안전성 (2026-11-04 검토)
def test_token_expired_and_refresh_failing_raises_clear_error_without_sending_old_token():
    fk = FakeKube(ttl=900)
    ka = pw.KubeAuth.try_create(fk)
    try:
        ka.expires = time.time() - 1                 # 만료
        fk.fail_token = True
        with pytest.raises(RuntimeError, match="만료"):
            ka.wrap(fk)(["kubectl", "get", "pods"])
        assert not any(c[:2] == ["kubectl", "--kubeconfig"] for c in fk.calls)      # 만료된 토큰으로 kubectl 을 실행하지 않았다
        n = fk.n("get-token")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                ka.kubeconfig()
        assert fk.n("get-token") == n                # 실패 직후에는 재시도 간격(30 s) 동안 get-token 을 다시 실행하지 않는다
        fk.fail_token = False
        ka.next_try = 0
        assert ka.kubeconfig() == ka.path            # 복구되면 다시 쓴다
    finally:
        ka.close()


def test_token_dir_is_user_restricted_and_removed_even_on_exception(tmp_path):
    fk = FakeKube()
    ka = pw.KubeAuth.try_create(fk)
    d = ka.dir
    try:
        if os.name == "nt":
            import subprocess as sp2
            acl = sp2.run(["icacls", d], capture_output=True, text=True).stdout
            assert "Everyone" not in acl and ("BUILTIN" + chr(92) + "Users") not in acl and "Authenticated Users" not in acl       # 다른 사용자 그룹에 열려 있지 않다
        else:
            assert (os.stat(d).st_mode & 0o077) == 0 and (os.stat(ka.path).st_mode & 0o077) == 0
        with pytest.raises(ValueError):
            try:
                raise ValueError("본문 예외")
            finally:
                ka.close()                                   # main 의 finally 와 같은 경로
        assert not os.path.exists(d)
    finally:
        ka.close()


def test_stale_token_dirs_from_killed_runs_are_cleaned_but_recent_ones_kept(tmp_path, monkeypatch):
    import tempfile as tf
    monkeypatch.setattr(tf, "tempdir", str(tmp_path))
    old, new = tmp_path / "p2w-old", tmp_path / "p2w-new"
    old.mkdir(); new.mkdir()
    past = time.time() - 4000
    os.utime(old, (past, past))
    ka = pw.KubeAuth.try_create(FakeKube())
    try:
        assert not old.exists() and new.exists()
    finally:
        ka.close()
