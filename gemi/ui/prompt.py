"""Powerline-style prompt renderer for the Buddy REPL.

Claude-Code-style minimal: a single-line `>` cursor with subtle agent
indicator, plus a clean bottom toolbar.
"""
from __future__ import annotations

from typing import Any

from prompt_toolkit.formatted_text import ANSI

from . import glyphs
from .theme import get_palette, get_active_theme_name


def _ansi_rgb(rgb: str, text: str, bold: bool = False) -> str:
    """Apply RGB color via ANSI escape codes for prompt-toolkit."""
    if rgb.startswith("rgb("):
        r, g, b = [int(x) for x in rgb[4:-1].split(",")]
        prefix = "\033[1;" if bold else "\033["
        return f"{prefix}38;2;{r};{g};{b}m{text}\033[0m"
    color_map = {
        "magenta": "35", "bright_magenta": "95",
        "blue": "34", "bright_blue": "94",
        "cyan": "36", "bright_cyan": "96",
        "green": "32", "bright_green": "92",
        "yellow": "33", "bright_yellow": "93",
        "red": "31", "bright_red": "91",
        "white": "37", "bright_white": "97",
        "black": "30", "bright_black": "90",
    }
    code = color_map.get(rgb, "37")
    return f"\033[{'1;' if bold else ''}{code}m{text}\033[0m"


def build_prompt_lines(
    agent_slug: str,
    proxy_running: bool,
    mode_label: str,
    turn_count: int,
    context_pct: float,
    cost_usd: float = 0.0,
    cache_hit_rate: float = 0.0,
    width: int = 80,
) -> ANSI:
    """Build the REPL prompt — minimal Claude-Code style.

    Single line: `>` cursor with optional mode badge.
    All metadata (agent, ctx %, cost, etc.) is pushed to the bottom_toolbar
    so the input line stays clean.
    """
    p = get_palette(get_active_theme_name())
    parts: list[str] = []

    # Mode badge (only shown if modes are active)
    if mode_label:
        if "YOLO" in mode_label:
            parts.append(_ansi_rgb(p.yolo, "YOLO ", bold=True))
        if "PLAN" in mode_label:
            parts.append(_ansi_rgb(p.plan, "PLAN ", bold=True))
        if "AUTO" in mode_label:
            parts.append(_ansi_rgb(p.auto, "AUTO ", bold=True))

    # The cursor itself
    parts.append(_ansi_rgb(p.buddy_shimmer, "> ", bold=True))

    return ANSI("".join(parts))


def build_compact_prompt(agent_slug: str, mode: str, turn: int) -> ANSI:
    """Even more minimal fallback prompt."""
    p = get_palette(get_active_theme_name())
    return ANSI(_ansi_rgb(p.buddy_shimmer, "> ", bold=True))


def build_bottom_toolbar(
    agent_slug: str,
    quant: str,
    proxy_running: bool,
    mode_label: str,
    context_pct: float,
    turn_count: int,
    cost_usd: float = 0.0,
    cache_hits: int = 0,
    cache_total: int = 0,
    cwd_basename: str = "",
    model_display: str = "",
) -> str:
    """Render the bottom toolbar — clean, minimal status strip.

    Order (Claude-Code-style left-to-right): status dot · agent · model ·
    cwd · mode · ctx% · turn · cost. Earlier truncated when the strip
    overflows so the most-load-bearing fields stay visible.
    """
    p = get_palette(get_active_theme_name())
    parts = []

    # Status dot
    if proxy_running:
        parts.append(_ansi_rgb(p.success, "● "))
    else:
        parts.append(_ansi_rgb(p.error, "○ "))

    # Agent slug
    parts.append(_ansi_rgb(p.info, agent_slug))

    # Model display name (compact, dim) — shown after agent slug
    if model_display:
        parts.append(_ansi_rgb(p.text_subtle, " "))
        parts.append(_ansi_rgb(p.text_muted, model_display))

    # Working directory basename — Claude Code shows this so you know
    # which project the agent will edit. Full path lives in the welcome.
    if cwd_basename:
        parts.append(_ansi_rgb(p.text_subtle, " · "))
        parts.append(_ansi_rgb(p.text_muted, f"⌂ {cwd_basename}"))

    # Mode (only if active)
    if mode_label:
        parts.append(_ansi_rgb(p.text_subtle, " · "))
        if "YOLO" in mode_label:
            parts.append(_ansi_rgb(p.yolo, "YOLO"))
        elif "PLAN" in mode_label:
            parts.append(_ansi_rgb(p.plan, "PLAN"))
        elif "AUTO" in mode_label:
            parts.append(_ansi_rgb(p.auto, "AUTO"))

    # Context %
    if context_pct > 0:
        parts.append(_ansi_rgb(p.text_subtle, "  "))
        if context_pct < 50:
            ctx_color = p.text_muted
        elif context_pct < 75:
            ctx_color = p.warning
        else:
            ctx_color = p.error
        parts.append(_ansi_rgb(ctx_color, f"ctx {int(context_pct)}%"))

    # Turn counter
    if turn_count > 0:
        parts.append(_ansi_rgb(p.text_subtle, "  "))
        parts.append(_ansi_rgb(p.text_muted, f"t{turn_count}"))

    # Cost (only if it's accumulated)
    if cost_usd > 0.0001:
        parts.append(_ansi_rgb(p.text_subtle, "  "))
        parts.append(_ansi_rgb(p.cost_low, f"${cost_usd:.3f}"))

    return "".join(parts)
