"""시험 전용 장비의 점검·등록·키 주입 (공식 API 만 사용, SUPER_ADMIN 로그인). 비밀번호·장비 키는 출력하지도 저장하지도 않는다.

  python tools/p2_devices.py preflight --base-url https://APP_HOST --lot 6 --serials RPI-E2E-TEST-01,RPI-LOAD-01,RPI-LOAD-02 [--expect-company E2E-TEST-COMPANY]
        읽기 전용. 로트·회사 확인, 시리얼 중복/존재/상태, 주차장의 장비 수. 문제가 있으면 종료 코드 2.
  python tools/p2_devices.py create    --base-url … --lot 6 --serials RPI-LOAD-01,RPI-LOAD-02 [--apply]
        기본은 "하려는 일"만 출력(변경 없음). --apply 면 없는 시리얼만 POST /machines 로 등록한다. 이미 있는 시리얼은 건드리지 않는다.
        등록 응답의 api_key 는 읽지도 않고 버린다(서버에는 해시만 남는다) - 키는 아래 launch 가 사용 직전에 따로 발급한다.
  python tools/p2_devices.py launch    --base-url … --serials A,B,C --yes-rotate -- k6 run … k6/scenarios_p2.js
        각 장비에 POST /machines/<id>/rotate-key 로 새 키를 받아 **자식 프로세스의 환경변수 DEVICE_KEYS 로만** 넘기고 자식을 실행한다.
        키는 이 프로세스의 메모리와 자식의 환경에만 있다(출력·파일·명령줄에 없다). 회전마다 감사 로그 MACHINE_KEY_ROTATED 가 장비당 1줄 남는다.
로그인 정보: 환경변수 SUPER_EMAIL, SUPER_PASSWORD (없으면 비밀번호는 getpass 로 묻는다). 이름은 --email-env / --password-env 로 바꿀 수 있다.
"""
import argparse
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_COMPANY = "E2E-TEST-COMPANY"


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/") + "/api/v1"
        self.token = None

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def login(self, email, password):
        status, body = self.call("POST", "/auth/login", {"email": email, "password": password})
        if status != 200 or "access_token" not in body:
            raise SystemExit(f"로그인 실패({status}). 계정 정보를 확인하세요(값은 출력하지 않습니다).")
        self.token = body["access_token"]


def credentials(email_env, password_env):
    email = os.environ.get(email_env) or input(f"{email_env} (SUPER_ADMIN 이메일): ")
    password = os.environ.get(password_env) or getpass.getpass("SUPER_ADMIN 비밀번호(화면에 표시되지 않음): ")
    return email, password


def lot_info(api, lot_id):
    status, body = api.call("GET", f"/parking-lots/{lot_id}")
    if status != 200:
        raise SystemExit(f"주차장 {lot_id} 조회 실패({status})")
    return body


def machines(api):
    status, body = api.call("GET", "/machines")
    if status != 200:
        raise SystemExit(f"장비 목록 조회 실패({status})")
    return body["items"]


def preflight(api, lot_id, serials, expect_company):
    lot, items = lot_info(api, lot_id), machines(api)
    by_serial = {m["serial_number"]: m for m in items}
    problems = []
    if lot.get("company_name") != expect_company:
        problems.append(f"주차장 {lot_id} 의 회사가 '{lot.get('company_name')}' 이다(기대 '{expect_company}')")
    if lot.get("status") != "ACTIVE":
        problems.append(f"주차장 상태가 {lot.get('status')}")
    rows = []
    for s in serials:
        m = by_serial.get(s)
        rows.append({"serial": s, "exists": m is not None, "id": m and m["id"], "status": m and m["status"], "parking_lot_id": m and m["parking_lot_id"]})
        if m and m["parking_lot_id"] != lot_id:
            problems.append(f"{s} 는 다른 주차장({m['parking_lot_id']})에 있다")
        if m and m["status"] != "ACTIVE":
            problems.append(f"{s} 상태가 {m['status']}")
    return {"lot": {"id": lot["id"], "name": lot.get("name"), "company": lot.get("company_name"), "status": lot.get("status"), "machine_count": lot.get("machine_count")},
            "devices": rows, "to_create": [r["serial"] for r in rows if not r["exists"]], "problems": problems}


def create(api, lot_id, serials, expect_company, apply):
    pf = preflight(api, lot_id, serials, expect_company)
    if pf["problems"]:
        return pf, [], 2
    todo = pf["to_create"]
    done = []
    if apply:
        for s in todo:
            status, body = api.call("POST", "/machines", {"parking_lot_id": lot_id, "serial_number": s, "name": f"부하 시험 장비 {s[-2:]}"})
            if status != 201:
                raise SystemExit(f"{s} 등록 실패({status}) - 중단. 이미 만든 것: {done}")
            done.append(s)                       # body["api_key"] 는 읽지 않는다
    return pf, done, 0


def launch(api, serials, cmd, extra_env=None):
    by_serial = {m["serial_number"]: m for m in machines(api)}
    missing = [s for s in serials if s not in by_serial]
    if missing:
        raise SystemExit(f"등록되지 않은 장비: {missing}. 키를 발급하지 않았다.")
    keys = []
    for s in serials:
        status, body = api.call("POST", f"/machines/{by_serial[s]['id']}/rotate-key")
        if status != 200 or "api_key" not in body:
            raise SystemExit(f"{s} 키 발급 실패({status}) - 중단(자식 프로세스를 실행하지 않았다)")
        keys.append(body["api_key"])
    env = dict(os.environ, **(extra_env or {}), DEVICE_KEYS=",".join(keys))
    print(f"키 {len(keys)}개를 발급해 자식 프로세스 환경변수 DEVICE_KEYS 로만 전달한다: {' '.join(cmd[:2])} …", flush=True)
    return subprocess.run(cmd, env=env).returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["preflight", "create", "launch"])
    ap.add_argument("--base-url", required=True); ap.add_argument("--lot", type=int, default=6); ap.add_argument("--serials", required=True)
    ap.add_argument("--expect-company", default=DEFAULT_COMPANY); ap.add_argument("--apply", action="store_true"); ap.add_argument("--yes-rotate", action="store_true")
    ap.add_argument("--email-env", default="SUPER_EMAIL"); ap.add_argument("--password-env", default="SUPER_PASSWORD")
    argv = list(sys.argv[1:] if argv is None else argv)
    child = []
    if "--" in argv:                       # `--` 뒤는 launch 가 실행할 명령이다(argparse 가 건드리지 않게 먼저 떼어 낸다)
        i = argv.index("--")
        argv, child = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    serials = [s for s in a.serials.split(",") if s]
    api = Api(a.base_url)
    api.login(*credentials(a.email_env, a.password_env))
    if a.cmd == "preflight":
        pf = preflight(api, a.lot, serials, a.expect_company)
        print(json.dumps(pf, indent=1, ensure_ascii=False))
        return 2 if pf["problems"] else 0
    if a.cmd == "create":
        pf, done, code = create(api, a.lot, serials, a.expect_company, a.apply)
        print(json.dumps({"preflight": pf, "created": done, "dry_run": not a.apply}, indent=1, ensure_ascii=False))
        if not a.apply and pf["to_create"]:
            print("(--apply 없음: 아무것도 등록하지 않았다)")
        return code
    cmd = child
    if not cmd:
        raise SystemExit("launch 는 `-- <실행할 명령>` 이 필요하다")
    if not a.yes_rotate:
        print("launch 는 장비 키를 회전한다(이전 키는 무효). 확인하려면 --yes-rotate 를 붙인다. 아무것도 하지 않았다.")
        return 2
    return launch(api, serials, cmd)


if __name__ == "__main__":
    sys.exit(main())
