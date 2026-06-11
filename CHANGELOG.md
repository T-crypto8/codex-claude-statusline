# Changelog

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
