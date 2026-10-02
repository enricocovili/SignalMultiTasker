"""Summarisation through any OpenAI-compatible chat-completions endpoint."""

import logging

import requests

from . import config
from .providers import bearer

logger = logging.getLogger(__name__)


class _SafeFields(dict):
    """Render unknown {placeholders} as empty instead of raising KeyError.

    The prompts are user-editable via the environment, so a stray placeholder
    must degrade the summary, not kill the poll loop.
    """

    def __missing__(self, key):
        return ""


def summarize(prompt_template, body, subject=""):
    """Return ``(summary, error)`` for ``body``; ``summary`` is None if unusable.

    ``error`` is None when the LLM is simply unconfigured (an intentional,
    silent degrade — see ``check_provider_config``) or when a summary was
    produced or withheld for content reasons. It is set to a short code only
    when the provider itself failed (timeout, HTTP error, connection error),
    so callers can tell "no provider configured" apart from "provider is
    broken" and react differently. Callers must handle a None summary either
    way: the provider is a best-effort dependency, and a summary is never
    worth dropping a notification over.
    """
    if not (config.LLM_URL and config.LLM_MODEL):
        return None, None

    prompt = prompt_template.format_map(
        _SafeFields(
            body=(body or "")[: config.LLM_MAX_INPUT_CHARS], subject=subject or ""
        )
    )
    payload = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": config.LLM_MAX_TOKENS,
        "stream": False,
    }
    try:
        response = requests.post(
            f"{config.LLM_URL}/chat/completions",
            json=payload,
            headers=bearer(config.LLM_API_KEY),
            timeout=config.LLM_TIMEOUT,
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
            logger.warning(
                "LLM returned no content in %.1fs (finish_reason=length): "
                "LLM_MAX_TOKENS=%d is too low for this model",
                took,
                config.LLM_MAX_TOKENS,
            )
            return None, None
        logger.info("LLM summary in %.1fs: %r", took, summary)
        return summary or None, None
    except requests.exceptions.Timeout:
        logger.warning("LLM timed out after %ds", config.LLM_TIMEOUT)
        return None, "timeout"
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "unknown"
        logger.warning("LLM HTTP error: %s", code)
        return None, str(code)
    except Exception as e:
        logger.warning("LLM error: %s: %s", type(e).__name__, e)
        return None, type(e).__name__
