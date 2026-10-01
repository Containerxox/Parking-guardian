"""Parking lots, zones and machines.

Reading is open to every admin of the company. Changing this structure (lots, zones, machines,
device keys) is reserved for SUPER_ADMIN: devices are installed by the service operator and a
device key is a credential.
"""
from flask import Blueprint, jsonify, request
from sqlalchemy import select

from ..auth import ensure_company_access, require_auth, scoped_company_id
from ..auth.security import new_device_api_key
from ..errors import bad_request, conflict
from ..extensions import db
from ..models import Admin, AdminParkingLot, Company, Machine, ParkingLot, ParkingZone, Role
from ..serializers import admin_to_dict, machine_to_dict, parking_lot_to_dict, zone_to_dict
from ..services import audit
from .utils import (get_or_404, int_arg, json_body, list_response, optional_text, required_text, status_value)

bp = Blueprint("parking", __name__)


def _load_lot(lot_id: int) -> ParkingLot:
    lot = get_or_404(ParkingLot, lot_id)
    ensure_company_access(lot.company_id)
    return lot


def _load_machine(machine_id: int) -> Machine:
    machine = get_or_404(Machine, machine_id)
    ensure_company_access(machine.parking_lot.company_id)
    return machine


# --- Parking lots ------------------------------------------------------------
@bp.get("/parking-lots")
@require_auth()
def list_parking_lots():
    company_id = scoped_company_id(request.args.get("company_id"))
    stmt = select(ParkingLot).order_by(ParkingLot.company_id, ParkingLot.id)
    if company_id is not None:
        stmt = stmt.where(ParkingLot.company_id == company_id)
    return list_response([parking_lot_to_dict(lot) for lot in db.session.scalars(stmt)])


@bp.post("/parking-lots")
@require_auth(Role.SUPER_ADMIN)
def create_parking_lot():
    data = json_body()
    name = required_text(data, "name", "주차장 이름", 100)
    address = required_text(data, "address", "주소")
    company_id = scoped_company_id(data.get("company_id"))
    if company_id is None:
        raise bad_request("company_id는 필수입니다.")
    get_or_404(Company, company_id)

    if db.session.scalar(select(ParkingLot).where(ParkingLot.company_id == company_id, ParkingLot.name == name)):
        raise conflict("같은 이름의 주차장이 이미 있습니다.")
    lot = ParkingLot(company_id=company_id, name=name, address=address)
    db.session.add(lot)
    db.session.flush()
    audit.record("PARKING_LOT_CREATED", "parking_lot", lot.id, after={"name": name, "address": address},
                 company_id=company_id)
    db.session.commit()
    return jsonify(parking_lot_to_dict(lot)), 201


@bp.get("/parking-lots/<int:lot_id>")
@require_auth()
def get_parking_lot(lot_id: int):
    lot = _load_lot(lot_id)
    admins = db.session.scalars(
        select(Admin).join(AdminParkingLot, AdminParkingLot.admin_id == Admin.id)
        .where(AdminParkingLot.parking_lot_id == lot.id).order_by(Admin.id)
    ).all()
    body = parking_lot_to_dict(lot)
    body["zones"] = [zone_to_dict(z) for z in lot.zones]
    body["machines"] = [machine_to_dict(m) for m in lot.machines]
    body["admins"] = [admin_to_dict(a) for a in admins]
    return jsonify(body)


@bp.patch("/parking-lots/<int:lot_id>")
@require_auth(Role.SUPER_ADMIN)
def update_parking_lot(lot_id: int):
    lot = _load_lot(lot_id)
    data = json_body()
    before = {"name": lot.name, "address": lot.address, "status": lot.status}

    name = optional_text(data, "name", 100)
    if name and name != lot.name:
        if db.session.scalar(select(ParkingLot).where(ParkingLot.company_id == lot.company_id,
                                                      ParkingLot.name == name, ParkingLot.id != lot.id)):
            raise conflict("같은 이름의 주차장이 이미 있습니다.")
        lot.name = name
    address = optional_text(data, "address")
    if address:
        lot.address = address
    status = status_value(data)
    if status:
        lot.status = status

    after = {"name": lot.name, "address": lot.address, "status": lot.status}
    if after != before:
        audit.record("PARKING_LOT_UPDATED", "parking_lot", lot.id, before=before, after=after,
                     company_id=lot.company_id)
    db.session.commit()
    return jsonify(parking_lot_to_dict(lot))


# --- Zones ---------------------------------------------------------------------
def _zone_machine_id(data: dict, lot: ParkingLot) -> int | None:
    machine_id = data.get("machine_id")
    if machine_id is None:
        return None
    if not isinstance(machine_id, int):
        raise bad_request("machine_id 형식이 올바르지 않습니다.")
    machine = db.session.get(Machine, machine_id)
    if machine is None or machine.parking_lot_id != lot.id:
        raise bad_request("같은 주차장에 설치된 장비만 연결할 수 있습니다.")
    return machine.id


@bp.post("/parking-lots/<int:lot_id>/zones")
@require_auth(Role.SUPER_ADMIN)
def create_zone(lot_id: int):
    lot = _load_lot(lot_id)
    data = json_body()
    zone_name = required_text(data, "zone_name", "구역 이름", 100)
    if db.session.scalar(select(ParkingZone).where(ParkingZone.parking_lot_id == lot.id,
                                                   ParkingZone.zone_name == zone_name)):
        raise conflict("같은 이름의 구역이 이미 있습니다.")
    zone = ParkingZone(parking_lot_id=lot.id, zone_name=zone_name,
                       location_description=optional_text(data, "location_description"),
                       machine_id=_zone_machine_id(data, lot))
    db.session.add(zone)
    db.session.flush()
    audit.record("PARKING_ZONE_CREATED", "parking_zone", zone.id, after={"zone_name": zone_name},
                 company_id=lot.company_id)
    db.session.commit()
    return jsonify(zone_to_dict(zone)), 201


@bp.patch("/zones/<int:zone_id>")
@require_auth(Role.SUPER_ADMIN)
def update_zone(zone_id: int):
    zone = get_or_404(ParkingZone, zone_id)
    lot = zone.parking_lot
    ensure_company_access(lot.company_id)
    data = json_body()
    before = zone_to_dict(zone)

    zone_name = optional_text(data, "zone_name", 100)
    if zone_name and zone_name != zone.zone_name:
        if db.session.scalar(select(ParkingZone).where(ParkingZone.parking_lot_id == lot.id,
                                                       ParkingZone.zone_name == zone_name,
                                                       ParkingZone.id != zone.id)):
            raise conflict("같은 이름의 구역이 이미 있습니다.")
        zone.zone_name = zone_name
    if "location_description" in data:
        zone.location_description = optional_text(data, "location_description")
    if "machine_id" in data:
        zone.machine_id = _zone_machine_id(data, lot)

    after = zone_to_dict(zone)
    if after != before:
        audit.record("PARKING_ZONE_UPDATED", "parking_zone", zone.id, before=before, after=after,
                     company_id=lot.company_id)
    db.session.commit()
    return jsonify(zone_to_dict(zone))


# --- Machines --------------------------------------------------------------------
@bp.get("/machines")
@require_auth()
def list_machines():
    company_id = scoped_company_id(request.args.get("company_id"))
    stmt = select(Machine).join(ParkingLot, Machine.parking_lot_id == ParkingLot.id).order_by(Machine.id)
    if company_id is not None:
        stmt = stmt.where(ParkingLot.company_id == company_id)
    lot_id = int_arg("parking_lot_id")
    if lot_id is not None:
        stmt = stmt.where(Machine.parking_lot_id == lot_id)
    return list_response([machine_to_dict(m) for m in db.session.scalars(stmt)])


@bp.post("/machines")
@require_auth(Role.SUPER_ADMIN)
def create_machine():
    data = json_body()
    lot_id = data.get("parking_lot_id")
    if not isinstance(lot_id, int):
        raise bad_request("parking_lot_id는 필수입니다.")
    lot = _load_lot(lot_id)
    serial_number = required_text(data, "serial_number", "시리얼 번호", 100)
    name = required_text(data, "name", "장비 이름", 100)
    if db.session.scalar(select(Machine).where(Machine.serial_number == serial_number)):
        raise conflict("이미 등록된 시리얼 번호입니다.")

    api_key, api_key_hash = new_device_api_key()
    machine = Machine(parking_lot_id=lot.id, serial_number=serial_number, name=name, api_key_hash=api_key_hash)
    db.session.add(machine)
    db.session.flush()
    audit.record("MACHINE_REGISTERED", "machine", machine.id,
                 after={"serial_number": serial_number, "name": name, "parking_lot_id": lot.id},
                 company_id=lot.company_id)
    db.session.commit()
    # The raw key is returned only here. Only its hash is stored.
    return jsonify({"machine": machine_to_dict(machine), "api_key": api_key}), 201


@bp.patch("/machines/<int:machine_id>")
@require_auth(Role.SUPER_ADMIN)
def update_machine(machine_id: int):
    machine = _load_machine(machine_id)
    data = json_body()
    before = {"name": machine.name, "status": machine.status}
    name = optional_text(data, "name", 100)
    if name:
        machine.name = name
    status = status_value(data)
    if status:
        machine.status = status
    after = {"name": machine.name, "status": machine.status}
    if after != before:
        audit.record("MACHINE_UPDATED", "machine", machine.id, before=before, after=after,
                     company_id=machine.parking_lot.company_id)
    db.session.commit()
    return jsonify(machine_to_dict(machine))


@bp.post("/machines/<int:machine_id>/rotate-key")
@require_auth(Role.SUPER_ADMIN)
def rotate_machine_key(machine_id: int):
    machine = _load_machine(machine_id)
    api_key, machine.api_key_hash = new_device_api_key()
    audit.record("MACHINE_KEY_ROTATED", "machine", machine.id, company_id=machine.parking_lot.company_id)
    db.session.commit()
    return jsonify({"machine": machine_to_dict(machine), "api_key": api_key})
