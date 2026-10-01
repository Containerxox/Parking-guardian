"""HTTP API. Every business route lives under /api/v1."""
from . import admins, auth, companies, device, health, notifications, parking, violations

API_PREFIX = "/api/v1"


def register_blueprints(app) -> None:
    for module in (auth, companies, admins, parking, violations, notifications, device):
        app.register_blueprint(module.bp, url_prefix=API_PREFIX)
    # Health checks are reachable both at the root (load balancer) and under the API prefix.
    app.register_blueprint(health.bp)
    app.register_blueprint(health.bp, url_prefix=API_PREFIX, name="health_v1")
