#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""
Status Line for Claude Code.

Format:
  [Opus 4.6] | [████░░░░] 6% ~14 left | In:64k Out:14k | Cache:85% | ⏱ 25m | 5h:8% · 7d:89% ~14p

Segments:
  1. Model name
  2. Context bar + % + estimated prompts left
  3. In/Out tokens
  4. Cache hit %
  5. Session duration
  6. Rate limits (5h / 7d) with color + estimated prompts remaining

Data sources:
  - stdin JSON from Claude Code (model, context, tokens, cost, rate_limits)
  - .claude/data/sessions/{session_id}.json (created_at for duration)
  - ~/.claude/data/rate_history.jsonl (rate limit history for prompt estimates)
"""

import json
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
# Context thresholds (based on degradation research for complex tasks)
# ---------------------------------------------------------------------------
# Green: 0-20% (0-200K) — safe, no degradation
# Yellow: 20-40% (200K-400K) — some degradation on complex tasks
# Red: 40%+ (400K+) — significant degradation, consider /clear

CTX_YELLOW = 20
CTX_RED = 40

# Rate limit thresholds
RATE_YELLOW = 50
RATE_RED = 80


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def ctx_color(pct: float) -> str:
    if pct < CTX_YELLOW:
        return GREEN
    elif pct < CTX_RED:
        return YELLOW
    return RED


def rate_color(pct: float) -> str:
    if pct < RATE_YELLOW:
        return GREEN
    elif pct < RATE_RED:
        return YELLOW
    return RED


def rate_bar(pct: float, width: int = 10) -> str:
    """Short progress bar for rate limits."""
    filled = int((pct / 100) * width)
    if pct > 0 and filled == 0:
        filled = 1  # show at least 1 block when not zero
    empty = width - filled
    color = rate_color(pct)
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


def progress_bar(pct: float, width: int = 10) -> str:
    filled = int((pct / 100) * width)
    empty = width - filled
    color = ctx_color(pct)
    return f"{color}{FILLED * filled}{DIM}{EMPTY * empty}{RESET}"


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
    effort = None
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
    parts.append(model_str)

    # 2. Context bar + % + estimated turns left
    ctx = data.get("context_window") or {}
    used_pct = ctx.get("used_percentage", 0) or 0
    bar = progress_bar(used_pct)
    color = ctx_color(used_pct)

    # Context window size label (1000000 → "1M")
    window_size_raw = ctx.get("context_window_size", 1000000) or 1000000
    if window_size_raw >= 1000000:
        ctx_label = f"{window_size_raw // 1000000}M"
    else:
        ctx_label = f"{window_size_raw // 1000}K"
    ctx_str = f"{BRIGHT_WHITE}{ctx_label}{RESET} {bar} {color}{used_pct:.0f}%{RESET}"

    # Estimate context turns left (rough: based on avg input per turn)
    current = ctx.get("current_usage") or {}
    total_in = (
        (current.get("input_tokens", 0) or 0)
        + (current.get("cache_creation_input_tokens", 0) or 0)
        + (current.get("cache_read_input_tokens", 0) or 0)
    )
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
            fc = rate_color(fh_pct)
            bar = rate_bar(fh_pct)
            lim_str = f"{BRIGHT_WHITE}5h{RESET} {bar} {fc}{fh_pct:.0f}%{RESET}"
            if fh_pct >= RATE_YELLOW:
                est = estimate_remaining_prompts(fh_pct, "5h_pct")
                if est is not None:
                    lim_str += f" {BRIGHT_WHITE}~{est}p{RESET}"
            resets_at = five_hour.get("resets_at")
            if resets_at:
                reset_in = resets_at - datetime.now(timezone.utc).timestamp()
                if reset_in > 0:
                    lim_str += f" {CYAN}\u21bb{fmt_duration(reset_in)}{RESET}"
            lim_parts.append(lim_str)

        if sd_pct is not None:
            sc = rate_color(sd_pct)
            bar = rate_bar(sd_pct)
            lim_str = f"{BRIGHT_WHITE}7d{RESET} {bar} {sc}{sd_pct:.0f}%{RESET}"
            if sd_pct >= RATE_YELLOW:
                est = estimate_remaining_prompts(sd_pct, "7d_pct")
                if est is not None:
                    lim_str += f" {BRIGHT_WHITE}~{est}p{RESET}"
            resets_at = seven_day.get("resets_at")
            if resets_at:
                reset_in = resets_at - datetime.now(timezone.utc).timestamp()
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
