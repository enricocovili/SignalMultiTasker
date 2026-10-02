"""Entrypoint: voice loop on a daemon thread, email loop on the main thread."""

import threading

from .email_loop import listen_for_emails
from .log import setup_logging
from .providers import check_provider_config
from .signal_api import ensure_signal_account
from .voice import listen_for_voice_notes


def main():
    threading.current_thread().name = "email-loop"
    setup_logging()
    check_provider_config()
    ensure_signal_account()
    threading.Thread(
        target=listen_for_voice_notes, name="voice-loop", daemon=True
    ).start()
    listen_for_emails()
