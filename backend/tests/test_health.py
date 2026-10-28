"""/healthz and /readyz, including the model warm-up state that gates readiness."""
from .conftest import API


class FakeInference:
    def __init__(self, state):
        self.state = state


def test_healthz_does_not_touch_dependencies(client):
    response = client.get(f"{API}/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_readyz_ok_with_mock_inference(client):
    response = client.get(f"{API}/readyz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "ok", "inference": "ready"}


def test_readyz_503_while_models_are_loading(app, client):
    app.extensions["pg_inference"] = FakeInference("loading")
    response = client.get(f"{API}/readyz")
    assert response.status_code == 503
    assert response.get_json()["inference"] == "loading"


def test_readyz_200_when_model_load_failed_so_the_api_keeps_serving(app, client):
    app.extensions["pg_inference"] = FakeInference("failed")
    response = client.get(f"{API}/readyz")
    assert response.status_code == 200
    assert response.get_json()["inference"] == "failed"


def test_readyz_200_when_cold_lazy_loading_is_unchanged(app, client):
    app.extensions["pg_inference"] = FakeInference("cold")
    assert client.get(f"{API}/readyz").status_code == 200


def test_warm_up_hook_is_a_no_op_for_mock(app):
    from app.adapters import warm_up_inference

    warm_up_inference(app)
    assert app.extensions["pg_inference"].state == "ready"
