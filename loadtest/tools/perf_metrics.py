"""Phase 1 · Phase 2 공통 지표 계산 (순수 함수, I/O 없음). 정의는 사전 등록 대상이다 — 결과를 보기 전에 고정하고, 바꾸면 변경 기록을 남긴다.

한 번의 "시도(attempt)" = 장비가 사진 한 장을 보내려고 한 것(k6 가 보낸 첫 요청. 같은 event_id 의 재시도는 한 시도로 묶는다).
시도마다 결과(outcome)는 다음 중 정확히 하나다:

  on_time            판정이 끝났고 접수부터 판정까지 걸린 시간 <= ON_TIME_S (기본 10 s)
  late               판정이 끝났지만 > ON_TIME_S
  request_failed     접수가 받아들여지지 않았다(Phase 1: 200/201 이 아닌 응답 — 503, 4xx, 5xx, 시간 초과; Phase 2: 2xx 가 아니거나 작업 행이 없음)
  processing_failed  Phase 2 에서 접수는 됐지만 작업이 rejected / dead_lettered 로 끝났다
  unfinished         Phase 2 에서 접수됐지만 평가 시점(eval_at)까지 판정이 없다(pending_publish / queued / processing)

"접수부터 판정까지" 시간: Phase 1 = 요청을 보낸 뒤 200 응답까지(판정이 응답 안에 있다). Phase 2 = 서버가 접수한 시각(detection_jobs.received_at)부터
판정이 저장된 시각(detection_results.created_at). Phase 2 의 장비 쪽 접수 응답 시간(202)은 이 값에 넣지 않는다 — 다른 의미이기 때문이다.

핵심 비율의 분모는 항상 "시도 전체"다. 실패하거나 평가 시점까지 끝나지 않은 사진은 on_time 이 아니다(분모에서 빼지 않는다).
  on_time_rate        = on_time / offered
  completion_rate     = (on_time + late) / offered
  request_fail_rate   = request_failed / offered
  processing_fail_rate= processing_failed / (offered - request_failed)       (Phase 1 은 해당 없음)
지연 분위수는 "판정이 끝난 것만"으로 계산한다(끝나지 않은 것은 지연을 모른다). 그러므로 분위수는 반드시 completion_rate 와 같이 읽는다.
unfinished 중 접수된 지 ON_TIME_S 도 안 된 것은 censored_young 으로 따로 센다(여전히 on_time 이 아니다). 평가 시점이 마지막 전송 + drain_wait 보다 이르면
eval_too_early 를 켠다 — 그 경우 unfinished 가 부풀려진 것이다.

처리량(장/초) = 단계 시간 창 안에 "판정이 완료된" 사진 수 / 창 길이 (완료 시각 기준). 지연 분포는 "보낸 시각이 속한 단계"(코호트) 기준이다. 두 기준은 다르다.
분위수는 선형 보간(numpy 'linear' 와 같음). p99 는 표본이 P99_MIN_N 미만이면 p99_reliable=false 로 표시한다.
"""
import math

ON_TIME_S = 10.0
P99_MIN_N = 300
OUTCOMES = ("on_time", "late", "request_failed", "processing_failed", "unfinished")


def percentile(values, q):
    """선형 보간 분위수 (q 는 0~100). 빈 목록이면 None."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    k = (len(xs) - 1) * (q / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(xs[lo])
    return float(xs[lo] + (xs[hi] - xs[lo]) * (k - lo))


def latency_stats(values):
    n = len(values)
    if not n:
        return {"n": 0, "min": None, "mean": None, "p50": None, "p95": None, "p99": None, "max": None, "p99_reliable": False}
    return {"n": n, "min": min(values), "mean": sum(values) / n, "p50": percentile(values, 50), "p95": percentile(values, 95),
            "p99": percentile(values, 99), "max": max(values), "p99_reliable": n >= P99_MIN_N}


def classify_p2(accepted, job, result_latency_s, eval_age_s, on_time_s=ON_TIME_S):
    """Phase 2 시도 하나의 결과. accepted: k6 가 2xx 를 받았는가 또는 DB 에 작업이 있는가. job: DB 작업 행(dict) 또는 None.
    result_latency_s: 판정이 있으면 received_at → result.created_at (초), 없으면 None. eval_age_s: 평가 시점 - received_at.
    반환: (outcome, censored_young)"""
    if job is None:
        return "request_failed", False
    status = job["status"]
    if status in ("rejected", "dead_lettered"):
        return "processing_failed", False
    if result_latency_s is not None and status in ("done", "stale"):
        return ("on_time" if result_latency_s <= on_time_s else "late"), False
    return "unfinished", (eval_age_s is not None and eval_age_s < on_time_s)


def classify_p1(status, duration_s, on_time_s=ON_TIME_S):
    """Phase 1 시도 하나: 200/201 이면 판정이 끝난 응답. 그 밖에는 모두 request_failed."""
    if status in (200, 201):
        return ("on_time" if duration_s <= on_time_s else "late"), False
    return "request_failed", False


def summarize_step(name, seconds, items, completions):
    """items: [{'outcome','latency_s'(판정 있으면)}], completions: 단계 창 안에 완료된 사진 수(완료 시각 기준)."""
    offered = len(items)
    counts = {o: 0 for o in OUTCOMES}
    lat = []
    young = 0
    for it in items:
        counts[it["outcome"]] += 1
        young += 1 if it.get("censored_young") else 0
        if it["outcome"] in ("on_time", "late") and it.get("latency_s") is not None:
            lat.append(it["latency_s"])
    accepted = offered - counts["request_failed"]
    rate = lambda a, b: (a / b) if b else None  # noqa: E731
    return {
        "step": name, "seconds": seconds, "offered": offered, "offered_per_s": rate(offered, seconds), **counts,
        "censored_young": young,
        "on_time_rate": rate(counts["on_time"], offered), "completion_rate": rate(counts["on_time"] + counts["late"], offered),
        "request_fail_rate": rate(counts["request_failed"], offered), "processing_fail_rate": rate(counts["processing_failed"], accepted),
        "completed_in_window": completions, "throughput_per_s": rate(completions, seconds),
        "latency_s": latency_stats(lat),
    }


def overall(step_rows):
    """단계별 행을 합친 전체 행(분위수는 단계별 분포를 합칠 수 없어 제외 — 필요하면 원자료에서 다시 계산)."""
    keys = ("offered", *OUTCOMES, "censored_young", "completed_in_window")
    tot = {k: sum(r[k] for r in step_rows) for k in keys}
    seconds = sum(r["seconds"] for r in step_rows)
    offered, accepted = tot["offered"], tot["offered"] - tot["request_failed"]
    rate = lambda a, b: (a / b) if b else None  # noqa: E731
    tot.update(seconds=seconds, on_time_rate=rate(tot["on_time"], offered), completion_rate=rate(tot["on_time"] + tot["late"], offered),
               request_fail_rate=rate(tot["request_failed"], offered), processing_fail_rate=rate(tot["processing_failed"], accepted),
               throughput_per_s=rate(tot["completed_in_window"], seconds))
    return tot


def flatten_csv_rows(step_rows):
    """CSV 로 쓰기 쉽게 latency_s 를 펼친다."""
    out = []
    for r in step_rows:
        row = {k: v for k, v in r.items() if k != "latency_s"}
        for k, v in r["latency_s"].items():
            row[f"lat_{k}"] = v
        out.append(row)
    return out
