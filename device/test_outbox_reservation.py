"""Outbox: one slot reserved per OPEN session for its `left` (decision 2026-11-02, ADR 0019 "중요 이벤트(특히 left)의 보존
정책", option C), logical capacity vs real disk errors, and outbox/health.json.

Invariant: while a session is open, every item other than its `left` (arrived, confirm, refresh) is admitted only if,
afterwards, at least one slot (and one photo of bytes) is still free. So the `left` of a session whose events the server
knows can never be refused for a logical reason. An `arrived` therefore needs TWO free slots: its own and its `left`'s.
Nothing that is already stored is ever deleted to make room, except the oldest `refresh` (design 6.10).

    python -m pytest device/test_outbox_reservation.py -q
"""
import errno
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from outbox import Outbox, OutboxFull, OutboxHealth, OutboxItem, classify_store_error  # noqa: E402
from pi_client import AsyncEventRunner  # noqa: E402
from test_async_mode import (SETTINGS, FakeCamera, FakeClock, FakeServer, ScriptedDetector, command,  # noqa: E402
                             run_until)
from test_outbox_saturation import make_runner, snapshot  # noqa: E402


def photo(tag, size=8):
    return b"\xff\xd8\xff" + tag.encode() + b"x" * max(0, size - 3 - len(tag))


def new_item(clock, session, seq, event, size=8):
    return OutboxItem.new(session, seq, event, photo(f"{event}{seq}", size), clock)


def make_outbox(tmp_path, max_items, max_bytes=50 * 1024 * 1024, reserve_bytes=2 * 1024 * 1024):
    clock = FakeClock()
    return Outbox(tmp_path / "outbox", clock=clock, max_items=max_items, max_bytes=max_bytes,
                  reserve_bytes=reserve_bytes), clock, str(uuid.uuid4())


def put(outbox, clock, session, seq, event, reserve=None, size=8):
    reserve = (event != "left") if reserve is None else reserve
    clock.sleep(1)
    return outbox.put(new_item(clock, session, seq, event, size), reserve_for_left=reserve)


# --- L1: nothing but the left may take the last slot -----------------------------------------------------------

@pytest.mark.parametrize("event", ["refresh", "confirm", "arrived"])
def test_l1_a_session_in_progress_keeps_the_last_slot_for_its_left(tmp_path, event):
    outbox, clock, session = make_outbox(tmp_path, max_items=4)
    for seq, kind in enumerate(["arrived", "confirm", "confirm"], start=1):
        put(outbox, clock, session, seq, kind)                              # three items, one slot kept free
    before = snapshot(outbox)
    with pytest.raises(OutboxFull) as info:
        put(outbox, clock, session, 9, event)
    assert info.value.reason == "items"
    assert snapshot(outbox) == before and outbox.dead_items() == []        # nothing stored, nothing deleted
    put(outbox, clock, session, 10, "left")                                 # the reserved slot is exactly for this
    assert [i.event for i in outbox.items()] == ["arrived", "confirm", "confirm", "left"]


# --- L2: an arrived needs room for its own left ----------------------------------------------------------------

def test_l2_an_arrived_needs_two_free_slots(tmp_path):
    outbox, clock, session = make_outbox(tmp_path, max_items=3)
    put(outbox, clock, session, 1, "arrived")
    put(outbox, clock, session, 2, "left")                                  # a closed session: 2 of 3 slots used
    before = snapshot(outbox)
    with pytest.raises(OutboxFull):
        put(outbox, clock, str(uuid.uuid4()), 1, "arrived")                 # one free slot: itself fits, its left would not
    assert snapshot(outbox) == before


def test_l2_with_two_free_slots_the_arrived_and_then_its_left_are_both_stored(tmp_path):
    outbox, clock, session = make_outbox(tmp_path, max_items=4)
    put(outbox, clock, session, 1, "arrived")
    put(outbox, clock, session, 2, "left")
    other = str(uuid.uuid4())
    put(outbox, clock, other, 1, "arrived")                                 # 2 free slots: admitted, 1 stays reserved
    put(outbox, clock, other, 2, "left")                                    # and that slot was there for it
    assert [i.event for i in outbox.items()] == ["arrived", "left", "arrived", "left"]


# --- L3 + the "several sessions pile up" case -----------------------------------------------------------------

def test_l3_back_to_back_departures_release_the_reservation_when_the_session_closes(tmp_path):
    outbox, clock, _ = make_outbox(tmp_path, max_items=5)
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    put(outbox, clock, a, 1, "arrived")
    put(outbox, clock, a, 2, "left")                                        # A closed: no reservation held for A any more
    put(outbox, clock, b, 1, "arrived")                                     # 3 stored + 1 reserved for B's left = 4 of 5
    put(outbox, clock, b, 2, "left")                                        # B closed: 4 stored
    with pytest.raises(OutboxFull):
        put(outbox, clock, c, 1, "arrived")                                 # would need 4+1+1 = 6 of 5
    assert [i.event for i in outbox.items()] == ["arrived", "left", "arrived", "left"]


def test_several_sessions_piling_up_never_leave_an_admitted_arrived_without_room_for_its_left(tmp_path):
    outbox, clock, _ = make_outbox(tmp_path, max_items=9)
    admitted, refused = 0, 0
    for n in range(10):
        session = str(uuid.uuid4())
        try:
            put(outbox, clock, session, 1, "arrived")
        except OutboxFull:
            refused += 1
            continue
        admitted += 1
        put(outbox, clock, session, 2, "left")                              # must never raise OutboxFull
    events = [i.event for i in outbox.items()]
    assert refused > 0 and admitted == events.count("arrived") == events.count("left") == 4
    assert len(events) <= 9


# --- L4: the byte limit gets the same reservation (one photo's worth) -----------------------------------------

def test_l4_bytes_reserve_room_for_the_left_photo(tmp_path):
    outbox, clock, session = make_outbox(tmp_path, max_items=50, max_bytes=1000, reserve_bytes=300)
    put(outbox, clock, session, 1, "arrived", size=200)                     # 200 stored + 300 reserved
    put(outbox, clock, session, 2, "confirm", size=200)                     # 400 + 300
    put(outbox, clock, session, 3, "confirm", size=200)                     # 600 + 300 = 900
    before = snapshot(outbox)
    with pytest.raises(OutboxFull) as info:
        put(outbox, clock, session, 4, "confirm", size=200)                 # 800 + 300 = 1100 > 1000
    assert info.value.reason == "bytes"
    assert snapshot(outbox) == before
    put(outbox, clock, session, 5, "left", size=200)                        # 800 <= 1000: the left uses the reserved bytes


# --- L5: evicting a refresh makes room for the new item AND the reservation, never more ------------------------

def test_l5_a_refresh_is_evicted_to_make_room_but_never_a_critical_event(tmp_path):
    outbox, clock, session = make_outbox(tmp_path, max_items=4)
    put(outbox, clock, session, 1, "arrived")
    put(outbox, clock, session, 2, "refresh")
    put(outbox, clock, session, 3, "refresh")
    put(outbox, clock, session, 4, "confirm")                               # 3 + 1 reserved: evicts the oldest refresh
    assert [(i.event, i.seq) for i in outbox.items()] == [("arrived", 1), ("refresh", 3), ("confirm", 4)]
    assert [(d.event, d.reason) for d in outbox.dead_items()] == [("refresh", "outbox full")]


def test_l5_when_no_refresh_is_left_nothing_is_evicted_for_the_reservation(tmp_path):
    outbox, clock, session = make_outbox(tmp_path, max_items=3)
    put(outbox, clock, session, 1, "arrived")
    put(outbox, clock, session, 2, "confirm")
    before = snapshot(outbox)
    with pytest.raises(OutboxFull):
        put(outbox, clock, session, 3, "refresh")
    assert snapshot(outbox) == before and outbox.dead_items() == []


# --- L6: restart -------------------------------------------------------------------------------------------------

def test_l6_a_restart_keeps_the_items_and_the_failure_counters(tmp_path):
    runner, clock, camera, server, outbox = make_runner(
        tmp_path, [True] * 4 + [False] * 6 + [True] * 1000, max_items=3)
    server.replies = [ConnectionError("down")] * 1000
    run_until(runner, clock, 100)           # arrived (10 s), left (45 s) stored; the second arrived (60 s) is refused
    assert [(i.event, i.seq) for i in outbox.items()] == [("arrived", 1), ("left", 2)]
    assert runner.store_failures == 1
    items_before = [(i.name, i.event_id, i.jpeg) for i in outbox.items()]

    # the process is killed and restarted on the same directory
    outbox2 = Outbox(tmp_path / "outbox", clock=clock, max_items=3)
    server2 = FakeServer(clock)
    runner2 = AsyncEventRunner(FakeCamera(), ScriptedDetector([False] * 1000), SETTINGS, outbox=outbox2,
                               send_job=server2.send_job, send_heartbeat=server2.heartbeat, clock=clock,
                               sleep=clock.sleep, wall=clock, rng=lambda: 0.5)
    assert [(i.name, i.event_id, i.jpeg) for i in outbox2.items()] == items_before     # same items, bytes and order
    assert runner2.store_failures == 0                                                  # this process has seen none
    snap = runner2.health.snapshot()
    assert snap["failures"]["total"] == 1 and snap["failures"]["by_kind"] == {"full_items": 1}
    assert snap["failures"]["by_event"]["arrived"] == 1
    assert snap["last_failure"]["event"] == "arrived" and snap["last_failure"]["kind"] == "full_items"
    run_until(runner2, clock, clock.elapsed() + 130)
    assert [meta["event"] for _, meta, _ in server2.attempts if meta["event_id"] in server2.accepted] == ["arrived", "left"]


# --- L7: a long outage with a flapping detector ---------------------------------------------------------------

def test_l7_flapping_detection_during_a_long_outage_never_refuses_the_left_of_a_known_session(tmp_path):
    flapping = ([True] * 3 + [False] * 6) * 200                             # one arrived/left pair every 45 s
    # An ODD capacity on purpose: events arrive in arrived/left pairs, so with an even capacity an arrived would never
    # meet exactly one free slot and a missing reservation would go unnoticed.
    runner, clock, camera, server, outbox = make_runner(tmp_path, flapping, max_items=19)
    server.replies = [ConnectionError("down")] * 100000
    run_until(runner, clock, 1200)                                          # 20 minutes: shorter than the 30 minute give-up
    events = [i.event for i in outbox.items()]
    assert len(events) <= 19
    arrived, left = events.count("arrived"), events.count("left")
    # every stored arrived has its left (or is the open session); a surplus left belongs to a session whose arrived was
    # refused, which the server never knew: harmless
    assert arrived >= 9 and left >= arrived - 1
    health = runner.health.snapshot()
    assert health["failures"]["by_event"].get("arrived", 0) > 0             # the box really did saturate
    assert health["left_of_known_session_refused"] == 0                     # ...and no left of a known session was refused
    assert health["given_up"] == {}                                         # nothing is old enough to be given up yet

    server.replies = []                                                     # the network comes back
    run_until(runner, clock, 1300)
    meta_of = {meta["event_id"]: meta for _, meta, _ in server.attempts}     # attempts repeat; accepted events do not
    sessions = {}
    for event_id in server.accepted:                                         # in the order the server accepted them
        meta = meta_of[event_id]
        sessions.setdefault(meta["session_id"], []).append(meta["event"])
    pairs = [events for events in sessions.values() if events == ["arrived", "left"]]
    strays = [events for events in sessions.values() if events not in (["arrived", "left"], ["left"])]
    assert len(pairs) >= 9                                                   # every admitted arrived got its left
    assert strays in ([], [["arrived"]])    # only the session still open at the end may lack its left; a lone ["left"]
    #                                       # (its arrived was refused, so the server never knew that session) is harmless


def test_l7_pinned_behaviour_the_30_minute_give_up_still_drops_arrived_and_left(tmp_path):
    """Unchanged on purpose (decision 2026-11-02) and recorded as an open risk: a `left` that could not be delivered
    for 30 minutes is given up, which leaves the server episode open."""
    runner, clock, camera, server, outbox = make_runner(tmp_path, [True] * 4 + [False] * 6 + [True] * 1000, max_items=20)
    server.replies = [ConnectionError("down")] * 100000
    run_until(runner, clock, 1700)
    assert [i.event for i in outbox.items()][:2] == ["arrived", "left"]
    run_until(runner, clock, 1900)                                          # 10 s + 1800 s have passed for the arrived
    assert "arrived" not in [i.event for i in outbox.items()]
    reasons = {d.event: d.reason for d in outbox.dead_items()}
    assert "gave up" in reasons["arrived"] and "gave up" in reasons["left"]
    assert runner.health.snapshot()["given_up"]["left"] == 1                # observable even though it is not fixed


# --- L8: real disk errors are not the logical limit -------------------------------------------------------------

@pytest.mark.parametrize("number,kind", [(errno.ENOSPC, "disk_full"), (errno.EROFS, "disk_read_only"),
                                         (errno.EIO, "disk_error")])
def test_classify_store_error_tells_the_disk_from_the_limit(number, kind):
    assert classify_store_error(OSError(number, "x")) == kind
    assert classify_store_error(OutboxFull("x", reason="items")) == "full_items"
    assert classify_store_error(OutboxFull("x", reason="bytes")) == "full_bytes"
    assert classify_store_error(ValueError("x")) == "error"


def test_l8_a_disk_error_is_recorded_as_such_and_changes_nothing_that_is_stored(tmp_path, monkeypatch, caplog):
    import outbox as outbox_module

    runner, clock, camera, server, outbox = make_runner(tmp_path, [True] * 1000, max_items=20)
    server.replies = [ConnectionError("down")] * 100000
    run_until(runner, clock, 20)                                            # arrived is stored
    before = snapshot(outbox)

    def disk_full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(outbox_module.os, "fsync", disk_full)
    stored = runner._send("refresh")                                        # a write that fails for a physical reason
    monkeypatch.undo()

    assert stored is False and runner.store_failures == 1
    assert runner.store_failures_disk == 1 and runner.store_failures_full == 0
    assert snapshot(outbox) == before and outbox.dead_items() == []        # nothing evicted, nothing half-written
    assert list((tmp_path / "outbox" / "items").glob("*.tmp")) == []
    assert any(r.levelname == "ERROR" and "disk_full" in r.getMessage() for r in caplog.records)
    snap = runner.health.snapshot()["failures"]
    assert snap["by_kind"] == {"disk_full": 1} and snap["by_event"] == {"refresh": 1}
    put_after = runner._send("refresh")                                     # the next one works once the disk does
    assert put_after is True


def test_l8_a_disk_error_on_the_left_of_a_known_session_is_flagged_loudly(tmp_path, monkeypatch, caplog):
    import outbox as outbox_module

    runner, clock, camera, server, outbox = make_runner(tmp_path, [True] * 4 + [False] * 1000, max_items=20)
    server.replies = [ConnectionError("down")] * 100000
    run_until(runner, clock, 20)                                            # arrived stored: the server will know this session
    assert runner.session_known is True

    def disk_full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(outbox_module.os, "fsync", disk_full)
    run_until(runner, clock, 60)                                            # the empty frames end the session: left cannot be written
    monkeypatch.undo()
    assert runner.health.snapshot()["left_of_known_session_refused"] == 1
    assert any("열린 채" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)
    assert [i.event for i in outbox.items()] == ["arrived"]                 # the stored arrived is untouched


def test_session_known_is_a_local_fact_not_a_server_ack_and_every_refused_left_is_counted(tmp_path, monkeypatch):
    """session_known = at least one event of this session was stored in the LOCAL outbox. It does not mean the server
    received anything: it stays True when the stored item was never delivered or was later given up. A refused left
    always counts in the total and the left-event failure statistics, flagged or not."""
    import outbox as outbox_module

    runner, clock, camera, server, outbox = make_runner(tmp_path, [True] * 4 + [False] * 1000, max_items=20)
    server.replies = [ConnectionError("down")] * 100000                     # the server never receives anything
    run_until(runner, clock, 20)
    assert [i.event for i in outbox.items()] == ["arrived"] and server.accepted == {}
    for stored in outbox.items():                                           # ...and the item is given up (age)
        outbox.discard(stored, "age")
    assert outbox.items() == []
    assert runner.session_known is True                                     # still local-True: not an ack, not undone by giving up

    def disk_full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(outbox_module.os, "fsync", disk_full)
    run_until(runner, clock, 60)
    monkeypatch.undo()
    snap = runner.health.snapshot()
    assert snap["left_of_known_session_refused"] == 1
    assert snap["failures"]["by_event"].get("left") == 1                    # also in the per-event statistics
    assert snap["failures"]["total"] == runner.store_failures >= 1          # and in the total


def test_the_left_of_a_session_the_server_never_heard_of_is_not_flagged_as_an_open_episode(tmp_path):
    flapping = ([True] * 3 + [False] * 6) * 200
    runner, clock, camera, server, outbox = make_runner(tmp_path, flapping, max_items=4)
    server.replies = [ConnectionError("down")] * 100000
    run_until(runner, clock, 400)
    health = runner.health.snapshot()
    assert health["failures"]["by_event"].get("left", 0) >= 1               # a left was refused: its arrived had been refused
    assert health["left_of_known_session_refused"] == 0                     # so the server never knew that session


# --- health.json ----------------------------------------------------------------------------------------------------

def test_health_json_is_written_atomically_and_survives_a_restart(tmp_path):
    clock = FakeClock()
    path = tmp_path / "outbox" / "health.json"
    health = OutboxHealth(path, clock=clock)
    health.record_failure("arrived", 3, "full_items", "no room")
    health.record_failure("left", 4, "disk_full", "ENOSPC", left_of_known_session=True)
    health.record_given_up("refresh")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["failures"]["total"] == 2
    assert data["failures"]["by_kind"] == {"full_items": 1, "disk_full": 1}
    assert data["left_of_known_session_refused"] == 1 and data["given_up"] == {"refresh": 1}
    assert data["last_failure"]["event"] == "left" and data["last_failure"]["seq"] == 4
    assert list(path.parent.glob("*.tmp")) == []
    again = OutboxHealth(path, clock=clock)
    assert again.snapshot()["failures"]["total"] == 2                        # restored


def test_a_corrupt_health_file_starts_fresh_instead_of_crashing(tmp_path):
    path = tmp_path / "outbox" / "health.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    health = OutboxHealth(path, clock=FakeClock())
    assert health.snapshot()["failures"]["total"] == 0
    health.record_failure("arrived", 1, "full_items", "x")
    assert json.loads(path.read_text(encoding="utf-8"))["failures"]["total"] == 1


def test_a_health_write_that_fails_is_logged_and_kept_in_memory_until_the_next_write(tmp_path, monkeypatch, caplog):
    import outbox as outbox_module

    path = tmp_path / "outbox" / "health.json"
    health = OutboxHealth(path, clock=FakeClock())

    def disk_full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(outbox_module.os, "fsync", disk_full)
    health.record_failure("arrived", 1, "disk_full", "x")                    # cannot be persisted: the disk is full
    monkeypatch.undo()
    assert health.persist_error is not None and not path.exists()
    assert health.snapshot()["failures"]["total"] == 1                      # still counted in memory
    assert any("health.json" in r.getMessage() for r in caplog.records)
    health.record_failure("arrived", 2, "disk_full", "x")                    # the next write succeeds and carries both
    assert json.loads(path.read_text(encoding="utf-8"))["failures"]["total"] == 2 and health.persist_error is None


# --- what the runner passes -----------------------------------------------------------------------------------------

def test_the_runner_reserves_for_everything_except_the_left(tmp_path):
    runner, clock, camera, server, outbox = make_runner(tmp_path, [True] * 4 + [False] * 1000, max_items=2)
    server.replies = [ConnectionError("down")] * 100000
    run_until(runner, clock, 20)                                            # arrived: 1 stored + 1 reserved = 2 of 2
    assert runner._send("refresh") is False and runner._send("confirm") is False   # neither may take the reserved slot
    assert [i.event for i in outbox.items()] == ["arrived"]
    run_until(runner, clock, 60)                                            # the car leaves: the left takes the reserved slot
    assert [i.event for i in outbox.items()] == ["arrived", "left"]
