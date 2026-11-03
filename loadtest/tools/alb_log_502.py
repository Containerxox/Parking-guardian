"""ALB 액세스 로그에서 구간의 502(대상 응답 없음)와 특정 대상(Pod IP)으로 간 요청을 조회한다. 읽기 전용(S3 내려받기 + 분석). 로그는 5분 단위로 전달되므로 실험 종료 뒤 5~6분 기다린다.

    python loadtest/tools/alb_log_502.py --from 2026-11-01T07:49:00Z --to 2026-11-01T07:56:00Z [--target-ip 10.0.14.55] [--after 2026-11-01T07:49:58Z]

출력: 상태 코드 쌍 분포, 502/504 요청 목록(클라이언트 IP 는 가린다), --target-ip 가 있으면 그 대상으로 간 요청의 시각별 분포와 마지막 라우팅 시각.
로그에는 요청 경로·클라이언트 IP 가 들어 있으므로 결과를 공유·커밋하지 않는다(내려받은 사본은 끝나면 지운다).
"""
import argparse
import collections
import datetime as dt
import gzip
import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile

NAMES = ["type", "time", "elb", "client", "target", "req_t", "tgt_t", "resp_t", "elb_status", "tgt_status", "rx", "tx", "request", "ua", "cipher",
         "proto", "tg", "trace", "domain", "cert", "prio", "req_created", "actions", "redirect", "error_reason", "target_list", "status_list",
         "class", "class_reason", "conn_trace"]


def aws(*args):
    r = subprocess.run(["aws", *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        sys.exit("aws 실패: " + r.stderr.strip()[:200])
    return r.stdout


def parse_ts(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="t_from", required=True)
    ap.add_argument("--to", dest="t_to", required=True)
    ap.add_argument("--target-ip")
    ap.add_argument("--after", help="이 시각 이후 --target-ip 로 간 요청만 분포를 본다(예: Pod 삭제 시각)")
    a = ap.parse_args()
    t0, t1 = parse_ts(a.t_from), parse_ts(a.t_to)
    acct = json.loads(aws("sts", "get-caller-identity", "--output", "json"))["Account"]
    bucket = "parking-guardian-alb-logs-" + acct
    base = "alb/AWSLogs/" + acct + "/elasticloadbalancing/ap-northeast-2/"
    keys = []
    day = t0.date()
    while day <= t1.date():
        out = aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", base + day.strftime("%Y/%m/%d/"), "--query", "Contents[].Key", "--output", "json")
        keys += json.loads(out or "null") or []
        day += dt.timedelta(days=1)
    # 파일 이름의 ..._20261101T0755Z_... 는 5분 구간의 끝. 구간 [끝-5분, 끝] 이 [from, to] 와 겹치면 받는다
    want = []
    for k in keys:
        stamp = next((p for p in k.split("_") if len(p) == 14 and p.endswith("Z") and "T" in p), None)
        if not stamp:
            continue
        end = dt.datetime.strptime(stamp, "%Y%m%dT%H%MZ").replace(tzinfo=dt.timezone.utc)
        if end >= t0 and end - dt.timedelta(minutes=5) <= t1 + dt.timedelta(minutes=5):
            want.append(k)
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        rows = []
        for k in want:
            f = tmp / k.rsplit("/", 1)[-1]
            aws("s3", "cp", "s3://" + bucket + "/" + k, str(f), "--quiet")
            for line in gzip.open(f, "rt", encoding="utf-8"):
                p = shlex.split(line)
                if len(p) >= 30:
                    r = dict(zip(NAMES, p))
                    if t0 <= parse_ts(r["time"]) <= t1:
                        rows.append(r)
        rows.sort(key=lambda r: r["time"])
        print(f"로그 파일 {len(want)}개, 구간 내 요청 {len(rows)}건")
        if not rows:
            print("(요청 없음: 아직 전달 전일 수 있습니다. 실험 종료 뒤 5~6분 기다리세요)")
            return
        c = collections.Counter((r["elb_status"], r["tgt_status"]) for r in rows)
        print("상태 코드 쌍 (elb, target) 중 200 이외:", {k: v for k, v in c.items() if k != ("200", "200")})
        for r in rows:
            if r["elb_status"] in ("502", "504") or (r["tgt_status"] == "-" and r["elb_status"] not in ("400", "404")):
                print({"time": r["time"][11:26], "elb": r["elb_status"], "target_status": r["tgt_status"], "target": r["target"],
                       "target_processing_s": r["tgt_t"], "error_reason": r["error_reason"], "request": r["request"].split(" ")[0] + " " + r["request"].split(" ")[1].split("/api/v1")[-1],
                       "actions": r["actions"]})
        if a.target_ip:
            after = parse_ts(a.after) if a.after else t0
            mine = [r for r in rows if r["target"].startswith(a.target_ip + ":") and parse_ts(r["time"]) >= after]
            by_sec = collections.Counter((r["time"][11:19], r["elb_status"]) for r in mine)
            print(f"--target-ip {a.target_ip}: {a.after or a.t_from} 이후 이 대상으로 간 요청 {len(mine)}건, 마지막 라우팅 {mine[-1]['time'][11:26] if mine else None}")
            print("  초별(상태):", sorted(by_sec.items()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
