"""External delivery for operator notifications (webhook, Telegram, email).

The in-app notification center stays the system of record.  This layer adds
an outbox: every notification at or above ``alert_min_severity`` gets one
delivery row per configured channel, deduplicated by a stable key within
``alert_dedupe_window_minutes`` (repeats only bump ``suppressed_count``).  A
scheduled job sends pending rows with bounded retries and exponential
backoff, and a daily digest summarises the last 24 hours.

Network I/O never happens inside the caller's transaction: callers only add
outbox rows, and the delivery job sends them.  Errors are recorded without
URLs or tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Notification, NotificationDelivery

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
UNCONFIGURED_CHANNEL = "unconfigured"
SEND_TIMEOUT_SECONDS = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _rank(severity: str) -> int:
    return SEVERITY_RANK.get(str(severity).lower(), 0)


def configured_channels() -> list[str]:
    channels = []
    if settings.alert_webhook_url.get_secret_value():
        channels.append("webhook")
    if settings.alert_telegram_bot_token.get_secret_value() and settings.alert_telegram_chat_id:
        channels.append("telegram")
    if settings.alert_email_smtp_host and settings.alert_email_from and settings.alert_email_to:
        channels.append("email")
    return channels


def notification_dedupe_key(notification: Notification) -> str:
    payload = notification.payload or {}
    if payload.get("dedupe_key"):
        return str(payload["dedupe_key"])[:255]
    return ":".join(str(part) for part in (
        notification.source, notification.category, notification.entity_type or "-",
        notification.entity_id if notification.entity_id is not None else "-", notification.title,
    ))[:255]


def queue_external_alert(
    db: Session,
    *,
    severity: str,
    title: str,
    message: str | None,
    dedupe_key: str,
    notification_id: int | None = None,
    kind: str = "alert",
    force: bool = False,
    dedupe_forever: bool = False,
    now: datetime | None = None,
) -> list[NotificationDelivery]:
    """Add outbox rows; returns the new rows (duplicates are suppressed)."""
    now = now or _now()
    if not force and _rank(severity) < _rank(settings.alert_min_severity):
        return []
    dedupe_key = dedupe_key[:255]
    created: list[NotificationDelivery] = []
    for channel in configured_channels() or [UNCONFIGURED_CHANNEL]:
        query = select(NotificationDelivery).where(
            NotificationDelivery.channel == channel,
            NotificationDelivery.dedupe_key == dedupe_key,
            NotificationDelivery.status != "failed",
        )
        if not dedupe_forever:
            query = query.where(
                NotificationDelivery.created_at >= now - timedelta(minutes=settings.alert_dedupe_window_minutes)
            )
        existing = db.scalar(query.order_by(NotificationDelivery.id.desc()))
        if existing is not None:
            existing.suppressed_count = int(existing.suppressed_count or 0) + 1
            continue
        row = NotificationDelivery(
            notification_id=notification_id,
            channel=channel,
            kind=kind,
            dedupe_key=dedupe_key,
            severity=severity,
            title=title[:255],
            message=message,
            status="pending" if channel != UNCONFIGURED_CHANNEL else "unconfigured",
            attempts=0,
            suppressed_count=0,
            next_attempt_at=now if channel != UNCONFIGURED_CHANNEL else None,
            created_at=now,
        )
        db.add(row)
        created.append(row)
    if created:
        db.flush()
    return created


def queue_notification(db: Session, notification: Notification) -> list[NotificationDelivery]:
    if _rank(notification.severity) < _rank(settings.alert_min_severity):
        return []
    if notification.id is None:
        db.flush()
    return queue_external_alert(
        db,
        severity=notification.severity,
        title=notification.title,
        message=notification.message,
        dedupe_key=notification_dedupe_key(notification),
        notification_id=notification.id,
    )


def _body(row: NotificationDelivery) -> dict:
    return {
        "kind": row.kind,
        "severity": row.severity,
        "title": row.title,
        "message": row.message,
        "dedupe_key": row.dedupe_key,
        "notification_id": row.notification_id,
        "created_at": _utc(row.created_at).isoformat() if row.created_at else None,
        "suppressed_duplicates": row.suppressed_count,
        "paper_only": True,
    }


def _send_webhook(row: NotificationDelivery) -> None:
    body = json.dumps(_body(row), sort_keys=True, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    secret = settings.alert_webhook_signing_secret.get_secret_value()
    if secret:
        headers["X-Alert-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = httpx.post(settings.alert_webhook_url.get_secret_value(), content=body, headers=headers,
                          timeout=SEND_TIMEOUT_SECONDS)
    response.raise_for_status()


def _text(row: NotificationDelivery) -> str:
    suffix = f"\n(+{row.suppressed_count} duplicate(s) suppressed)" if row.suppressed_count else ""
    return f"[{row.severity.upper()}] {row.title}\n{row.message or ''}{suffix}"


def _send_telegram(row: NotificationDelivery) -> None:
    token = settings.alert_telegram_bot_token.get_secret_value()
    response = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": settings.alert_telegram_chat_id, "text": _text(row)[:4000]},
        timeout=SEND_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _send_email(row: NotificationDelivery) -> None:
    message = EmailMessage()
    message["Subject"] = f"[{row.severity.upper()}] {row.title}"
    message["From"] = settings.alert_email_from
    message["To"] = ", ".join(settings.alert_email_to)
    message.set_content(_text(row))
    port = settings.alert_email_smtp_port
    smtp_class = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with smtp_class(settings.alert_email_smtp_host, port, timeout=SEND_TIMEOUT_SECONDS) as client:
        if port != 465:
            client.starttls()
        if settings.alert_email_smtp_username:
            client.login(settings.alert_email_smtp_username, settings.alert_email_smtp_password.get_secret_value())
        client.send_message(message)


SENDERS: dict[str, Callable[[NotificationDelivery], None]] = {
    "webhook": _send_webhook,
    "telegram": _send_telegram,
    "email": _send_email,
}


def _redacted_error(exc: Exception) -> str:
    # Never persist URLs, bot tokens, or SMTP credentials from exception text.
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTPStatusError: status {exc.response.status_code}"
    return exc.__class__.__name__


def deliver_pending_alerts(
    db: Session,
    *,
    now: datetime | None = None,
    senders: dict[str, Callable[[NotificationDelivery], None]] | None = None,
    limit: int = 50,
) -> dict:
    """Send due outbox rows once; failed sends retry with exponential backoff."""
    now = now or _now()
    senders = senders or SENDERS
    rows = db.scalars(select(NotificationDelivery).where(
        NotificationDelivery.status == "pending",
        NotificationDelivery.next_attempt_at <= now,
    ).order_by(NotificationDelivery.id).limit(limit)).all()
    sent = failed = retried = 0
    for row in rows:
        sender = senders.get(row.channel)
        row.attempts = int(row.attempts or 0) + 1
        try:
            if sender is None:
                raise RuntimeError(f"No sender for channel {row.channel}")
            sender(row)
        except Exception as exc:  # delivery failures must never break the job
            row.last_error = _redacted_error(exc)
            if row.attempts >= settings.alert_max_attempts:
                row.status = "failed"
                row.next_attempt_at = None
                failed += 1
            else:
                row.next_attempt_at = now + timedelta(minutes=2 ** row.attempts)
                retried += 1
        else:
            row.status = "sent"
            row.sent_at = now
            row.last_error = None
            row.next_attempt_at = None
            sent += 1
    db.commit()
    return {"status": "complete", "due": len(rows), "sent": sent, "retrying": retried, "failed": failed,
            "channels": configured_channels()}


def queue_daily_digest(db: Session, *, now: datetime | None = None) -> dict:
    """Queue one digest per New York date summarising the last 24 hours."""
    from app.services.intraday_data import NY

    now = now or _now()
    day = now.astimezone(NY).date().isoformat()
    since = (now - timedelta(hours=24)).replace(tzinfo=None)
    counts = dict(db.execute(select(Notification.severity, func.count()).where(
        Notification.created_at >= since).group_by(Notification.severity)).all())
    open_critical = db.scalars(select(Notification).where(
        Notification.severity == "critical", Notification.status == "open",
    ).order_by(Notification.created_at.desc()).limit(10)).all()
    since_utc = now - timedelta(hours=24)
    suppressed = db.scalar(select(func.coalesce(func.sum(NotificationDelivery.suppressed_count), 0)).where(
        NotificationDelivery.created_at >= since_utc)) or 0
    failed = db.scalar(select(func.count()).select_from(NotificationDelivery).where(
        NotificationDelivery.status == "failed", NotificationDelivery.created_at >= since_utc)) or 0
    lines = [
        f"Notifications in the last 24h: critical {counts.get('critical', 0)}, "
        f"warning {counts.get('warning', 0)}, info {counts.get('info', 0)}.",
        f"Duplicate alerts suppressed: {int(suppressed)}. Failed deliveries: {int(failed)}.",
    ]
    if open_critical:
        lines.append("Open critical items:")
        lines.extend(f"- {row.title}: {(row.message or '')[:160]}" for row in open_critical)
    else:
        lines.append("No open critical items.")
    rows = queue_external_alert(
        db, severity="info", title=f"Paper trading daily digest {day}", message="\n".join(lines),
        dedupe_key=f"digest:{day}", kind="digest", force=True, dedupe_forever=True, now=now,
    )
    db.commit()
    return {"status": "queued" if rows else "already_queued", "date": day, "channels": configured_channels()}


def alert_delivery_status(db: Session, *, limit: int = 20) -> dict:
    """Read-only delivery configuration and recent outbox rows (no secrets)."""
    rows = db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id.desc()).limit(limit)).all()
    return {
        "channels": configured_channels(),
        "min_severity": settings.alert_min_severity,
        "dedupe_window_minutes": settings.alert_dedupe_window_minutes,
        "max_attempts": settings.alert_max_attempts,
        "recent": [
            {
                "id": row.id, "channel": row.channel, "kind": row.kind, "severity": row.severity,
                "title": row.title, "status": row.status, "attempts": row.attempts,
                "suppressed_count": row.suppressed_count, "last_error": row.last_error,
                "notification_id": row.notification_id,
                "created_at": _utc(row.created_at).isoformat() if row.created_at else None,
                "sent_at": _utc(row.sent_at).isoformat() if row.sent_at else None,
            }
            for row in rows
        ],
    }
