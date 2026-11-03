"""승인된 반복 회차를 순서대로 실행한다: S0-A2 -> S1 2 -> S0-A3 -> S1 3. 회차 전 P3 fixed 확인, 회차 사이 180초 휴식, 하나라도 실패(비정상 종료)하면 중단."""
import os
import subprocess
import sys
import time

PY = sys.executable
ROUNDS = [("s0_run.py", "A2"), ("s1_run.py", "2"), ("s0_run.py", "A3"), ("s1_run.py", "3")]
here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(os.path.dirname(here))
for i, (script, arg) in enumerate(ROUNDS):
    st = subprocess.run([PY, os.path.join(here, "p3.py"), "status", "--expect", "fixed"], cwd=repo, capture_output=True, text=True, encoding="utf-8")
    if st.returncode:
        print("P3 fixed 상태 아님 — 중단\n" + st.stdout[-600:], flush=True)
        sys.exit(3)
    print(f"=== {script} {arg} 시작", flush=True)
    rc = subprocess.run([PY, os.path.join(here, script), arg], cwd=repo).returncode
    print(f"=== {script} {arg} 종료 rc={rc}", flush=True)
    if rc != 0:
        print("비정상 종료(무효/중단) — 다음 회차를 실행하지 않습니다.", flush=True)
        sys.exit(rc)
    if i < len(ROUNDS) - 1:
        print("휴식 180초", flush=True)
        time.sleep(180)
