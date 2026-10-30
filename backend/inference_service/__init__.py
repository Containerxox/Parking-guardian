"""Inference service: the two YOLO models behind one HTTP route (ADR 0018, design doc 3.2-3.3).

It has no database, no photo storage and mounts no secrets; a failure here means "cannot judge", nothing more.
The API side (app/adapters/inference.py HttpInference) sends the raw photo bytes and parses the JSON back
with InferenceResult.from_dict.

    POST /v1/analyze   body = image bytes, headers X-Filename, X-Request-Id
      200 InferenceResult.to_dict()
      400 {"error": "invalid_image"}
      503 {"error": "model_unavailable"}     (loading, or the model failed to load / run)
      503 {"error": "busy"} + Retry-After: 1 (one photo running and INFERENCE_MAX_WAITING already waiting)
    GET  /healthz      process is alive
    GET  /readyz       200 only when the model is ready
"""
import logging
import time
from urllib.parse import unquote

from flask import Flask, jsonify, request

from app.adapters.inference import InferenceError, InvalidImageError, build_inference
from app.config import Config
from app.metrics import INFERENCE_REJECTED  # noqa: F401  (registers the metrics this service reports)

from .admission import AdmissionGate, Busy

log = logging.getLogger(__name__)

EXTENSION_KEY = "pg_inference"
GATE_KEY = "pg_gate"


def create_app(test_config: dict | None = None, inference=None) -> Flask:
    """`inference` lets tests inject a stand-in; otherwise it is built from the same settings as the API."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    service = Flask(__name__)
    service.config.from_object(Config)  # same env names as the API (model paths, confidences, MAX_UPLOAD_MB)
    if test_config:
        service.config.update(test_config)
    service.extensions[EXTENSION_KEY] = inference if inference is not None else build_inference(service.config)

    gate = AdmissionGate(max_waiting=service.config["INFERENCE_MAX_WAITING"])
    service.extensions[GATE_KEY] = gate
    fault_delay = service.config["INFERENCE_FAULT_DELAY_MS"] / 1000

    def model():
        return service.extensions[EXTENSION_KEY]

    @service.post("/v1/analyze")
    def analyze():
        image_bytes = request.get_data()
        request_id = request.headers.get("X-Request-Id", "")
        filename = unquote(request.headers.get("X-Filename", ""))  # sent percent-encoded (ASCII headers)
        response, status = _analyze(model(), gate, fault_delay, image_bytes, filename, request_id)
        reply = jsonify(response)
        reply.status_code = status
        if status == 503 and response["error"] == "busy":
            reply.headers["Retry-After"] = "1"
        if request_id:
            reply.headers["X-Request-Id"] = request_id
        return reply

    @service.get("/healthz")
    def healthz():
        # Alive, unless the model could not be loaded even after the bounded retries (YoloInference.warm_up):
        # then a restart is the only way forward, and a failing liveness probe is how Kubernetes does it.
        if getattr(model(), "gave_up", False):
            return jsonify({"status": "model_failed"}), 503
        return jsonify({"status": "ok"})

    @service.get("/readyz")
    def readyz():
        state = getattr(model(), "state", "ready")
        return jsonify({"status": "ok" if state == "ready" else "unavailable", "inference": state}), \
            (200 if state == "ready" else 503)

    @service.errorhandler(Exception)
    def unexpected(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        log.exception("unexpected error")
        return jsonify({"error": "internal_error"}), 500

    return service


def _analyze(model, gate, fault_delay: float, image_bytes: bytes, filename: str, request_id: str):
    if not image_bytes:
        return {"error": "invalid_image"}, 400
    if getattr(model, "state", "ready") in ("loading", "failed"):
        # Warming up, or a failed load that the background retries handle. A photo never triggers a load
        # here: a NotReady Pod gets no photos from the Service anyway, so "retry on the next photo" cannot work.
        return {"error": "model_unavailable"}, 503
    try:
        with gate.slot():  # at most one photo in the model, at most max_waiting queued, the rest refused at once
            if fault_delay:
                time.sleep(fault_delay)
            return model.analyze(image_bytes, filename).to_dict(), 200
    except Busy:
        return {"error": "busy"}, 503
    except InvalidImageError:
        return {"error": "invalid_image"}, 400
    except InferenceError:
        log.warning("analyze failed request_id=%s", request_id, exc_info=True)
        return {"error": "model_unavailable"}, 503
