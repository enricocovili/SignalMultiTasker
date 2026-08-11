import email
import imaplib
import json
import os
import socket
import sys
import time
from email.utils import parseaddr

import requests

# ── Configuration from Environment Variables ──────────────────────────────────
IMAP_SERVER = "imap.hostinger.com"
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

SIGNAL_API_BASE = os.getenv("SIGNAL_API_BASE", "http://signal-api:8080")
SIGNAL_API_URL = os.getenv("SIGNAL_API_URL", f"{SIGNAL_API_BASE}/v2/send")
SIGNAL_SENDER = os.getenv("SIGNAL_SENDER")
SIGNAL_GROUP_ID = os.getenv("SIGNAL_GROUP_ID")

# Device name shown in the linked-devices list of the main Signal account, and
# where the linking QR (a PNG) is written for the user to scan on first start.
SIGNAL_DEVICE_NAME = os.getenv("SIGNAL_DEVICE_NAME", "signal-multitasker")
SIGNAL_LINK_QR_PATH = os.getenv("SIGNAL_LINK_QR_PATH", "/app/state/signal-link-qr.png")
SIGNAL_AUTH_POLL_INTERVAL = int(os.getenv("SIGNAL_AUTH_POLL_INTERVAL", "5"))

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:e2b")

EMAIL_POLL_INTERVAL = int(os.getenv("EMAIL_POLL_INTERVAL", "180"))

# If a single poll finds more than this many new mails, skip processing them
# individually and just warn that a manual check is needed.
MAX_NEW_EMAILS = int(os.getenv("MAX_NEW_EMAILS", "10"))

# Local state file. Persisted on a volume so it survives container restarts.
STATE_FILE = os.getenv("STATE_FILE", "/app/state/email_state.json")

# Timeouts (these calls can be slow on CPU)
OLLAMA_TIMEOUT = 180
SIGNAL_TIMEOUT = 60

# Socket timeout for every IMAP operation (connect, login, search, fetch).
# Without this imaplib blocks forever on a half-open connection: the container
# stays "Up" but silently stops polling, and no restart policy can save it.
IMAP_TIMEOUT = int(os.getenv("IMAP_TIMEOUT", "60"))

# Transient network failures (DNS hiccups, dropped links) are normal: back off
# and retry. But if they never stop, exit non-zero so Docker restarts us with a
# clean process, fresh DNS state and new sockets, rather than looping forever.
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "10"))
FAILURE_BACKOFF_MAX = int(os.getenv("FAILURE_BACKOFF_MAX", "300"))

EMAIL_LLM_PROMPT = (
    "You are a summary bot. Summarize the following email in ONE short sentence "
    "suitable for a Signal message. Ignore headers and signatures.\n\n"
    "Email Content: {body}"
)


# ── Local state ───────────────────────────────────────────────────────────────
def load_state():
    """Return the persisted state dict, or an empty dict if none exists."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"⚠️ Could not read state file, treating as empty: {e}")
        return {}


def save_state(state):
    """Atomically persist the state dict to disk."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ── Ollama ────────────────────────────────────────────────────────────────────
def get_llm_summary(text, prompt_template=EMAIL_LLM_PROMPT):
    """Send text to Ollama and return a short summary, or None on failure."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "user", "content": prompt_template.format(body=text[:3000])},
        ],
        "stream": False,
        "options": {
            "temperature": 0.3,
            "num_predict": 512,
        },
    }
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()
        print(f"DEBUG: LLM inference time: {response.elapsed.total_seconds():.1f}s")
        print(f"DEBUG: {data}")
        return (data.get("message", {}).get("content") or "").strip()
    except requests.exceptions.Timeout:
        print("⚠️ Ollama timed out")
        return None
    except Exception as e:
        print(f"⚠️ Error generating summary: {e}")
        return None


# ── Signal auth ───────────────────────────────────────────────────────────────
def signal_registered_numbers():
    """Return the list of accounts registered/linked in signal-api."""
    r = requests.get(f"{SIGNAL_API_BASE}/v1/accounts", timeout=SIGNAL_TIMEOUT)
    r.raise_for_status()
    return r.json() or []


def fetch_link_qr():
    """Generate a device-link QR (PNG) and write it to the state volume.

    Each call creates a fresh linking URI, so call it once per link attempt.
    """
    r = requests.get(
        f"{SIGNAL_API_BASE}/v1/qrcodelink",
        params={"device_name": SIGNAL_DEVICE_NAME},
        timeout=SIGNAL_TIMEOUT,
    )
    r.raise_for_status()
    os.makedirs(os.path.dirname(SIGNAL_LINK_QR_PATH), exist_ok=True)
    with open(SIGNAL_LINK_QR_PATH, "wb") as f:
        f.write(r.content)
    return SIGNAL_LINK_QR_PATH


def ensure_signal_account():
    """Block until SIGNAL_SENDER is linked with signal-api.

    On first start the account is not yet linked: we emit a QR (saved on the
    state volume) for the user to scan from their main phone
    (Signal → Settings → Linked devices → Link new device), then poll until the
    link completes. If already linked, returns immediately.
    """
    # signal-api may not be reachable yet (compose only waits for start, not
    # readiness) — retry the first lookup until it answers.
    while True:
        try:
            numbers = signal_registered_numbers()
            break
        except Exception as e:
            print(f"⏳ Waiting for signal-api to come up: {e}")
            time.sleep(SIGNAL_AUTH_POLL_INTERVAL)

    if SIGNAL_SENDER in numbers:
        print(f"✅ Signal account {SIGNAL_SENDER} already linked.")
        return

    print(f"🔑 Signal account {SIGNAL_SENDER} is not linked yet.")
    try:
        path = fetch_link_qr()
        print(
            f"📲 Scan the QR at {path} from your phone "
            "(Signal → Settings → Linked devices → Link new device) to link this bridge."
        )
    except Exception as e:
        print(f"⚠️ Could not generate the link QR: {e}")

    while True:
        time.sleep(SIGNAL_AUTH_POLL_INTERVAL)
        try:
            if SIGNAL_SENDER in signal_registered_numbers():
                print(f"✅ Signal account {SIGNAL_SENDER} linked successfully.")
                return
        except Exception as e:
            print(f"⚠️ Re-checking signal-api: {e}")


# ── Signal helpers ────────────────────────────────────────────────────────────
def send_signal_message(message):
    payload = {
        "message": message,
        "number": SIGNAL_SENDER,
        "recipients": [SIGNAL_GROUP_ID],
        "text_mode": "styled",
    }
    try:
        r = requests.post(SIGNAL_API_URL, json=payload, timeout=SIGNAL_TIMEOUT)
        if r.status_code == 201:
            print("✅ Successfully forwarded to Signal.")
            return True
        print(f"❌ Failed to send: {r.text}")
        return False
    except Exception as e:
        print(f"⚠️ Signal send error: {e}")
        return False


# ── Forwarding rules ──────────────────────────────────────────────────────────
# A rule short-circuits normal forwarding: when an email matches, we send the
# rule's own message instead of the full "New Email" forward. This keeps noisy,
# predictable senders (security alerts, login links) to a one-line notice.
#
# Each rule is a dict:
#   {
#     "name": "<human label, for logs only>",
#     "match": { "<condition>": "<value>", ... },   # ALL conditions must hold (AND)
#     "message": "<Signal message to send when matched>",
#   }
#
# Supported match conditions (see MATCHERS). All are case-insensitive. To add a
# new condition, register a predicate in MATCHERS; to add a new rule, append to
# RULES. Rules are evaluated top-to-bottom; the first match wins.
#
#   sender_contains  — substring of the raw From header (name <addr>)
#   sender_equals    — exact match of the parsed email address only
#   subject_contains — substring of the Subject header
#   subject_equals   — exact match of the whole Subject
MATCHERS = {
    "sender_contains": lambda ctx, v: v.casefold() in ctx["sender"].casefold(),
    "sender_equals": lambda ctx, v: v.casefold() == ctx["sender_addr"].casefold(),
    "subject_contains": lambda ctx, v: v.casefold() in ctx["subject"].casefold(),
    "subject_equals": lambda ctx, v: v.casefold() == ctx["subject"].casefold(),
}

RULES = [
    {
        "name": "google-security-alert",
        "match": {
            "sender_equals": "no-reply@accounts.google.com",
            "subject_contains": "Avviso di sicurezza",
        },
        "message": (
            "🔐 **Google account** — a new security event was reported "
            "(*Avviso di sicurezza*). Check the account activity."
        ),
    },
    {
        "name": "anthropic-login-link",
        "match": {
            "sender_contains": "mail.anthropic.com",
            "subject_contains": "Your secure link to Claude.ai",
        },
        "message": "🔑 **Claude.ai** — a new login link was requested for your account.",
    },
    {
        "name": "cloudflare-login-token",
        "match": {
            "sender_equals": "noreply@notify.cloudflare.com",
            "subject_contains": "Your Cloudflare login token",
        },
        "message": "🔑 **Cloudflare** — a new login token was requested for your account.",
    },
]


def match_rule(sender, subject):
    """Return the first RULES entry matching this email, or None.

    All conditions in a rule's ``match`` dict must hold. Unknown conditions make
    a rule never match (fail closed) so a typo can't silently forward nothing.
    """
    ctx = {
        "sender": sender,
        "sender_addr": parseaddr(sender)[1],
        "subject": subject,
    }
    for rule in RULES:
        try:
            if all(
                cond in MATCHERS and MATCHERS[cond](ctx, value)
                for cond, value in rule["match"].items()
            ):
                return rule
        except Exception as e:
            print(f"⚠️ Error evaluating rule {rule.get('name')!r}: {e}")
    return None


# ── Email helpers ─────────────────────────────────────────────────────────────
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


def process_email(mail, uid):
    """Fetch one email by UID (without marking it seen) and forward to Signal."""
    # BODY.PEEK avoids setting the \Seen flag — we track state ourselves.
    _, data = mail.uid("fetch", uid, "(BODY.PEEK[])")
    if not data or not data[0]:
        print(f"⚠️ Could not fetch email UID {uid}")
        return

    msg = email.message_from_bytes(data[0][1])
    subject = msg.get("Subject", "No Subject")
    sender = msg.get("From", "Unknown Sender")
    print(f"📩 New email detected (UID {uid}): {subject}")

    # A matching rule replaces the full forward with its own short notice.
    rule = match_rule(sender, subject)
    if rule:
        print(f"➡️ Matched rule {rule['name']!r}; sending its notice.")
        send_signal_message(rule["message"])
        return

    body = extract_body(msg)
    summary = body if len(body) < 500 else body[:500] + "...\nMessaggio Troncato"

    message = f"📩 **New Email**\n\n**From:** {sender}\n\n**Subject:** {subject}"
    if summary:
        message += f"\n\n**Summary:** {summary}"

    send_signal_message(message)


# ── Email loop ────────────────────────────────────────────────────────────────
def close_imap(mail):
    """Best-effort logout; never let cleanup mask the original error."""
    try:
        mail.logout()
    except Exception:
        try:
            mail.shutdown()
        except Exception:
            pass


def listen_for_emails():
    print(f"🚀 Email listener started for {EMAIL_USER}...")

    consecutive_failures = 0

    while True:
        mail = None
        try:
            # timeout= applies to connect and to every later socket read, so a
            # stalled server or a network drop raises instead of hanging.
            mail = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=IMAP_TIMEOUT)
            mail.login(EMAIL_USER, EMAIL_PASS)
            status, select_data = mail.select("inbox")

            # UIDVALIDITY changes mean the server's UID space was reset; our stored
            # UIDs are then meaningless and must be treated as a fresh start.
            uidvalidity = None
            try:
                _, uv = mail.status("inbox", "(UIDVALIDITY)")
                uidvalidity = int(uv[0].split(b"UIDVALIDITY ")[1].strip(b") ").strip())
            except Exception as e:
                print(f"⚠️ Could not read UIDVALIDITY: {e}")

            # All UIDs currently in the inbox.
            _, search_data = mail.uid("search", None, "ALL")
            all_uids = (
                [int(x) for x in search_data[0].split()] if search_data[0] else []
            )
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
                save_state({"uidvalidity": uidvalidity, "last_uid": max_uid})
            else:
                new_uids = sorted(uid for uid in all_uids if uid > last_uid)

                if not new_uids:
                    print("There is not any new mail")
                elif len(new_uids) > MAX_NEW_EMAILS:
                    # Too many at once — likely a backlog or import; ask for manual
                    # review instead of spamming individual summaries.
                    send_signal_message(
                        f"⚠️ {len(new_uids)} new emails found (limit {MAX_NEW_EMAILS}). "
                        "Manual check needed — not forwarding them individually."
                    )
                    save_state({"uidvalidity": uidvalidity, "last_uid": max_uid})
                else:
                    for uid in new_uids:
                        try:
                            process_email(mail, str(uid).encode())
                        except Exception as e:
                            print(f"⚠️ Error processing email UID {uid}: {e}")
                    save_state({"uidvalidity": uidvalidity, "last_uid": max_uid})

            consecutive_failures = 0
        except (OSError, socket.timeout, imaplib.IMAP4.error) as e:
            # Transient by nature: DNS resolution failures, refused/reset
            # connections, TLS errors, read timeouts, server-side hiccups.
            consecutive_failures += 1
            print(
                f"⚠️ Poll failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): "
                f"{type(e).__name__}: {e}"
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Give up on this process: exiting non-zero lets Docker restart
                # us, which is the only way to recover from a wedged resolver or
                # a broken network namespace.
                print("💥 Too many consecutive failures — exiting for a restart.")
                sys.exit(1)  # `finally` below still closes the connection
        except Exception as e:
            consecutive_failures += 1
            print(f"⚠️ Error: {type(e).__name__}: {e}")
        finally:
            if mail is not None:
                close_imap(mail)

        if consecutive_failures:
            # Exponential backoff, capped, so a flapping network isn't hammered.
            delay = min(
                FAILURE_BACKOFF_MAX,
                EMAIL_POLL_INTERVAL * (2 ** (consecutive_failures - 1)),
            )
        else:
            delay = EMAIL_POLL_INTERVAL
        time.sleep(delay)


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ensure_signal_account()
    listen_for_emails()
