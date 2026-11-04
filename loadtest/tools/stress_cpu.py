"""시험용 CPU 부하 발생기(로컬 전용): 바쁜 루프 N 개를 SECONDS 초 동안 돌린다. 간헐 실패가 PC 부하 때문인지 재현해 보려는 용도."""
import multiprocessing as mp
import sys
import time


def burn(deadline):
    x = 0
    while time.time() < deadline:
        x = (x * 31 + 7) % 1000003


if __name__ == "__main__":
    n, secs = int(sys.argv[1]), float(sys.argv[2])
    end = time.time() + secs
    procs = [mp.Process(target=burn, args=(end,)) for _ in range(n)]
    [p.start() for p in procs]
    [p.join() for p in procs]
