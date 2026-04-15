#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""
Status Line for Claude Code.

Format:
  [Opus 4.6] git-branch-name | [████░░░░] 6% ~14 left | In:64k Out:14k | Cache:85% | ⏱ 25m | 5h:8% · 7d:89% ~14p

Segments:
  1. Model name
  2. Git branch name
  3. Context bar + % + estimated prompts left
  4. In/Out tokens
  5. Cache hit %
  6. Session duration
  7. Rate limits (5h / 7d) with color + estimated prompts remaining

Data sources:
  - stdin JSON from Claude Code (model, context, tokens, cost, rate_limits)
  - .claude/data/sessions/{session_id}.json (created_at for duration)
  - ~/.claude/data/rate_history.jsonl (rate limit history for prompt estimates)
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BRIGHT_RED = "\033[91m"
BRIGHT_MAGENTA = "\033[95m"
DIM = "\033[90m"
BRIGHT_WHITE = "\033[97m"
BRIGHT_CYAN = "\033[96m"
RESET = "\033[0m"

FILLED = "\u2588"  # █
EMPTY = "\u2591"   # ░

# ---------------------------------------------------------------------------
# Context thresholds — absolute tokens (not % of window)
# ---------------------------------------------------------------------------
# Based on model degradation research: quality drops depend on absolute
# token count, not on how full the effective window is. This keeps color
# signals meaningful when users cap CLAUDE_CODE_AUTO_COMPACT_WINDOW below
# the native context size.
#
# Green: <200K tokens — safe, no degradation
# Yellow: 200K–400K tokens — some degradation on complex tasks
# Red: 400K+ tokens — significant degradation, consider /clear

CTX_YELLOW_TOKENS = 200_000
CTX_RED_TOKENS = 400_000

# Rate limit pacing thresholds (difference between used% and expected%)
# expected% = (elapsed_time / total_window) * 100
# difference = used% - expected%
RATE_PACE_YELLOW = 0   # any amount ahead of schedule → yellow
RATE_PACE_RED = 20     # >20% ahead of schedule → red


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def ctx_color(tokens: int) -> str:
    """Color context bar by absolute token count (not %), since model
    degradation thresholds depend on tokens, not on effective window size."""
    if tokens < CTX_YELLOW_TOKENS:
        return GREEN
    elif tokens < CTX_RED_TOKENS:
        return YELLOW
    return RED


def rate_color_paced(used_pct: float, resets_at: float = 0, window_hours: float = 5) -> str:
    """Color based on pacing — are you burning faster than sustainable?

    Compares actual usage to expected linear usage based on elapsed time.
    Green = on track or under budget. Yellow = slightly ahead. Red = burning fast.
    At 100% the limit is exhausted — always red regardless of pacing.
    """
    if used_pct >= 100:
        return RED

    if resets_at <= 0:
        # No reset data — fall back to simple thresholds
        if used_pct < 50:
            return GREEN
        elif used_pct < 80:
            return YELLOW
        return RED

    now = datetime.now(timezone.utc).timestamp()
    time_remaining = resets_at - now
    if time_remaining <= 0:
        return GREEN  # about to reset

    total_window = window_hours * 3600
    elapsed = total_window - time_remaining
    if elapsed <= 0:
        elapsed = 60  # just started

    expected_pct = (elapsed / total_window) * 100
    difference = used_pct - expected_pct

    if difference <= RATE_PACE_YELLOW:
        return GREEN
    elif difference <= RATE_PACE_RED:
        return YELLOW
    return RED


def rate_bar(pct: float, color: str, width: int = 10) -> str:
    """Short progress bar for rate limits."""
    filled = int((pct / 100) * width)
    if pct > 0 and filled == 0:
        filled = 1  # show at least 1 block when not zero
    empty = width - filled
    return f"{color}{FILLED * filled}{DIM}{EMPTY * empty}{RESET}"


def fmt_tokens(tokens: int) -> str:
    if not tokens:
        return "0"
    if tokens < 1000:
        return str(tokens)
    elif tokens < 10000:
        return f"{tokens / 1000:.1f}k"
    elif tokens < 1000000:
        return f"{tokens / 1000:.0f}k"
    else:
        return f"{tokens / 1000000:.1f}M"


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m"
    elif seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m:02d}m" if m else f"{h}h"
    else:
        d = int(seconds // 86400)
        h = int((seconds % 86400) // 3600)
        return f"{d}d{h}h" if h else f"{d}d"


def progress_bar(pct: float, tokens: int, width: int = 10) -> str:
    """Draw a progress bar. Width is based on % fill of the effective window;
    color is based on absolute token count (degradation thresholds)."""
    filled = int((pct / 100) * width)
    empty = width - filled
    color = ctx_color(tokens)
    return f"{color}{FILLED * filled}{DIM}{EMPTY * empty}{RESET}"


# ---------------------------------------------------------------------------
# Settings / env lookup
# ---------------------------------------------------------------------------

_SETTINGS_ENV_CACHE: dict | None = None


def _settings_env() -> dict:
    """Load env block from ~/.claude/settings.json (cached per process)."""
    global _SETTINGS_ENV_CACHE
    if _SETTINGS_ENV_CACHE is not None:
        return _SETTINGS_ENV_CACHE
    try:
        settings_file = Path.home() / ".claude" / "settings.json"
        if settings_file.exists():
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            _SETTINGS_ENV_CACHE = data.get("env", {}) or {}
            return _SETTINGS_ENV_CACHE
    except Exception:
        pass
    _SETTINGS_ENV_CACHE = {}
    return _SETTINGS_ENV_CACHE


def _get_env(key: str) -> str | None:
    """Read an env var from process env, falling back to settings.json env block."""
    val = os.environ.get(key)
    if val:
        return val
    val = _settings_env().get(key)
    return val if val else None


# ---------------------------------------------------------------------------
# Session duration
# ---------------------------------------------------------------------------

def _find_session_file(session_id: str, workspace_dir: str | None = None) -> Path | None:
    """Find session file, checking CWD and workspace_dir."""
    candidates = [Path(".claude/data/sessions") / f"{session_id}.json"]
    if workspace_dir:
        candidates.append(Path(workspace_dir) / ".claude" / "data" / "sessions" / f"{session_id}.json")
    # Also check home dir as fallback
    candidates.append(Path.home() / ".claude" / "data" / "sessions" / f"{session_id}.json")
    for p in candidates:
        if p.exists():
            return p
    return None


def get_session_duration(session_id: str, workspace_dir: str | None = None) -> float | None:
    session_file = _find_session_file(session_id, workspace_dir)
    if not session_file:
        return None
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
        created_at = data.get("created_at")
        if created_at:
            start = datetime.fromisoformat(created_at)
            now = datetime.now(timezone.utc)
            return (now - start).total_seconds()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Rate limit prompt estimation
# ---------------------------------------------------------------------------

RATE_HISTORY_FILE = Path.home() / ".claude" / "data" / "rate_history.jsonl"


def record_rate_snapshot(data: dict, prompt_count: int | None) -> None:
    """Append rate limits to history, once per prompt (keyed by prompt_count)."""
    rate_limits = data.get("rate_limits")
    if not rate_limits or not prompt_count:
        return

    five_hour = rate_limits.get("five_hour", {})
    seven_day = rate_limits.get("seven_day", {})
    session_id = data.get("session_id", "")

    entry = {
        "ts": datetime.now().astimezone().isoformat(),
        "sid": session_id,
        "pc": prompt_count,
        "5h_pct": five_hour.get("used_percentage"),
        "7d_pct": seven_day.get("used_percentage"),
    }

    RATE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Check if we already recorded this prompt_count for this session
    try:
        if RATE_HISTORY_FILE.exists():
            lines = RATE_HISTORY_FILE.read_text(encoding="utf-8").strip().split("\n")
            # Check last 20 lines for duplicate (multiple sessions interleave)
            for line in lines[-20:]:
                try:
                    prev = json.loads(line)
                    if prev.get("sid") == session_id and prev.get("pc") == prompt_count:
                        return  # already recorded
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass

    try:
        with open(RATE_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_rate_entries() -> list[dict] | None:
    """Load parsed entries from rate_history.jsonl."""
    if not RATE_HISTORY_FILE.exists():
        return None
    try:
        lines = RATE_HISTORY_FILE.read_text(encoding="utf-8").strip().split("\n")
        if len(lines) < 2:
            return None
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries if len(entries) >= 2 else None
    except Exception:
        return None


def estimate_remaining_prompts(current_pct: float, key: str = "5h_pct") -> int | None:
    """Estimate remaining prompts for a given rate limit.

    Groups entries by session, calculates per-session delta (last - first pct)
    and prompt count, then averages across all sessions. This avoids
    cross-session interleaving and outlier issues.
    """
    entries = _load_rate_entries()
    if not entries:
        return None

    try:
        # Group by session
        sessions: dict[str, list[dict]] = {}
        for e in entries:
            sid = e.get("sid", "")
            if sid and e.get(key) is not None:
                sessions.setdefault(sid, []).append(e)

        total_delta = 0.0
        total_prompts = 0

        for sid, sess_entries in sessions.items():
            if len(sess_entries) < 2:
                continue
            first_pct = sess_entries[0].get(key, 0)
            last_pct = sess_entries[-1].get(key, 0)
            delta = last_pct - first_pct
            if delta <= 0:
                continue  # skip sessions with no consumption or resets
            n_prompts = len(sess_entries) - 1  # deltas = entries - 1
            total_delta += delta
            total_prompts += n_prompts

        if total_prompts == 0 or total_delta <= 0:
            return None

        avg_per_prompt = total_delta / total_prompts
        remaining_pct = 100.0 - current_pct
        return max(0, int(remaining_pct / avg_per_prompt))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Build status line
# ---------------------------------------------------------------------------

def generate(data: dict) -> str:
    parts = []

    # 1. Model + reasoning effort
    model = (data.get("model") or {}).get("display_name", "Claude")
    # Clean up verbose display name: "Opus 4.6 (1M context)" → "Opus 4.6"
    if "(" in model:
        model = model[:model.index("(")].strip()
    # Reasoning effort: prefer CLAUDE_CODE_EFFORT_LEVEL (env var or settings.json env
    # block); fall back to legacy top-level "effortLevel" key in settings.json.
    effort = _get_env("CLAUDE_CODE_EFFORT_LEVEL")
    if not effort:
        try:
            settings_file = Path.home() / ".claude" / "settings.json"
            if settings_file.exists():
                effort = json.loads(settings_file.read_text(encoding="utf-8")).get("effortLevel")
        except Exception:
            pass
    # Model color: more powerful = more aggressive
    model_lower = model.lower()
    if "opus" in model_lower:
        mc = BRIGHT_MAGENTA
    elif "sonnet" in model_lower:
        mc = YELLOW
    elif "haiku" in model_lower:
        mc = DIM
    else:
        mc = BRIGHT_WHITE

    model_str = f"{mc}[{model}"
    if effort:
        model_str += f" \u00b7 {effort}"
    model_str += f"]{RESET}"

    # Branch name from workspace dir
    ws = data.get("workspace") or {}
    cwd = ws.get("current_dir") or ws.get("project_dir") or ""
    if cwd:
        try:
            branch = subprocess.run(
                ["git", "-C", cwd, "--no-optional-locks", "symbolic-ref", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            if branch:
                BRANCH_ICONS = {
                    "feat": ("\U0001F680", CYAN),       # 🚀
                    "fix": ("\U0001F41B", YELLOW),       # 🐛
                    "chore": ("\U0001F527", DIM),        # 🔧
                    "refactor": ("\u267B\uFE0F", BRIGHT_MAGENTA),  # ♻️
                    "docs": ("\U0001F4DD", DIM),         # 📝
                }
                if branch in ("main", "master"):
                    model_str += f" \U0001F451 {DIM}{branch}{RESET}"  # 👑
                else:
                    prefix = branch.split("/")[0] if "/" in branch else ""
                    icon, color = BRANCH_ICONS.get(prefix, ("\U0001F4CC", BRIGHT_WHITE))  # 📌
                    short = branch.split("/", 1)[1] if "/" in branch else branch
                    model_str += f" {icon} {color}{short}{RESET}"
        except Exception:
            pass

    parts.append(model_str)

    # 2. Context bar + % + estimated turns left
    ctx = data.get("context_window") or {}
    used_pct = ctx.get("used_percentage", 0) or 0

    # Context window size from stdin (typically 1M for Opus 4.6 1M context)
    window_size_raw = ctx.get("context_window_size", 1000000) or 1000000

    # Current token usage (reused below for turns_left and cache stats)
    current = ctx.get("current_usage") or {}
    total_in = (
        (current.get("input_tokens", 0) or 0)
        + (current.get("cache_creation_input_tokens", 0) or 0)
        + (current.get("cache_read_input_tokens", 0) or 0)
    )

    # Override with auto-compact window if user has set it lower than the native
    # context (CLAUDE_CODE_AUTO_COMPACT_WINDOW triggers compaction earlier).
    # Backward compat: if the env var is not set, stdin values are used as-is.
    override_raw = _get_env("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if override_raw:
        try:
            override_size = int(override_raw)
            if 0 < override_size < window_size_raw:
                window_size_raw = override_size
                # Recalculate percentage against the effective (smaller) window
                if total_in > 0:
                    used_pct = min(100.0, (total_in / window_size_raw) * 100)
        except ValueError:
            pass

    bar = progress_bar(used_pct, total_in)
    color = ctx_color(total_in)

    # Context window size label (1000000 → "1M", 400000 → "400K")
    if window_size_raw >= 1000000:
        ctx_label = f"{window_size_raw // 1000000}M"
    else:
        ctx_label = f"{window_size_raw // 1000}K"
    ctx_str = f"{BRIGHT_WHITE}{ctx_label}{RESET} {bar} {color}{used_pct:.0f}%{RESET}"

    window_size = window_size_raw

    # Get prompt count from session file
    session_id = data.get("session_id", "") or ""
    ws = data.get("workspace") or {}
    workspace_dir = ws.get("project_dir") or ws.get("current_dir") if isinstance(ws, dict) else ws
    prompt_count = None
    sf = _find_session_file(session_id, workspace_dir)
    if sf:
        try:
            sd = json.loads(sf.read_text(encoding="utf-8"))
            prompt_count = sd.get("prompt_count")
        except Exception:
            pass

    # Record rate limits snapshot (once per prompt)
    record_rate_snapshot(data, prompt_count)

    if prompt_count and prompt_count > 1 and total_in > 0:
        avg_per_turn = total_in / prompt_count
        remaining_tokens = window_size - total_in
        if avg_per_turn > 0 and remaining_tokens > 0:
            turns_left = int(remaining_tokens / avg_per_turn)
            ctx_str += f" {BRIGHT_WHITE}~{turns_left}p{RESET}"

    parts.append(ctx_str)

    # 3. Cache hit %
    cache_read = current.get("cache_read_input_tokens", 0) or 0
    cache_write = current.get("cache_creation_input_tokens", 0) or 0
    plain_input = current.get("input_tokens", 0) or 0
    cache_total = plain_input + cache_write + cache_read
    if cache_total > 0 and cache_read > 0:
        hit_pct = (cache_read / cache_total) * 100
        if hit_pct >= 80:
            cc = GREEN
        elif hit_pct >= 50:
            cc = YELLOW
        else:
            cc = RED
        parts.append(f"{BRIGHT_WHITE}Cache:{RESET}{cc}{hit_pct:.0f}%{RESET}")

    # 5. Session duration
    if session_id:
        dur = get_session_duration(session_id, workspace_dir)
        if dur is not None:
            parts.append(f"{BRIGHT_WHITE}\u23f1 {fmt_duration(dur)}{RESET}")

    # 7. Rate limits
    rate_limits = data.get("rate_limits") or {}
    five_hour = rate_limits.get("five_hour", {})
    seven_day = rate_limits.get("seven_day", {})
    fh_pct = five_hour.get("used_percentage")
    sd_pct = seven_day.get("used_percentage")

    if fh_pct is not None or sd_pct is not None:
        lim_parts = []

        if fh_pct is not None:
            fh_resets = five_hour.get("resets_at", 0)
            fc = rate_color_paced(fh_pct, fh_resets, window_hours=5)
            bar = rate_bar(fh_pct, fc)
            lim_str = f"{BRIGHT_WHITE}5h{RESET} {bar} {fc}{fh_pct:.0f}%{RESET}"
            if fc != GREEN:
                est = estimate_remaining_prompts(fh_pct, "5h_pct")
                if est is not None:
                    lim_str += f" {BRIGHT_WHITE}~{est}p{RESET}"
            if fh_resets:
                reset_in = fh_resets - datetime.now(timezone.utc).timestamp()
                if reset_in > 0:
                    lim_str += f" {CYAN}\u21bb{fmt_duration(reset_in)}{RESET}"
            lim_parts.append(lim_str)

        if sd_pct is not None:
            sd_resets = seven_day.get("resets_at", 0)
            sc = rate_color_paced(sd_pct, sd_resets, window_hours=168)
            bar = rate_bar(sd_pct, sc)
            lim_str = f"{BRIGHT_WHITE}7d{RESET} {bar} {sc}{sd_pct:.0f}%{RESET}"
            if sc != GREEN:
                est = estimate_remaining_prompts(sd_pct, "7d_pct")
                if est is not None:
                    lim_str += f" {BRIGHT_WHITE}~{est}p{RESET}"
            if sd_resets:
                reset_in = sd_resets - datetime.now(timezone.utc).timestamp()
                if reset_in > 0:
                    lim_str += f" {CYAN}\u21bb{fmt_duration(reset_in)}{RESET}"
            lim_parts.append(lim_str)

        parts.append(" \u00b7 ".join(lim_parts))

    return " | ".join(parts)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
        print(generate(data))
    except Exception as e:
        print(f"{RED}[Error] {e}{RESET}")


if __name__ == "__main__":
    main()
