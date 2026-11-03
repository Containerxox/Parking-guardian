"""Developer commands.

    flask seed                 create the demo data (skips anything that already exists)
    flask device-key SERIAL    print the key a seeded demo machine uses (for PG_DEVICE_KEY)

Two secrets shape the demo data. Without them the fixed local values are used, which is fine on a
developer PC. On a public server set both (they arrive through Secrets Manager → Kubernetes Secret):

    SEED_ADMIN_PASSWORD      initial password of the seeded admin accounts (default Passw0rd!)
    SEED_DEVICE_KEY_SECRET   the demo machine keys are derived from it with HMAC (default pgk_dev_<serial>)

Re-running `seed` never changes an existing account's password hash or an existing machine's key hash.
Changing the secrets therefore only affects rows that do not exist yet.
"""
import hashlib
import hmac
import os

import click
from sqlalchemy import select

from .auth.security import hash_password, sha256_hex
from .extensions import db
from .models import Admin, AdminParkingLot, Company, Machine, ParkingLot, ParkingZone, Role

DEMO_PASSWORD = "Passw0rd!"


def seed_password() -> str:
    return os.environ.get("SEED_ADMIN_PASSWORD") or DEMO_PASSWORD

# company -> admins, lots (zones, machine), and which admin is responsible for which lot
DEMO = [
    {
        "company": "A회사",
        "admins": [("kim@a.com", "김관리자"), ("lee@a.com", "이관리자"), ("park@a.com", "박관리자")],
        "lots": [
            ("강남 주차장", "서울 강남구 테헤란로 1", ["A-01", "A-02"], ("RPI-A-GANGNAM-01", "Camera-01")),
            ("판교 주차장", "경기 성남시 분당구 판교역로 1", ["B-01"], ("RPI-A-PANGYO-01", "Camera-01")),
            ("수원 주차장", "경기 수원시 영통구 광교로 1", ["C-01"], ("RPI-A-SUWON-01", "Camera-01")),
        ],
        "assignments": {"kim@a.com": ["강남 주차장", "판교 주차장"], "lee@a.com": ["판교 주차장"],
                        "park@a.com": ["수원 주차장"]},
    },
    {
        "company": "B회사",
        "admins": [("choi@b.com", "최관리자")],
        "lots": [
            ("본사 주차장", "서울 중구 세종대로 1", ["1F-01"], ("RPI-B-HQ-01", "Camera-01")),
            ("물류센터 주차장", "인천 서구 물류로 1", ["D-01"], ("RPI-B-LOGIS-01", "Camera-01")),
        ],
        "assignments": {"choi@b.com": ["본사 주차장", "물류센터 주차장"]},
    },
]


def dev_api_key(serial_number: str) -> str:
    """Key of a seeded demo machine. Real machines get a random key at registration.

    With SEED_DEVICE_KEY_SECRET the key is derived (pgk_ + 32 hex chars of HMAC-SHA256), so it is not
    guessable from the serial number and can be recomputed by whoever holds the secret. Without it the
    fixed local value is returned.
    """
    secret = os.environ.get("SEED_DEVICE_KEY_SECRET")
    if secret:
        digest = hmac.new(secret.encode("utf-8"), serial_number.upper().encode("utf-8"), hashlib.sha256)
        return "pgk_" + digest.hexdigest()[:32]
    return f"pgk_dev_{serial_number.lower()}"


def register_cli(app) -> None:
    @app.cli.command("judge-worker")
    @click.option("--until-empty", is_flag=True, help="Stop when the queue has nothing (tests, one-off drains).")
    @click.option("--max-messages", type=int, default=0, help="Stop after this many messages (0 = no limit).")
    def judge_worker(until_empty: bool, max_messages: int):
        """Phase 2 judgement worker (ADR 0019): receive, judge and apply queued photos, one at a time."""
        from . import adapters
        from .services.worker import JudgementWorker

        if adapters.queue() is None:
            raise click.ClickException("QUEUE_BACKEND=none: there is no queue to work on.")
        worker = JudgementWorker()
        handled = 0
        click.echo(f"judge-worker {worker.worker_id} started")
        while not max_messages or handled < max_messages:
            outcome = worker.run_once()   # long poll (20 s on SQS) when idle
            if outcome is None:
                if until_empty:
                    break
                continue
            handled += 1
        click.echo(f"judge-worker {worker.worker_id} handled {handled} message(s)")

    @app.cli.command("outbox-relay")
    @click.option("--once", is_flag=True, help="One pass (publish, DLQ, effects, clean-up) and exit.")
    @click.option("--interval", type=float, default=2.0, help="Seconds between passes (design 6.7: 2 s).")
    @click.option("--clean-every", type=int, default=1800, help="Run retention clean-up every N passes.")
    def outbox_relay(once: bool, interval: float, clean_every: int):
        """Phase 2 outbox relay (ADR 0019 decision 3): resend unpublished jobs, record dead letters, finish
        pending effects, apply retention. Safe to run more than one."""
        import time

        from . import adapters
        from .services.relay import OutboxRelay

        if adapters.queue() is None:
            raise click.ClickException("QUEUE_BACKEND=none: there is no queue to relay to.")
        relay = OutboxRelay()
        passes = 0
        while True:
            stats = relay.run_once(clean=once or (clean_every and passes % clean_every == 0))
            passes += 1
            if once:
                click.echo(f"outbox-relay pass: {stats}")
                return
            time.sleep(interval)

    @app.cli.group("jobs")
    def jobs():
        """Phase 2 detection job operations."""

    @jobs.command("redrive")
    @click.argument("job_ids", nargs=-1)
    @click.option("--all-dead", is_flag=True, help="Every dead_lettered job.")
    def jobs_redrive(job_ids, all_dead: bool):
        """Send dead_lettered jobs again (they go back to pending_publish; the relay sends them)."""
        import uuid as uuid_module

        from .services.relay import redrive

        if not job_ids and not all_dead:
            raise click.UsageError("give job ids or --all-dead")
        try:
            ids = [uuid_module.UUID(value) for value in job_ids]
        except ValueError:
            raise click.UsageError("job ids must be UUIDs")
        count = redrive(ids, all_dead=all_dead)
        if not all_dead and count != len(ids):
            raise click.ClickException(f"redrove {count} job(s); the others are not dead_lettered or do not exist")
        click.echo(f"redrove {count} job(s)")

    @app.cli.command("device-key")
    @click.argument("serial")
    def device_key(serial: str):
        """Print the demo device key for SERIAL (computed, not read from the database)."""
        click.echo(dev_api_key(serial))

    @app.cli.command("seed")
    def seed():
        """Create a SUPER_ADMIN and two demo companies (skips anything that already exists)."""
        password = seed_password()
        if not db.session.scalar(select(Admin).where(Admin.email == "super@pg.local")):
            db.session.add(Admin(email="super@pg.local", name="서비스 운영자", role=Role.SUPER_ADMIN,
                                 password_hash=hash_password(password)))

        device_keys = []
        for entry in DEMO:
            company = db.session.scalar(select(Company).where(Company.name == entry["company"]))
            if company is None:
                company = Company(name=entry["company"])
                db.session.add(company)
                db.session.flush()

            admins = {}
            for email, name in entry["admins"]:
                admin = db.session.scalar(select(Admin).where(Admin.email == email))
                if admin is None:
                    admin = Admin(email=email, name=name, role=Role.COMPANY_ADMIN, company_id=company.id,
                                  password_hash=hash_password(password))
                    db.session.add(admin)
                    db.session.flush()
                admins[email] = admin

            lots = {}
            for lot_name, address, zone_names, (serial, machine_name) in entry["lots"]:
                lot = db.session.scalar(select(ParkingLot).where(ParkingLot.company_id == company.id,
                                                                 ParkingLot.name == lot_name))
                if lot is None:
                    lot = ParkingLot(company_id=company.id, name=lot_name, address=address)
                    db.session.add(lot)
                    db.session.flush()
                lots[lot_name] = lot

                machine = db.session.scalar(select(Machine).where(Machine.serial_number == serial))
                if machine is None:
                    machine = Machine(parking_lot_id=lot.id, serial_number=serial, name=machine_name,
                                      api_key_hash=sha256_hex(dev_api_key(serial)))
                    db.session.add(machine)
                    db.session.flush()
                device_keys.append((entry["company"], lot_name, serial, dev_api_key(serial)))

                for index, zone_name in enumerate(zone_names):
                    if not db.session.scalar(select(ParkingZone).where(ParkingZone.parking_lot_id == lot.id,
                                                                       ParkingZone.zone_name == zone_name)):
                        db.session.add(ParkingZone(parking_lot_id=lot.id, zone_name=zone_name,
                                                   machine_id=machine.id if index == 0 else None))

            for email, lot_names in entry["assignments"].items():
                admin = admins[email]
                assigned = {a.parking_lot_id for a in admin.assignments}
                for lot_name in lot_names:
                    lot = lots[lot_name]
                    if lot.id not in assigned:
                        admin.assignments.append(AdminParkingLot(parking_lot_id=lot.id, company_id=company.id))

        db.session.commit()

        # Secrets never go to stdout: in a container this output ends up in the log pipeline.
        click.echo("Seed complete.\n")
        if os.environ.get("SEED_ADMIN_PASSWORD"):
            click.echo("Login accounts (initial password: from SEED_ADMIN_PASSWORD, applies to newly created accounts)")
        else:
            click.echo(f"Login accounts (password for all: {DEMO_PASSWORD})")
        click.echo("  SUPER_ADMIN    super@pg.local")
        for entry in DEMO:
            for email, name in entry["admins"]:
                lots_text = ", ".join(entry["assignments"].get(email, []))
                click.echo(f"  COMPANY_ADMIN  {email:<12} {entry['company']} {name} (담당: {lots_text})")
        click.echo("\nDemo device API keys (X-Device-Key)")
        derived = bool(os.environ.get("SEED_DEVICE_KEY_SECRET"))
        for company_name, lot_name, serial, key in device_keys:
            shown = "(derived from SEED_DEVICE_KEY_SECRET; run: flask device-key " + serial + ")" if derived else key
            click.echo(f"  {company_name} / {lot_name:<10} {serial:<18} {shown}")
