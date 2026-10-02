"""Everything that talks to signal-api: account linking, send, receive, attachments."""

import logging
import os
import threading
import time
from urllib.parse import quote

import requests

from . import config

logger = logging.getLogger(__name__)

# signal-cli serialises access to the account store: a send issued while a
# receive is running fails with a lock error. One lock around every signal-api
# call keeps the email loop and the voice loop from stepping on each other.
SIGNAL_LOCK = threading.Lock()


# ── Account linking ───────────────────────────────────────────────────────────
def signal_registered_numbers():
    """Return the list of accounts registered/linked in signal-api."""
    with SIGNAL_LOCK:
        r = requests.get(
            f"{config.SIGNAL_API_BASE}/v1/accounts", timeout=config.SIGNAL_TIMEOUT
        )
    r.raise_for_status()
    return r.json() or []


def fetch_link_qr():
    """Generate a device-link QR (PNG) and write it to the state volume.

    Each call creates a fresh linking URI, so call it once per link attempt.
    """
    with SIGNAL_LOCK:
        r = requests.get(
            f"{config.SIGNAL_API_BASE}/v1/qrcodelink",
            params={"device_name": config.SIGNAL_DEVICE_NAME},
            timeout=config.SIGNAL_TIMEOUT,
        )
    r.raise_for_status()
    os.makedirs(os.path.dirname(config.SIGNAL_LINK_QR_PATH), exist_ok=True)
    with open(config.SIGNAL_LINK_QR_PATH, "wb") as f:
        f.write(r.content)
    return config.SIGNAL_LINK_QR_PATH


def ensure_signal_account():
    """Block until SIGNAL_SENDER is linked with signal-api.

    On first start the account is not yet linked: we emit a QR (saved on the
    state volume) for the user to scan from their main phone
    (Signal → Settings → Linked devices → Link new device), then poll until the
    link completes. If already linked, returns immediately.
    """
    sender = config.SIGNAL_SENDER
    # signal-api may not be reachable yet (compose only waits for start, not
    # readiness) — retry the first lookup until it answers.
    while True:
        try:
            numbers = signal_registered_numbers()
            break
        except Exception as e:
            logger.info("Waiting for signal-api to come up: %s", e)
            time.sleep(config.SIGNAL_AUTH_POLL_INTERVAL)

    if sender in numbers:
        logger.info("Signal account %s already linked", sender)
        return

    logger.warning("Signal account %s is not linked yet", sender)
    try:
        path = fetch_link_qr()
        logger.warning(
            "Scan the QR at %s from your phone "
            "(Signal → Settings → Linked devices → Link new device) to link this bridge",
            path,
        )
    except Exception as e:
        logger.error("Could not generate the link QR: %s", e)

    while True:
        time.sleep(config.SIGNAL_AUTH_POLL_INTERVAL)
        try:
            if sender in signal_registered_numbers():
                logger.info("Signal account %s linked successfully", sender)
                return
        except Exception as e:
            logger.warning("Re-checking signal-api: %s", e)


# ── Send / notify ─────────────────────────────────────────────────────────────
def send_signal_message(message, recipient=None):
    """Send a styled message. Returns the send timestamp, or None on failure."""
    payload = {
        "message": message,
        "number": config.SIGNAL_SENDER,
        "recipients": [recipient or config.SIGNAL_GROUP_ID],
        "text_mode": "styled",
    }
    try:
        with SIGNAL_LOCK:
            r = requests.post(
                config.SIGNAL_API_URL, json=payload, timeout=config.SIGNAL_TIMEOUT
            )
        if r.status_code == 201:
            logger.info("Message forwarded to Signal")
            # 201 bodies carry {"timestamp": "<millis as string>"}.
            try:
                return int(r.json().get("timestamp"))
            except Exception:
                return None
        logger.error("Failed to send to Signal: %s %s", r.status_code, r.text)
        return None
    except Exception as e:
        logger.error("Signal send error: %s", e)
        return None


def notify_error(text, recipient=None):
    """Log an error and mirror it into the Signal chat, best-effort.

    ``text`` is plain (no emoji): it is logged as-is and sent with a 🚨 prefix.

    Reserved for failures that would otherwise be silent — only visible in
    `docker compose logs`, which nobody watches continuously. That blind spot
    is exactly what let the bridge stay dead for 11 days (see the network
    resilience notes in CLAUDE.md). Never call this from send_signal_message's
    own error path: a failing Signal send must not try to notify itself.
    """
    logger.error(text)
    send_signal_message(f"🚨 {text}", recipient=recipient)


# ── Receive / attachments ─────────────────────────────────────────────────────
def fetch_signal_envelopes():
    """GET /v1/receive/{number} — returns a list of envelopes, consuming them."""
    url = f"{config.SIGNAL_API_BASE}/v1/receive/{quote(config.SIGNAL_SENDER, safe='')}"
    params = {"timeout": "1", "ignore_attachments": "false", "ignore_stories": "true"}
    started = time.monotonic()
    with SIGNAL_LOCK:
        r = requests.get(url, params=params, timeout=config.SIGNAL_RECEIVE_TIMEOUT)
    elapsed = time.monotonic() - started
    if elapsed > 30:
        # Normal is ~5s. Much longer means an initial sync or lock contention;
        # surface it, because it also stalls outbound sends behind SIGNAL_LOCK.
        logger.warning("Signal receive took %.0fs (expected ~5s)", elapsed)
    if r.status_code == 400:
        # signal-cli is busy or returned a transient 400; log body and skip this tick
        logger.warning("Signal /receive 400: %s", r.text.strip()[:200])
        return []
    r.raise_for_status()
    return r.json() or []


def download_attachment(att_id):
    with SIGNAL_LOCK:
        r = requests.get(
            f"{config.SIGNAL_API_BASE}/v1/attachments/{att_id}",
            timeout=config.SIGNAL_TIMEOUT,
        )
    r.raise_for_status()
    return r.content


def delete_attachment(att_id):
    """Drop the downloaded audio from the signal-api container's disk."""
    try:
        with SIGNAL_LOCK:
            requests.delete(
                f"{config.SIGNAL_API_BASE}/v1/attachments/{att_id}",
                timeout=config.SIGNAL_TIMEOUT,
            )
    except Exception as e:
        logger.warning("Could not delete attachment %s: %s", att_id, e)
