"""Liveness and readiness checks."""
import logging

from flask import Blueprint, jsonify
from sqlalchemy import text

from .. import adapters
from ..extensions import db

log = logging.getLogger(__name__)
bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz():
    """The process is alive. Does not touch dependencies."""
    return jsonify({"status": "ok"})


@bp.get("/readyz")
def readyz():
    """The process can serve requests: the database answers and the models are not mid-load.

    inference states (adapters/inference.py): ready/cold -> 200; loading -> 503 so a Pod that is still
    warming up receives no traffic (ALB target group, readinessProbe, HPA all key off this); failed ->
    200, because the API must keep serving admins even when the models cannot be loaded (ADR 0005:
    a detection then gets 503 inference_unavailable and the next photo retries the load).
    """
    inference_state = getattr(adapters.inference(), "state", "ready")
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        db.session.rollback()
        log.exception("readiness check failed")
        return jsonify({"status": "unavailable", "database": "error", "inference": inference_state}), 503
    if inference_state == "loading":
        return jsonify({"status": "unavailable", "database": "ok", "inference": inference_state}), 503
    return jsonify({"status": "ok", "database": "ok", "inference": inference_state})
