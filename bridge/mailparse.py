"""Pure email-parsing helpers: header decoding, sender, body, quoted-reply stripping."""

import re
from email.header import decode_header, make_header
from email.utils import parseaddr


def decode_mime(value):
    """Decode an RFC 2047 encoded header (=?utf-8?B?…?=) into plain text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def extract_sender(msg):
    """Return ``(display, address)`` for the From header — no LLM involved.

    The sender is parsed deterministically from the header rather than inferred
    from the body: the model never sees it and can never get it wrong.
    """
    raw = msg.get("From", "")
    name, addr = parseaddr(raw)
    name = decode_mime(name)
    addr = addr.strip()
    # parseaddr yields a bare token as the "address" for a malformed From
    # (e.g. "From: Weird Name Only"). Only an @ makes it a real address.
    if "@" not in addr:
        addr = ""
    if name and addr:
        display = f"{name} <{addr}>"
    else:
        display = addr or decode_mime(raw) or "Unknown Sender"
    return display, addr


def extract_body(msg):
    """Return the plain-text body of an email.message.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload is not None:
                    return payload.decode("utf-8", errors="ignore")
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode("utf-8", errors="ignore") if payload is not None else ""


# A reply's body is the new text followed by the whole quoted thread below it.
# Without cutting that off, summarize() sees the entire conversation and
# summarises the thread instead of the one new message. Patterns cover the
# common mail clients/locales; a miss just leaves some quoted text in, a false
# positive would truncate real content, so these stay conservative.
_QUOTE_LINE_RE = re.compile(r"^\s*>")
_ON_WROTE_RE = re.compile(
    r"^\s*(On|Il|El|Le) .{0,120} (wrote|ha scritto|escribi[oó]|a écrit)\s*:?\s*$",
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(
    r"^\s*(-{2,}\s*Original Message\s*-{2,}|_{8,})\s*$", re.IGNORECASE
)
_HEADER_FROM_RE = re.compile(r"^\s*(From|Da|De)\s*:", re.IGNORECASE)
_HEADER_SENT_RE = re.compile(
    r"^\s*(Sent|Date|Inviato|Data|Enviado|Envoyé)\s*:", re.IGNORECASE
)


def strip_quoted_reply(body):
    """Cut ``body`` at the point a reply's quoted history begins."""
    if not body:
        return body
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if (
            _QUOTE_LINE_RE.match(line)
            or _ON_WROTE_RE.match(line)
            or _SEPARATOR_RE.match(line)
        ):
            return "\n".join(lines[:i]).rstrip()
        if _HEADER_FROM_RE.match(line) and i + 1 < len(lines) and _HEADER_SENT_RE.match(
            lines[i + 1]
        ):
            return "\n".join(lines[:i]).rstrip()
    return body.rstrip()
