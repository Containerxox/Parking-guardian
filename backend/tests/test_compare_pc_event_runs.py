"""The comparison used in step 4 to say "Phase 1 behaves like the Prototype" (scripts/compare_pc_event_runs.py).

It compares what the device sent and what the server answered, in order, and the device state changes. Times
(`sim_s`, milliseconds) and database ids are left out on purpose: they differ between any two runs.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compare_pc_event_runs import compare, event_signature, transition_signature  # noqa: E402


def run(**changes):
    base = {
        "events": [
            {"sim_s": 78.3, "image": "sample_b.jpg", "event": "arrived", "kb": 328, "result": "violation_pending",
             "violation_id": None, "episode_ended": False, "ok": True},
            {"sim_s": 146.7, "image": "sample_b.jpg", "event": "confirm", "kb": 328, "result": "violation",
             "violation_id": 2, "episode_ended": False, "ok": True},
            {"sim_s": 293.9, "image": "empty", "event": "left", "kb": 74, "result": "no_vehicle",
             "violation_id": 2, "episode_ended": True, "ok": True},
        ],
        "state_transitions": [[1.9, None], [11.2, False], [71.6, True], [289.2, False]],
        "heartbeats": 6,
        "led_on_at_end": False,
    }
    base.update(changes)
    return base


def test_the_signature_keeps_what_the_device_sent_and_the_server_answered_in_order():
    assert event_signature(run()) == [
        ("sample_b.jpg", "arrived", "violation_pending", False, True),
        ("sample_b.jpg", "confirm", "violation", False, True),
        ("empty", "left", "no_vehicle", True, True),
    ]


def test_times_ids_and_sizes_are_not_part_of_the_comparison():
    other = run()
    for index, event in enumerate(other["events"]):
        event.update(sim_s=event["sim_s"] + 3.1, kb=event["kb"] + 1, violation_id=99 if event["violation_id"] else None)
    other["state_transitions"] = [[t + 2.0, state] for t, state in other["state_transitions"]]

    assert compare(run(), other)["identical"] is True


def test_state_changes_are_compared_without_their_times():
    assert transition_signature(run()) == [None, False, True, False]


def test_a_different_server_answer_is_reported_with_its_position():
    other = run()
    other["events"][1]["result"] = "violation_pending"

    result = compare(run(), other)

    assert result["identical"] is False
    assert result["event_differences"] == [(1, ("sample_b.jpg", "confirm", "violation", False, True),
                                            ("sample_b.jpg", "confirm", "violation_pending", False, True))]


def test_a_missing_event_is_a_difference():
    other = run()
    other["events"].pop()

    result = compare(run(), other)

    assert result["identical"] is False
    assert result["event_count"] == (3, 2)


def test_different_state_changes_or_heartbeat_count_are_differences():
    assert compare(run(), run(state_transitions=[[1.9, None], [11.2, False]]))["identical"] is False
    assert compare(run(), run(heartbeats=7))["identical"] is False
    assert compare(run(), run(led_on_at_end=True))["identical"] is False
