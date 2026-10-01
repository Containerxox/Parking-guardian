"""Parking lots, admin assignments, zones and machines."""
from ..extensions import db
from .base import Status, in_list, utcnow


class ParkingLot(db.Model):
    __tablename__ = "parking_lots"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=Status.ACTIVE)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    company = db.relationship("Company", lazy="joined")
    zones = db.relationship("ParkingZone", back_populates="parking_lot", order_by="ParkingZone.zone_name")
    machines = db.relationship("Machine", back_populates="parking_lot", order_by="Machine.id")
    assignments = db.relationship("AdminParkingLot", back_populates="parking_lot", lazy="selectin",
                                  primaryjoin="ParkingLot.id == AdminParkingLot.parking_lot_id",
                                  foreign_keys="[AdminParkingLot.parking_lot_id]")

    __table_args__ = (
        db.CheckConstraint(in_list("status", Status.ALL), name="ck_parking_lots_status"),
        db.UniqueConstraint("company_id", "name", name="uq_parking_lots_company_name"),
        # Target of the composite foreign key in admin_parking_lots
        db.UniqueConstraint("id", "company_id", name="uq_parking_lots_id_company"),
    )


class AdminParkingLot(db.Model):
    """Which admin is responsible for which parking lot (notification target).

    company_id is stored here and both composite foreign keys include it, so the database itself
    rejects a row that links an admin of one company to a parking lot of another company.
    """

    __tablename__ = "admin_parking_lots"

    admin_id = db.Column(db.Integer, primary_key=True)
    parking_lot_id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    admin = db.relationship("Admin", back_populates="assignments", foreign_keys="[AdminParkingLot.admin_id]",
                            primaryjoin="AdminParkingLot.admin_id == Admin.id")
    parking_lot = db.relationship("ParkingLot", back_populates="assignments",
                                  foreign_keys="[AdminParkingLot.parking_lot_id]",
                                  primaryjoin="AdminParkingLot.parking_lot_id == ParkingLot.id")

    __table_args__ = (
        db.ForeignKeyConstraint(["admin_id", "company_id"], ["admins.id", "admins.company_id"],
                                name="fk_apl_admin_company", ondelete="CASCADE"),
        db.ForeignKeyConstraint(["parking_lot_id", "company_id"], ["parking_lots.id", "parking_lots.company_id"],
                                name="fk_apl_lot_company", ondelete="CASCADE"),
        db.Index("ix_apl_parking_lot", "parking_lot_id"),
    )


class Machine(db.Model):
    """A detection device (Raspberry Pi + camera). It belongs to a parking lot, not to a person."""

    __tablename__ = "machines"

    id = db.Column(db.Integer, primary_key=True)
    parking_lot_id = db.Column(db.Integer, db.ForeignKey("parking_lots.id", ondelete="RESTRICT"),
                               nullable=False, index=True)
    serial_number = db.Column(db.String(100), nullable=False, unique=True)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=Status.ACTIVE)
    api_key_hash = db.Column(db.String(64), nullable=False, unique=True)
    installed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    last_heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Episode tracking state (see services/detections.py). Kept in the database, not in memory,
    # so it survives restarts and works when several API instances serve the same machine.
    last_detection_at = db.Column(db.DateTime(timezone=True), nullable=True)   # last judged photo
    pending_violation_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    pending_since = db.Column(db.DateTime(timezone=True), nullable=True)       # first unconfirmed sighting
    clear_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    parking_lot = db.relationship("ParkingLot", back_populates="machines", lazy="joined")

    __table_args__ = (db.CheckConstraint(in_list("status", Status.ALL), name="ck_machines_status"),)


class ParkingZone(db.Model):
    """One disabled-parking space inside a lot, optionally watched by a machine."""

    __tablename__ = "parking_zones"

    id = db.Column(db.Integer, primary_key=True)
    parking_lot_id = db.Column(db.Integer, db.ForeignKey("parking_lots.id", ondelete="RESTRICT"),
                               nullable=False, index=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id", ondelete="SET NULL"), nullable=True, index=True)
    zone_name = db.Column(db.String(100), nullable=False)
    location_description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    parking_lot = db.relationship("ParkingLot", back_populates="zones")
    machine = db.relationship("Machine")

    __table_args__ = (db.UniqueConstraint("parking_lot_id", "zone_name", name="uq_parking_zones_lot_name"),)
