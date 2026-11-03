"""S5-c(backend Pod 1개 삭제) 결과 두 개를 나란히 비교한다. 읽기 전용.

    python loadtest/tools/s5c_compare.py <변경 전 결과 폴더> <변경 후 결과 폴더>
    예: python loadtest/tools/s5c_compare.py docs/measurements/phase1/results/2026-11-01-s5c-1 docs/measurements/phase1/results/2026-11-02-s5c-2

비교 항목: 삭제→교체 Pod Ready·전체 복구 시간, Endpoint·ALB healthy 최솟값, 장애 구간 관리자 API(건수·p95·p99·최대·5xx), inject 단계 전체의 일반 API 정확한 성공률(원본 점에서 계산:
토큰 만료 401 은 따로 센다), 사진 상태 분포, 502 건수. ALB 로그의 502 대상 대조는 alb_log_502.py 로 따로 본다(삭제한 Pod IP 와 삭제 시각을 넘긴다).
"""
import collections
import json
import pathlib
import sys


def load(folder):
    folder = pathlib.Path(folder)
    r = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    st = collections.Counter()
    first = None
    for line in open(folder / "points.json", encoding="utf-8"):
        if '"Point"' in line and '"metric":"http_reqs"' in line:
            o = json.loads(line)
            t = o["data"]["tags"]
            if t.get("kind") == "api" and t.get("phase") != "warmup":
                st[(t.get("step"), t.get("status"))] += 1
    return r, st


def fmt(x, nd=1):
    return "-" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    (a, sa), (b, sb) = load(sys.argv[1]), load(sys.argv[2])
    A, B = a["s5"], b["s5"]
    rows = [("유효 여부", a["valid"], b["valid"]),
            ("삭제 Pod / 교체 Pod", f"{A['deleted_pod']['name'][-5:]} -> {(A.get('replacement_pod') or {}).get('name', '-')[-5:]}",
             f"{B['deleted_pod']['name'][-5:]} -> {(B.get('replacement_pod') or {}).get('name', '-')[-5:]}"),
            ("삭제→교체 Pod Ready (s)", A.get("delete_to_replacement_ready_s"), B.get("delete_to_replacement_ready_s")),
            ("삭제→Endpoint 2·ALB healthy 2 복구 (s)", A.get("delete_to_all_recovered_s"), B.get("delete_to_all_recovered_s")),
            ("Ready Endpoint 최솟값", A.get("min_backend_ready_endpoints"), B.get("min_backend_ready_endpoints")),
            ("ALB healthy 최솟값", A.get("min_alb_healthy"), B.get("min_alb_healthy")),
            ("장애 구간 길이 (s)", A.get("window_s"), B.get("window_s")),
            ("장애 구간 관리자 API 건수", A["admin_api_in_window"]["n"], B["admin_api_in_window"]["n"]),
            ("  p95 (ms)", fmt(A["admin_api_in_window"]["p95"]), fmt(B["admin_api_in_window"]["p95"])),
            ("  p99 (ms)", fmt(A["admin_api_in_window"]["p99"]), fmt(B["admin_api_in_window"]["p99"])),
            ("  최대 (ms)", fmt(A["admin_api_in_window"]["max"]), fmt(B["admin_api_in_window"]["max"])),
            ("  5xx·연결 오류 건수", len(A["admin_api_5xx_or_conn_errors_in_window"]), len(B["admin_api_5xx_or_conn_errors_in_window"])),
            ("장애 구간 사진 상태", A["photo_status_in_window"], B["photo_status_in_window"]),
            ("장애 구간 사진 p95 (ms)", fmt(A["photo_ms_in_window"]["p95"]), fmt(B["photo_ms_in_window"]["p95"])),
            ("전체 일반 API 502 건수", sum(v for (s_, c), v in sa.items() if c == "502"), sum(v for (s_, c), v in sb.items() if c == "502")),
            ("전체 일반 API 성공률(200)", f"{sum(v for (s_, c), v in sa.items() if c == '200')}/{sum(sa.values())}",
             f"{sum(v for (s_, c), v in sb.items() if c == '200')}/{sum(sb.values())}"),
            ("전체 사진 503 수", a["photo_http_503_total"], b["photo_http_503_total"])]
    print(f"{'항목':<36}{'변경 전':<34}{'변경 후':<34}")
    for name, x, y in rows:
        print(f"{name:<36}{str(x):<34}{str(y):<34}")


if __name__ == "__main__":
    main()
