"""P4 정상 사진 스모크: 기기 2·4·5 의 키로 sample_a(허가 판정) 1장씩만 보내고 결과·불변 항목을 확인한다. 부하 아님(3건).

    python loadtest/tools/p4_smoke.py

기대: 세 요청 모두 HTTP 200, 판정 permitted. 위반 1~4 불변, 기기 1·3 행 불변, violations/notifications/violation_history 행 수 불변, S3 객체 수 불변,
Pod 재시작 0. (기기 2·4·5 의 마지막 감지 시각·카운터는 갱신되는 것이 정상이다.) 이상이면 비정상 종료. 키는 출력·기록하지 않는다.
"""
import datetime as dt
import json
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from uprobe_run import REPO, cluster_state, s3_count, sh, snapshot  # noqa: E402

DEVICES = {2: "RPI-A-PANGYO-01", 4: "RPI-B-HQ-01", 5: "RPI-B-LOGIS-01"}
PHOTO = REPO / "backend" / "uploads" / "sample_a.jpg"
RESULTS = REPO / "docs" / "measurements" / "phase1" / "results"


def main():
    host = sh(["kubectl", "-n", "parking-guardian", "get", "ingress", "-o", "jsonpath={.items[0].spec.rules[0].host}"]).stdout.strip()
    url = f"https://{host}/api/v1/device/detections"
    pre_c, pre_db, pre_s3 = cluster_state(), snapshot(), s3_count()
    out, problems = [], []
    keys = {}
    for did, serial in DEVICES.items():
        r = sh(["kubectl", "-n", "parking-guardian", "exec", "deploy/backend", "-c", "backend", "--", "flask", "device-key", serial])
        keys[did] = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
        if not keys[did]:
            sys.exit(f"장비 키 조회 실패: 기기 {did}")
    for did, key in keys.items():
        t0 = time.time()
        with open(PHOTO, "rb") as fh:
            r = requests.post(url, headers={"X-Device-Key": key}, files={"file": ("sample_a.jpg", fh, "image/jpeg")},
                              data={"detected_at": dt.datetime.now(dt.timezone.utc).isoformat()}, timeout=60)
        code, secs, body = str(r.status_code), f"{time.time() - t0:.2f}", r.text
        out.append({"device": did, "http": r.status_code, "seconds": float(secs), "body": body[:300]})
        print(f"기기 {did}: HTTP {code} {secs}s {body[:160]}", flush=True)
        if code != "200" or "permitted" not in body.lower():
            problems.append(f"기기 {did}: HTTP {code} / permitted 아님")
    post_c, post_db, post_s3 = cluster_state(), snapshot(), s3_count()
    if pre_db["violations"] != post_db["violations"]:
        problems.append("위반 행 변화")
    keep = lambda db: [m for m in db["machines"] if m[0] in ("1", "3")]
    if keep(pre_db) != keep(post_db):
        problems.append("기기 1·3 행 변화")
    for t in ("violations", "notifications", "violation_history"):
        if pre_db["counts"][t] != post_db["counts"][t]:
            problems.append(f"{t} 행 수 {pre_db['counts'][t]} -> {post_db['counts'][t]}")
    if pre_s3 != post_s3:
        problems.append(f"S3 객체 수 {pre_s3} -> {post_s3}")
    for n, rw in post_c["pods"].items():
        p0 = pre_c["pods"].get(n)
        if p0 is None or p0["uid"] != rw["uid"] or p0["restarts"] != rw["restarts"] or not rw["ready"]:
            problems.append(f"Pod 변화: {n}")
    for key in keys.values():
        if any(key in json.dumps(o) for o in out):
            problems.append("응답에 키 포함?")
    folder = RESULTS / f"{dt.date.today().isoformat()}-p4-smoke"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps({"ok": not problems, "problems": problems, "requests": out, "s3": [pre_s3, post_s3],
                                                    "machines_before": pre_db["machines"], "machines_after": post_db["machines"],
                                                    "counts": [pre_db["counts"], post_db["counts"]]}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("P4:", "통과" if not problems else "실패 " + "; ".join(problems))
    sys.exit(0 if not problems else 2)


if __name__ == "__main__":
    main()
