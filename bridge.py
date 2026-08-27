import base64
import email
import imaplib
import json
import os
import socket
import subprocess
import threading
import time
from email.header import decode_header, make_header
from email.utils import parseaddr
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

# Device name shown in the linked-devices list of the main Signal account, and
# where the linking QR (a PNG) is written for the user to scan on first start.
SIGNAL_DEVICE_NAME = os.getenv("SIGNAL_DEVICE_NAME", "signal-multitasker")
SIGNAL_LINK_QR_PATH = os.getenv("SIGNAL_LINK_QR_PATH", "/app/state/signal-link-qr.png")
SIGNAL_AUTH_POLL_INTERVAL = int(os.getenv("SIGNAL_AUTH_POLL_INTERVAL", "5"))

# How often the voice-note loop drains the Signal inbox.
SIGNAL_POLL_INTERVAL = int(os.getenv("SIGNAL_POLL_INTERVAL", "10"))

EMAIL_POLL_INTERVAL = int(os.getenv("EMAIL_POLL_INTERVAL", "180"))

# If a single poll finds more than this many new mails, skip processing them
# individually and just warn that a manual check is needed.
MAX_NEW_EMAILS = int(os.getenv("MAX_NEW_EMAILS", "10"))

# Local state file. Persisted on a volume so it survives container restarts.
STATE_FILE = os.getenv("STATE_FILE", "/app/state/email_state.json")

# Timeouts. In MODE=normal signal-api spawns a JVM per request, so even a
# healthy call costs seconds.
SIGNAL_TIMEOUT = int(os.getenv("SIGNAL_TIMEOUT", "60"))

# Receiving needs its own, far longer budget than sending: signal-cli's first
# receive after a start does an initial sync (observed: 2m), and a receive that
# collides with another signal-cli invocation blocks on the config-file lock
# (observed: 1m5s). Timing out does not cancel the server-side work — it just
# abandons a request that keeps running and holding the lock, so the next poll
# collides too. Too short a value here turns one slow call into a cascade.
SIGNAL_RECEIVE_TIMEOUT = int(os.getenv("SIGNAL_RECEIVE_TIMEOUT", "300"))
WHISPER_TIMEOUT = int(os.getenv("WHISPER_TIMEOUT", "600"))

# Socket timeout for every IMAP operation (connect, login, search, fetch).
# Without this imaplib blocks forever on a half-open connection: the container
# stays "Up" but silently stops polling, and no restart policy can save it.
IMAP_TIMEOUT = int(os.getenv("IMAP_TIMEOUT", "60"))

# Transient network failures (DNS hiccups, dropped links) are normal: back off
# and retry. But if they never stop, exit non-zero so Docker restarts us with a
# clean process, fresh DNS state and new sockets, rather than looping forever.
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "10"))
FAILURE_BACKOFF_MAX = int(os.getenv("FAILURE_BACKOFF_MAX", "300"))

# ── LLM configuration ─────────────────────────────────────────────────────────
# The summariser is any OpenAI-compatible chat-completions endpoint: a hosted
# provider, a gateway, or a local runtime that serves the /v1 surface. Nothing
# here is provider-specific — endpoint, key, model, decoding, input budget and
# the two prompts all come from the environment (wired in docker-compose.yaml).
#
# LLM_URL is the API *base* (the part ending in /v1); "/chat/completions" is
# appended to it.
LLM_URL = os.getenv("LLM_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "180"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
# Budget for the whole completion, not just the answer. Reasoning models spend
# it on reasoning_content first and only then emit content — at 160 a 12B
# reasoning model burns the entire budget thinking and returns an empty string.
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))

# Hard cap on how much text is handed to the model, so a 2 MB newsletter can't
# blow past the context window (or run up a bill on a per-token provider).
LLM_MAX_INPUT_CHARS = int(os.getenv("LLM_MAX_INPUT_CHARS", "4000"))

# Both prompts may use {subject} and {body}; unknown placeholders render empty.
EMAIL_SUMMARY_PROMPT = os.getenv(
    "EMAIL_SUMMARY_PROMPT",
    "You summarise emails for a notification bot.\n"
    "If the email is a login/sign-in verification code, a one-time "
    "passcode, a \"new sign-in\" alert, or a successful-login notification, "
    "output ONLY: Login attempt for <Service> — where <Service> is the "
    "product or company name (e.g. Cloudflare, Anthropic, Google). Do not "
    "include the code itself.\n"
    "Otherwise, in AT MOST 2 short sentences, state what the email is about "
    "and what it asks the reader to do (if anything).\n"
    "Rules: write in the same language as the email; do not mention or guess "
    "the sender; do not greet, introduce yourself or add any preamble; no "
    "bullet points, no markdown, no quotes. Output only the summary.\n\n"
    "Subject: {subject}\n\n"
    "Body:\n{body}",
)
VOICE_SUMMARY_PROMPT = os.getenv(
    "VOICE_SUMMARY_PROMPT",
    "You summarise voice messages for a notification bot.\n"
    "In AT MOST 2 short sentences, state what the speaker says and what they "
    "ask for (if anything).\n"
    "Rules: write in the same language as the transcript; do not greet, "
    "introduce yourself or add any preamble; no bullet points, no markdown, no "
    "quotes. Output only the summary.\n\n"
    "Transcript:\n{body}",
)

# ── Speech-to-text configuration ──────────────────────────────────────────────
# Likewise an OpenAI-compatible audio-transcriptions endpoint. WHISPER_URL is
# the API base; "/audio/transcriptions" is appended to it.
WHISPER_URL = os.getenv("WHISPER_URL", "").rstrip("/")
WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
# Empty means "let the provider auto-detect the spoken language".
WHISPER_LANG = os.getenv("WHISPER_LANG", "")

# Include the raw transcript alongside the summary in the Signal reply.
VOICE_INCLUDE_TRANSCRIPT = os.getenv("VOICE_INCLUDE_TRANSCRIPT", "false").lower() in (
    "1",
    "true",
    "yes",
)
VOICE_PENDING_MESSAGE = os.getenv(
    "VOICE_PENDING_MESSAGE", "🎧 Transcribing and summarizing voice message..."
)


def bearer(api_key):
    """Authorization header for a provider key, or {} when none is configured.

    The key stays optional so the bridge can also point at an unauthenticated
    endpoint on a trusted network.
    """
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def check_provider_config():
    """Warn loudly about unset provider config; never refuse to start.

    Forwarding mail is the job that must not stop: without an LLM the bridge
    still forwards a body excerpt, and voice notes are simply not handled.
    """
    for name, value, effect in (
        ("LLM_URL", LLM_URL, "emails forward as a plain excerpt"),
        ("LLM_MODEL", LLM_MODEL, "emails forward as a plain excerpt"),
        ("WHISPER_URL", WHISPER_URL, "voice messages cannot be transcribed"),
    ):
        if not value:
            print(f"⚠️ {name} is not set — {effect}.")
    if LLM_URL and not LLM_API_KEY:
        print("ℹ️ LLM_API_KEY is not set — calling the LLM without authentication.")
    if WHISPER_URL and not WHISPER_API_KEY:
        print("ℹ️ WHISPER_API_KEY is not set — calling Whisper without authentication.")


# signal-cli serialises access to the account store: a send issued while a
# receive is running fails with a lock error. One lock around every signal-api
# call keeps the email loop and the voice loop from stepping on each other.
SIGNAL_LOCK = threading.Lock()


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


# ── LLM ───────────────────────────────────────────────────────────────────────
class _SafeFields(dict):
    """Render unknown {placeholders} as empty instead of raising KeyError.

    The prompts are user-editable via the environment, so a stray placeholder
    must degrade the summary, not kill the poll loop.
    """

    def __missing__(self, key):
        return ""


def summarize(prompt_template, body, subject=""):
    """Return a short LLM summary of ``body``, or None if the LLM is unusable.

    Callers must handle None: the provider is a best-effort dependency, and a
    summary is never worth dropping a notification over.
    """
    if not (LLM_URL and LLM_MODEL):
        return None

    prompt = prompt_template.format_map(
        _SafeFields(body=(body or "")[:LLM_MAX_INPUT_CHARS], subject=subject or "")
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "stream": False,
    }
    try:
        response = requests.post(
            f"{LLM_URL}/chat/completions",
            json=payload,
            headers=bearer(LLM_API_KEY),
            timeout=LLM_TIMEOUT,
        )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        choice = choices[0] if choices else {}
        summary = (choice.get("message", {}).get("content") or "").strip()
        took = response.elapsed.total_seconds()
        if not summary and choice.get("finish_reason") == "length":
            # Classic reasoning-model symptom: the token budget was consumed
            # before any answer was emitted. Say so, rather than reporting an
            # anonymous empty summary.
            print(
                f"⚠️ LLM returned no content in {took:.1f}s (finish_reason=length) — "
                f"LLM_MAX_TOKENS={LLM_MAX_TOKENS} is too low for this model."
            )
            return None
        print(f"🧠 LLM summary in {took:.1f}s: {summary!r}")
        return summary or None
    except requests.exceptions.Timeout:
        print(f"⚠️ LLM timed out after {LLM_TIMEOUT}s")
        return None
    except Exception as e:
        print(f"⚠️ LLM error: {type(e).__name__}: {e}")
        return None


# ── Signal auth ───────────────────────────────────────────────────────────────
def signal_registered_numbers():
    """Return the list of accounts registered/linked in signal-api."""
    with SIGNAL_LOCK:
        r = requests.get(f"{SIGNAL_API_BASE}/v1/accounts", timeout=SIGNAL_TIMEOUT)
    r.raise_for_status()
    return r.json() or []


def fetch_link_qr():
    """Generate a device-link QR (PNG) and write it to the state volume.

    Each call creates a fresh linking URI, so call it once per link attempt.
    """
    with SIGNAL_LOCK:
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
def send_signal_message(message, recipient=None):
    """Send a styled message. Returns the send timestamp, or None on failure.

    The timestamp is what identifies the message later — it is the handle needed
    to remote-delete the "transcribing…" placeholder.
    """
    payload = {
        "message": message,
        "number": SIGNAL_SENDER,
        "recipients": [recipient or SIGNAL_GROUP_ID],
        "text_mode": "styled",
    }
    try:
        with SIGNAL_LOCK:
            r = requests.post(SIGNAL_API_URL, json=payload, timeout=SIGNAL_TIMEOUT)
        if r.status_code == 201:
            print("✅ Successfully forwarded to Signal.")
            # 201 bodies carry {"timestamp": "<millis as string>"}.
            try:
                return int(r.json().get("timestamp"))
            except Exception:
                return None
        print(f"❌ Failed to send: {r.text}")
        return None
    except Exception as e:
        print(f"⚠️ Signal send error: {e}")
        return None


def delete_signal_message(timestamp, recipient=None):
    """Remote-delete one of our own sent messages (removes it for everyone)."""
    if not timestamp:
        return False
    url = f"{SIGNAL_API_BASE}/v1/remote-delete/{quote(SIGNAL_SENDER, safe='')}"
    payload = {"recipient": recipient or SIGNAL_GROUP_ID, "timestamp": int(timestamp)}
    try:
        with SIGNAL_LOCK:
            r = requests.delete(url, json=payload, timeout=SIGNAL_TIMEOUT)
        if r.status_code in (200, 201, 204):
            print(f"🗑️ Deleted placeholder message {timestamp}.")
            return True
        print(f"⚠️ Could not delete message {timestamp}: {r.status_code} {r.text}")
        return False
    except Exception as e:
        print(f"⚠️ Signal delete error: {e}")
        return False


def fetch_signal_envelopes():
    """GET /v1/receive/{number} — returns a list of envelopes, consuming them."""
    url = f"{SIGNAL_API_BASE}/v1/receive/{quote(SIGNAL_SENDER, safe='')}"
    params = {"timeout": "1", "ignore_attachments": "false", "ignore_stories": "true"}
    started = time.monotonic()
    with SIGNAL_LOCK:
        r = requests.get(url, params=params, timeout=SIGNAL_RECEIVE_TIMEOUT)
    elapsed = time.monotonic() - started
    if elapsed > 30:
        # Normal is ~5s. Much longer means an initial sync or lock contention;
        # surface it, because it also stalls outbound sends behind SIGNAL_LOCK.
        print(f"🐌 Signal receive took {elapsed:.0f}s (expected ~5s).")
    if r.status_code == 400:
        # signal-cli is busy or returned a transient 400; log body and skip this tick
        print(f"⚠️ Signal /receive 400: {r.text.strip()[:200]}")
        return []
    r.raise_for_status()
    return r.json() or []


def download_attachment(att_id):
    with SIGNAL_LOCK:
        r = requests.get(
            f"{SIGNAL_API_BASE}/v1/attachments/{att_id}", timeout=SIGNAL_TIMEOUT
        )
    r.raise_for_status()
    return r.content


def delete_attachment(att_id):
    """Drop the downloaded audio from the signal-api container's disk."""
    try:
        with SIGNAL_LOCK:
            requests.delete(
                f"{SIGNAL_API_BASE}/v1/attachments/{att_id}", timeout=SIGNAL_TIMEOUT
            )
    except Exception as e:
        print(f"⚠️ Could not delete attachment {att_id}: {e}")


# ── Speech-to-text ────────────────────────────────────────────────────────────
def transcode_to_wav(audio_bytes):
    """Convert arbitrary audio to 16kHz mono WAV via ffmpeg.

    Signal voice notes arrive as AAC, which this project's Whisper-compatible
    endpoint rejects outright (400) even though it accepts WAV/MP3/FLAC fine.
    Normalising to WAV before upload sidesteps that instead of relying on any
    given provider to support Signal's codec.
    """
    proc = subprocess.run(
        [
            "ffmpeg",
            "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "wav",
            "-ar", "16000",
            "-ac", "1",
            "pipe:1",
        ],
        input=audio_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=WHISPER_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg exited {proc.returncode}: {proc.stderr.decode(errors='replace')[:200]}"
        )
    return proc.stdout


def transcribe_audio(audio_bytes):
    """Upload audio to the transcription provider and return the text, or None."""
    if not WHISPER_URL:
        return None

    try:
        audio_bytes = transcode_to_wav(audio_bytes)
    except Exception as e:
        print(f"⚠️ Audio transcode error: {type(e).__name__}: {e}")
        return None

    data = {"model": WHISPER_MODEL, "response_format": "json"}
    if WHISPER_LANG:
        data["language"] = WHISPER_LANG
    files = {"file": ("voice.wav", audio_bytes)}
    try:
        r = requests.post(
            f"{WHISPER_URL}/audio/transcriptions",
            data=data,
            files=files,
            headers=bearer(WHISPER_API_KEY),
            timeout=WHISPER_TIMEOUT,
        )
        r.raise_for_status()
        return (r.json().get("text") or "").strip()
    except requests.exceptions.Timeout:
        print(f"⚠️ Transcription timed out after {WHISPER_TIMEOUT}s")
        return None
    except Exception as e:
        print(f"⚠️ Transcription error: {type(e).__name__}: {e}")
        return None


# ── Forwarding rules ──────────────────────────────────────────────────────────
# A rule short-circuits normal forwarding: when an email matches, we send the
# rule's own message instead of the full "New Email" forward. This keeps noisy,
# predictable senders (security alerts, login links) to a one-line notice and
# skips the LLM entirely.
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
#   sender_contains  — substring of the decoded From header (name <addr>)
#   sender_equals    — exact match of the parsed email address only
#   subject_contains — substring of the decoded Subject header
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


def match_rule(sender_addr, sender, subject):
    """Return the first RULES entry matching this email, or None.

    All conditions in a rule's ``match`` dict must hold. Unknown conditions make
    a rule never match (fail closed) so a typo can't silently forward nothing.
    """
    ctx = {"sender": sender, "sender_addr": sender_addr, "subject": subject}
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


def process_email(mail, uid):
    """Fetch one email by UID (without marking it seen) and forward to Signal."""
    # BODY.PEEK avoids setting the \Seen flag — we track state ourselves.
    _, data = mail.uid("fetch", uid, "(BODY.PEEK[])")
    if not data or not data[0]:
        print(f"⚠️ Could not fetch email UID {uid}")
        return

    msg = email.message_from_bytes(data[0][1])
    subject = decode_mime(msg.get("Subject", "")) or "No Subject"
    sender, sender_addr = extract_sender(msg)
    print(f"📩 New email detected (UID {uid}): {subject}")

    # A matching rule replaces the full forward with its own short notice.
    rule = match_rule(sender_addr, sender, subject)
    if rule:
        print(f"➡️ Matched rule {rule['name']!r}; sending its notice.")
        send_signal_message(rule["message"])
        return

    body = extract_body(msg)
    # The LLM only ever produces the "what does this mail want" line. If it is
    # down, fall back to a truncated body so the mail is still forwarded.
    summary = summarize(EMAIL_SUMMARY_PROMPT, body, subject=subject)

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

    message = f"📩 **New Email**\n\n**From:** {sender}\n\n**Subject:** {subject}"
    if summary:
        message += f"\n\n**{label}:** {summary}"

    send_signal_message(message)


# ── Voice-note loop ───────────────────────────────────────────────────────────
def conversation_recipient(envelope, data):
    """Where to reply: the group the note came from, else the 1:1 conversation.

    For a sync message (the owner's own note, echoed to this linked device) the
    envelope source is *us*, so the conversation is the message's ``destination``.
    """
    group_id = (data.get("groupInfo") or {}).get("groupId")
    if group_id:
        # /v1/receive returns the raw group ID, but /v2/send (like /v1/groups)
        # expects it base64-wrapped again and prefixed with "group.".
        return f"group.{base64.b64encode(group_id.encode()).decode()}"
    return (
        data.get("destinationNumber")
        or data.get("destination")
        or envelope.get("sourceNumber")
        or envelope.get("source")
        or SIGNAL_GROUP_ID
    )


def process_voice_attachment(att, sender, recipient):
    """Acknowledge, transcribe, summarise, answer — then drop the ack."""
    att_id = att.get("id")
    if not att_id:
        return
    print(f"🎤 Voice note from {sender} ({att.get('contentType')}, id={att_id})")

    # Tell the chat we're on it: Whisper + the LLM take tens of seconds on CPU
    # and silence looks like a broken bridge.
    pending_ts = send_signal_message(VOICE_PENDING_MESSAGE, recipient=recipient)

    try:
        audio = download_attachment(att_id)
        transcript = transcribe_audio(audio)
        if not transcript:
            send_signal_message(
                "⚠️ Could not transcribe the voice message.", recipient=recipient
            )
            return

        print(f"📝 Transcript ({len(transcript)} chars): {transcript[:120]}...")
        summary = summarize(VOICE_SUMMARY_PROMPT, transcript)

        msg = f"🎤 **Voice message** from {sender}"
        if summary:
            msg += f"\n\n**Summary:** {summary}"
            if VOICE_INCLUDE_TRANSCRIPT:
                msg += f"\n\n**Transcript:** {transcript}"
        else:
            # No summary available — the transcript is better than nothing.
            msg += f"\n\n**Transcript:** {transcript}"
        send_signal_message(msg, recipient=recipient)
    finally:
        # Always retract the placeholder, including on a failure path, so the
        # chat is never left with a "transcribing…" that never resolves.
        delete_signal_message(pending_ts, recipient=recipient)
        delete_attachment(att_id)


def iter_voice_messages(envelopes):
    """Yield ``(envelope, data_message)`` pairs that carry audio attachments.

    Voice notes reach a *linked* device two ways: from other people as a
    ``dataMessage``, and from the user's own phone as ``syncMessage.sentMessage``.
    Both are handled, or the bridge would ignore the owner's own voice notes.
    """
    for env in envelopes:
        envelope = env.get("envelope", {}) if isinstance(env, dict) else {}
        candidates = [envelope.get("dataMessage")]
        sync = envelope.get("syncMessage") or {}
        candidates.append(sync.get("sentMessage"))
        for data in candidates:
            if not data:
                continue
            if any(
                (att.get("contentType") or "").startswith("audio/")
                for att in (data.get("attachments") or [])
            ):
                yield envelope, data


def listen_for_voice_notes():
    print(f"🎙️  Voice-note listener started for {SIGNAL_SENDER}...")
    while True:
        try:
            for envelope, data in iter_voice_messages(fetch_signal_envelopes()):
                sender = (
                    envelope.get("sourceName") or envelope.get("source") or "unknown"
                )
                recipient = conversation_recipient(envelope, data)
                for att in data.get("attachments") or []:
                    if not (att.get("contentType") or "").startswith("audio/"):
                        continue
                    try:
                        process_voice_attachment(att, sender, recipient)
                    except Exception as e:
                        print(f"⚠️ Error processing voice attachment: {e}")
        except requests.exceptions.RequestException as e:
            print(f"⚠️ Signal receive error: {e}")
        except Exception as e:
            print(f"⚠️ Voice loop error: {type(e).__name__}: {e}")
        time.sleep(SIGNAL_POLL_INTERVAL)


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
        fatal = False
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
                # a broken network namespace. Flagged rather than exited here so
                # the `finally` below still closes the IMAP connection.
                print("💥 Too many consecutive failures — exiting for a restart.")
                fatal = True
        except Exception as e:
            consecutive_failures += 1
            print(f"⚠️ Error: {type(e).__name__}: {e}")
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
                FAILURE_BACKOFF_MAX,
                EMAIL_POLL_INTERVAL * (2 ** (consecutive_failures - 1)),
            )
        else:
            delay = EMAIL_POLL_INTERVAL
        time.sleep(delay)


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    check_provider_config()
    ensure_signal_account()
    threading.Thread(
        target=listen_for_voice_notes, name="voice-loop", daemon=True
    ).start()
    listen_for_emails()
