import email
import imaplib
import os
import threading
import time
from urllib.parse import quote

import requests

# ── Configuration from Environment Variables ──────────────────────────────────
IMAP_SERVER = "imap.hostinger.com"
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

SIGNAL_API_BASE = os.getenv("SIGNAL_API_BASE", "http://signal-api:8080")
SIGNAL_API_URL = os.getenv("SIGNAL_API_URL", f"{SIGNAL_API_BASE}/v2/send")
SIGNAL_SENDER = os.getenv("SIGNAL_SENDER")
SIGNAL_GROUP_ID = os.getenv("SIGNAL_GROUP_ID")

WHISPER_URL = os.getenv("WHISPER_URL", "http://whisper:9000")
WHISPER_LANG = os.getenv("WHISPER_LANG", "it")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:e2b")

EMAIL_POLL_INTERVAL = int(os.getenv("EMAIL_POLL_INTERVAL", "180"))
SIGNAL_POLL_INTERVAL = int(os.getenv("SIGNAL_POLL_INTERVAL", "10"))

# Timeouts (these calls can be slow on CPU)
WHISPER_TIMEOUT = 600
OLLAMA_TIMEOUT = 180
SIGNAL_TIMEOUT = 60

EMAIL_LLM_PROMPT = (
    "You are a summary bot. Summarize the following email in ONE short sentence "
    "suitable for a Signal message. Ignore headers and signatures.\n\n"
    "Email Content: {body}"
)
VOICE_LLM_PROMPT = (
    "You are a summary bot. Summarize the following voice-message transcript "
    "concisely in 1-2 sentences. Preserve the original language.\n\n"
    "Transcript: {body}"
)


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


def fetch_signal_envelopes():
    """GET /v1/receive/{number} — returns a list of envelopes, consuming them."""
    url = f"{SIGNAL_API_BASE}/v1/receive/{quote(SIGNAL_SENDER, safe='')}"
    params = {"timeout": "1", "ignore_attachments": "false", "ignore_stories": "true"}
    r = requests.get(url, params=params, timeout=SIGNAL_TIMEOUT)
    if r.status_code == 400:
        # signal-cli is busy or returned a transient 400; log body and skip this tick
        print(f"⚠️ Signal /receive 400: {r.text.strip()[:200]}")
        return []
    r.raise_for_status()
    return r.json() or []


def download_attachment(att_id):
    url = f"{SIGNAL_API_BASE}/v1/attachments/{att_id}"
    r = requests.get(url, timeout=SIGNAL_TIMEOUT)
    r.raise_for_status()
    return r.content


# ── Whisper ───────────────────────────────────────────────────────────────────
def transcribe_audio(audio_bytes, filename="voice.ogg"):
    """Upload audio bytes to the Whisper ASR webservice and return the text."""
    url = f"{WHISPER_URL}/asr"
    params = {
        "task": "transcribe",
        "language": WHISPER_LANG,
        "output": "json",
        "encode": "true",
    }
    files = {"audio_file": (filename, audio_bytes)}
    try:
        r = requests.post(url, params=params, files=files, timeout=WHISPER_TIMEOUT)
        r.raise_for_status()
        return (r.json().get("text") or "").strip()
    except requests.exceptions.Timeout:
        print("⚠️ Whisper timed out")
        return None
    except Exception as e:
        print(f"⚠️ Whisper error: {e}")
        return None


# ── Voice note loop ───────────────────────────────────────────────────────────
def process_voice_attachment(att, sender):
    att_id = att.get("id")
    if not att_id:
        return
    filename = att.get("filename") or f"{att_id}.ogg"
    print(f"🎤 Voice note from {sender} ({att.get('contentType')}, id={att_id})")

    audio = download_attachment(att_id)
    transcript = transcribe_audio(audio, filename=filename)
    if not transcript:
        send_signal_message("⚠️ Could not transcribe the voice message.")
        return

    print(f"📝 Transcript ({len(transcript)} chars): {transcript[:120]}...")
    summary = get_llm_summary(transcript, prompt_template=VOICE_LLM_PROMPT)

    msg = f"🎤 **Voice note from** {sender}\n\n**Transcript:** {transcript}"
    if summary:
        msg += f"\n\n**Summary:** {summary}"
    send_signal_message(msg)


def listen_for_voice_notes():
    print(f"🎙️  Voice-note listener started for {SIGNAL_SENDER}...")
    while True:
        try:
            envelopes = fetch_signal_envelopes()
            for env in envelopes:
                envelope = env.get("envelope", {}) if isinstance(env, dict) else {}
                data = envelope.get("dataMessage") or {}
                attachments = data.get("attachments") or []
                if not attachments:
                    continue
                sender = (
                    envelope.get("sourceName") or envelope.get("source") or "unknown"
                )
                for att in attachments:
                    if (att.get("contentType") or "").startswith("audio/"):
                        try:
                            process_voice_attachment(att, sender)
                        except Exception as e:
                            print(f"⚠️ Error processing voice attachment: {e}")
        except requests.exceptions.RequestException as e:
            print(f"⚠️ Signal receive error: {e}")
        except Exception as e:
            print(f"⚠️ Voice loop error: {e}")
        time.sleep(SIGNAL_POLL_INTERVAL)


# ── Email loop (existing) ─────────────────────────────────────────────────────
def listen_for_emails():
    print(f"🚀 Email listener started for {EMAIL_USER}...")

    while True:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_SERVER)

            mail.login(EMAIL_USER, EMAIL_PASS)
            mail.select("inbox")

            # Search for unread emails
            status, messages = mail.search(None, "UNSEEN")

            for num in messages[0].split():
                _, data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])

                subject = msg.get("Subject", "No Subject")
                sender = msg.get("From", "Unknown Sender")

                print(f"📩 New email detected: {subject}")

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(
                                "utf-8", errors="ignore"
                            )
                            break
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                # summary = get_llm_summary(body) if len(body) > 100 else body
                summary = (
                    body if len(body) < 500 else body[:500] + "...\nMessaggio Troncato"
                )

                message = (
                    f"📩 **New Email**\n\n**From:** {sender}\n\n**Subject:** {subject}"
                )
                if summary is not None:
                    message = message + f"\n\n**Summary:** {summary}"

                send_signal_message(message)
            else:
                print("There is not any new mail")

            mail.logout()
        except Exception as e:
            print(f"⚠️ Error: {e}")

        time.sleep(EMAIL_POLL_INTERVAL)


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threads = [
        threading.Thread(target=listen_for_emails, name="email-loop", daemon=True),
        threading.Thread(target=listen_for_voice_notes, name="voice-loop", daemon=True),
    ]
    for t in threads:
        t.start()
    # Keep main thread alive
    while True:
        time.sleep(3600)
