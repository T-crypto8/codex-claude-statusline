#!/usr/bin/env python3
"""Multi-line status line for Claude Code.

Reads the statusLine JSON payload from stdin and renders up to 5 lines:

  1: 🐶 📁 cwd │ 🌿 git branch
  2: 🤖 model │ 📈 context% │ 🕐 5h limit ↺reset │ 📅 7d limit ↺reset
  3: 🐾 Codex CLI remaining quota + estimated session cost (optional)
  4: 💰 Claude session cost/tokens │ daily cost/tokens
  5: 🤝 parallel Claude Code sessions (optional)

Everything (emoji, labels, currency, masking, which lines render) is
configurable via a JSON config file. See config.example.json.

Config resolution order:
  $CLAUDE_STATUSLINE_CONFIG > ~/.config/claude-statusline/config.json > defaults

Design constraints: the status line is invoked frequently, so anything
expensive (daily cost scan, FX rate, Codex session scan) is cached under a
temp directory. Network failures and missing files degrade to "—" instead
of dropping a whole line.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# --- configuration ----------------------------------------------------------

DEFAULTS: dict = {
    "icons": {
        "prefix": "\U0001f436",      # 🐶  the mascot — change me!
        "dir": "\U0001f4c1",         # 📁
        "branch": "\U0001f33f",      # 🌿
        "model": "\U0001f916",       # 🤖
        "context": "\U0001f4c8",     # 📈
        "limit_5h": "\U0001f550",    # 🕐
        "limit_7d": "\U0001f4c5",    # 📅
        "codex": "\U0001f43e",       # 🐾
        "cost": "\U0001f4b0",        # 💰
        "parallel": "\U0001f91d",    # 🤝
        "busy": "●",            # ●
        "idle": "○",            # ○
        "reset": "↺",           # ↺
        "warn": "⚠️",           # ⚠️
        "forecast": "→",        # →
    },
    "labels": {
        "session": "session",
        "daily": "daily",
        "recovered": "0% (recovered)",
        "no_parallel": "solo",
        "parallel": "parallel",
    },
    "lines": {
        "codex": True,      # line 3: Codex CLI quota/cost (needs ~/.codex)
        "cost": True,       # line 4: Claude cost estimate
        "parallel": True,   # line 5: parallel sessions
    },
    "currency": {
        "code": "USD",      # "USD" renders as-is; anything else converts
        "symbol": "$",
        "fallback_rate": 1.0,   # used when the FX API is unreachable
    },
    "separator": " │ ",    # " │ "
    # Strings to redact from the rendered output (for screenshot safety).
    # Example: ["my-real-name", "my-company"]
    "mask_patterns": [],
    "mask_replacement": "＊",  # ＊
    # Codex pricing (USD per 1M tokens) used for the rough session estimate.
    "codex_pricing": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
    "cache_ttl": {"fx_hours": 12, "daily_seconds": 60, "codex_seconds": 120},
    # Quota depletion forecast. The statusline learns your hour-of-day burn
    # rate from locally accumulated snapshots (percentages only, no content,
    # never leaves your machine) and falls back to linear extrapolation until
    # enough history exists (~2h of active use).
    #   mode: "warn"   show ⚠️~HH:MM only above thresholds (quiet default)
    #         "always" additionally show →~HH:MM whenever depletion is projected
    #         "off"    disable forecast and snapshot collection
    "forecast": {
        "mode": "warn",
        "warn_percent": 80,
        "depletion_floor_percent": 50,
    },
    "git_dirty": True,           # Δn next to the branch (tracked changes only)
    "context_warn_percent": 80,  # ⚠️ on the context gauge at/above this (0 = off)
}

CACHE_DIR = Path(tempfile.gettempdir()) / "claude-statusline"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "claude-statusline"
QUOTA_SNAP_LOG = STATE_DIR / "quota_snapshots.jsonl"  # {ts, five_hour/seven_day: {pct, resets_at}} only
SNAP_THROTTLE_SEC = 300
SNAP_MAX_BYTES = 4_000_000  # rotate to the newest tail when exceeded
MIN_PROFILE_SAMPLES = 24    # ~2h of active use at the 300s throttle
PROFILE_TTL_SEC = 1800
CODEX_SESS_DIR = Path.home() / ".codex" / "sessions"
CLAUDE_SESS_DIR = Path.home() / ".claude" / "sessions"
CLAUDE_PROJ_DIR = Path.home() / ".claude" / "projects"
USAGE_TOOL = Path(__file__).resolve().parent / "usage_estimate.py"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    candidates = []
    env_path = os.environ.get("CLAUDE_STATUSLINE_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.home() / ".config" / "claude-statusline" / "config.json")
    for path in candidates:
        try:
            return _deep_merge(DEFAULTS, json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return DEFAULTS


CFG = load_config()
ICON = CFG["icons"]
LABEL = CFG["labels"]
SEP = CFG["separator"]


# --- small utilities ---------------------------------------------------------

def _read_cache(path: Path, ttl: float) -> dict | None:
    try:
        raw = json.loads(path.read_text())
        if time.time() - raw.get("_at", 0) <= ttl:
            return raw
    except (OSError, ValueError):
        pass
    return None


def _write_cache(path: Path, payload: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**payload, "_at": time.time()}))
        tmp.replace(path)
    except OSError:
        pass


def _tail_text(path: Path, max_bytes: int) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - max_bytes))
        return fh.read().decode("utf-8", "replace")


def sanitize(text: str) -> str:
    for pat in CFG["mask_patterns"]:
        text = re.sub(re.escape(pat), CFG["mask_replacement"], text, flags=re.I)
    return text


def fx_rate() -> float:
    """USD -> configured currency. Returns 1.0 when currency is USD."""
    code = CFG["currency"]["code"].upper()
    if code == "USD":
        return 1.0
    cache = CACHE_DIR / f"fx_{code}.json"
    cached = _read_cache(cache, CFG["cache_ttl"]["fx_hours"] * 3600)
    if cached:
        return cached["rate"]
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"https://api.frankfurter.app/latest?from=USD&to={code}", timeout=1.5
        ) as resp:
            rate = float(json.load(resp)["rates"][code])
        _write_cache(cache, {"rate": rate})
        return rate
    except Exception:
        try:  # an expired cached real rate beats the static fallback
            return json.loads(cache.read_text())["rate"]
        except (OSError, ValueError, KeyError):
            return float(CFG["currency"]["fallback_rate"])


def daily_usage() -> tuple[float | None, int | None]:
    """Today's Claude total (usd, tokens) via usage_estimate.py, cached."""
    cache = CACHE_DIR / "daily_cost.json"
    cached = _read_cache(cache, CFG["cache_ttl"]["daily_seconds"])
    if cached:
        return cached.get("usd"), cached.get("tok")
    usd: float | None = None
    tok: int | None = None
    try:
        out = subprocess.run(
            [sys.executable, str(USAGE_TOOL), "--today", "--json"],
            capture_output=True, text=True, timeout=8,
        )
        if out.returncode == 0:
            d = json.loads(out.stdout)
            usd = float(d["estimated_cost_usd"])
            tok = int(d.get("total_tokens") or 0)
    except Exception:
        usd, tok = None, None
    if usd is not None:
        _write_cache(cache, {"usd": usd, "tok": tok})
    return usd, tok


def git_branch(cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        branch = out.stdout.strip()
        return branch if out.returncode == 0 and branch else None
    except Exception:
        return None


# --- quota forecast (local statistics only; no LLM, no network) -----------------

def snapshot_quota(limits: dict) -> None:
    """Accumulate 5h/7d usage snapshots (percentages only) for the burn profile.

    Throttled to one line per SNAP_THROTTLE_SEC; the file is rotated to its
    newest tail when it exceeds SNAP_MAX_BYTES. Data never leaves the machine.
    """
    if not limits or CFG["forecast"]["mode"] == "off":
        return
    if _read_cache(CACHE_DIR / "quota_snap_mark.json", SNAP_THROTTLE_SEC):
        return
    rec: dict = {"ts": round(time.time(), 1)}
    for key in ("five_hour", "seven_day"):
        b = limits.get(key)
        if isinstance(b, dict):
            rec[key] = {"pct": b.get("used_percentage"), "resets_at": b.get("resets_at")}
    if len(rec) == 1:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with QUOTA_SNAP_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        if QUOTA_SNAP_LOG.stat().st_size > SNAP_MAX_BYTES:
            tail = _tail_text(QUOTA_SNAP_LOG, SNAP_MAX_BYTES // 2)
            tail = tail[tail.find("\n") + 1:]  # drop a possibly partial first line
            tmp = QUOTA_SNAP_LOG.with_suffix(".tmp")
            tmp.write_text(tail, encoding="utf-8")
            tmp.replace(QUOTA_SNAP_LOG)
    except OSError:
        return
    _write_cache(CACHE_DIR / "quota_snap_mark.json", {})


def burn_profile(key: str) -> dict | None:
    """Learn the hour-of-day burn rate (%/h) from accumulated snapshots.

    Averages pct deltas of adjacent snapshot pairs into hour buckets; pairs
    crossing a window reset (negative delta) or further than 1h apart are
    discarded. Returns None until MIN_PROFILE_SAMPLES pairs exist, in which
    case the caller falls back to linear extrapolation. Pure statistics.
    """
    cache = CACHE_DIR / f"burn_profile_{key}.json"
    cached = _read_cache(cache, PROFILE_TTL_SEC)
    if cached is not None and "profile" in cached:
        return cached["profile"]
    try:
        text = _tail_text(QUOTA_SNAP_LOG, 512_000)
    except OSError:
        return None
    sums: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    total = 0.0
    n = 0
    prev: tuple[float, float, object] | None = None
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = d.get("ts")
        b = d.get(key)
        if not isinstance(ts, (int, float)) or not isinstance(b, dict):
            continue
        pct = b.get("pct")
        if not isinstance(pct, (int, float)):
            prev = None
            continue
        cur = (float(ts), float(pct), b.get("resets_at"))
        if prev is not None:
            dt = cur[0] - prev[0]
            dp = cur[1] - prev[1]
            same_window = (cur[2] == prev[2]) if (cur[2] and prev[2]) else dp >= 0
            if 0 < dt <= 3600 and dp >= 0 and same_window:
                rate = dp / dt * 3600  # %/h
                hour = datetime.fromtimestamp(prev[0]).hour
                sums[hour] += rate
                counts[hour] += 1
                total += rate
                n += 1
        prev = cur
    profile = None
    if n >= MIN_PROFILE_SAMPLES and total > 0:
        profile = {
            "hour_rates": {str(h): sums[h] / counts[h] for h in counts},
            "overall": total / n,
            "samples": n,
        }
    _write_cache(cache, {"profile": profile})
    return profile


def project_depletion(pct: float, now: float, resets: float, profile: dict) -> float | None:
    """Walk the learned hourly rates forward; return the 100% ETA, else None."""
    hour_rates = profile.get("hour_rates") or {}
    overall = profile.get("overall") or 0
    if overall <= 0:
        return None
    remaining = 100.0 - pct
    t = now
    while t < resets:
        rate = hour_rates.get(str(datetime.fromtimestamp(t).hour), overall)
        next_hour = (
            datetime.fromtimestamp(t).replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1)
        ).timestamp()
        step = min(next_hour, resets) - t
        burn = rate * step / 3600
        if rate > 0 and burn >= remaining:
            return t + remaining / rate * 3600
        remaining -= burn
        t = min(next_hour, resets)
    return None


def quota_suffix(block: dict | None, window_sec: float, now: float | None = None,
                 profile_key: str | None = None) -> str:
    """Forecast suffix for a rate-limit block, honoring CFG["forecast"].

    "warn" (default): ⚠️~HH:MM only at/above warn_percent, or when depletion is
    projected inside the window at/above depletion_floor_percent. Quiet otherwise.
    "always": additionally →~HH:MM whenever a depletion ETA exists.
    Learned profile when available, linear extrapolation before that (the first
    10% of the window is ignored to avoid jumpy early estimates).
    """
    fc = CFG["forecast"]
    if fc["mode"] == "off" or not isinstance(block, dict):
        return ""
    pct = block.get("used_percentage")
    if not isinstance(pct, (int, float)):
        return ""
    now = now or time.time()
    depleted_at: float | None = None
    resets = block.get("resets_at")
    if isinstance(resets, (int, float)) and resets > now and 0 < pct < 100:
        profile = burn_profile(profile_key) if profile_key else None
        if profile:
            depleted_at = project_depletion(float(pct), now, float(resets), profile)
        else:
            elapsed = now - (resets - window_sec)
            if elapsed >= window_sec * 0.1:
                projected = now + (100 - pct) * elapsed / pct
                if projected < resets:
                    depleted_at = projected
    eta = f"~{datetime.fromtimestamp(depleted_at):%H:%M}" if depleted_at else ""
    if pct >= fc["warn_percent"]:
        return ICON["warn"] + eta
    if depleted_at is not None and pct >= fc["depletion_floor_percent"]:
        return ICON["warn"] + eta
    if depleted_at is not None and fc["mode"] == "always":
        return ICON["forecast"] + eta
    return ""


def git_dirty(cwd: str) -> int | None:
    """Tracked modified/added file count (cached). Untracked and deletions are
    excluded so long-lived repos with pending cleanups don't pin the badge on."""
    cache = CACHE_DIR / "git_dirty.json"
    cached = _read_cache(cache, 30)
    if cached is not None and cached.get("cwd") == cwd:
        return cached.get("n")
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain", "--no-renames", "--untracked-files=no"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            n: int | None = sum(
                1 for line in out.stdout.splitlines()
                if line[:2].strip() and "D" not in line[:2]
            )
        else:
            n = None
    except Exception:
        n = None
    _write_cache(cache, {"cwd": cwd, "n": n})
    return n


# --- /clear-aware session cost -------------------------------------------------

def _last_clear_marker(transcript_path: str) -> str | None:
    """Identifier of the most recent /clear record in the transcript tail, if any."""
    if not transcript_path:
        return None
    try:
        lines = _tail_text(Path(transcript_path), 48_000).splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if "<command-name>/clear" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "user":
            continue
        return str(d.get("uuid") or d.get("timestamp") or "clear")
    return None


def session_cost_after_clear(data: dict, total_usd: float | None) -> float | None:
    """Show session cost relative to the last /clear.

    cost.total_cost_usd never resets on /clear (official docs: "Accumulates the
    estimated cost of all API calls in the current session", i.e. the process).
    Detect /clear per session_id — either a new /clear record in the transcript
    or a transcript_path switch — and save the preceding value as the baseline.
    """
    sid = data.get("session_id") or ""
    if total_usd is None or not sid:
        return total_usd
    safe_sid = re.sub(r"[^A-Za-z0-9_-]", "", sid)[:64]
    cache = CACHE_DIR / f"session_base_{safe_sid}.json"
    transcript = data.get("transcript_path") or ""
    marker = _last_clear_marker(transcript)
    ent: dict | None = None
    try:
        ent = json.loads(cache.read_text())
    except (OSError, ValueError):
        pass
    if not isinstance(ent, dict):
        ent = {"base": 0.0, "prev": total_usd, "marker": marker, "transcript": transcript}
    else:
        prev_transcript = ent.get("transcript") or ""
        if (transcript and prev_transcript and transcript != prev_transcript) or (
            marker and marker != ent.get("marker")
        ):
            # value at the render just before /clear ~= cost at clear time
            ent["base"] = float(ent.get("prev") or 0.0)
        if marker:
            ent["marker"] = marker
        if transcript:
            ent["transcript"] = transcript
        ent["prev"] = total_usd
    _write_cache(cache, ent)
    return max(0.0, total_usd - float(ent.get("base") or 0.0))


# --- Codex CLI quota + session usage -----------------------------------------

def parse_codex_snapshot(lines: list[str]) -> dict | None:
    """Last valid rate_limits from a Codex rollout jsonl (primary may be null)."""
    for line in reversed(lines):
        if '"rate_limits"' not in line or '"primary"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        rl = (d.get("payload") or {}).get("rate_limits") or {}
        if isinstance(rl.get("primary"), dict):
            return {"primary": rl["primary"], "secondary": rl.get("secondary")}
    return None


def parse_codex_usage(lines: list[str]) -> dict | None:
    for line in reversed(lines):
        if '"total_token_usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        info = (d.get("payload") or {}).get("info") or {}
        usage = info.get("total_token_usage")
        if isinstance(usage, dict):
            return usage
    return None


def codex_status() -> tuple[dict | None, dict | None]:
    cache = CACHE_DIR / "codex_limits.json"
    cached = _read_cache(cache, CFG["cache_ttl"]["codex_seconds"])
    if cached:
        return cached.get("snap"), cached.get("usage")
    snap = usage = None
    try:
        files = sorted(
            CODEX_SESS_DIR.glob("*/*/*/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:8]
        for f in files:
            lines = _tail_text(f, 200_000).splitlines()
            snap = snap or parse_codex_snapshot(lines)
            usage = usage or parse_codex_usage(lines)
            if snap and usage:
                break
    except OSError:
        pass
    _write_cache(cache, {"snap": snap, "usage": usage})
    return snap, usage


def codex_cost_usd(usage: dict) -> float:
    p = CFG["codex_pricing"]
    inp = usage.get("input_tokens", 0)
    cached = min(usage.get("cached_input_tokens", 0), inp)
    out = usage.get("output_tokens", 0)
    return ((inp - cached) * p["input"] + cached * p["cached_input"] + out * p["output"]) / 1e6


def fmt_codex(snap: dict | None, usage: dict | None, rate: float) -> str:
    parts = [f"{ICON['codex']} Codex"]
    if not snap:
        parts[0] += " —"
    else:
        now = time.time()
        for icon, label, key in (
            (ICON["limit_5h"], "5h", "primary"),
            (ICON["limit_7d"], "7d", "secondary"),
        ):
            block = snap.get(key)
            if not isinstance(block, dict):
                continue
            resets = block.get("resets_at") or 0
            if resets and now > resets:
                # window has rolled over: fully recovered; next reset date is
                # unknown until the next request starts a new rolling window
                parts.append(f"{icon}{label} {LABEL['recovered']}")
            else:
                reset_s = fmt_reset(resets)
                parts.append(
                    f"{icon}{label} {block.get('used_percent', 0):.0f}%"
                    f"{(' ' + reset_s) if reset_s else ''}"
                )
    if usage:
        tok = usage.get("total_tokens") or (
            usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        )
        parts.append(fmt_cost_tok(codex_cost_usd(usage), tok, rate, LABEL["session"], approx=True))
    else:
        parts.append(fmt_cost_tok(None, None, rate, LABEL["session"]))
    return SEP.join(parts)


# --- parallel sessions --------------------------------------------------------

def clean_text(content: object) -> str:
    """Displayable text from a transcript user message (strip tags/injections)."""
    if isinstance(content, list):
        content = " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    if not isinstance(content, str):
        return ""
    t = re.sub(r"<system-reminder>.*?</system-reminder>", " ", content, flags=re.S)
    t = re.sub(r"<command-name>\s*(.*?)\s*</command-name>", r"\1", t, flags=re.S)
    t = re.sub(r"<local-command-(stdout|caveat)>.*?</local-command-\1>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]*>[^<]*</[^>]*>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(t.split())


def _project_dir(cwd: str) -> Path:
    return CLAUDE_PROJ_DIR / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def last_user_snippet(cwd: str, session_id: str) -> str:
    p = _project_dir(cwd) / f"{session_id}.jsonl"
    if not p.exists():
        return ""
    last_any, last_substantive = "", ""
    try:
        for line in _tail_text(p, 64_000).splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "user" or d.get("isSidechain"):
                continue
            t = clean_text((d.get("message") or {}).get("content"))
            if not t:
                continue
            last_any = t
            if not t.startswith("/"):  # prefer real instructions over slash commands
                last_substantive = t
    except OSError:
        pass
    return last_substantive or last_any


def parallel_sessions(own_session_id: str) -> list[dict]:
    out: list[dict] = []
    try:
        files = list(CLAUDE_SESS_DIR.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sid = d.get("sessionId")
        if not sid or sid == own_session_id:
            continue
        try:
            os.kill(int(d["pid"]), 0)  # liveness probe only (signal 0)
        except Exception:
            continue
        if time.time() - d.get("updatedAt", 0) / 1000 > 86400:
            continue  # stale registry entry attached to a recycled pid
        cwd = d.get("cwd") or ""
        label = last_user_snippet(cwd, sid) or Path(cwd or "?").name
        out.append({"busy": d.get("status") == "busy", "label": label})
    return out


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_parallel(sessions: list[dict]) -> str:
    if not sessions:
        return f"{ICON['parallel']}{LABEL['no_parallel']}"
    items = " ".join(
        (ICON["busy"] if s["busy"] else ICON["idle"]) + _trunc(s["label"], 20)
        for s in sessions[:4]
    )
    return f"{ICON['parallel']}{LABEL['parallel']}{len(sessions)}: {items}"


# --- rendering ----------------------------------------------------------------

def fmt_reset(epoch: object) -> str:
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return ""
    dt = datetime.fromtimestamp(epoch)
    if dt.date() == datetime.now().date():
        return f"{ICON['reset']}{dt:%H:%M}"
    return f"{ICON['reset']}{dt.month}/{dt.day} {dt:%H:%M}"


def fmt_limit(icon: str, label: str, block: dict | None, window_sec: float | None = None,
              profile_key: str | None = None) -> str | None:
    if not isinstance(block, dict):
        return None
    pct = block.get("used_percentage")
    if pct is None:
        return None
    suffix = quota_suffix(block, window_sec, profile_key=profile_key) if window_sec else ""
    reset = fmt_reset(block.get("resets_at"))
    return f"{icon}{label} {pct:.0f}%{suffix}{(' ' + reset) if reset else ''}"


def fmt_tok(tok: int | None) -> str:
    if tok is None:
        return "—tok"
    if tok >= 1_000_000:
        return f"{tok / 1e6:.1f}Mtok"
    if tok >= 1_000:
        return f"{tok / 1e3:.0f}ktok"
    return f"{tok}tok"


def fmt_cost_tok(usd: float | None, tok: int | None, rate: float, label: str,
                 approx: bool = False) -> str:
    sym = CFG["currency"]["symbol"]
    if usd is None:
        cost = f"{sym}—"
    else:
        amount = usd * rate
        cost = f"{sym}{amount:,.0f}" if rate != 1.0 else f"{sym}{amount:,.2f}"
        if approx:
            cost = "≈" + cost
    return f"{cost}・{fmt_tok(tok)} ({label})"


def main() -> int:
    # Standalone mode (no stdin payload): works without Claude Code, e.g. for
    # Codex CLI users — run `python3 statusline.py` directly, or keep it live
    # with `watch -n 30 python3 statusline.py` / a tmux pane.
    if sys.stdin.isatty():
        data = {}
    else:
        try:
            data = json.load(sys.stdin)
        except ValueError:
            data = {}

    cwd = (data.get("workspace") or {}).get("current_dir") or str(Path.cwd())
    dir_name = "~" if cwd == str(Path.home()) else Path(cwd).name
    line1 = [f"{ICON['prefix']} {ICON['dir']}{dir_name}"]
    branch = git_branch(cwd)
    if branch:
        dirty = git_dirty(cwd) if CFG["git_dirty"] else 0
        line1.append(f"{ICON['branch']} {branch}" + (f" Δ{dirty}" if dirty else ""))

    model = (data.get("model") or {}).get("display_name") or "?"
    line2 = [f"{ICON['model']}{model}"]
    cw = data.get("context_window") or {}
    ctx_pct = cw.get("used_percentage")
    if ctx_pct is None:
        line2.append(f"{ICON['context']}—")
    else:
        warn_pct = CFG["context_warn_percent"]
        ctx_warn = ICON["warn"] if warn_pct and ctx_pct >= warn_pct else ""
        line2.append(f"{ICON['context']}{ctx_pct:.0f}%{ctx_warn}")
    limits = data.get("rate_limits") or {}
    snapshot_quota(limits)
    for part in (
        fmt_limit(ICON["limit_5h"], "5h", limits.get("five_hour"), 5 * 3600, "five_hour"),
        fmt_limit(ICON["limit_7d"], "7d", limits.get("seven_day"), 7 * 86400, "seven_day"),
    ):
        if part:
            line2.append(part)

    lines = [SEP.join(line1), SEP.join(line2)]

    rate = fx_rate()
    if CFG["lines"]["codex"]:
        snap, usage = codex_status()
        lines.append(fmt_codex(snap, usage, rate))
    if CFG["lines"]["cost"]:
        session_usd = session_cost_after_clear(data, (data.get("cost") or {}).get("total_cost_usd"))
        session_tok = None
        if "total_input_tokens" in cw or "total_output_tokens" in cw:
            session_tok = (cw.get("total_input_tokens") or 0) + (cw.get("total_output_tokens") or 0)
        daily_usd, daily_tok = daily_usage()
        lines.append(SEP.join([
            f"{ICON['cost']}Claude " + fmt_cost_tok(session_usd, session_tok, rate, LABEL["session"]),
            fmt_cost_tok(daily_usd, daily_tok, rate, LABEL["daily"]),
        ]))
    if CFG["lines"]["parallel"]:
        lines.append(fmt_parallel(parallel_sessions(data.get("session_id") or "")))

    print(sanitize("\n".join(lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
