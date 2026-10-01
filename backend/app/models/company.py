"""Tenant (Company) and login accounts (Admin)."""
from ..extensions import db
from .base import Role, Status, in_list, utcnow


class Company(db.Model):
    """A customer company. This is the tenant boundary."""

    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default=Status.ACTIVE)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (db.CheckConstraint(in_list("status", Status.ALL), name="ck_companies_status"),)


class Admin(db.Model):
    """A login user. SUPER_ADMIN belongs to no company. COMPANY_ADMIN belongs to exactly one."""

    __tablename__ = "admins"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True, index=True)
    email = db.Column(db.String(255), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=Status.ACTIVE)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    company = db.relationship("Company", lazy="joined")
    # Joined on admin_id only. company_id on the assignment row is set explicitly by the caller.
    assignments = db.relationship("AdminParkingLot", back_populates="admin", lazy="selectin",
                                  cascade="all, delete-orphan",
                                  primaryjoin="Admin.id == AdminParkingLot.admin_id",
                                  foreign_keys="[AdminParkingLot.admin_id]")

    __table_args__ = (
        db.CheckConstraint(in_list("role", Role.ALL), name="ck_admins_role"),
        db.CheckConstraint(in_list("status", Status.ALL), name="ck_admins_status"),
        db.CheckConstraint(
            "(role = 'SUPER_ADMIN' AND company_id IS NULL) OR (role = 'COMPANY_ADMIN' AND company_id IS NOT NULL)",
            name="ck_admins_role_company",
        ),
        # Target of the composite foreign key in admin_parking_lots
        db.UniqueConstraint("id", "company_id", name="uq_admins_id_company"),
    )

    @property
    def is_super_admin(self) -> bool:
        return self.role == Role.SUPER_ADMIN


class RefreshToken(db.Model):
    """Server-side record of a refresh token so that logout and rotation can revoke it."""

    __tablename__ = "refresh_tokens"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    admin = db.relationship("Admin")
