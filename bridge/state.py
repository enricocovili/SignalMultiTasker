"""Persisted email-tracking state (``{"uidvalidity": int, "last_uid": int}``)."""

import json
import logging
import os

from . import config

logger = logging.getLogger(__name__)


def load_state():
    """Return the persisted state dict, or an empty dict if none exists."""
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("Could not read state file, treating as empty: %s", e)
        return {}


def save_state(state):
    """Atomically persist the state dict to disk."""
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    tmp = f"{config.STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, config.STATE_FILE)
