"""Parking Guardian backend application factory."""
import logging
from pathlib import Path

from flask import Flask
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from .adapters import init_adapters
from .api import register_blueprints
from .cli import register_cli
from .config import Config
from .errors import register_error_handlers
from .extensions import db, migrate
from .metrics import init_metrics


def create_app(test_config: dict | None = None) -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    app.json.ensure_ascii = False  # return Korean text as-is

    # One trusted proxy in front (nginx in docker compose, the ALB on AWS). Absolute URLs the app builds
    # (image URLs via url_for(_external=True)) then use the scheme and host the browser used, so they
    # come out as https://... behind TLS termination. Direct access without the headers is unaffected.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _prepare_sqlite_dir(app.config["SQLALCHEMY_DATABASE_URI"])

    from . import models  # noqa: F401  (register models before migrations run)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)

    CORS(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )

    register_error_handlers(app)
    register_blueprints(app)
    init_metrics(app)
    init_adapters(app)
    register_cli(app)
    return app


def _prepare_sqlite_dir(database_uri: str) -> None:
    prefix = "sqlite:///"
    if database_uri.startswith(prefix) and ":memory:" not in database_uri:
        Path(database_uri[len(prefix):]).parent.mkdir(parents=True, exist_ok=True)
