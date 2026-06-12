# Changelog

## v0.2.0 — 2026-06-12

- **Session cost now resets on `/clear`.** `cost.total_cost_usd` from Claude
  Code is a process-lifetime accumulator and never resets on `/clear`; the
  statusline now detects `/clear` (new clear record in the transcript, or a
  transcript path switch, tracked per `session_id`) and shows the cost
  relative to that point.
- **Daily cost now resets at local midnight.** Usage rows are dated by their
  own message timestamps (UTC → local) instead of attributing whole transcript
  files to their mtime date — long-lived sessions spanning midnight no longer
  leak yesterday's usage into today's total.
- 4 new regression tests (23 total).

## v0.1.0 — 2026-06-11

Initial public release.

- 5-line cockpit: cwd/branch · model/context/5h/7d limits · Codex CLI quota
  and session cost · Claude session + daily cost · parallel session view
- Fully configurable icons, labels, currency, separators, and per-line
  toggles via `~/.config/claude-statusline/config.json`
- Screenshot-safe output masking (`mask_patterns`)
- Standalone mode: works without Claude Code (Codex-only users — run
  directly, or live via `watch` / a tmux pane)
- `usage_estimate.py` doubles as a standalone usage report CLI
  (`--today` / `--days N` / `--json`, by date/project/model)
- Dated model ids (`claude-haiku-4-5-20251001`) priced at their base entry;
  unknown models priced at the most expensive tier on purpose
- Stdlib only, fail-soft everywhere, MIT
