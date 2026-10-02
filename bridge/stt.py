"""Speech-to-text: ffmpeg transcode, then an OpenAI-compatible transcriptions call."""

import logging
import subprocess

import requests

from . import config
from .providers import bearer

logger = logging.getLogger(__name__)


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
        timeout=config.WHISPER_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg exited {proc.returncode}: {proc.stderr.decode(errors='replace')[:200]}"
        )
    return proc.stdout


def transcribe_audio(audio_bytes):
    """Upload audio to the transcription provider and return the text, or None."""
    if not config.WHISPER_URL:
        return None

    try:
        audio_bytes = transcode_to_wav(audio_bytes)
    except Exception as e:
        logger.warning("Audio transcode error: %s: %s", type(e).__name__, e)
        return None

    data = {"model": config.WHISPER_MODEL, "response_format": "json"}
    if config.WHISPER_LANG:
        data["language"] = config.WHISPER_LANG
    files = {"file": ("voice.wav", audio_bytes)}
    try:
        r = requests.post(
            f"{config.WHISPER_URL}/audio/transcriptions",
            data=data,
            files=files,
            headers=bearer(config.WHISPER_API_KEY),
            timeout=config.WHISPER_TIMEOUT,
        )
        r.raise_for_status()
        return (r.json().get("text") or "").strip()
    except requests.exceptions.Timeout:
        logger.warning("Transcription timed out after %ds", config.WHISPER_TIMEOUT)
        return None
    except Exception as e:
        logger.warning("Transcription error: %s: %s", type(e).__name__, e)
        return None
