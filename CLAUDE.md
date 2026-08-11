# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-purpose bridge that polls an IMAP inbox and forwards new emails to a
Signal group. All application logic lives in `bridge.py`; everything else is
container orchestration. It runs as a multi-container `docker compose` stack.

## Architecture

Three services on the `signal-network` bridge network (`docker-compose.yaml`):

- **`email-bridge`** — built from `Dockerfile`, runs `bridge.py`. The only
  custom code. Polls IMAP, calls the other two services over HTTP.
- **`signal-api`** — `bbernhard/signal-cli-rest-api`. Outbound messages POST to
  `/v2/send` with `text_mode: styled`. State (registered Signal account) lives in
  the `signal-cli-config` volume.
- **`ollama`** — LLM summariser. `get_llm_summary()` talks to `/api/chat`.
  **The model must be pulled manually after first start** (see Commands). Note:
  `get_llm_summary` exists but the email loop currently uses plain truncation,
  not the LLM — the summary call is intentionally not wired into `process_email`.

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
`EMAIL_PASS`, `SIGNAL_SENDER`, `SIGNAL_GROUP_ID`. Tunables include
`MAX_NEW_EMAILS`, `EMAIL_POLL_INTERVAL`, `OLLAMA_MODEL`, `IMAP_TIMEOUT`,
`MAX_CONSECUTIVE_FAILURES`, `FAILURE_BACKOFF_MAX`. `IMAP_SERVER` is hardcoded to
`imap.hostinger.com`.

## Commands

```bash
# Build and start the stack
docker compose up -d --build

# Pull the Ollama model once (required before summaries work)
docker exec -it ollama ollama pull gemma4:e2b

# Follow bridge logs
docker compose logs -f email-bridge

# Validate compose file
docker compose config -q

# Syntax-check the bridge (no test suite exists)
python -m py_compile bridge.py
```

There is no test suite, linter config, or build step beyond the Docker image.

## Branches

`voicenotes-summary-support` holds an earlier voice-note transcription pipeline
(Whisper ASR → Ollama summary of Signal voice messages). It was removed from
`main`; that branch is the reference for resuming that feature.
