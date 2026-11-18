"""AWS SES transport, the same set-up as the school systems.

Uses AWS_SES_ACCESS_KEY_ID / AWS_SES_SECRET_ACCESS_KEY / AWS_SES_REGION from the shared `.env`.
The sender is SH_FROM_EMAIL (falls back to FROM_EMAIL) and must be a verified SES identity.
With SH_EMAIL_MODE=log nothing is sent: mail is kept in OUTBOX (tests and offline work) and only the
subject and recipients are logged.
"""

import asyncio
import logging
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)
OUTBOX: list[dict] = []
_client = None


def _ses():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "ses", region_name=settings.AWS_SES_REGION, aws_access_key_id=settings.AWS_SES_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SES_SECRET_ACCESS_KEY,
            config=Config(connect_timeout=15, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"}),
        )
    return _client


def send_email(*, to: list[str], subject: str, html: str, text: Optional[str] = None, reply_to: Optional[str] = None) -> None:
    """Send one email. Raises on failure. `reply_to` is where a reply from the person's mail app goes."""
    real = [a for a in to if not a.lower().endswith((".test", ".example", ".invalid"))]
    if settings.SH_EMAIL_MODE == "log" or not real:  # sample accounts never reach SES
        if settings.is_production and settings.SH_EMAIL_MODE == "log":
            raise RuntimeError("SH_EMAIL_MODE=log is for development only")
        OUTBOX.append({"to": to, "subject": subject, "html": html, "text": text or "", "reply_to": reply_to})
        del OUTBOX[:-200]
        if settings.is_production:
            logger.info("[EMAIL:skipped] '%s' to sample address(es)", subject)
        else:  # development: the one-time code is in the subject so sample accounts can sign in
            logger.info("[EMAIL:log] '%s' to %s", subject, to)
        return
    to = real
    body = {"Html": {"Data": html, "Charset": "UTF-8"}}
    if text:
        body["Text"] = {"Data": text, "Charset": "UTF-8"}
    extra = {"ReplyToAddresses": [reply_to]} if reply_to else {}
    response = _ses().send_email(
        Source=settings.from_email, Destination={"ToAddresses": to},
        Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body}, **extra,
    )
    logger.info("[EMAIL] sent '%s' to %s, message id %s", subject, to, response.get("MessageId"))


async def send_async(**kwargs) -> bool:
    """Send without blocking the request. Returns False (and logs) on failure."""
    try:
        await asyncio.to_thread(send_email, **kwargs)
        return True
    except Exception:
        logger.exception("[EMAIL] could not send '%s'", kwargs.get("subject"))
        return False
