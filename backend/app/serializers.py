"""Model -> JSON dictionaries. Field names follow docs/design/API.md."""
from sqlalchemy import func, select

from .auth import machine_online
from .extensions import db
from .models import (Admin, ImageStatus, Machine, ParkingLot, ParkingZone, Role, Violation, ViolationStatus, iso)


def _count(model, *conditions) -> int:
    return db.session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def admin_to_dict(admin: Admin) -> dict:
    return {
        "id": admin.id,
        "email": admin.email,
        "name": admin.name,
        "role": admin.role,
        "status": admin.status,
        "company_id": admin.company_id,
        "company_name": admin.company.name if admin.company else None,
        "parking_lot_ids": sorted(a.parking_lot_id for a in admin.assignments),
        "created_at": iso(admin.created_at),
    }


def company_to_dict(company) -> dict:
    return {
        "id": company.id,
        "name": company.name,
        "status": company.status,
        "created_at": iso(company.created_at),
        "admin_count": _count(Admin, Admin.company_id == company.id, Admin.role == Role.COMPANY_ADMIN),
        "parking_lot_count": _count(ParkingLot, ParkingLot.company_id == company.id),
        "open_violation_count": _count(Violation, Violation.company_id == company.id,
                                       Violation.status.in_(ViolationStatus.OPEN)),
    }


def parking_lot_to_dict(lot: ParkingLot) -> dict:
    return {
        "id": lot.id,
        "company_id": lot.company_id,
        "company_name": lot.company.name,
        "name": lot.name,
        "address": lot.address,
        "status": lot.status,
        "zone_count": _count(ParkingZone, ParkingZone.parking_lot_id == lot.id),
        "machine_count": _count(Machine, Machine.parking_lot_id == lot.id),
        "open_violation_count": _count(Violation, Violation.parking_lot_id == lot.id,
                                       Violation.status.in_(ViolationStatus.OPEN)),
        "admin_ids": sorted(a.admin_id for a in lot.assignments),
    }


def zone_to_dict(zone: ParkingZone) -> dict:
    return {
        "id": zone.id,
        "parking_lot_id": zone.parking_lot_id,
        "zone_name": zone.zone_name,
        "location_description": zone.location_description,
        "machine_id": zone.machine_id,
    }


def machine_to_dict(machine: Machine) -> dict:
    lot = machine.parking_lot
    return {
        "id": machine.id,
        "company_id": lot.company_id,
        "company_name": lot.company.name,
        "parking_lot_id": machine.parking_lot_id,
        "parking_lot_name": lot.name,
        "serial_number": machine.serial_number,
        "name": machine.name,
        "status": machine.status,
        "installed_at": iso(machine.installed_at),
        "last_heartbeat_at": iso(machine.last_heartbeat_at),
        "online": machine_online(machine),
    }


def violation_to_dict(v: Violation) -> dict:
    return {
        "id": v.id,
        "company_id": v.company_id,
        "company_name": v.company.name,
        "parking_lot_id": v.parking_lot_id,
        "parking_lot_name": v.parking_lot.name,
        "parking_zone_id": v.parking_zone_id,
        "zone_name": v.parking_zone.zone_name if v.parking_zone else None,
        "machine_id": v.machine_id,
        "machine_name": v.machine.name,
        "detected_at": iso(v.detected_at),
        "last_seen_at": iso(v.last_seen_at or v.detected_at),
        "detection_count": v.detection_count,
        "vehicle_present": v.ended_at is None,
        "ended_at": iso(v.ended_at),
        "end_reason": v.end_reason,
        "status": v.status,
        "image_status": v.image_status,
        "has_image": bool(v.image_key) and v.image_status == ImageStatus.UPLOADED,
        "resolved_at": iso(v.resolved_at),
        "resolved_by": v.resolved_by,
        "resolved_by_name": v.resolver.name if v.resolver else None,
        "resolution_note": v.resolution_note,
        "created_at": iso(v.created_at),
    }


def history_to_dict(h) -> dict:
    return {
        "id": h.id,
        "action": h.action,
        "admin_id": h.admin_id,
        "admin_name": h.admin.name if h.admin else None,
        "previous_status": h.previous_status,
        "new_status": h.new_status,
        "message": h.message,
        "created_at": iso(h.created_at),
    }


def notification_to_dict(n) -> dict:
    v = n.violation
    return {
        "id": n.id,
        "violation_id": n.violation_id,
        "channel": n.channel,
        "status": n.status,
        "created_at": iso(n.created_at),
        "sent_at": iso(n.sent_at),
        "read_at": iso(n.read_at),
        "parking_lot_name": v.parking_lot.name,
        "zone_name": v.parking_zone.zone_name if v.parking_zone else None,
        "detected_at": iso(v.detected_at),
        "violation_status": v.status,
    }


def audit_to_dict(a) -> dict:
    return {
        "id": a.id,
        "actor_id": a.actor_id,
        "actor_email": a.actor.email if a.actor else None,
        "company_id": a.company_id,
        "action": a.action,
        "resource_type": a.resource_type,
        "resource_id": a.resource_id,
        "before_value": a.before_value,
        "after_value": a.after_value,
        "created_at": iso(a.created_at),
    }
