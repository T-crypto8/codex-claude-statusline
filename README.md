# codex-claude-statusline

A multi-line status line for [Claude Code](https://claude.com/claude-code) that turns the bottom of your terminal into a cockpit:

```
🐶 📁my-repo │ 🌿 main
🤖Opus 4.8 │ 📈42% │ 🕐5h 31% ↺18:00 │ 📅7d 67% ↺6/14 09:00
🐾 Codex │ 🕐5h 12% ↺19:30 │ 📅7d 48% ↺6/15 11:00 │ ≈$3.21・1.2Mtok (session)
💰Claude $1.84・890ktok (session) │ $12.40・8.1Mtok (daily)
🤝parallel2: ●fixing the flaky test ○docs review
```

| Line | What it shows | Source |
|---|---|---|
| 1 | cwd + git branch | statusLine payload + `git` |
| 2 | model, context %, 5h / 7d rate limits with reset times | statusLine payload |
| 3 | **Codex CLI** remaining quota + rough session cost | `~/.codex/sessions` rollout files |
| 4 | Claude session cost + **daily total across all sessions** | statusLine payload + local transcripts |
| 5 | other live Claude Code sessions and what they're doing | `~/.claude/sessions` registry |

Costs are estimates at API list prices — on a subscription plan they are a relative gauge, not a bill. Unknown models are priced at the most expensive tier on purpose (estimates err high, never silently low).

## Install

Requires Python 3.10+. No third-party packages.

```bash
git clone https://github.com/T-crypto8/codex-claude-statusline.git ~/.claude-statusline
```

Add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude-statusline/statusline.py"
  }
}
```

## Configure

Copy the example and edit — every emoji, label, and line is yours to change:

```bash
mkdir -p ~/.config/claude-statusline
cp ~/.claude-statusline/config.example.json ~/.config/claude-statusline/config.json
```

- **Change the mascot**: set `icons.prefix` to anything (`"🐱"`, `"🦊"`, `"❯"`, your initials…). Every other icon is configurable too.
- **Hide lines you don't need**: `lines.codex / cost / parallel` → `false`. No Codex CLI installed? Line 3 just shows `—` (or turn it off).
- **Currency**: `currency.code` `"USD"` shows raw dollars. Set e.g. `{"code": "JPY", "symbol": "¥", "fallback_rate": 155}` to convert via the free [frankfurter.app](https://frankfurter.app) API (cached 12h, falls back gracefully offline).
- **Screenshot safety**: put any strings you never want rendered (your name, company, client names) in `mask_patterns` — they are replaced with `mask_replacement` in the final output, whatever line they appear on. Useful because line 5 echoes prompts from your other sessions.
- Config path override: `CLAUDE_STATUSLINE_CONFIG=/path/to/config.json`.
- Pricing override: drop a `pricing.json` next to the scripts (same shape as `DEFAULT_PRICING` in `usage_estimate.py`) or set `CLAUDE_STATUSLINE_PRICING`.

## Using with Codex CLI (no Claude Code needed)

Line 3 already tracks Codex CLI quota and session cost from `~/.codex/sessions`. If you live in Codex (or anything else), run the cockpit standalone — it detects the missing payload and renders what it can:

```bash
python3 ~/.claude-statusline/statusline.py        # one-shot
watch -n 30 python3 ~/.claude-statusline/statusline.py   # live, e.g. in a tmux pane
```

Tip for Codex-only users: set `lines.cost` to `false` (Claude transcript scan) and keep `codex` + `parallel`.

## Standalone usage report

The daily-cost engine also works as a CLI:

```bash
python3 usage_estimate.py --today          # today's sessions
python3 usage_estimate.py --days 7         # last week, by date/project/model
python3 usage_estimate.py --days 30 --json # machine-readable
```

## Notes

- Heavy work is cached under the system temp dir (`claude-statusline/`): daily cost 60s, Codex scan 120s, FX 12h. The status line itself stays fast.
- Everything fails soft: no network, no Codex, no session registry → `—`, never a crash.
- Line 5 liveness uses `kill -0` on registered pids; sessions older than 24h are ignored even if the pid was recycled.

## License

MIT
