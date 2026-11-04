"""conftest.py 의 안전장치가 실제로 작동하는지 확인한다."""
import subprocess

import pytest


def test_guard_blocks_cluster_and_cloud_commands():
    with pytest.raises(AssertionError):
        subprocess.run(["kubectl", "get", "pods"])
    with pytest.raises(AssertionError):
        subprocess.run(["aws.exe", "sts", "get-caller-identity"])
    with pytest.raises(AssertionError):
        subprocess.Popen(["terraform", "plan"])
