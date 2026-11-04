"""시험 안전장치: 시험 중에는 kubectl · aws · terraform · helm · docker · psql 을 절대 실행하지 못한다.

이 도구들의 시험은 AWS·클러스터·실제 DB 를 건드리지 않는다는 것이 조건이다. 코드가 (실수로) 그 명령을 실행하려 하면 시험이 즉시 실패한다.
허용: python, k6, git(읽기). 가짜 실행기(runner)나 가짜 kube 를 주입한 시험은 이 명령을 실제로 실행하지 않으므로 영향이 없다.
"""
import os
import subprocess

import pytest

FORBIDDEN = {"kubectl", "aws", "terraform", "helm", "docker", "psql", "argocd", "eksctl"}


def _name(cmd):
    first = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
    return os.path.basename(str(first)).lower().removesuffix(".exe") if isinstance(first, (str, os.PathLike)) else ""


@pytest.fixture(autouse=True)
def no_cluster_or_cloud_commands(monkeypatch):
    real_run, real_popen = subprocess.run, subprocess.Popen

    def guarded_run(cmd, *a, **kw):
        if _name(cmd) in FORBIDDEN:
            raise AssertionError(f"시험 중에는 실행할 수 없는 명령: {_name(cmd)}")
        return real_run(cmd, *a, **kw)

    class GuardedPopen(real_popen):
        def __init__(self, cmd, *a, **kw):
            if _name(cmd) in FORBIDDEN:
                raise AssertionError(f"시험 중에는 실행할 수 없는 명령: {_name(cmd)}")
            super().__init__(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)          # 자격 증명이 있어도 쓰지 않는다는 것을 시험 환경에서 보장
    yield
