"""Cliente IMAP síncrono (usado via asyncio.to_thread).

Reusa credenciais SMTP quando IMAP_* não está definido.
Para Zoho Mail Pro: smtppro.zoho.com → imappro.zoho.com.
"""

from __future__ import annotations

import imaplib
import re
from dataclasses import dataclass
from typing import Iterator

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# hosts SMTP → IMAP conhecidos
_SMTP_TO_IMAP = {
    "smtppro.zoho.com": "imappro.zoho.com",
    "smtp.zoho.com": "imap.zoho.com",
    "smtp.zoho.eu": "imap.zoho.eu",
    "smtp.zoho.in": "imap.zoho.in",
    "smtp.gmail.com": "imap.gmail.com",
    "smtp.office365.com": "outlook.office365.com",
    "smtp-mail.outlook.com": "outlook.office365.com",
}


def default_imap_host(smtp_host: str | None = None) -> str:
    settings = get_settings()
    if settings.imap_host:
        return settings.imap_host
    host = (smtp_host or settings.smtp_host or "").strip().lower()
    if host in _SMTP_TO_IMAP:
        return _SMTP_TO_IMAP[host]
    # fallback: smtp.X → imap.X
    if host.startswith("smtp."):
        return "imap." + host[5:]
    if host.startswith("smtppro."):
        return "imappro." + host[8:]
    return host or "localhost"


@dataclass
class ImapMessage:
    uid: str
    raw: bytes


class ImapMailbox:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        *,
        use_ssl: bool | None = None,
        folder: str | None = None,
    ) -> None:
        settings = get_settings()
        self.host = host or default_imap_host()
        self.port = port if port is not None else settings.imap_port
        self.user = user if user is not None else (settings.imap_user or settings.smtp_user)
        self.password = (
            password
            if password is not None
            else (settings.imap_password or settings.smtp_password)
        )
        self.use_ssl = settings.imap_use_ssl if use_ssl is None else use_ssl
        self.folder = folder or settings.imap_folder
        self._conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        if not self.host or not self.user or not self.password:
            raise RuntimeError(
                "IMAP não configurado: defina IMAP_HOST/IMAP_USER/IMAP_PASSWORD "
                "ou reutilize SMTP_* (Zoho: imappro.zoho.com)"
            )
        if self.use_ssl:
            self._conn = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            self._conn = imaplib.IMAP4(self.host, self.port)
            try:
                self._conn.starttls()
            except Exception:
                pass
        self._conn.login(self.user, self.password)
        typ, _ = self._conn.select(self.folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Não foi possível abrir pasta IMAP {self.folder!r}")
        logger.info(
            "imap_connected",
            host=self.host,
            folder=self.folder,
            user=self.user,
        )

    def close(self) -> None:
        if not self._conn:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            self._conn.logout()
        except Exception:
            pass
        self._conn = None

    def __enter__(self) -> "ImapMailbox":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search_uids(
        self,
        *,
        since_days: int | None = 30,
        subjects_hint: bool = True,
        from_daemon: bool = True,
    ) -> list[str]:
        """Busca UIDs candidatos a bounce. Union de critérios OR no cliente."""
        assert self._conn is not None
        uids: set[str] = set()

        criteria: list[str] = []
        if since_days and since_days > 0:
            # IMAP SINCE dd-Mon-yyyy
            import datetime as dt

            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)).strftime(
                "%d-%b-%Y"
            )
            date_crit = f'SINCE {since}'
        else:
            date_crit = "ALL"

        # várias buscas e união (IMAP OR aninhado é chato)
        if from_daemon:
            for name in ("mailer-daemon", "postmaster", "Mail Delivery"):
                criteria.append(f'(FROM "{name}" {date_crit})')
        if subjects_hint:
            for sub in (
                "Undeliver",
                "Delivery Status",
                "Mail delivery failed",
                "Failure Notice",
                "Returned mail",
                "Undeliverable",
                "não entreg",
                "falha na entrega",
            ):
                criteria.append(f'(SUBJECT "{sub}" {date_crit})')
        # Content-Type report — nem todo servidor indexa; tenta TEXT
        criteria.append(f'(TEXT "Delivery-Status" {date_crit})')
        criteria.append(f'(TEXT "Diagnostic-Code" {date_crit})')
        criteria.append(f'(TEXT "Final-Recipient" {date_crit})')

        if not criteria:
            criteria = [f"({date_crit})"]

        for crit in criteria:
            try:
                typ, data = self._conn.uid("SEARCH", None, crit)
            except Exception as exc:
                logger.debug("imap_search_failed", criteria=crit, error=str(exc))
                continue
            if typ != "OK" or not data or not data[0]:
                continue
            for uid in data[0].split():
                uids.add(uid.decode() if isinstance(uid, bytes) else str(uid))

        # ordena numericamente
        return sorted(uids, key=lambda x: int(x) if x.isdigit() else 0)

    def fetch_raw(self, uid: str) -> bytes | None:
        assert self._conn is not None
        typ, data = self._conn.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not data:
            return None
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                return bytes(part[1])
        return None

    def iter_candidates(
        self,
        *,
        since_days: int = 30,
        limit: int = 200,
    ) -> Iterator[ImapMessage]:
        uids = self.search_uids(since_days=since_days)
        if limit and len(uids) > limit:
            uids = uids[-limit:]  # mais recentes
        for uid in uids:
            raw = self.fetch_raw(uid)
            if raw:
                yield ImapMessage(uid=uid, raw=raw)


def is_likely_imap_uid(value: str) -> bool:
    return bool(re.fullmatch(r"\d+", value or ""))
