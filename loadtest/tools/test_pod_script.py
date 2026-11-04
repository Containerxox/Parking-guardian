"""DB 조회 고정 스크립트(POD_SCRIPT v2, create_app 없이 psycopg 직접 연결) 시험.
실제 DB 에 연결하지 않는다: 가짜 `psycopg` 모듈을 PYTHONPATH 앞에 두고 **실제 스크립트 바이트를 그대로** 하위 프로세스(python -)로 실행한다."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import p2_collect as pc  # noqa: E402
import p2_protect as pp  # noqa: E402
import p2_watch as pw  # noqa: E402

PW = "CANARYPW-db-9f3a7c1e"
POD_SCRIPT_SHA256 = "ec543089fd71c0cd8c3f06a79c2fca8414bc7db902abd2cc6e01037903fda076"

FAKE_PSYCOPG = '''
import json, os
LOG = os.environ["FAKE_LOG"]
def _log(**kw):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, default=str) + "\\n")
class OperationalError(Exception): pass
class QueryCanceled(Exception): pass
class _Cur:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        _log(ev="execute", sql=sql, params=params, read_only=self.conn.read_only)
        mode = os.environ.get("FAKE_MODE", "")
        if mode == "timeout": raise QueryCanceled("canceling statement due to statement timeout")
        self.sql = sql
    def fetchall(self):
        import datetime, decimal
        return [{"n": 3, "oldest": datetime.datetime(2026, 11, 4, 10, 0, 1, tzinfo=datetime.timezone.utc), "d": decimal.Decimal("1.50")}]
class _Conn:
    read_only = False
    def cursor(self, row_factory=None): return _Cur(self)
    def rollback(self): _log(ev="rollback")
    def close(self): _log(ev="close")
def connect(**kw):
    _log(ev="connect", kw=kw)
    if os.environ.get("FAKE_MODE") == "refuse":
        raise OperationalError('connection failed: password authentication failed for user "%s" (password=%s)' % (kw.get("user"), kw.get("password")))
    return _Conn()
'''
FAKE_ROWS = '''
class _R:
    @staticmethod
    def dict_row(*a, **k): return None
rows = _R
'''


@pytest.fixture
def fake_env(tmp_path):
    pkg = tmp_path / "fakepkg" / "psycopg"
    (pkg / "rows").mkdir(parents=True)
    (pkg / "__init__.py").write_text(FAKE_PSYCOPG, encoding="utf-8")
    (pkg / "rows" / "__init__.py").write_text("def dict_row(*a, **k):\n    return None\n", encoding="utf-8")
    log = tmp_path / "fake.log"

    def run(queries, mode="", env_extra=None, drop=()):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DB_", "DATABASE_"))}
        env.update({"PYTHONPATH": str(tmp_path / "fakepkg"), "FAKE_LOG": str(log), "FAKE_MODE": mode, "PYTHONUTF8": "1",
                    "DB_HOST": "db.internal", "DB_PORT": "5432", "DB_NAME": "parking_guardian", "DB_USER": "parking", "DB_PASSWORD": PW, "DB_SSLMODE": "require"})
        env.update(env_extra or {})
        for k in drop:
            env.pop(k, None)
        log.write_text("", encoding="utf-8")
        arg = json.dumps({"lo": "2026-11-04T10:00:00+00:00", "hi": "2026-11-04T10:02:00+00:00", "queries": queries})
        r = subprocess.run([sys.executable, "-", arg], input=pc.POD_SCRIPT.encode(), capture_output=True, env=env, timeout=60)
        events = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
        return r, events
    return run


# ------------------------------------------------------------------------------------------ 무결성(SHA-256)
def test_pod_script_hash_is_pinned_and_runner_requires_exact_bytes():
    assert hashlib.sha256(pc.POD_SCRIPT.encode("utf-8")).hexdigest() == POD_SCRIPT_SHA256       # 스크립트를 바꾸면 이 해시와 시험을 의도적으로 갱신해야 한다
    seen = []
    run = pw.readonly_runner(lambda cmd, **kw: seen.append((cmd, kw.get("input"))) or "{}")
    cmd = ["kubectl", "exec", "-i", "deploy/backend", "-n", "parking-guardian", "--", "python", "-", "{}"]
    run(cmd, input=pc.POD_SCRIPT.encode())
    assert hashlib.sha256(seen[0][1]).hexdigest() == POD_SCRIPT_SHA256                            # 실행기에 전달된 바이트 = 검증 대상 바이트
    for tampered in (pc.POD_SCRIPT + "\nimport os; os.system('id')", pc.POD_SCRIPT.replace("read_only = True", "read_only = False"),
                     pc.POD_SCRIPT.replace("statement_timeout=8000", "statement_timeout=0")):
        with pytest.raises(PermissionError):
            run(cmd, input=tampered.encode())                                                      # 한 글자만 달라도 거부
    with pytest.raises(PermissionError):
        run(cmd, input=None)
    with pytest.raises(PermissionError):
        run(["kubectl", "exec", "-i", "deploy/backend", "--", "sh", "-c", "env"], input=pc.POD_SCRIPT.encode())


def test_other_tools_use_the_same_pinned_script():
    sent = []

    def fake(cmd, input=None, **kw):
        sent.append(input)
        return json.dumps({"companies": [{"n": 1, "max_id": 1, "digest": "d"}]})
    pp.fetch({"companies": "select 1"}, runner=fake)
    assert hashlib.sha256(sent[0]).hexdigest() == POD_SCRIPT_SHA256                                # p2_protect 도 같은(검증된) 스크립트를 보낸다
    assert "pc.POD_SCRIPT" in (HERE / "p2_watch.py").read_text(encoding="utf-8") and "POD_SCRIPT" in (HERE / "p2_protect.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------ 실행: 읽기 전용·타임아웃·종료
def test_script_connects_read_only_with_timeouts_runs_converted_sql_and_closes(fake_env):
    q = {"pending": "select status, count(*) n from detection_jobs where received_at >= cast(:lo as timestamptz) and note like 'a:b%' and x::text <> 'y'"}
    r, ev = fake_env(q)
    assert r.returncode == 0, r.stderr.decode()
    out = json.loads(r.stdout.decode().strip().splitlines()[-1])
    assert out == {"pending": [{"n": 3, "oldest": "2026-11-04 10:00:01+00:00", "d": "1.50"}]}        # v1 과 같은 형식(datetime·Decimal 은 문자열)
    conn = next(e for e in ev if e["ev"] == "connect")["kw"]
    assert conn["host"] == "db.internal" and conn["port"] == 5432 and conn["sslmode"] == "require" and conn["connect_timeout"] == 5
    assert "default_transaction_read_only=on" in conn["options"] and "statement_timeout=8000" in conn["options"] and "lock_timeout=2000" in conn["options"]
    ex = next(e for e in ev if e["ev"] == "execute")
    assert ex["read_only"] is True                                                                  # 트랜잭션 READ ONLY
    assert "cast(%(lo)s as timestamptz)" in ex["sql"] and "'a:b%%'" in ex["sql"] and "x::text" in ex["sql"]     # 이름 인수만 바뀌고 문자열·캐스트·%는 보존
    assert ex["params"] == {"lo": "2026-11-04T10:00:00+00:00"}                                       # 쓰인 인수만 전달
    assert [e["ev"] for e in ev][-2:] == ["rollback", "close"]                                      # 조회 뒤 롤백하고 연결을 닫는다


@pytest.mark.parametrize("bad", ["delete from detection_jobs", "update detection_jobs set status='x'", "select 1; select 2", "insert into x values (1)",
                                 "select 1 from x for update", "with a as (select 1) delete from y", "drop table x", "copy x to program 'id'"])
def test_script_rejects_non_select_before_any_sql_reaches_the_db(fake_env, bad):
    r, ev = fake_env({"ok": "select 1", "bad": bad})
    assert r.returncode == 3 and not any(bad.split(";")[-1].strip() in e["sql"] and bad.split()[0] != "select" or e["sql"] == bad for e in ev if e["ev"] == "execute")
    assert all(e["sql"] == "select 1" for e in ev if e["ev"] == "execute")                       # 허용된 앞 질의 말고는 DB 로 나간 SQL 이 없다
    assert json.loads(r.stderr.decode().strip().splitlines()[-1])["error"] == "AssertionError"
    assert r.stdout.decode().strip() == "" and ev[-1]["ev"] == "close"                              # 실패해도 연결은 닫는다


def test_connection_failure_and_timeout_exit_3_without_leaking_password(fake_env):
    r, ev = fake_env({"a": "select 1"}, mode="refuse")
    err = r.stderr.decode()
    assert r.returncode == 3 and "OperationalError" in err and PW not in err and PW not in r.stdout.decode() and "***" in err
    r, ev = fake_env({"a": "select 1"}, mode="timeout")
    assert r.returncode == 3 and "QueryCanceled" in r.stderr.decode() and ev[-1]["ev"] == "close"
    # 오류 때 쓰기·비밀 조회 같은 우회를 하지 않는다: 연결 시도는 한 번뿐
    assert sum(1 for e in ev if e["ev"] == "connect") == 1


def test_password_never_in_stdout_stderr_or_any_logged_sql(fake_env):
    r, ev = fake_env({"a": "select 1"})
    assert PW not in r.stdout.decode() and PW not in r.stderr.decode()
    assert all(PW not in json.dumps({k: v for k, v in e.items() if k != "kw"}) for e in ev)         # SQL·인수에도 없다(연결 인수에만 전달된다)


def test_database_url_fallback_and_missing_config(fake_env):
    r, ev = fake_env({"a": "select 1"}, env_extra={"DATABASE_URL": "postgresql+psycopg://u%40x:p%2Fw@h2:6543/dbz?sslmode=disable"}, drop=("DB_HOST",))
    kw = next(e for e in ev if e["ev"] == "connect")["kw"]
    assert r.returncode == 0 and (kw["host"], kw["port"], kw["dbname"], kw["user"], kw["password"], kw["sslmode"]) == ("h2", 6543, "dbz", "u@x", "p/w", "disable")
    r, ev = fake_env({"a": "select 1"}, drop=("DB_HOST", "DATABASE_URL"))
    assert r.returncode == 3 and not ev and "KeyError" in r.stderr.decode()                          # 설정이 없으면 연결 시도도 없이 실패


def test_script_reads_only_db_environment_and_does_not_touch_secrets_manager():
    src = pc.POD_SCRIPT
    assert "boto3" not in src and "secretsmanager" not in src.lower() and "kubectl" not in src
    import re
    names = set(re.findall(r'e(?:nviron)?(?:\.get)?\(?\[?"([A-Z_]+)"', src)) | set(re.findall(r'e\["([A-Z_]+)"\]', src))
    assert names <= {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE", "DATABASE_URL"}, names
    assert "print(" in src and src.count("print(") == 2                                              # 출력은 오류 한 줄과 결과 한 줄뿐
    assert "password" not in src.split("print(")[1] or "kw.get(\"password\")" in src                # 비밀번호는 출력 쪽에서 지우기에만 쓰인다


def test_existing_observer_queries_pass_the_script_guard_and_conversion():
    ns = {}
    exec(pc.POD_SCRIPT.split("arg = json.loads")[0], ns)                                            # noqa: S102 - 가드·변환 함수만 꺼내 실행
    for sql in list(pw.DB_QUERIES.values()) + [pc.render(pc.sql_jobs([7, 8]), "pg"), pc.render(pc.sql_results(None), "pg"), pp.sql_for("violations", 5, (1,))]:
        pg = ns["to_pg"](ns["only_select"](sql))
        assert ":lo" not in pg.replace("::", "") and ":hi" not in pg.replace("::", "")              # 이름 인수가 모두 바뀌었다
        assert "%(lo)s" in pg or "%(hi)s" in pg or ":lo" not in sql                                  # lo/hi 를 쓰는 질의는 psycopg 형식으로
    assert ns["to_pg"]("select '%' , a::int , cast(:hi as timestamptz)") == "select '%%' , a::int , cast(%(hi)s as timestamptz)"
