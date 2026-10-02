"""Helpers shared by the external, keyed providers (LLM and speech-to-text)."""

import logging

from . import config

logger = logging.getLogger(__name__)


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
        ("LLM_URL", config.LLM_URL, "emails forward as a plain excerpt"),
        ("LLM_MODEL", config.LLM_MODEL, "emails forward as a plain excerpt"),
        ("WHISPER_URL", config.WHISPER_URL, "voice messages cannot be transcribed"),
    ):
        if not value:
            logger.warning("%s is not set: %s", name, effect)
    if config.LLM_URL and not config.LLM_API_KEY:
        logger.info("LLM_API_KEY is not set: calling the LLM without authentication")
    if config.WHISPER_URL and not config.WHISPER_API_KEY:
        logger.info(
            "WHISPER_API_KEY is not set: calling Whisper without authentication"
        )
