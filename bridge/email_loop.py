"""Email poll loop: IMAP fetch, own UID tracking, forwarding to Signal.

Tracking deliberately ignores the IMAP ``\\Seen`` flag (emails are fetched with
``BODY.PEEK``); see "Email tracking" in CLAUDE.md before changing it.
"""

import email
import imaplib
import logging
import os
import socket
import time

from . import config
from .llm import summarize
from .mailparse import decode_mime, extract_body, extract_sender, strip_quoted_reply
from .rules import match_rule
from .signal_api import notify_error, send_signal_message
from .state import load_state, save_state

logger = logging.getLogger(__name__)


def format_email_message(sender, subject, label=None, text=None):
    message = f"📩 **New Email**\n\n**From:** {sender}\n\n**Subject:** {subject}"
    if text:
        message += f"\n\n**{label}:** {text}"
    return message


def process_email(mail, uid):
    """Fetch one email by UID (without marking it seen) and forward to Signal."""
    # BODY.PEEK avoids setting the \Seen flag — we track state ourselves.
    _, data = mail.uid("fetch", uid, "(BODY.PEEK[])")
    if not data or not data[0]:
        logger.warning("Could not fetch email UID %s", uid)
        return

    msg = email.message_from_bytes(data[0][1])
    subject = decode_mime(msg.get("Subject", "")) or "No Subject"
    sender, sender_addr = extract_sender(msg)
    logger.info("New email detected (UID %s): %s", uid, subject)

    # A matching rule replaces the full forward with its own short notice.
    rule = match_rule(sender_addr, sender, subject)
    if rule:
        logger.info("Matched rule %r; sending its notice", rule["name"])
        send_signal_message(rule["message"])
        return

    # Strip quoted history first so a one-line reply on a long thread doesn't
    # get summarised as "the whole conversation" — only the new text remains.
    body = strip_quoted_reply(extract_body(msg))
    # The LLM only ever produces the "what does this mail want" line. If it's
    # simply unconfigured, fall back to a truncated body so the mail is still
    # forwarded; if the provider itself is broken, say so instead — a
    # truncated excerpt would silently hide a wider outage.
    summary, llm_error = summarize(config.EMAIL_SUMMARY_PROMPT, body, subject=subject)

    if llm_error:
        message = format_email_message(sender, subject)
        send_signal_message(f"{message}\n\nError with LLM endpoint {llm_error}")
        return

    # Login codes/OTPs/sign-in alerts get a bare one-liner instead of the full
    # sender/subject template — no code, no formatting.
    if summary and summary.strip().startswith("Login attempt for"):
        send_signal_message(summary.strip())
        return

    if summary:
        label = "Summary"
    else:
        label = "Excerpt"
        summary = body.strip()
        if len(summary) > 500:
            summary = summary[:500] + "...\nMessaggio Troncato"

    send_signal_message(format_email_message(sender, subject, label, summary))


def close_imap(mail):
    """Best-effort logout; never let cleanup mask the original error."""
    try:
        mail.logout()
    except Exception:
        try:
            mail.shutdown()
        except Exception:
            pass


def read_uidvalidity(mail):
    """Return the mailbox UIDVALIDITY, or None if the server won't say."""
    try:
        _, uv = mail.status("inbox", "(UIDVALIDITY)")
        return int(uv[0].split(b"UIDVALIDITY ")[1].strip(b") ").strip())
    except Exception as e:
        logger.warning("Could not read UIDVALIDITY: %s", e)
        return None


def poll_once(mail):
    """One poll on an open connection: baseline, warn, or forward new mail."""
    mail.login(config.EMAIL_USER, config.EMAIL_PASS)
    mail.select("inbox")

    # UIDVALIDITY changes mean the server's UID space was reset; our stored
    # UIDs are then meaningless and must be treated as a fresh start.
    uidvalidity = read_uidvalidity(mail)

    # All UIDs currently in the inbox.
    _, search_data = mail.uid("search", None, "ALL")
    all_uids = [int(x) for x in search_data[0].split()] if search_data[0] else []
    max_uid = max(all_uids) if all_uids else 0

    state = load_state()
    last_uid = state.get("last_uid")
    stored_validity = state.get("uidvalidity")

    empty_state = not state or last_uid is None
    validity_reset = (
        stored_validity is not None
        and uidvalidity is not None
        and stored_validity != uidvalidity
    )

    if empty_state or validity_reset:
        # First run, manual reset, or server UID space changed: don't flood
        # the channel with every existing email — just baseline the state.
        send_signal_message(
            "ℹ️ Email state is empty — this looks like a first start or a "
            "manual reset. Establishing baseline; existing emails will not "
            "be forwarded."
        )
    else:
        new_uids = sorted(uid for uid in all_uids if uid > last_uid)

        if not new_uids:
            logger.info("No new mail")
            return
        if len(new_uids) > config.MAX_NEW_EMAILS:
            # Too many at once — likely a backlog or import; ask for manual
            # review instead of spamming individual summaries.
            send_signal_message(
                f"⚠️ {len(new_uids)} new emails found (limit {config.MAX_NEW_EMAILS}). "
                "Manual check needed — not forwarding them individually."
            )
        else:
            for uid in new_uids:
                try:
                    process_email(mail, str(uid).encode())
                except Exception as e:
                    notify_error(f"Error processing email UID {uid}: {e}")
    save_state({"uidvalidity": uidvalidity, "last_uid": max_uid})


def listen_for_emails():
    logger.info("Email listener started for %s", config.EMAIL_USER)

    consecutive_failures = 0

    while True:
        mail = None
        fatal = False
        try:
            # timeout= applies to connect and to every later socket read, so a
            # stalled server or a network drop raises instead of hanging.
            mail = imaplib.IMAP4_SSL(config.IMAP_SERVER, timeout=config.IMAP_TIMEOUT)
            poll_once(mail)
            consecutive_failures = 0
        except (OSError, socket.timeout, imaplib.IMAP4.error) as e:
            # Transient by nature: DNS resolution failures, refused/reset
            # connections, TLS errors, read timeouts, server-side hiccups.
            consecutive_failures += 1
            notify_error(
                f"Poll failed ({consecutive_failures}/{config.MAX_CONSECUTIVE_FAILURES}): "
                f"{type(e).__name__}: {e}"
            )
            if consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES:
                # Give up on this process: exiting non-zero lets Docker restart
                # us, which is the only way to recover from a wedged resolver or
                # a broken network namespace. Flagged rather than exited here so
                # the `finally` below still closes the IMAP connection.
                notify_error("Too many consecutive failures — exiting for a restart.")
                fatal = True
        except Exception as e:
            consecutive_failures += 1
            notify_error(f"Poll error: {type(e).__name__}: {e}")
        finally:
            if mail is not None:
                close_imap(mail)

        if fatal:
            # os._exit, not sys.exit: SystemExit only unwinds the calling thread,
            # and the daemon voice thread must not be able to keep us alive.
            os._exit(1)

        if consecutive_failures:
            # Exponential backoff, capped, so a flapping network isn't hammered.
            delay = min(
                config.FAILURE_BACKOFF_MAX,
                config.EMAIL_POLL_INTERVAL * (2 ** (consecutive_failures - 1)),
            )
        else:
            delay = config.EMAIL_POLL_INTERVAL
        time.sleep(delay)
