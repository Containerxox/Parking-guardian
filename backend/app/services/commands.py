"""Device commands carried by the heartbeat response (ADR 0019 decision 8; design 6.9, 6.10).

    pending   -> delivered   this heartbeat response carries it. Repeated on every heartbeat of that session until
                             it is no longer active, so a lost response loses nothing (the device ignores a
                             command_id it has already executed).
    pending|delivered -> fulfilled   a confirm photo naming the command_id was received (services/ingest.py)
    pending|delivered -> cancelled   the session ended (left) or was superseded (ingest), or the device listed the
                                     command in acked_commands (it will not execute it, e.g. wrong session)
    pending|delivered -> expired     expires_at has passed (marked here, at the machine's next heartbeat)

"Delivered" only means the server put it in a response; "fulfilled" is the device's proof of execution.
Every change is a conditional UPDATE on (machine, status in pending/delivered), so a heartbeat can never move a
command that ingest has just fulfilled or cancelled back to delivered, and a machine can only touch its own
commands. Only the session the device reports, while it is the machine's active session, gets commands.
"""
import uuid
from datetime import datetime

from sqlalchemy import exists, select, update

from ..extensions import db
from ..models import CommandStatus, DeviceCommand, DeviceSession, SessionStatus, Violation, iso

_ACTIVE = DeviceCommand.status.in_(CommandStatus.ACTIVE)


def _set_status(machine_id: int, condition, status: str, now: datetime) -> None:
    db.session.execute(update(DeviceCommand).where(DeviceCommand.machine_id == machine_id, _ACTIVE, condition)
                       .values(status=status, updated_at=now).execution_options(synchronize_session=False))


def for_heartbeat(machine_id: int, session_id: uuid.UUID | None, acked: list[uuid.UUID],
                  now: datetime) -> tuple[list[dict], bool]:
    """Apply the device's acknowledgements and expiries, then return (commands to deliver, violation_active).
    The caller commits."""
    if acked:
        _set_status(machine_id, DeviceCommand.command_id.in_(acked), CommandStatus.CANCELLED, now)
    _set_status(machine_id, DeviceCommand.expires_at <= now, CommandStatus.EXPIRED, now)

    session = db.session.get(DeviceSession, session_id) if session_id else None
    if session is not None and session.machine_id != machine_id:
        session = None   # another machine's session id: nothing to deliver, nothing to change

    commands = []
    if session is not None and session.status == SessionStatus.ACTIVE:
        rows = db.session.execute(
            update(DeviceCommand).where(DeviceCommand.machine_id == machine_id,
                                        DeviceCommand.session_id == session.session_id, _ACTIVE,
                                        DeviceCommand.expires_at > now)
            .values(status=CommandStatus.DELIVERED, updated_at=now)
            .returning(DeviceCommand.command_id, DeviceCommand.type, DeviceCommand.session_id,
                       DeviceCommand.due_at, DeviceCommand.expires_at)
            .execution_options(synchronize_session=False)).all()
        commands = [{"command_id": str(r.command_id), "type": r.type, "session_id": str(r.session_id),
                     "due_at": iso(r.due_at), "expires_at": iso(r.expires_at)}
                    for r in sorted(rows, key=lambda r: (r.due_at, str(r.command_id)))]

    # LED (design 6.2, 6.10): an open episode, unless the device already reported this session's car as gone.
    episode_open = db.session.scalar(select(exists().where(Violation.machine_id == machine_id,
                                                           Violation.ended_at.is_(None))))
    left_reported = session is not None and session.left_reported_at is not None
    return commands, bool(episode_open and not left_reported)
