"""Phase 1 의 k6 결과(--out json=points.json)에서 Phase 2 와 "같은 의미"의 지표를 계산하고, 두 보고서를 나란히 비교한다. 읽기 전용.

  report   points.json (scenarios.js 가 만든 k6 JSON 점) → p1-report.json / p1-steps.csv   (정의: perf_metrics.py)
  compare  p1-report.json 과 p2-report.json 을 단계별로 나란히 → compare.csv (같은 단계 이름끼리)

Phase 1 에서는 판정이 응답 안에 있으므로: 시도 = 사진 요청 1건, 응답 200/201 = 판정 완료, 지연 = 응답 시간(http_req_duration),
200/201 이 아닌 모든 것(503, 4xx, 5xx, 시간 초과 status 0) = request_failed. processing_failed·unfinished 는 해당 없음(0).
워밍업(tag phase=warmup)과 사진이 아닌 요청(kind != photo)은 제외한다. 시도 수는 k6 가 실제로 보낸 요청 수다 — dropped_iterations(보내지 못한 것)는
시도가 아니라 무효 사유다(k6 요약의 임계값으로 본다).
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perf_metrics as pm  # noqa: E402

for _stream in (sys.stdout, sys.stderr):      # Windows 콘솔(cp949)에서도 한글·기호 출력이 깨지거나 예외가 나지 않게
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

S1_STEPS = [("0.2C", 180), ("0.5C", 180), ("0.8C", 180), ("1.0C", 180), ("1.2C", 180), ("1.5C", 180)]


def parse_steps(text, scale=1.0):
    if not text:
        return [(n, s if scale >= 1 else max(5, round(s * scale))) for n, s in S1_STEPS]
    out = []
    for part in text.split(","):
        name, sec = part.rsplit(":", 1)
        out.append((name, float(sec)))
    return out


def read_points(path):
    """photo 요청의 (step, status, duration_s) 목록."""
    pts = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if "http_req_duration" not in line or "Point" not in line:      # 빠른 걸러내기(공백 유무와 무관)
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") != "Point" or obj.get("metric") != "http_req_duration":
                    continue
                d = obj["data"]
            except (ValueError, KeyError):
                continue
            tags = d.get("tags", {})
            if tags.get("kind") != "photo" or tags.get("phase") == "warmup":
                continue
            pts.append((tags.get("step", "?"), int(tags.get("status", 0) or 0), float(d["value"]) / 1000.0))
    return pts


def build_report(points, steps, on_time_s=pm.ON_TIME_S, meta=None):
    by = {n: [] for n, _ in steps}
    done = {n: 0 for n, _ in steps}
    for step, status, dur in points:
        if step not in by:
            continue
        outcome, _ = pm.classify_p1(status, dur, on_time_s)
        item = {"outcome": outcome, "latency_s": dur if outcome in ("on_time", "late") else None}
        by[step].append(item)
        done[step] += 1 if outcome in ("on_time", "late") else 0
    rows = [pm.summarize_step(n, sec, by[n], done[n]) for n, sec in steps]
    return {"phase": 1, "definition": {"on_time_s": on_time_s, "p99_min_n": pm.P99_MIN_N, "see": "perf_metrics.py 머리말"},
            "run": meta or {}, "steps": rows, "overall": pm.overall(rows),
            "notes": ["Phase 1: 지연 = 요청→응답(판정 포함). 503 은 모두 request_failed", "처리량은 단계 태그 기준 200/201 수 / 단계 길이"]}


def write_csv(path, rows):
    rows = list(rows)
    with open(path, "w", newline="", encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)


def compare(r1, r2):
    """같은 이름의 단계끼리 나란히. 이름이 없는 단계는 빈 칸."""
    s1 = {r["step"]: r for r in r1["steps"]}
    s2 = {r["step"]: r for r in r2["steps"]}
    rows = []
    for name in list(s1) + [n for n in s2 if n not in s1]:
        a, b = s1.get(name), s2.get(name)
        row = {"step": name}
        for label, r in (("p1", a), ("p2", b)):
            g = lambda k: (r or {}).get(k)  # noqa: E731
            lat = (r or {}).get("latency_s") or {}
            row.update({f"{label}_offered_per_s": g("offered_per_s"), f"{label}_throughput_per_s": g("throughput_per_s"),
                        f"{label}_on_time_rate": g("on_time_rate"), f"{label}_completion_rate": g("completion_rate"),
                        f"{label}_request_fail_rate": g("request_fail_rate"), f"{label}_processing_fail_rate": g("processing_fail_rate"),
                        f"{label}_unfinished": g("unfinished"), f"{label}_lat_n": lat.get("n"), f"{label}_lat_p50": lat.get("p50"),
                        f"{label}_lat_p95": lat.get("p95"), f"{label}_lat_p99": lat.get("p99"), f"{label}_p99_reliable": lat.get("p99_reliable")})
        rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--points", required=True); r.add_argument("--out", required=True); r.add_argument("--steps", default="")
    r.add_argument("--scale", type=float, default=1.0); r.add_argument("--on-time", type=float, default=pm.ON_TIME_S)
    c = sub.add_parser("compare")
    c.add_argument("--p1", required=True); c.add_argument("--p2", required=True); c.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "report":
        rep = build_report(read_points(a.points), parse_steps(a.steps, a.scale), a.on_time)
        out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
        (out / "p1-report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        write_csv(out / "p1-steps.csv", pm.flatten_csv_rows(rep["steps"]))
        o = rep["overall"]
        print(f"저장: {out}  시도 {o['offered']}, on_time {o['on_time_rate']}, request_fail {o['request_fail_rate']}")
        return 0
    rows = compare(json.loads(Path(a.p1).read_text(encoding="utf-8")), json.loads(Path(a.p2).read_text(encoding="utf-8")))
    write_csv(a.out, rows)
    print(f"{len(rows)}행 저장: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
