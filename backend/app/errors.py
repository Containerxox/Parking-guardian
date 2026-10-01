"""Uniform JSON error responses: {"error": {"code": ..., "message": ...}}."""
import logging

from flask import jsonify
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException

from .extensions import db

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def bad_request(message: str) -> ApiError:
    return ApiError(400, "validation_error", message)


def unauthorized(message: str = "로그인이 필요합니다.", code: str = "unauthorized") -> ApiError:
    return ApiError(401, code, message)


def forbidden(message: str = "접근 권한이 없습니다.") -> ApiError:
    return ApiError(403, "forbidden", message)


def not_found(message: str = "대상을 찾을 수 없습니다.") -> ApiError:
    return ApiError(404, "not_found", message)


def conflict(message: str, code: str = "conflict") -> ApiError:
    return ApiError(409, code, message)


def _body(code: str, message: str):
    return jsonify({"error": {"code": code, "message": message}})


def register_error_handlers(app):
    @app.errorhandler(ApiError)
    def handle_api_error(err: ApiError):
        db.session.rollback()
        return _body(err.code, err.message), err.status

    @app.errorhandler(IntegrityError)
    def handle_integrity_error(err: IntegrityError):
        db.session.rollback()
        log.warning("integrity error: %s", err.orig)
        return _body("conflict", "이미 존재하거나 제약 조건에 맞지 않는 데이터입니다."), 409

    @app.errorhandler(HTTPException)
    def handle_http_exception(err: HTTPException):
        codes = {400: "validation_error", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                 405: "method_not_allowed", 413: "payload_too_large"}
        return _body(codes.get(err.code, "http_error"), err.description or err.name), err.code

    @app.errorhandler(Exception)
    def handle_unexpected(err: Exception):
        db.session.rollback()
        log.exception("unhandled error")
        return _body("internal_error", "서버 처리 중 오류가 발생했습니다."), 500
