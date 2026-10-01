"""Liveness and readiness checks."""
import logging

from flask import Blueprint, jsonify
from sqlalchemy import text

from ..extensions import db

log = logging.getLogger(__name__)
bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz():
    """The process is alive. Does not touch dependencies."""
    return jsonify({"status": "ok"})


@bp.get("/readyz")
def readyz():
    """The process can serve requests: the database answers."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "ok"})
    except Exception:
        db.session.rollback()
        log.exception("readiness check failed")
        return jsonify({"status": "unavailable", "database": "error"}), 503
