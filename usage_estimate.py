#!/usr/bin/env python3
"""Estimate Claude Code usage/cost from local transcript files.

Scans ~/.claude/projects/*/*.jsonl, sums per-message token usage, and prices
it with a built-in table (override via pricing.json next to this script or
$CLAUDE_STATUSLINE_PRICING). Costs are estimates at API list prices — useful
as a relative gauge on subscription plans, not a bill.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# USD per 1M tokens. Unknown models are priced at the most expensive entry so
# the estimate errs high instead of silently undercounting.
DEFAULT_PRICING = {
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_create": 12.5, "cache_read": 1.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_create": 6.25, "cache_read": 0.5},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_create": 6.25, "cache_read": 0.5},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_create": 6.25, "cache_read": 0.5},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_create": 3.75, "cache_read": 0.3},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_create": 1.25, "cache_read": 0.1},
}
DEFAULT_FALLBACK_KEY = "claude-fable-5"


def _load_pricing() -> tuple[dict[str, dict[str, float]], str]:
    candidates = []
    env_path = os.environ.get("CLAUDE_STATUSLINE_PRICING")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path(__file__).resolve().parent / "pricing.json")
    for path in candidates:
        try:
            d = json.loads(path.read_text())
            return d["pricing"], d.get("fallback_key", DEFAULT_FALLBACK_KEY)
        except (OSError, ValueError, KeyError):
            continue
    return DEFAULT_PRICING, DEFAULT_FALLBACK_KEY


PRICING, FALLBACK_PRICING_KEY = _load_pricing()


@dataclass(frozen=True)
class SessionUsage:
    session_key: str
    project: str
    model: str
    session_date: str
    input_tokens: int
    output_tokens: int
    cache_create_tokens: int
    cache_read_tokens: int
    cost_usd: float
    unknown_model: bool

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_create_tokens
            + self.cache_read_tokens
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int)
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--project")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def get_cutoff(days: int | None, today: bool = False) -> datetime | None:
    if today:
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if days is None:
        return None
    return datetime.now() - timedelta(days=days)


_HOME_PREFIX = "-" + str(Path.home()).strip("/").replace("/", "-")


def project_label(path: Path) -> str:
    # Directory names encode the cwd ("-Users-x-my-repo"); strip the home
    # prefix so multi-word project names survive intact ("my-repo").
    name = path.name
    if name.startswith(_HOME_PREFIX):
        return name[len(_HOME_PREFIX):].lstrip("-") or "home"
    return name.lstrip("-")


def iter_session_files(
    cutoff: datetime | None = None,
    project_filter: str | None = None,
) -> list[tuple[str, Path, datetime]]:
    session_files: list[tuple[str, Path, datetime]] = []
    if not PROJECTS_ROOT.exists():
        return session_files
    for base_dir in PROJECTS_ROOT.iterdir():
        if not base_dir.is_dir():
            continue
        label = project_label(base_dir)
        if project_filter and label != project_filter:
            continue
        for jsonl_path in base_dir.glob("*.jsonl"):
            mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime)
            if cutoff and mtime < cutoff:
                continue
            session_files.append((label, jsonl_path, mtime))
    return sorted(session_files, key=lambda item: item[2])


def _extract_usage(assistant_message: dict[str, Any]) -> tuple[str | None, dict[str, int]]:
    usage = assistant_message.get("usage")
    if not isinstance(usage, dict):
        return None, {}
    return assistant_message.get("model"), {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_create_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
    }


def _resolve_pricing_key(model: str | None) -> str | None:
    if not model:
        return None
    if model in PRICING:
        return model
    # Dated model ids ("claude-haiku-4-5-20251001") should match their base
    # entry instead of falling through to the most expensive tier.
    matches = [k for k in PRICING if model.startswith(k)]
    return max(matches, key=len) if matches else None


def estimate_cost(model: str | None, usage: dict[str, int]) -> tuple[float, bool]:
    key = _resolve_pricing_key(model)
    unknown_model = key is None
    pricing = PRICING[FALLBACK_PRICING_KEY] if unknown_model else PRICING[key]
    return (
        usage["input_tokens"] / 1_000_000 * pricing["input"]
        + usage["output_tokens"] / 1_000_000 * pricing["output"]
        + usage["cache_create_tokens"] / 1_000_000 * pricing["cache_create"]
        + usage["cache_read_tokens"] / 1_000_000 * pricing["cache_read"],
        unknown_model,
    )


def _row_datetime(data: dict[str, Any], mtime: datetime) -> datetime:
    """Per-record timestamp (UTC ISO) as local naive datetime; file mtime fallback.

    Attributing a whole file to its mtime date makes long-lived sessions
    (spanning midnight) dump yesterday's usage into "today", so daily totals
    never appear to reset. Dating each record individually fixes that.
    """
    ts = data.get("timestamp")
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return mtime
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    return mtime


def parse_session_file(
    jsonl_path: Path,
    project_name: str,
    mtime: datetime,
    cutoff: datetime | None = None,
) -> tuple[list[SessionUsage], set[str]]:
    session_key = jsonl_path.stem
    rows: list[SessionUsage] = []
    warnings: set[str] = set()

    with jsonl_path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") != "assistant":
                continue
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            row_dt = _row_datetime(data, mtime)
            if cutoff and row_dt < cutoff:
                continue
            session_date = row_dt.strftime("%Y-%m-%d")
            model, usage = _extract_usage(message)
            if not usage or not any(usage.values()):
                continue  # all-zero rows (synthetic messages) are noise
            cost_usd, unknown_model = estimate_cost(model, usage)
            if unknown_model:
                warnings.add(model or "unknown")
            rows.append(
                SessionUsage(
                    session_key=session_key,
                    project=project_name,
                    model=model or "unknown",
                    session_date=session_date,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    cache_create_tokens=usage["cache_create_tokens"],
                    cache_read_tokens=usage["cache_read_tokens"],
                    cost_usd=cost_usd,
                    unknown_model=unknown_model,
                )
            )
    return rows, warnings


def collect_usage(
    cutoff: datetime | None = None,
    project_filter: str | None = None,
) -> tuple[list[SessionUsage], list[str], int]:
    rows: list[SessionUsage] = []
    warnings: set[str] = set()
    session_count = 0
    for project_name, jsonl_path, mtime in iter_session_files(cutoff, project_filter):
        session_count += 1
        parsed_rows, parsed_warnings = parse_session_file(jsonl_path, project_name, mtime, cutoff)
        rows.extend(parsed_rows)
        warnings.update(parsed_warnings)
    return rows, sorted(warnings), session_count


def summarize_usage(rows: list[SessionUsage], session_count: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "session_count": session_count,
        "estimated_cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_create_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "by_date": [],
        "by_project": [],
        "by_model": [],
    }
    by_date: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"date": "", "sessions": 0, "cost_usd": 0.0, "total_tokens": 0}
    )
    by_project: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"project": "", "cost_usd": 0.0, "total_tokens": 0}
    )
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"model": "", "cost_usd": 0.0, "total_tokens": 0}
    )
    sessions_by_date: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        summary["estimated_cost_usd"] += row.cost_usd
        summary["input_tokens"] += row.input_tokens
        summary["output_tokens"] += row.output_tokens
        summary["cache_create_tokens"] += row.cache_create_tokens
        summary["cache_read_tokens"] += row.cache_read_tokens
        summary["total_tokens"] += row.total_tokens

        date_bucket = by_date[row.session_date]
        date_bucket["date"] = row.session_date
        date_bucket["cost_usd"] += row.cost_usd
        date_bucket["total_tokens"] += row.total_tokens

        project_bucket = by_project[row.project]
        project_bucket["project"] = row.project
        project_bucket["cost_usd"] += row.cost_usd
        project_bucket["total_tokens"] += row.total_tokens

        model_bucket = by_model[row.model]
        model_bucket["model"] = row.model
        model_bucket["cost_usd"] += row.cost_usd
        model_bucket["total_tokens"] += row.total_tokens

        sessions_by_date[row.session_date].add(row.session_key)

    for day, sessions in sessions_by_date.items():
        by_date[day]["sessions"] = len(sessions)

    summary["by_date"] = sorted(by_date.values(), key=lambda item: item["date"])
    summary["by_project"] = sorted(
        by_project.values(), key=lambda item: (-item["cost_usd"], item["project"])
    )
    summary["by_model"] = sorted(
        by_model.values(), key=lambda item: (-item["cost_usd"], item["model"])
    )
    return summary


def build_report(
    days: int | None = None,
    today: bool = False,
    project_filter: str | None = None,
) -> dict[str, Any]:
    cutoff = get_cutoff(days, today)
    session_files = iter_session_files(cutoff, project_filter)
    rows, warnings, session_count = collect_usage(cutoff, project_filter)
    summary = summarize_usage(rows, session_count)
    sessions_by_date: dict[str, int] = defaultdict(int)
    for _project_name, _jsonl_path, mtime in session_files:
        sessions_by_date[mtime.strftime("%Y-%m-%d")] += 1
    by_date = {item["date"]: item for item in summary["by_date"]}
    for day, count in sessions_by_date.items():
        if day not in by_date:
            by_date[day] = {"date": day, "sessions": count, "cost_usd": 0.0, "total_tokens": 0}
            continue
        by_date[day]["sessions"] = count
    summary["by_date"] = sorted(by_date.values(), key=lambda item: item["date"])
    summary["warnings"] = warnings
    summary["period"] = "today" if today else f"{days}d" if days is not None else "all"
    if project_filter:
        summary["project_filter"] = project_filter
    return summary


def _format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000_000:
        return f"{tokens / 1_000_000_000:.2f}B"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.2f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return str(tokens)


def _render_text(summary: dict[str, Any], days: int | None, today: bool) -> str:
    period = "today" if today else f"last {days}d" if days is not None else "all time"
    lines = [f"=== Claude Code usage [{period}] ===", ""]
    lines.append(
        "total: "
        f"{summary['session_count']} sessions / "
        f"est. ${summary['estimated_cost_usd']:.2f} / "
        f"{_format_tokens(summary['total_tokens'])} tokens "
        f"(cache_read {_format_tokens(summary['cache_read_tokens'])})"
    )

    if summary["by_date"]:
        lines.extend(["", "by date:"])
        for item in summary["by_date"]:
            mmdd = datetime.strptime(item["date"], "%Y-%m-%d").strftime("%m/%d")
            lines.append(
                f"  {mmdd}   {item['sessions']} sessions  "
                f"${item['cost_usd']:.2f}  ({_format_tokens(item['total_tokens'])} tokens)"
            )

    if summary["by_project"]:
        total_cost = summary["estimated_cost_usd"]
        lines.extend(["", "by project:"])
        for item in summary["by_project"]:
            pct = (item["cost_usd"] / total_cost * 100) if total_cost else 0.0
            lines.append(f"  {item['project']:<18} ${item['cost_usd']:.2f} ({pct:.0f}%)")

    if summary["by_model"]:
        total_cost = summary["estimated_cost_usd"]
        lines.extend(["", "by model:"])
        for item in summary["by_model"]:
            pct = (item["cost_usd"] / total_cost * 100) if total_cost else 0.0
            lines.append(f"  {item['model']:<18} ${item['cost_usd']:.2f} ({pct:.0f}%)")

    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    summary = build_report(days=args.days, today=args.today, project_filter=args.project)
    for model in summary["warnings"]:
        print(
            f"warning: unknown model pricing for {model}; "
            f"estimated at {FALLBACK_PRICING_KEY} rates (may overestimate)",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(_render_text(summary, args.days, args.today))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
