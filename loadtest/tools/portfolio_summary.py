"""포트폴리오용 요약 만들기: 측정 보고서(p1-report.json, p2-report.json, compare.csv)에서 "집계 숫자만" 골라 summary.md / summary.json 으로 쓴다.

  python tools/portfolio_summary.py --p1 <p1 dir>/p1-report.json --p2 <p2 dir>/p2-report.json --out <dir> --label "2026-11-0X 비교" [--build 60c2e43] [--notes "..."]

원자료(시도별 CSV, event_id, Worker/Pod 이름, 호스트, IP, 계정, 시각 원본)는 요약에 넣지 않는다. 쓰기 전에 요약 문자열에 공개하면 안 되는 패턴이 있는지 검사하고
있으면 파일을 쓰지 않고 중단한다. 요약에는 반드시 정의·무효 여부·표본 수가 같이 들어간다(숫자만 떼어 쓰는 것을 막으려고).
"""
import argparse
import json
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

FORBIDDEN = [
    (re.compile(r"\b\d{12}\b"), "12자리 숫자(AWS 계정 ID 형태)"),
    (re.compile(r"arn:aws", re.I), "ARN"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "IPv4 주소"),
    (re.compile(r"amazonaws\.com|\.cloud\b|\.elb\.", re.I), "AWS/도메인 호스트"),
    (re.compile(r"x-device-key|authorization|bearer|password|secret", re.I), "인증 관련 단어"),
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "UUID(event_id/session_id)"),
    (re.compile(r"mock-worker|worker-[0-9a-f]{5,}|-[0-9a-f]{9,10}-[a-z0-9]{5}\b"), "Pod/Worker 이름"),
    (re.compile(r"\bRPI-[A-Z0-9-]+\b"), "장비 일련번호"),
]

STEP_FIELDS = ("step", "seconds", "offered", "offered_per_s", "on_time", "late", "request_failed", "processing_failed", "unfinished", "on_time_rate", "completion_rate",
               "request_fail_rate", "processing_fail_rate", "throughput_per_s")
LAT_FIELDS = ("n", "p50", "p95", "p99", "p99_reliable")


def pick_steps(report):
    rows = []
    for r in report["steps"]:
        row = {k: r.get(k) for k in STEP_FIELDS}
        row["latency_s"] = {k: (r.get("latency_s") or {}).get(k) for k in LAT_FIELDS}
        rows.append(row)
    return rows


def validity(report):
    flags = []
    if report.get("eval_too_early"):
        flags.append("평가 시점이 일러 미완료가 부풀려졌을 수 있음")
    if report.get("accepted_despite_client_failure"):
        flags.append(f"클라이언트 실패 후 서버에는 접수된 건 {report['accepted_despite_client_failure']}")
    return flags


def fmt(v, digits=3, pct=False):
    if v is None:
        return "-"
    if pct:
        return f"{v * 100:.1f}%"
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def build(p1, p2, label, build_tag="", notes=""):
    summary = {"label": label, "build": build_tag, "notes": notes,
               "definition": "perf_metrics.py 의 정의(사전 등록): 10초 이내 판정 완료율 = on_time / 시도 전체(실패·미완료 포함), 지연은 판정이 끝난 것만, 처리량은 완료 시각 기준",
               "on_time_s": (p2 or p1)["definition"]["on_time_s"], "p99_note": "p99 는 표본 300 미만이면 신뢰하지 않는다(p99_reliable=false)"}
    if p1:
        summary["phase1"] = {"steps": pick_steps(p1), "overall": {k: p1["overall"].get(k) for k in ("offered", "on_time_rate", "completion_rate", "request_fail_rate", "throughput_per_s")}}
    if p2:
        summary["phase2"] = {"run": {k: p2["run"].get(k) for k in ("scenario", "c", "scale", "ndev", "event_mode", "retry")}, "steps": pick_steps(p2),
                             "overall": {k: p2["overall"].get(k) for k in ("offered", "on_time_rate", "completion_rate", "request_fail_rate", "processing_fail_rate", "throughput_per_s")},
                             "validity_flags": validity(p2), "stale_judged": p2.get("stale_judged")}
    return summary


def to_markdown(s):
    lines = [f"# {s['label']}", ""]
    if s["build"]:
        lines.append(f"- 빌드: `{s['build']}`")
    lines += [f"- 정의: {s['definition']}", f"- {s['p99_note']}", ""]
    if s["notes"]:
        lines += [s["notes"], ""]
    for key, title in (("phase1", "Phase 1 (동기)"), ("phase2", "Phase 2 (비동기)")):
        if key not in s:
            continue
        sec = s[key]
        lines += [f"## {title}", ""]
        if key == "phase2":
            lines += [f"- 시나리오 {sec['run'].get('scenario')}, C={sec['run'].get('c')}, 장비 {sec['run'].get('ndev')}대, 이벤트 모드 {sec['run'].get('event_mode')}, 재시도 {sec['run'].get('retry')}"]
            if sec["validity_flags"]:
                lines += ["- **유효성 주의**: " + "; ".join(sec["validity_flags"])]
            lines += [""]
        lines += ["| 단계 | 도착률(장/초) | 처리량(장/초) | 10초 이내 | 완료율 | 요청 실패 | 처리 실패 | 미완료 | p50(s) | p95(s) | p99(s) | 표본 |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in sec["steps"]:
            lat = r["latency_s"]
            p99 = fmt(lat["p99"]) + ("" if lat["p99_reliable"] else " (표본 부족)") if lat["p99"] is not None else "-"
            lines.append(f"| {r['step']} | {fmt(r['offered_per_s'], 2)} | {fmt(r['throughput_per_s'], 2)} | {fmt(r['on_time_rate'], pct=True)} | {fmt(r['completion_rate'], pct=True)} | "
                         f"{fmt(r['request_fail_rate'], pct=True)} | {fmt(r['processing_fail_rate'], pct=True)} | {r['unfinished']} | {fmt(lat['p50'])} | {fmt(lat['p95'])} | {p99} | {lat['n']} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def check_publishable(text):
    for rx, what in FORBIDDEN:
        m = rx.search(text)
        if m:
            raise SystemExit(f"중단: 요약에 공개하면 안 되는 내용이 있다 ({what}): {m.group(0)[:30]!r}. 파일을 쓰지 않았다.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--p1"); ap.add_argument("--p2"); ap.add_argument("--out", required=True); ap.add_argument("--label", required=True)
    ap.add_argument("--build", default=""); ap.add_argument("--notes", default="")
    a = ap.parse_args(argv)
    if not (a.p1 or a.p2):
        raise SystemExit("--p1 또는 --p2 가 필요하다")
    p1 = json.loads(Path(a.p1).read_text(encoding="utf-8")) if a.p1 else None
    p2 = json.loads(Path(a.p2).read_text(encoding="utf-8")) if a.p2 else None
    s = build(p1, p2, a.label, a.build, a.notes)
    md, js = to_markdown(s), json.dumps(s, indent=1, ensure_ascii=False)
    check_publishable(md + js)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(md, encoding="utf-8")
    (out / "summary.json").write_text(js + "\n", encoding="utf-8")
    print(f"저장: {out}/summary.md, summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
