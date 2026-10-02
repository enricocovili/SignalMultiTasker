"""Voice-note loop: drain Signal, transcribe, summarise, reply in the same chat."""

import base64
import logging
import time

import requests

from . import config
from .llm import summarize
from .signal_api import (
    delete_attachment,
    download_attachment,
    fetch_signal_envelopes,
    notify_error,
    send_signal_message,
)
from .stt import transcribe_audio

logger = logging.getLogger(__name__)


def is_audio(att):
    return (att.get("contentType") or "").startswith("audio/")


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
        or config.SIGNAL_GROUP_ID
    )


def is_allowed_chat(recipient):
    """Whether voice notes from this conversation may be processed."""
    return recipient in config.VOICE_ALLOWED_CHATS


def process_voice_attachment(att, sender, recipient):
    """Transcribe, summarise, and answer with the result."""
    att_id = att.get("id")
    if not att_id:
        return
    logger.info(
        "Voice note from %s (%s, id=%s)", sender, att.get("contentType"), att_id
    )

    try:
        audio = download_attachment(att_id)
        transcript = transcribe_audio(audio)
        if not transcript:
            send_signal_message(
                "⚠️ Could not transcribe the voice message.", recipient=recipient
            )
            return

        logger.info("Transcript (%d chars): %s...", len(transcript), transcript[:120])
        summary, _llm_error = summarize(config.VOICE_SUMMARY_PROMPT, transcript)

        msg = f"🎤 **Voice message** from {sender}"
        if summary:
            msg += f"\n\n**Summary:** {summary}"
            if config.VOICE_INCLUDE_TRANSCRIPT:
                msg += f"\n\n**Transcript:** {transcript}"
        else:
            # No summary available — the transcript is better than nothing.
            msg += f"\n\n**Transcript:** {transcript}"
        send_signal_message(msg, recipient=recipient)
    finally:
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
            if data and any(is_audio(att) for att in (data.get("attachments") or [])):
                yield envelope, data


def listen_for_voice_notes():
    logger.info("Voice-note listener started for %s", config.SIGNAL_SENDER)
    if config.VOICE_ALLOWED_CHATS:
        logger.info(
            "Voice notes enabled for: %s", ", ".join(sorted(config.VOICE_ALLOWED_CHATS))
        )
    else:
        logger.warning(
            "VOICE_ALLOWED_CHATS and SIGNAL_GROUP_ID are unset: voice notes will be ignored"
        )
    while True:
        try:
            for envelope, data in iter_voice_messages(fetch_signal_envelopes()):
                sender = (
                    envelope.get("sourceName") or envelope.get("source") or "unknown"
                )
                recipient = conversation_recipient(envelope, data)
                if not is_allowed_chat(recipient):
                    # Logged so the ID can be copied into VOICE_ALLOWED_CHATS.
                    logger.info(
                        "Ignoring voice note from %s in %s (not whitelisted)",
                        sender,
                        recipient,
                    )
                    continue
                for att in filter(is_audio, data.get("attachments") or []):
                    try:
                        process_voice_attachment(att, sender, recipient)
                    except Exception as e:
                        notify_error(
                            f"Error processing voice attachment: {e}",
                            recipient=recipient,
                        )
        except requests.exceptions.RequestException as e:
            notify_error(f"Signal receive error: {e}")
        except Exception as e:
            notify_error(f"Voice loop error: {type(e).__name__}: {e}")
        time.sleep(config.SIGNAL_POLL_INTERVAL)
