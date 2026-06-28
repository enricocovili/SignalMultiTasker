import email
import imaplib
import json
import os
import time

import requests

# ── Configuration from Environment Variables ──────────────────────────────────
IMAP_SERVER = "imap.hostinger.com"
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

SIGNAL_API_BASE = os.getenv("SIGNAL_API_BASE", "http://signal-api:8080")
SIGNAL_API_URL = os.getenv("SIGNAL_API_URL", f"{SIGNAL_API_BASE}/v2/send")
SIGNAL_SENDER = os.getenv("SIGNAL_SENDER")
SIGNAL_GROUP_ID = os.getenv("SIGNAL_GROUP_ID")

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

    body = extract_body(msg)
    summary = body if len(body) < 500 else body[:500] + "...\nMessaggio Troncato"

    message = f"📩 **New Email**\n\n**From:** {sender}\n\n**Subject:** {subject}"
    if summary:
        message += f"\n\n**Summary:** {summary}"

    send_signal_message(message)


# ── Email loop ────────────────────────────────────────────────────────────────
def listen_for_emails():
    print(f"🚀 Email listener started for {EMAIL_USER}...")

    while True:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_SERVER)
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
                save_state({"uidvalidity": uidvalidity, "last_uid": max_uid})
                mail.logout()
                time.sleep(EMAIL_POLL_INTERVAL)
                continue

            new_uids = sorted(uid for uid in all_uids if uid > last_uid)

            if not new_uids:
                print("There is not any new mail")
            elif len(new_uids) > MAX_NEW_EMAILS:
                # Too many at once — likely a backlog or import; ask for manual review
                # instead of spamming individual summaries.
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

            mail.logout()
        except Exception as e:
            print(f"⚠️ Error: {e}")

        time.sleep(EMAIL_POLL_INTERVAL)


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    listen_for_emails()
