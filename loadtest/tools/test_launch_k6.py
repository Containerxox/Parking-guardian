"""p2_devices.py launch -> k6 scenarios_p2.js (SMOKE 10장) 끝에서 끝까지 (모의 서버): 키가 환경변수로만 k6 에 닿고, 사진 10장이 장비별 4/3/3 으로 접수된다."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
K6DIR = HERE.parent / "k6"
sys.path.insert(0, str(HERE))
import p2_collect as pc  # noqa: E402
from test_p2_integration import mock, wait_drained  # noqa: E402,F401

pytestmark = pytest.mark.skipif(shutil.which("k6") is None, reason="k6 가 없다")
PY = sys.executable


def dev(args, env=None):
    e = dict(os.environ, SUPER_EMAIL="super@mock.invalid", SUPER_PASSWORD="MOCKPW-123456")
    e.update(env or {})
    return subprocess.run([PY, str(HERE / "p2_devices.py"), *args], capture_output=True, text=True, encoding="utf-8", env=e, timeout=120)


def test_launcher_feeds_keys_to_k6_through_the_environment_and_ten_photos_arrive_4_3_3(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    log = tmp_path / "sends.log"
    with mock(tmp_path, "--super-password", "MOCKPW-123456", "--workers", 2, "--service-ms", 50) as m:
        base = ["--base-url", m.base, "--lot", "6"]
        assert dev(["create", *base, "--serials", "RPI-LOAD-01,RPI-LOAD-02", "--apply"]).returncode == 0
        k6 = [shutil.which("k6"), "run", "--quiet", f"--log-output=file={log}", "--log-format=raw", "-e", f"BASE_URL={m.base}", "-e", "API_RATE=0",
              "-e", "P2_SCENARIO=SMOKE", "-e", "STEPS=smoke10:1:10", "-e", "RUN_ID=smoke-itest", "-e", f"OUT_DIR={out}", "scenarios_p2.js"]
        cwd = os.getcwd()
        os.chdir(K6DIR)                                  # scenarios_p2.js 상대 경로(사진 위치)가 k6 폴더 기준이다
        try:
            r = dev(["launch", *base, "--serials", "RPI-E2E-TEST-01,RPI-LOAD-01,RPI-LOAD-02", "--yes-rotate", "--", *k6])
        finally:
            os.chdir(cwd)
        stats = m.stats()
        wait_drained(m.db, 10)
        rows = pc.fetch_sqlite(str(m.db), 0, 4_000_000_000, [])
    assert r.returncode == 0, r.stdout[-600:] + r.stderr[-600:]
    log_text = log.read_text(encoding="utf-8")
    metas, sends = pc.parse_sends(log_text)
    assert len(sends) == 10 and all(s["status"] == 202 and s["attempt"] == 0 for s in sends)
    assert sorted(len(v) for v in stats["arrival_seqs"].values()) == [3, 3, 4] and stats["order_violations"] == []
    assert stats["max_inflight_per_device"] and all(v == 1 for v in stats["max_inflight_per_device"].values())
    assert [x for x in stats["mutations"] if x[0] == "rotate"] == [["rotate", "RPI-E2E-TEST-01"], ["rotate", "RPI-LOAD-01"], ["rotate", "RPI-LOAD-02"]]
    # refresh 연속: 4장 받은 장비는 arrived 1 + refresh 3
    ev = {}
    for s in sends:
        ev.setdefault(s["dev"], []).append((s["seq"], s["event"]))
    assert all(sorted(v)[0] == (1, "arrived") and all(e == "refresh" for _, e in sorted(v)[1:]) for v in ev.values())
    # 키는 어디에도 새지 않았다: 런처 출력, k6 출력, 로그, 요약
    blobs = [r.stdout, r.stderr, log_text, (out / "summary.json").read_text(encoding="utf-8")]
    assert not any("rotated-key" in b or "created-key" in b for b in blobs)
    # 작업은 모두 접수됐고(jobs 10, 세션 3) 판정까지 끝났다
    assert len([j for j in rows["jobs"] if j["status"] in ("done", "stale")]) == 10 and len({j["session_id"] for j in rows["jobs"]}) == 3
