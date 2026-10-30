"""The JSON that the inference service returns and the API reads back (docs/design/ARCHITECTURE_EVOLUTION.md 3.3).

`InferenceResult.to_dict()` is what the service sends; `InferenceResult.from_dict()` is what HttpInference
parses. They must round-trip exactly, and a malformed body must become an InferenceError so that device.py
keeps its existing 503 contract.
"""
import json

import pytest

from app.adapters.inference import (NO_VEHICLE, PERMITTED, VIOLATION, InferenceError, InferenceResult)

WINDOW = {"coords": [10, 20, 300, 200], "confidence": 0.9132}
STICKER = {"coords": [50, 60, 90, 100], "confidence": 0.7741}


def violation():
    return InferenceResult(has_window=True, window=WINDOW, stickers=[], elapsed_ms=812, backend="yolo")


def permitted():
    return InferenceResult(has_window=True, window=WINDOW, stickers=[STICKER], elapsed_ms=950, backend="yolo")


def no_vehicle():
    return InferenceResult(has_window=False, elapsed_ms=300, backend="yolo")


@pytest.mark.parametrize("make, verdict", [(violation, VIOLATION), (permitted, PERMITTED), (no_vehicle, NO_VEHICLE)])
def test_round_trip_keeps_every_field(make, verdict):
    original = make()
    restored = InferenceResult.from_dict(original.to_dict())

    assert restored == original
    assert restored.verdict == verdict
    assert restored.to_dict() == original.to_dict()


def test_round_trip_survives_a_json_encoding():
    """The wire format is JSON, so tuples/ints must come back as what the dataclass expects."""
    original = permitted()
    restored = InferenceResult.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored == original


def test_derived_fields_are_not_trusted_over_the_data():
    """`verdict` and `sticker_count` are computed from stickers/window, so extra or unknown keys are ignored."""
    body = permitted().to_dict()
    body["something_new"] = "added by a newer service version"

    assert InferenceResult.from_dict(body) == permitted()


@pytest.mark.parametrize("body", [
    None,
    [],
    "violation",
    {},
    {"verdict": "maybe", "window": None, "stickers": [], "elapsed_ms": 1, "backend": "yolo"},
    {"verdict": VIOLATION, "window": None, "stickers": [], "elapsed_ms": 1, "backend": "yolo"},  # window missing
    {"verdict": PERMITTED, "window": WINDOW, "stickers": [], "elapsed_ms": 1, "backend": "yolo"},  # no sticker
    {"verdict": VIOLATION, "window": WINDOW, "stickers": [STICKER], "elapsed_ms": 1, "backend": "yolo"},  # sticker
    {"verdict": NO_VEHICLE, "window": WINDOW, "stickers": [], "elapsed_ms": 1, "backend": "yolo"},  # window present
    {"verdict": VIOLATION, "window": WINDOW, "stickers": "none", "elapsed_ms": 1, "backend": "yolo"},
    {"verdict": VIOLATION, "window": WINDOW, "stickers": [], "elapsed_ms": "slow", "backend": "yolo"},
])
def test_malformed_body_is_an_inference_error(body):
    with pytest.raises(InferenceError):
        InferenceResult.from_dict(body)
