"""Logging setup: one stdlib root handler, per-module loggers underneath."""

import logging

from . import config

FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s"


def setup_logging():
    """Log to stderr (captured by ``docker compose logs``) at ``LOG_LEVEL``."""
    logging.basicConfig(level=config.LOG_LEVEL, format=FORMAT, force=True)
    # urllib3 logs every request at DEBUG; keep it out of LOG_LEVEL=DEBUG runs.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
