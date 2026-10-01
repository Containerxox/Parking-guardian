"""Notification publishing port and delivery adapters.

Current phase: SyncNotificationPublisher handles the event inside the request.
Later phase: an SQS publisher puts {"violation_id": ...} on a queue and a worker calls the same
service function, so the API no longer depends on notification delivery at all.
"""
import logging

log = logging.getLogger(__name__)


class SyncNotificationPublisher:
    name = "sync"

    def publish_violation_detected(self, violation_id: int) -> None:
        from ..services import notifications

        notifications.notify_violation(violation_id)


class ConsoleEmailSender:
    """Local stand-in for Amazon SES: writes the email to the log instead of sending it."""

    name = "console"

    def send(self, to: str, subject: str, body: str) -> None:
        log.info("[EMAIL to=%s] %s | %s", to, subject, body.replace("\n", " / "))


def build_publisher(config) -> object:
    return SyncNotificationPublisher()


def build_email_sender(config) -> object:
    return ConsoleEmailSender()
