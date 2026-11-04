"""기존 데이터 보호 확인 (읽기 전용): 시험 전 기준선을 찍고, 시험 후 "기준선 시점에 있던 행이 그대로인가"를 해시로 비교한다.

  python tools/p2_protect.py snapshot --source kubectl --out baseline.json
  python tools/p2_protect.py verify   --source kubectl --baseline baseline.json [--exclude machines:7,8,9] [--allow-new audit_logs]

보호 대상 테이블(시험이 건드리면 안 된다): companies, parking_lots, parking_zones, machines, admins, violations, violation_history, notifications, audit_logs.
시험이 만드는 detection_jobs / detection_results / device_sessions / device_commands / refresh_tokens 는 대상이 아니다(새 행만 늘어난다).
판정: 각 테이블에서 "기준선 시점의 최대 id 이하 행들의 해시"가 같고, 최대 id 보다 큰 새 행이 없어야 한다.
  --exclude 테이블:id,id  그 행은 해시에서 뺀다(예: 시험 전용 장비 행은 마지막 신호가 바뀌는 것이 정상)
  --allow-new 테이블      그 테이블의 새 행은 허용한다(예: 장비 등록·키 발급 감사 로그)
값(비밀번호 해시·장비 키 해시·토큰)은 읽거나 출력하지 않는다 - 행을 DB 안에서 md5 로 요약한 값과 건수만 나온다.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p2_collect as pc  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROTECTED = ("companies", "parking_lots", "parking_zones", "machines", "admins", "violations", "violation_history", "notifications", "audit_logs")


def parse_exclude(text):
    # 형식: "machines:7,8,9" 또는 "machines:7,8;violations:5"
    out = {}
    for part in [p for p in text.replace(" ", "").split(";") if p]:
        table, ids = part.split(":")
        if table not in PROTECTED:
            raise SystemExit(f"--exclude 의 테이블이 보호 대상이 아니다: {table}")
        out[table] = [int(i) for i in ids.split(",") if i]
    return out


def sql_for(table, cap=None, exclude=()):
    """한 테이블의 요약 SELECT: 건수, 최대 id, (cap 이하 행들의) md5. 값은 DB 안에서 해시로만 계산한다."""
    if table not in PROTECTED:
        raise ValueError(table)
    ex = f" and id not in ({','.join(str(int(i)) for i in exclude)})" if exclude else ""
    where = "true" if cap is None else f"id <= {int(cap)}"
    return (f"select (select count(*) from {table}) as n, (select max(id) from {table}) as max_id, "
            f"(select md5(coalesce(string_agg(x::text, '|' order by id), '')) from (select * from {table} where {where}{ex}) x) as digest")


def fetch(queries, runner=None):
    for q in queries.values():
        pc.assert_select_only(q)
    arg = json.dumps({"lo": "2026-01-01T00:00:00+00:00", "hi": "2026-12-31T00:00:00+00:00", "queries": queries})
    run = runner or pc.default_runner
    out = run(["kubectl", "exec", "-i", "deploy/backend", "-n", pc.APP_NS, "--", "python", "-", arg], input=pc.POD_SCRIPT.encode("utf-8"))
    raw = json.loads(out.strip().splitlines()[-1])
    return {t: rows[0] for t, rows in raw.items()}


def take_snapshot(runner=None, exclude=None):
    exclude = exclude or {}
    return {"tables": fetch({t: sql_for(t, None, exclude.get(t, ())) for t in PROTECTED}, runner), "exclude": exclude}


def compare(base, now, allow_new=()):
    """반환: [(테이블, 통과 여부, 설명)]. now 는 기준선의 max_id 를 cap 으로 다시 뽑은 요약이다."""
    res = []
    for t in PROTECTED:
        b, n = base["tables"][t], now[t]
        same = b["digest"] == n["digest"]
        new_rows = (n["n"] or 0) - (b["n"] or 0)
        grew = new_rows > 0 or (n["max_id"] or 0) > (b["max_id"] or 0)
        ok = same and (not grew or t in allow_new)
        detail = f"행 {b['n']} -> {n['n']}" + ("" if same else ", 기존 행 해시가 다르다(변경됨)") + (f", 새 행 {new_rows}" if grew else "")
        res.append((t, ok, detail))
    return res


def verify(base, runner=None, allow_new=(), exclude=None):
    exclude = exclude if exclude is not None else base.get("exclude", {})
    q = {t: sql_for(t, base["tables"][t]["max_id"], exclude.get(t, ())) for t in PROTECTED}
    return compare(base, fetch(q, runner), allow_new)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--source", default="kubectl"); s.add_argument("--out", required=True); s.add_argument("--exclude", default="")
    v = sub.add_parser("verify"); v.add_argument("--source", default="kubectl"); v.add_argument("--baseline", required=True)
    v.add_argument("--exclude", default=None); v.add_argument("--allow-new", default="")
    a = ap.parse_args(argv)
    if a.source != "kubectl":
        raise SystemExit("--source 는 kubectl 만 지원한다(로컬 시험은 fetch 를 가짜 실행기로 바꿔 한다)")
    if a.cmd == "snapshot":
        snap = take_snapshot(None, parse_exclude(a.exclude))
        Path(a.out).write_text(json.dumps(snap, indent=1, default=str), encoding="utf-8")
        print(f"저장: {a.out}  " + ", ".join(f"{t}={snap['tables'][t]['n']}" for t in PROTECTED))
        return 0
    base = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
    res = verify(base, None, tuple(x for x in a.allow_new.split(",") if x), parse_exclude(a.exclude) if a.exclude is not None else None)
    for t, ok, detail in res:
        print(f"  {'PASS' if ok else 'FAIL'}  {t}  {detail}")
    ok = all(r[1] for r in res)
    print("=> " + ("기존 데이터 변경 없음" if ok else "변경 감지 - 보고하고 사용자 판단을 기다린다"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
