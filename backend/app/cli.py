"""Developer commands: `flask seed` creates demo data for local runs."""
import click
from sqlalchemy import select

from .auth.security import hash_password, sha256_hex
from .extensions import db
from .models import Admin, AdminParkingLot, Company, Machine, ParkingLot, ParkingZone, Role

DEMO_PASSWORD = "Passw0rd!"

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
    """Fixed, predictable key for seeded demo machines only. Real machines get a random key."""
    return f"pgk_dev_{serial_number.lower()}"


def register_cli(app) -> None:
    @app.cli.command("seed")
    def seed():
        """Create a SUPER_ADMIN and two demo companies (skips anything that already exists)."""
        if not db.session.scalar(select(Admin).where(Admin.email == "super@pg.local")):
            db.session.add(Admin(email="super@pg.local", name="서비스 운영자", role=Role.SUPER_ADMIN,
                                 password_hash=hash_password(DEMO_PASSWORD)))

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
                                  password_hash=hash_password(DEMO_PASSWORD))
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

        click.echo("Seed complete.\n")
        click.echo(f"Login accounts (password for all: {DEMO_PASSWORD})")
        click.echo("  SUPER_ADMIN    super@pg.local")
        for entry in DEMO:
            for email, name in entry["admins"]:
                lots_text = ", ".join(entry["assignments"].get(email, []))
                click.echo(f"  COMPANY_ADMIN  {email:<12} {entry['company']} {name} (담당: {lots_text})")
        click.echo("\nDemo device API keys (X-Device-Key)")
        for company_name, lot_name, serial, key in device_keys:
            click.echo(f"  {company_name} / {lot_name:<10} {serial:<18} {key}")
