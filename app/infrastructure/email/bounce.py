"""Parse de bounces / DSN (Delivery Status Notification).

Cobre o caso clássico: SMTP aceitou o envio (status=sent), mas o servidor
destino devolve NDR porque a caixa postal não existe — mesmo com domínio/MX
válidos (e-mail antigo excluído, tipagem inventada no site, etc.).
"""

from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.utils import parseaddr
from typing import Any

# --- classificação ---
CLASS_MAILBOX_MISSING = "mailbox_missing"  # domínio ok / caixa inexistente
CLASS_DOMAIN_REJECT = "domain_reject"  # domínio/MX recusou ou não existe
CLASS_POLICY = "policy"  # spam, greylist, bloqueio, full
CLASS_UNKNOWN = "unknown"

# padrões fortes de "usuário não existe"
_MAILBOX_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.I)
    for p in (
        r"\b5\.1\.1\b",
        r"\b5\.1\.3\b",
        r"\b550\b.*\b(user|mailbox|recipient|address)\b",
        r"user\s*(unknown|not\s*found|doesn'?t\s*exist|does\s*not\s*exist)",
        r"mailbox\s*(unavailable|not\s*found|doesn'?t\s*exist|does\s*not\s*exist|unknown)",
        r"no\s+such\s+(user|mailbox|recipient)",
        r"recipient\s+address\s+rejected",
        r"unknown\s+user",
        r"invalid\s+recipient",
        r"address\s+rejected",
        r"does\s+not\s+exist",
        r"inexistent|inexistente|inexistente",
        r"caixa\s+(postal|n[aã]o\s+existe|inexistente)",
        r"usu[aá]rio\s+(desconhecido|n[aã]o\s+existe|inexistente)",
        r"undeliverable.*address",
        r"the\s+email\s+account\s+that\s+you\s+tried\s+to\s+reach\s+does\s+not\s+exist",
    )
]

_DOMAIN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.I)
    for p in (
        r"\b5\.1\.2\b",
        r"\b5\.4\.4\b",
        r"\b5\.7\.1\b.*\b(relay|domain)\b",
        r"domain\s+(not\s+found|does\s+not\s+exist|unknown)",
        r"no\s+MX\b",
        r"host\s+(not\s+found|unknown)",
        r"name\s+or\s+service\s+not\s+known",
        r"dns\s+(error|failure|lookup)",
        r"unrouteable\s+address",
    )
]

_POLICY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.I)
    for p in (
        r"\b4\.\d\.\d\b",  # transient
        r"mailbox\s+(full|quota)",
        r"over\s+quota",
        r"greylist",
        r"blocked",
        r"spam",
        r"reputation",
        r"policy\s+rejection",
        r"message\s+rejected",
    )
]

_BOUNCE_FROM = re.compile(
    r"(mailer-daemon|postmaster|mail-daemon|noreply.*bounce|"
    r"bounce|delivery.?status|mail.?delivery)",
    re.I,
)
_BOUNCE_SUBJECT = re.compile(
    r"(undeliver|delivery\s+status|mail\s+delivery\s+failed|"
    r"failure\s+notice|returned\s+mail|n[aã]o\s+entreg|"
    r"falha\s+na\s+entrega|aviso\s+de\s+falha|"
    r"delivery\s+notification|returned\s+to\s+sender)",
    re.I,
)

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)
_MSG_ID_RE = re.compile(r"<[^>]+>")
_FINAL_RECIP = re.compile(
    r"(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*([^\s;]+)",
    re.I,
)
_ORIG_MSG_ID = re.compile(
    r"(?:Original-Message-ID|Message-ID):\s*(<[^>]+>)",
    re.I,
)
_DIAG = re.compile(r"Diagnostic-Code:\s*(.+)", re.I)
_STATUS_DSN = re.compile(r"^Status:\s*([245]\.\d+\.\d+)", re.I | re.M)


@dataclass
class BounceHit:
    """Um bounce extraído da caixa IMAP."""

    imap_uid: str
    subject: str = ""
    from_addr: str = ""
    date_header: str = ""
    recipients: list[str] = field(default_factory=list)
    original_message_ids: list[str] = field(default_factory=list)
    diagnostic: str = ""
    dsn_status: str = ""
    classification: str = CLASS_UNKNOWN
    raw_snippet: str = ""
    is_bounce: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "imap_uid": self.imap_uid,
            "subject": self.subject,
            "from_addr": self.from_addr,
            "date_header": self.date_header,
            "recipients": self.recipients,
            "original_message_ids": self.original_message_ids,
            "diagnostic": self.diagnostic[:500],
            "dsn_status": self.dsn_status,
            "classification": self.classification,
            "is_bounce": self.is_bounce,
            "raw_snippet": self.raw_snippet[:400],
        }


def normalize_message_id(value: str | None) -> str:
    if not value:
        return ""
    v = value.strip()
    if not v:
        return ""
    if not v.startswith("<"):
        v = f"<{v}>"
    return v.lower()


def looks_like_bounce(msg: Message) -> bool:
    """Heurística rápida: From/Subject/Content-Type de NDR/DSN."""
    from_h = msg.get("From") or ""
    subj = msg.get("Subject") or ""
    ctype = (msg.get_content_type() or "").lower()
    if "report" in ctype or "delivery-status" in ctype:
        return True
    if _BOUNCE_FROM.search(from_h):
        return True
    if _BOUNCE_SUBJECT.search(subj):
        return True
    # alguns provedores mandam como multipart/mixed com Attachment.eml
    return False


def _part_as_text(part: Message) -> str:
    """Extrai texto de um part MIME (compatível com policy.default e classic)."""
    ctype = (part.get_content_type() or "").lower()

    # get_content() primeiro — no policy.default:
    #   message/delivery-status → bytes com Final-Recipient/Status/...
    #   message/rfc822 → EmailMessage aninhada
    try:
        content = part.get_content()  # type: ignore[attr-defined]
        if isinstance(content, bytes):
            return content.decode(part.get_content_charset() or "utf-8", errors="replace")
        if isinstance(content, str):
            return content
        if isinstance(content, Message):
            hdrs = "".join(f"{k}: {v}\n" for k, v in content.items())
            body = _part_as_text(content) if not content.is_multipart() else _walk_text(content)
            return hdrs + "\n" + body
    except Exception:
        pass

    # message/rfc822 aninhado (compat32 / payload list)
    if ctype in {"message/rfc822", "message/global"} or (
        ctype.startswith("message/")
        and ctype
        not in {"message/delivery-status", "message/disposition-notification"}
    ):
        try:
            inner = part.get_payload()
            if isinstance(inner, list):
                chunks: list[str] = []
                for m in inner:
                    if isinstance(m, Message):
                        hdrs = "".join(f"{k}: {v}\n" for k, v in m.items())
                        chunks.append(hdrs + "\n" + _walk_text(m))
                    else:
                        chunks.append(str(m))
                return "\n".join(chunks)
            if isinstance(inner, Message):
                hdrs = "".join(f"{k}: {v}\n" for k, v in inner.items())
                return hdrs + "\n" + _walk_text(inner)
            if isinstance(inner, str):
                return inner
        except Exception:
            pass

    # classic get_payload(decode=True)
    try:
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if isinstance(payload, str):
            return payload
    except Exception:
        pass

    try:
        payload = part.get_payload(decode=False)
        if isinstance(payload, str):
            return payload
    except Exception:
        pass
    return ""


def _walk_text(msg: Message) -> str:
    """Concatena partes relevantes do NDR (texto, DSN, rfc822 original)."""
    if not msg.is_multipart():
        return _part_as_text(msg)

    parts: list[str] = []
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype.startswith("multipart/"):
            continue
        if ctype.startswith("image/") or ctype.startswith("application/"):
            continue
        # delivery-status/rfc822: is_multipart=True no policy.default, mas
        # o conteúdo útil está no próprio part (get_content), não nos filhos vazios.
        if ctype in {
            "message/delivery-status",
            "message/disposition-notification",
            "message/rfc822",
            "message/global",
            "text/rfc822-headers",
        }:
            t = _part_as_text(part)
            if t.strip():
                parts.append(t)
            continue
        if part.is_multipart():
            continue
        if ctype.startswith("text/") or ctype in {"text/plain", "text/html"}:
            t = _part_as_text(part)
            if t.strip():
                parts.append(t)
    return "\n".join(parts)


def _extract_emails(blob: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in _EMAIL_RE.finditer(blob or ""):
        addr = m.group(0).lower().strip(".,;<>\"'")
        # ignora daemons comuns
        local = addr.split("@", 1)[0]
        if local in {
            "mailer-daemon",
            "postmaster",
            "noreply",
            "no-reply",
            "mail-daemon",
            "daemon",
        }:
            continue
        if addr not in seen:
            seen.add(addr)
            found.append(addr)
    return found


def classify_bounce(text: str, dsn_status: str = "") -> str:
    blob = f"{dsn_status}\n{text}"
    if any(p.search(blob) for p in _MAILBOX_PATTERNS):
        return CLASS_MAILBOX_MISSING
    if any(p.search(blob) for p in _DOMAIN_PATTERNS):
        return CLASS_DOMAIN_REJECT
    if any(p.search(blob) for p in _POLICY_PATTERNS):
        return CLASS_POLICY
    # Status DSN 5.1.x costuma ser recipient permanent
    if dsn_status.startswith("5.1."):
        return CLASS_MAILBOX_MISSING
    if dsn_status.startswith("5.4.") or dsn_status.startswith("5.0."):
        return CLASS_DOMAIN_REJECT
    if dsn_status.startswith("4."):
        return CLASS_POLICY
    if dsn_status.startswith("5."):
        return CLASS_MAILBOX_MISSING  # permanente genérico — trate como mailbox
    return CLASS_UNKNOWN


def parse_bounce_message(raw: bytes | str, *, imap_uid: str = "") -> BounceHit:
    """Extrai destinatários rejeitados, Message-ID original e diagnóstico."""
    if isinstance(raw, bytes):
        msg = email.message_from_bytes(raw, policy=policy.default)
    else:
        msg = email.message_from_string(raw, policy=policy.default)

    hit = BounceHit(
        imap_uid=str(imap_uid),
        subject=str(msg.get("Subject") or "")[:300],
        from_addr=parseaddr(msg.get("From") or "")[1],
        date_header=str(msg.get("Date") or "")[:80],
        is_bounce=looks_like_bounce(msg),
    )

    body = _walk_text(msg)
    hit.raw_snippet = body[:800]

    # Final/Original-Recipient no DSN
    for m in _FINAL_RECIP.finditer(body):
        addr = m.group(1).strip().lower().strip("<>")
        if "@" in addr and addr not in hit.recipients:
            hit.recipients.append(addr)

    # Message-IDs originais
    for m in _ORIG_MSG_ID.finditer(body):
        mid = normalize_message_id(m.group(1))
        if mid and mid not in hit.original_message_ids:
            hit.original_message_ids.append(mid)

    # também headers do envelope atual (raro no bounce externo)
    for h in ("In-Reply-To", "References", "X-Failed-Recipients"):
        val = msg.get(h) or ""
        if h == "X-Failed-Recipients":
            for addr in _extract_emails(val):
                if addr not in hit.recipients:
                    hit.recipients.append(addr)
        else:
            for mid_m in _MSG_ID_RE.finditer(val):
                mid = normalize_message_id(mid_m.group(0))
                if mid and mid not in hit.original_message_ids:
                    hit.original_message_ids.append(mid)

    diag_m = _DIAG.search(body)
    if diag_m:
        hit.diagnostic = diag_m.group(1).strip()[:500]
    status_m = _STATUS_DSN.search(body)
    if status_m:
        hit.dsn_status = status_m.group(1).strip()

    # se não achou recipient no DSN, tenta extrair e-mails do corpo
    # (excluindo o from do bounce e nosso smtp)
    if not hit.recipients:
        for addr in _extract_emails(body):
            if addr == (hit.from_addr or "").lower():
                continue
            if addr not in hit.recipients:
                hit.recipients.append(addr)
            if len(hit.recipients) >= 5:
                break

    if not hit.diagnostic:
        # primeira linha com 550/5xx
        for line in body.splitlines():
            if re.search(r"\b(550|5\.\d\.\d|554|553)\b", line):
                hit.diagnostic = line.strip()[:500]
                break

    hit.classification = classify_bounce(
        f"{hit.subject}\n{hit.diagnostic}\n{body[:2000]}",
        hit.dsn_status,
    )

    # se From/Subject parecem bounce mas classificação unknown, ainda conta
    if not hit.is_bounce and (
        hit.dsn_status
        or hit.classification != CLASS_UNKNOWN
        or hit.diagnostic
    ):
        hit.is_bounce = True

    return hit
