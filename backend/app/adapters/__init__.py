"""Adapters for things outside the domain: AI inference, image storage, notification delivery."""
from flask import current_app

from .inference import build_inference
from .notifier import build_email_sender, build_publisher
from .storage import build_storage


def init_adapters(app) -> None:
    app.extensions["pg_inference"] = build_inference(app.config)
    app.extensions["pg_storage"] = build_storage(app.config)
    app.extensions["pg_publisher"] = build_publisher(app.config)
    app.extensions["pg_email"] = build_email_sender(app.config)


def warm_up_inference(app) -> None:
    """Start loading the AI models now instead of on the first photo. Called by the gunicorn worker
    hook (gunicorn.conf.py), never by the CLI: `flask db upgrade` must not pull 400 MB of models."""
    app.extensions["pg_inference"].warm_up()


def inference():
    return current_app.extensions["pg_inference"]


def storage():
    return current_app.extensions["pg_storage"]


def publisher():
    return current_app.extensions["pg_publisher"]


def email_sender():
    return current_app.extensions["pg_email"]
