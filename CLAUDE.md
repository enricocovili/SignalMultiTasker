# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Signal bridge with two jobs: it polls an IMAP inbox and forwards new emails to
a Signal group as LLM summaries, and it transcribes voice messages sent to
Signal and answers with an LLM summary. All application logic lives in
`bridge.py`; everything else is container orchestration. It runs as a
multi-container `docker compose` stack.

## Architecture

Two services on the `signal-network` bridge network (`docker-compose.yaml`):

- **`email-bridge`** — built from `Dockerfile`, runs `bridge.py`. The only
  custom code. Two loops: the email poll loop runs on the main thread, the
  voice-note loop on a daemon thread.
- **`signal-api`** — `bbernhard/signal-cli-rest-api`. Outbound messages POST to
  `/v2/send` with `text_mode: styled`; inbound drain via `GET /v1/receive/{number}`.
  State (linked Signal account) lives in the `signal-cli-config` volume.

Summarisation and speech-to-text are **external, keyed providers**, not
containers. Both are assumed to speak the OpenAI-compatible surface, and both
are optional at runtime.

`SIGNAL_LOCK` serialises *every* call to signal-api. signal-cli takes a file
lock on the account store, so a send issued while a receive is in flight fails;
the two loops must not race. Provider calls (LLM, transcription) stay outside
the lock — they take tens of seconds.

The email loop signals a fatal restart with `os._exit(1)`, not `sys.exit`:
`SystemExit` only unwinds the calling thread and the daemon voice thread must
not be able to keep a wedged process alive.

### Summarisation (the LLM contract)

Every forwarded email and voice note is summarised by default — `summarize()` is
the single entry point for both.

- **The sender is never LLM-derived.** `extract_sender()` parses the `From`
  header with `parseaddr` + RFC 2047 decoding and returns `Name <addr>`. The
  prompt explicitly forbids the model from mentioning or guessing the sender.
  Do not "improve" this by letting the model read it off the body.
- **The model produces only the intention**, in at most 2 sentences, in the
  source language, with no preamble or markdown.
- **The provider is any OpenAI-compatible chat-completions endpoint.**
  `LLM_URL` is the API *base* (ending in `/v1`) and the code appends
  `/chat/completions`; `LLM_API_KEY` becomes a `Bearer` header via `bearer()`.
  Keep the client provider-agnostic — no vendor SDK, no vendor-specific fields.
- **The key is optional by design.** `bearer()` returns `{}` when it is unset,
  so the bridge can also point at an unauthenticated endpoint on a trusted
  network. Keys live in `.env`, never in `docker-compose.yaml`.
- **All LLM config lives in `docker-compose.yaml`** — endpoint, key, model,
  timeout, temperature, `max_tokens`, input char cap, and both prompts. The
  `bridge.py` constants are only fallback defaults; retuning must not need a
  code change. The prompts are YAML block scalars in the map-form `environment:`
  block *on purpose*: in the `- KEY=value` list form, the `{subject}`/`{body}`
  placeholders break compose's `${...}` interpolation.
- `summarize()` returns `None` on any failure — including unset `LLM_URL` or
  `LLM_MODEL`, which it checks before making a call. Callers must degrade, never
  drop: emails fall back to a truncated body excerpt, voice notes to the raw
  transcript. An unreachable provider must not cost a notification.
- `check_provider_config()` runs at startup and **warns without exiting**.
  Forwarding mail is the job that must not stop because a provider is
  misconfigured.
- Rule matches (see below) short-circuit *before* the LLM — no summary is
  generated for them.

### Voice notes

`listen_for_voice_notes()` drains `/v1/receive` every `SIGNAL_POLL_INTERVAL`
seconds and handles any attachment whose `contentType` starts with `audio/`.
Signal always sends voice notes as AAC, which some Whisper-compatible backends
reject outright, so `transcode_to_wav()` pipes the audio through `ffmpeg`
(installed in the `Dockerfile`) to 16kHz mono WAV before it's ever uploaded.
Transcription then posts multipart `file` + `model` to
`{WHISPER_URL}/audio/transcriptions` with `WHISPER_API_KEY` as a `Bearer` header.

- Both `dataMessage` (someone else's voice note) and `syncMessage.sentMessage`
  (a voice note sent from the owner's own phone) are handled. This bridge is a
  *linked device*, so dropping the sync case would ignore the owner's own notes.
- The flow is: download the attachment, transcribe, summarise, send one
  message with the result — no "transcribing…" placeholder. `summarize()`
  degrading to `None` and `transcribe_audio()` degrading to `None` already
  cover the failure paths without needing an interim message.
- Replies go back to the conversation the note came from
  (`conversation_recipient()`), falling back to `SIGNAL_GROUP_ID`. Its raw
  `groupId` from `/v1/receive` must be base64-re-encoded and `group.`-prefixed
  before being used as a `/v2/send` recipient — `/v1/receive` and `/v2/send`
  disagree on group-ID encoding.

### Email tracking (the core design)

The bridge does **not** rely on the IMAP `\Seen` flag — emails are fetched with
`BODY.PEEK` so server flags are never modified. Instead it keeps its own state:

- State file: `STATE_FILE` (`/app/state/email_state.json`), persisted on the
  `./bridge_state` volume so it survives container restarts. Written atomically
  (tmp + `os.replace`).
- Shape: `{"uidvalidity": <int>, "last_uid": <int>}`. New mail = IMAP UID greater
  than `last_uid`. Only the max UID is tracked, not a full seen-set.
- **Empty state or changed `UIDVALIDITY`** (first start / manual reset / server
  UID-space reset) → baseline the state and send an info message instead of
  flooding the channel with every existing email.
- **More than `MAX_NEW_EMAILS` new mails in one poll** → send a "manual check
  needed" warning and advance state, rather than forwarding each individually.

When changing email logic, preserve these three behaviours and keep using
`BODY.PEEK` — switching to `SEEN`-based search reintroduces the bug this design
removed.

### Network resilience (learned from an 11-day silent outage)

The poll loop must never be able to hang or die quietly:

- **Always pass `timeout=IMAP_TIMEOUT` to `IMAP4_SSL`.** Without it imaplib
  blocks forever on a half-open connection — the container stays `Up`, logs
  nothing, and no restart policy can recover it.
- **Close the connection in a `finally`** (`close_imap`), or failed polls leak
  sockets.
- **Transient errors** (DNS resolution, refused/reset connections, TLS, read
  timeouts) are counted, logged, and retried with capped exponential backoff.
  After `MAX_CONSECUTIVE_FAILURES` the process exits non-zero on purpose so
  Docker restarts it with fresh DNS state — do not swallow that exit.
- **`email-bridge` uses `restart: unless-stopped`.** `on-failure` does *not*
  restart a container after a host reboot or Docker daemon restart; that is what
  kept the bridge dead from 2026-07-30 to 2026-08-11.

## Configuration

All config is environment-driven (`os.getenv` in `bridge.py`, wired in
`docker-compose.yaml`). Secrets live in `.env` (gitignored): `EMAIL_USER`,
`EMAIL_PASS`, `SIGNAL_SENDER`, `SIGNAL_GROUP_ID`, `LLM_API_KEY`,
`WHISPER_API_KEY`. `.env.example` is the tracked template — add every new
variable there. Tunables include `MAX_NEW_EMAILS`, `EMAIL_POLL_INTERVAL`,
`SIGNAL_POLL_INTERVAL`, `IMAP_TIMEOUT`, `MAX_CONSECUTIVE_FAILURES`,
`FAILURE_BACKOFF_MAX`, the `LLM_*` block, and `WHISPER_*` /
`VOICE_INCLUDE_TRANSCRIPT`. `IMAP_SERVER` is hardcoded to `imap.hostinger.com`.

## Commands

```bash
# Build and start the stack (fill in .env first — see .env.example)
docker compose up -d --build

# Follow bridge logs; the first lines report any unset provider config
docker compose logs -f email-bridge

# Validate compose file
docker compose config -q

# Syntax-check the bridge (no test suite exists)
python -m py_compile bridge.py
```

There is no test suite, linter config, or build step beyond the Docker image.

## Branches

`voicenotes-summary-support` holds the original voice-note pipeline that was
removed from `main` in f509d31 and reinstated (rewritten) here.
