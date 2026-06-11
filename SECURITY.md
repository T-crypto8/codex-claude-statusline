# Security Policy

## Scope

This tool runs locally and reads only:

- the statusLine JSON payload Claude Code pipes to it
- local transcript/session files under `~/.claude/`
- local Codex CLI rollout files under `~/.codex/sessions/`
- one optional outbound HTTPS request to `api.frankfurter.app` (currency
  conversion; never made when `currency.code` is `USD`)

It sends no telemetry, executes nothing from the files it reads, and writes
only to a cache directory under the system temp dir.

## Privacy notes

- Line 5 displays labels derived from your *other* Claude Code sessions.
  Use `mask_patterns` in your config to redact names you never want rendered
  (useful before sharing screenshots).
- Cost figures are local estimates computed from your own transcript files.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** on this repository
(Security tab → "Report a vulnerability"), or open an issue *without*
exploit details and ask for a private channel. You can expect an initial
response within a week.

## Supported versions

Only the latest release receives fixes.
