"""Helpers for the Phase 2 Compose integration checks. Talks only to the pg-phase2 stack (ports 15000/15432/19324/19500).
Refuses to run against anything else (never the development database on 5432)."""
import os, subprocess, sys, time, uuid
import boto3, psycopg, requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "device"))
from outbox import OutboxItem  # noqa: E402
from pi_client import post_job  # noqa: E402

COMPOSE = ["docker", "compose", "-f", os.path.join(ROOT, "docker-compose.phase2.yml")]
API = "http://127.0.0.1:15000/api/v1"
KEY = "pgk_dev_rpi-a-gangnam-01"
DSN = "postgresql://parking:phase2_local_only@127.0.0.1:15432/pg_phase2"
KW = dict(region_name="us-east-1", aws_access_key_id="local-test", aws_secret_access_key="local-test")
IMG_EMPTY = open(os.path.join(ROOT, "backend/uploads/sample_f.jpg"), "rb").read()
IMG_CAR = open(os.path.join(ROOT, "backend/uploads/sample_b.jpg"), "rb").read()


def dc(*args, check=True):
    return subprocess.run(COMPOSE + list(args), capture_output=True, text=True, check=check).stdout


def db(sql, *params):
    with psycopg.connect(DSN, autocommit=True) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else None


def sqs():
    return boto3.client("sqs", endpoint_url="http://127.0.0.1:19324", **KW)


def s3():
    return boto3.client("s3", endpoint_url="http://127.0.0.1:19500", **KW)


def qcount(name="pg-judgement.fifo"):
    """Queue depth, read from inside the compose network (host port forwarding of the sqs container breaks after a
    `docker network disconnect/connect` outage test, so the host-side client is not reliable afterwards)."""
    code = ("import boto3,json;c=boto3.client('sqs');a=c.get_queue_attributes(QueueUrl=c.get_queue_url(QueueName=%r)"
            "['QueueUrl'],AttributeNames=['All'])['Attributes'];print(json.dumps({k[19:]:int(v) for k,v in a.items() "
            "if k.startswith('ApproximateNumberOf')}))" % name)
    import json
    return json.loads(dc("exec", "-T", "api", "python", "-c", code).strip().splitlines()[-1])


def item(session, seq, event, jpeg=IMG_EMPTY, command_id=None):
    return OutboxItem.new(session, seq, event, jpeg, time.time, command_id)


def send(it):
    return post_job(API, KEY, it, timeout=(3, 120))


def wait(cond, timeout=120, step=1.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timeout waiting for {what}")


def job_rows(session):
    return db("select seq,event,status,claim_attempt,effects_status,violation_id,result_id is not null from detection_jobs"
              " where session_id=%s order by seq", session)


def new_session():
    return str(uuid.uuid4())


def guard():
    assert ":15432/" in DSN and "5432/parking_guardian" not in DSN
    assert "pg_phase2" in db("select current_database()")[0][0]


def s3_keys(prefix=""):
    """Object keys, listed from inside the compose network (host port forwarding breaks after an outage test)."""
    code = ("import boto3,json;c=boto3.client('s3');print(json.dumps([o['Key'] for o in "
            "c.list_objects_v2(Bucket='pg-phase2',Prefix=%r).get('Contents',[])]))" % prefix)
    import json
    return json.loads(dc("exec", "-T", "api", "python", "-c", code).strip().splitlines()[-1])
