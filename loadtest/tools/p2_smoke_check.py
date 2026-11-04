"""스모크(사진 10장) 판정: 수집 결과와 관찰자 기록으로 통과/실패를 항목별로 낸다 (읽기 전용, 파일만 읽는다).

  python tools/p2_smoke_check.py --report <dir>/p2-report.json --attempts <dir>/p2-attempts.csv [--timeline timeline.jsonl]
         [--sqs sqs-main.csv] [--dlq sqs-dlq.csv] [--expect 10] [--devices 3] [--require-scale-down]

각 항목은 PASS / FAIL / SKIP(근거 자료가 없어 판정 불가 — 통과로 치지 않는다). 하나라도 FAIL 이면 종료 코드 1, SKIP 만 있으면 2, 전부 PASS 면 0.
이 판정은 "도구와 서버 경로가 정상인가"를 보는 것이지 성능 결과가 아니다(사진 10장은 성능을 말하지 못한다).
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p2_watch as pw  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def evaluate_smoke(report, attempts, scale_events=None, sqs_rows=None, dlq_rows=None, expect=10, ndev=3, require_scale_down=False):
    """반환: [(이름, 'PASS'|'FAIL'|'SKIP', 설명)]"""
    out = []

    def add(name, ok, detail="", skip=False):
        out.append((name, "SKIP" if skip else ("PASS" if ok else "FAIL"), detail))
    o = report["overall"]
    add(f"시도 {expect}건", report["attempts"] == expect and len(attempts) == expect, f"{report['attempts']}")
    add("접수 응답이 전부 202 (요청 실패 0, 재시도 0)", o["request_failed"] == 0 and all(str(a["k6_status"]) == "202" and str(a["attempts"]) == "1" for a in attempts),
        f"request_failed={o['request_failed']}")
    add("판정 완료율 100% · 10초 이내 100%", o["completion_rate"] == 1.0 and o["on_time_rate"] == 1.0, f"completion={o['completion_rate']} on_time={o['on_time_rate']}")
    add("미완료·처리 실패 0", o["unfinished"] == 0 and o["processing_failed"] == 0, f"unfinished={o['unfinished']} processing_failed={o['processing_failed']}")
    add("stale 0 (장비별 순서 위반 없음)", report.get("stale_judged", 0) == 0, f"stale={report.get('stale_judged')}")
    devs = {}
    for a in attempts:
        devs.setdefault(str(a["dev"]), []).append((float(a["send_ts"]), int(a["seq"])))
    ordered = all([seq for _, seq in sorted(v)] == list(range(1, len(v) + 1)) for v in devs.values()) and bool(devs)
    add("장비별 seq 가 전송 순서대로 1,2,3…", ordered, f"장비 {len(devs)}대")
    add(f"장비 {ndev}대가 모두 참여", len(devs) == ndev, f"{len(devs)}대")
    add("event_id 전부 유일", len({a["eid"] for a in attempts}) == len(attempts), "")
    add("평가 시점이 충분히 늦다 (eval_too_early 아님)", report.get("eval_too_early") is False, "")
    warm = report.get("warmup") or {}
    if warm.get("jobs"):
        add("워밍업(콜드 스타트) 작업도 판정 완료", warm["judged"] == warm["jobs"], f"{warm['judged']}/{warm['jobs']}, 지연 p50={warm['latency_s'].get('p50')}")
    else:
        add("워밍업(콜드 스타트) 작업 관찰", False, "워밍업 사진 없음(WARMUP_PHOTOS=0) - Worker 가 0 이면 측정 사진이 콜드 스타트를 치러 10초 기준을 넘는다", skip=True)
    if sqs_rows is None:
        add("SQS 본 큐가 비었다", False, "자료 없음(--sqs)", skip=True)
    else:
        last = sqs_rows[-1] if sqs_rows else {}
        left = sum(float(last.get(k) or 0) for k in ("visible", "not_visible", "delayed"))
        add("SQS 본 큐가 비었다 (마지막 표본)", bool(sqs_rows) and left == 0, f"남은 메시지 {left}")
    if dlq_rows is None:
        add("DLQ 가 비어 있다", False, "자료 없음(--dlq)", skip=True)
    else:
        total = sum(float(r.get(k) or 0) for r in dlq_rows for k in ("visible", "not_visible"))
        add("DLQ 가 비어 있다 (전 기간)", total == 0, f"DLQ 합 {total}")
    if scale_events is None:
        add("Worker 확장 0 -> 1 이상", False, "자료 없음(--timeline)", skip=True)
        add("Worker 축소 (쿨다운 후)", False, "자료 없음", skip=True)
    else:
        up = [e for e in scale_events if e["kind"] == "worker_replicas" and e["to"] > e["from"]]
        down = [e for e in scale_events if e["kind"] == "worker_replicas" and e["to"] < e["from"]]
        add("Worker 확장 (replicas 증가 관찰)", bool(up), f"{[(e['from'], e['to']) for e in up]}")
        if require_scale_down:
            add("Worker 축소 관찰 (쿨다운 300초 후)", bool(down), f"{[(e['from'], e['to']) for e in down]}")
        else:
            add("Worker 축소 (쿨다운 후)", False, "관찰 시간이 짧으면 건너뜀 - 끝나고 p3 status 로 확인", skip=not down)
    return out


def verdict(results):
    if any(s == "FAIL" for _, s, _ in results):
        return 1
    return 2 if any(s == "SKIP" for _, s, _ in results) else 0


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True); ap.add_argument("--attempts", required=True); ap.add_argument("--timeline")
    ap.add_argument("--sqs"); ap.add_argument("--dlq"); ap.add_argument("--expect", type=int, default=10); ap.add_argument("--devices", type=int, default=3)
    ap.add_argument("--require-scale-down", action="store_true")
    a = ap.parse_args(argv)
    report = json.loads(Path(a.report).read_text(encoding="utf-8"))
    attempts = read_csv(a.attempts)
    events = None
    if a.timeline:
        samples = [json.loads(line) for line in Path(a.timeline).read_text(encoding="utf-8").splitlines() if line.strip()]
        events = pw.scale_events(samples)
    res = evaluate_smoke(report, attempts, events, read_csv(a.sqs) if a.sqs else None, read_csv(a.dlq) if a.dlq else None, a.expect, a.devices, a.require_scale_down)
    for name, status, detail in res:
        print(f"  {status:4}  {name}  {detail}")
    code = verdict(res)
    print("=> " + {0: "전부 통과", 1: "실패 항목 있음", 2: "실패는 없으나 판정하지 못한 항목(SKIP)이 있다"}[code])
    return code


if __name__ == "__main__":
    sys.exit(main())
