"""Adapters for things outside the domain: AI inference, image storage, notification delivery."""
from flask import current_app

from .inference import build_inference
from .notifier import build_email_sender, build_publisher
from .ocr import build_ocr
from .storage import build_storage


def init_adapters(app) -> None:
    app.extensions["pg_inference"] = build_inference(app.config)
    app.extensions["pg_storage"] = build_storage(app.config)
    app.extensions["pg_publisher"] = build_publisher(app.config)
    app.extensions["pg_email"] = build_email_sender(app.config)
    app.extensions["pg_ocr"] = build_ocr(app.config)


def inference():
    return current_app.extensions["pg_inference"]


def storage():
    return current_app.extensions["pg_storage"]


def publisher():
    return current_app.extensions["pg_publisher"]


def email_sender():
    return current_app.extensions["pg_email"]


def ocr():
    return current_app.extensions["pg_ocr"]
