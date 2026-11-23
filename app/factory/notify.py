"""Notifications: email (SMTP) and Microsoft Teams (incoming webhook). Messages are informational only (findings, reports,
pending approvals); they never carry a command to any plant system. When a channel is not configured the message is stored as
NOT_CONFIGURED with its full preview; nothing is ever reported as sent unless the server accepted it."""
from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage

import httpx
from sqlalchemy import text

from ..config import settings
from ..deps import get_engine, query
from ..sources import _scrub
from ..twin.scenarios import audit


def email_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def teams_url() -> str:
    from .integrations import secret_value
    return secret_value("teams", "webhook_url") or settings.teams_webhook_url.get_secret_value()


def teams_configured() -> bool:
    return bool(teams_url())


def channel_status() -> dict:
    return {"EMAIL": "CONFIGURED" if email_configured() else "NOT_CONFIGURED", "TEAMS": "CONFIGURED" if teams_configured() else "NOT_CONFIGURED"}


def teams_card(subject: str, body: str) -> dict:
    """Plain MessageCard (accepted by Teams incoming webhooks and Workflows)."""
    return {"@type": "MessageCard", "@context": "https://schema.org/extensions", "summary": subject[:150], "themeColor": "1F2F59",
            "title": subject[:200], "text": body.replace("\n", "<br>")[:20000]}


def _send_email(recipients: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, recipients, subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password.get_secret_value())
        s.send_message(msg)


def _send_teams(subject: str, body: str) -> None:
    url = teams_url()
    r = httpx.post(url, json=teams_card(subject, body), timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"Teams webhook returned HTTP {r.status_code}")


def prepare(channel: str, subject: str, body: str, actor: str, recipients: str | None = None, run_id: str | None = None,
            report_id: str | None = None) -> dict:
    status = "PREPARED" if (email_configured() if channel == "EMAIL" else teams_configured()) else "NOT_CONFIGURED"
    with get_engine().begin() as c:
        nid = c.execute(text("INSERT INTO twin.notification (run_id, report_id, channel, recipients, subject, body, status, actor) "
                             "VALUES (CAST(:r AS uuid), :rep, :ch, :to, :s, :b, :st, :a) RETURNING id"),
                        dict(r=run_id, rep=report_id, ch=channel, to=recipients, s=subject, b=body, st=status, a=actor)).scalar_one()
        audit(c, actor, "NOTIFICATION_PREPARED", "notification", str(nid), {"channel": channel, "status": status, "report_id": report_id})
    return {"id": nid, "channel": channel, "status": status, "subject": subject}


def send(notification_id: int, actor: str, recipients: str | None = None) -> dict:
    rows = query("SELECT * FROM twin.notification WHERE id = :i", i=notification_id)
    if not rows:
        raise LookupError("notification not found")
    n = rows[0]
    to = (recipients or n["recipients"] or "").strip()
    status, err = "SENT", None
    try:
        if n["channel"] == "EMAIL":
            if not email_configured():
                status, err = "NOT_CONFIGURED", "SMTP is not configured (SMTP_HOST, SMTP_FROM in backend/.env)."
            elif not to:
                status, err = "FAILED", "No recipients."
            else:
                _send_email(to, n["subject"], n["body"])
        else:
            if not teams_configured():
                status, err = "NOT_CONFIGURED", "Microsoft Teams is not configured (webhook URL on the Integrations page or TEAMS_WEBHOOK_URL)."
            else:
                _send_teams(n["subject"], n["body"])
    except Exception as e:                       # message only, scrubbed: never a URL or credential
        status, err = "FAILED", _scrub(f"{type(e).__name__}: {e}", settings.smtp_password.get_secret_value(), teams_url())
    with get_engine().begin() as c:
        c.execute(text("UPDATE twin.notification SET status = :s, error = :e, recipients = :to, sent_at = CASE WHEN :s = 'SENT' THEN now() END, "
                       "actor = :a WHERE id = :i"), dict(s=status, e=err, to=to or None, a=actor, i=notification_id))
        audit(c, actor, "NOTIFICATION_SEND", "notification", str(notification_id), {"channel": n["channel"], "status": status, "error": err})
        c.execute(text("INSERT INTO twin.integration_log (connector_id, action, record_ref, status, records, actor, error, details) "
                       "VALUES (:c, 'SEND', :r, :s, :n, :a, :e, CAST(:d AS jsonb))"),
                  dict(c="teams" if n["channel"] == "TEAMS" else "email", r=n["report_id"] or f"notification:{notification_id}",
                       s="OK" if status == "SENT" else ("NOT_CONFIGURED" if status == "NOT_CONFIGURED" else "FAILED"),
                       n=1 if status == "SENT" else 0, a=actor, e=err, d=json.dumps({"subject": n["subject"]})))
    return {"id": notification_id, "channel": n["channel"], "status": status, "error": err}
