"""Envio de e-mail via SMTP — HTML do template + imagens em CID (não data-URI)."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import aiosmtplib

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infrastructure.email.inline_images import build_mime_images, prepare_html_with_cid

logger = get_logger(__name__)

# Zoho/Gmail/etc. recusam o lote inteiro — não é bounce do destinatário.
_PROVIDER_BLOCK_NEEDLES = (
    "unusual sending activity",
    "5.4.6",
    "unblockme",
    "temporarily blocked",
    "sending limit",
    "daily sending limit",
    "relay not permitted",
    "too many messages",
    "try after sometime",
)


def is_smtp_provider_block(error: str | None) -> bool:
    text = (error or "").lower()
    return any(n in text for n in _PROVIDER_BLOCK_NEEDLES)


async def count_recent_sends(session, *, since: datetime) -> int:
    from sqlalchemy import func, select

    from app.infrastructure.database.models import EmailRecord

    q = (
        select(func.count())
        .select_from(EmailRecord)
        .where(
            EmailRecord.status.in_(["sent", "dry_run"]),
            EmailRecord.sent_at.is_not(None),
            EmailRecord.sent_at >= since,
        )
    )
    return int((await session.execute(q)).scalar() or 0)


async def smtp_quota_status(session) -> tuple[bool, str]:
    """False se o teto horário/diário já foi atingido (não é bloqueio Zoho)."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    daily = int(settings.email_daily_limit or 0)
    hourly = int(settings.email_hourly_limit or 0)
    if hourly > 0:
        n = await count_recent_sends(session, since=now - timedelta(hours=1))
        if n >= hourly:
            return False, f"hourly_limit:{n}/{hourly}"
    if daily > 0:
        n = await count_recent_sends(session, since=now - timedelta(hours=24))
        if n >= daily:
            return False, f"daily_limit:{n}/{daily}"
    return True, "ok"


async def pace_after_send(delay_seconds: float | None = None) -> None:
    """Pausa entre disparos + jitter pra não parecer rajada."""
    settings = get_settings()
    delay = float(
        settings.dispatch_delay_seconds if delay_seconds is None else delay_seconds
    )
    jitter = max(0.0, float(settings.dispatch_delay_jitter_seconds))
    wait = max(0.0, delay) + (random.uniform(0.0, jitter) if jitter else 0.0)
    if wait > 0:
        await asyncio.sleep(wait)


class SMTPService:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        from_addr: str | None = None,
        use_tls: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.host = host if host is not None else settings.smtp_host
        self.port = port if port is not None else settings.smtp_port
        self.user = user if user is not None else settings.smtp_user
        self.password = password if password is not None else settings.smtp_password
        self.from_addr = from_addr if from_addr is not None else settings.smtp_from
        self.use_tls = use_tls if use_tls is not None else settings.smtp_use_tls

    async def build_message(
        self,
        to: str,
        subject: str,
        html_body: str,
        *,
        from_addr: str | None = None,
        convert_inline_images: bool = True,
    ) -> tuple[MIMEMultipart, str, int]:
        """
        Monta mensagem multipart/related:
          related
            alternative
              text/html (src=cid:...)
            image/* (Content-ID)

        Returns:
            (message, message_id, n_images)
        """
        sender = from_addr or self.from_addr
        domain = sender.split("@")[-1] if "@" in sender else "localhost"
        message_id = make_msgid(domain=domain)

        html = html_body
        n_images = 0
        image_parts = []

        if convert_inline_images:
            prepared = await prepare_html_with_cid(html_body, convert_svg=True)
            html = prepared.html
            n_images = prepared.count
            image_parts = build_mime_images(prepared.images)

        # related raiz (HTML + imagens inline)
        related = MIMEMultipart("related")
        related["From"] = sender
        related["To"] = to
        related["Subject"] = subject
        related["Date"] = formatdate(localtime=True)
        related["Message-ID"] = message_id
        related["MIME-Version"] = "1.0"

        alt = MIMEMultipart("alternative")
        # fallback texto
        alt.attach(
            MIMEText(
                "Este e-mail contém conteúdo HTML. Se não visualizar as imagens, "
                "verifique as configurações do seu cliente de e-mail.",
                "plain",
                "utf-8",
            )
        )
        alt.attach(MIMEText(html, "html", "utf-8"))
        related.attach(alt)

        for part in image_parts:
            related.attach(part)

        return related, message_id, n_images

    async def send_html(
        self,
        to: str,
        subject: str,
        html_body: str,
        *,
        from_addr: str | None = None,
        dry_run: bool = False,
        convert_inline_images: bool = True,
    ) -> dict:
        """
        Envia e-mail HTML.

        O texto/layout do template é preservado; imagens data:image;base64
        são convertidas para anexos CID (e SVG→PNG) para Gmail/Outlook.
        """
        msg, message_id, n_images = await self.build_message(
            to,
            subject,
            html_body,
            from_addr=from_addr,
            convert_inline_images=convert_inline_images,
        )
        sender = from_addr or self.from_addr

        if dry_run or (not self.host or (self.host == "localhost" and not self.user)):
            logger.info(
                "email_dry_run",
                to=to,
                subject=subject,
                message_id=message_id,
                body_len=len(html_body),
                inline_images=n_images,
            )
            return {
                "status": "dry_run",
                "message_id": message_id,
                "to": to,
                "subject": subject,
                "inline_images": n_images,
            }

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.host,
                port=self.port,
                username=self.user or None,
                password=self.password or None,
                start_tls=self.use_tls,
            )
            logger.info(
                "email_sent",
                to=to,
                subject=subject,
                message_id=message_id,
                inline_images=n_images,
            )
            return {
                "status": "sent",
                "message_id": message_id,
                "to": to,
                "subject": subject,
                "inline_images": n_images,
            }
        except Exception as exc:
            err = str(exc)
            blocked = is_smtp_provider_block(err)
            logger.error(
                "email_send_failed",
                to=to,
                error=err,
                provider_blocked=blocked,
            )
            return {
                "status": "blocked" if blocked else "failed",
                "message_id": message_id,
                "to": to,
                "subject": subject,
                "inline_images": n_images,
                "error": err,
                "provider_blocked": blocked,
            }
