"""Environment-driven configuration.

Every tunable is read once, at import time. Defaults here are only fallbacks;
the real values are wired in docker-compose.yaml / .env.
"""

import os


def env_bool(name, default="false"):
    return os.getenv(name, default).lower() in ("1", "true", "yes")


# ── Configuration from Environment Variables ──────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

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
    "The length of the summary should be linear in size with the size of the trascript, "
    "with about a 1/5 ratio (5 phrases in transcribe = 1 in summary). \n"
    "State what the speaker says and what they ask for (if anything).\n"
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
VOICE_INCLUDE_TRANSCRIPT = env_bool("VOICE_INCLUDE_TRANSCRIPT")

# Conversations the voice-note pipeline may act on, comma-separated: group IDs in
# /v2/send form ("group.…", same as SIGNAL_GROUP_ID) and/or phone numbers for 1:1
# chats. Voice notes anywhere else are ignored. Empty = only SIGNAL_GROUP_ID, so
# the pipeline is never global.
VOICE_ALLOWED_CHATS = {
    c.strip()
    for c in (os.getenv("VOICE_ALLOWED_CHATS") or SIGNAL_GROUP_ID or "").split(",")
    if c.strip()
}
