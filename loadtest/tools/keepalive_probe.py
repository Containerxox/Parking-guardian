"""서버가 유휴 keep-alive 연결을 몇 초 뒤에 닫는지 재는 소형 프로브 (가설 검증용). 부하 테스트가 아니다: 표본당 요청 1건.

    python loadtest/tools/keepalive_probe.py --port 15000 [--host 127.0.0.1] [--path /healthz] [--samples 20] [--max-wait 8] [--gap 0]

동작: 연결 1개를 열고 GET 한 번 → 응답을 다 읽은 뒤 유휴 상태로 두고 서버가 연결을 닫는(EOF) 시각을 잰다(최대 --max-wait 초).
--gap N(>0)이면 닫히기를 기다리는 대신 N초 쉰 뒤 같은 연결로 두 번째 GET 을 보내 재사용이 되는지(성공/끊김) 센다.
쓰는 곳: backend Pod 에 `kubectl port-forward pod/<이름> 15000:5000` 을 연 뒤 --port 15000. 클러스터에는 요청이 표본 수만큼만 간다(읽기 전용 /healthz).
기대: 현재 gunicorn(keep-alive 2초)은 2~3초에 닫는다(gthread 는 최대 1초 간격으로 만료를 확인). --keep-alive 130 이면 --max-wait 안에서 닫히지 않는다.
"""
import argparse
import select
import socket
import statistics
import time


def read_response(sock, timeout=10.0):
    """헤더와 Content-Length 만큼의 본문을 읽는다. 서버가 먼저 끊으면 None."""
    sock.settimeout(timeout)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        length = 0
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(body) < length:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            body += chunk
        return status
    except (socket.timeout, ConnectionError, OSError):
        return None


def request(sock, host, path):
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\n\r\n".encode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--path", default="/healthz")
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--max-wait", type=float, default=8.0)
    ap.add_argument("--gap", type=float, default=0.0)
    a = ap.parse_args()
    closes, kept, reuse_ok, reuse_fail = [], 0, 0, 0
    for i in range(a.samples):
        s = socket.create_connection((a.host, a.port), timeout=5)
        request(s, a.host, a.path)
        if read_response(s) != 200:
            print(f"표본 {i}: 첫 요청 실패 (응답 없음/200 아님)")
            s.close()
            continue
        t0 = time.monotonic()
        if a.gap > 0:
            time.sleep(a.gap)
            try:
                request(s, a.host, a.path)
                ok = read_response(s) == 200
            except OSError:
                ok = False
            reuse_ok += ok
            reuse_fail += (not ok)
            print(f"표본 {i}: {a.gap:.2f}초 유휴 뒤 같은 연결 재사용 -> {'성공' if ok else '끊김/실패'}")
        else:
            closed_at = None
            while time.monotonic() - t0 < a.max_wait:
                r, _, _ = select.select([s], [], [], 0.05)
                if r:
                    try:
                        data = s.recv(1)
                    except OSError:
                        data = b""
                    if not data:
                        closed_at = time.monotonic() - t0
                        break
            if closed_at is None:
                kept += 1
            else:
                closes.append(closed_at)
            print(f"표본 {i}: " + (f"서버가 {closed_at:.2f}초 뒤 연결을 닫음" if closed_at is not None else f"{a.max_wait:.0f}초 안에 닫히지 않음"))
        s.close()
    if a.gap > 0:
        print(f"\n요약: gap={a.gap}s 재사용 성공 {reuse_ok} / 끊김 {reuse_fail}")
    else:
        print(f"\n요약: 닫힌 연결 {len(closes)}개" + (f" (최소 {min(closes):.2f} / 중앙값 {statistics.median(closes):.2f} / 최대 {max(closes):.2f} s)" if closes else "")
              + f", {a.max_wait:.0f}초 안에 안 닫힌 연결 {kept}개")


if __name__ == "__main__":
    main()
