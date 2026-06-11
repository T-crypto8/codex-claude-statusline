# Contributing

Thanks for considering a contribution!

## Ground rules

- **Stdlib only.** No third-party runtime dependencies — that's a feature.
  PRs adding dependencies need a very strong reason.
- **Fail soft.** A missing file, dead network, or absent Codex install must
  degrade to `—`, never crash or drop a line.
- **Privacy first.** Nothing in this tool may transmit transcript or session
  content anywhere. New data sources must be local and documented in
  SECURITY.md.
- Python 3.10+, no type-checker config enforced, but keep annotations.

## Workflow

1. Open or pick an issue (roadmap items are labeled `roadmap`).
2. Fork, branch, change.
3. `python3 -m unittest discover -s tests` must pass; add tests for parser
   or pricing logic changes.
4. Smoke-test: `echo '{}' | python3 statusline.py`.
5. Open a PR with a short before/after description (a terminal paste of the
   rendered lines helps a lot).

## Release process

Maintainer-side: update `CHANGELOG.md`, tag `vX.Y.Z`, publish a GitHub
Release with the changelog section as notes.
