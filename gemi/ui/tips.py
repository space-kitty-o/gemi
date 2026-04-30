"""Rotating tips shown in the banner — one per launch.

Mirrors Claude Code's tips system: a deterministic-but-varied tip surfaces
on each session start so the user gradually learns the surface area without
being lectured. Tips are kept short and concrete.
"""
from __future__ import annotations

import random
import time

# Each tip is one short line. No periods on the end (cleaner against the dim
# styling). Group by category in source order so additions are easy.
TIPS: list[str] = [
    # Slash commands
    "Type / to see all slash commands; Tab to complete",
    "/agent <slug> switches the active model mid-session",
    "/help shows everything; /help <topic> drills in",
    "/yolo toggles all permission checks (use carefully)",
    "/plan makes the agent show a plan before executing",
    "/auto runs autopilot — non-stop autonomous work",
    "/theme cycles colour schemes",
    "/save names the current session for /resume later",
    "/export dumps the transcript as markdown",
    "/cost shows tokens, dollars, and elapsed time",
    "/model swaps the active model without leaving the REPL",

    # Keybindings
    "Ctrl+M opens the agent picker mid-prompt",
    "Ctrl+L clears the screen but keeps your prompt",
    "Ctrl+Y toggles YOLO mode",
    "Ctrl+C cancels the current generation, not the session",
    "Up/Down navigates prompt history",
    "Shift+Enter adds a newline; Enter submits",

    # Power-user moves
    "Prefix any command with `!` to run it in the shell directly",
    "Prefix with `#` to add a note to the conversation only",
    "Drop a file path into the prompt — Gemi reads it for you",
    "Pipe stdin in with `gemi -e \"...\"` for one-shot runs",
    "Use --resume to pick up the last session exactly where you left off",
    "Set GEMI_COMPACT_RENDER=1 for Claude-Code-style ⏺/⎿ tool output",

    # Tools
    "todo_write tracks multi-step work with a live ✔/◼/◻ checklist",
    "Run `gemi --list-tools` to see all 100+ built-in tools by tier",
    "MCP servers auto-load from ~/.gemi/mcp.json — see SECURITY.md",
    "Hooks run before/after every tool call — configure in ~/.gemi/hooks.json",
    "Free APIs (weather, hn_top, crypto_price, wiki…) need zero keys",

    # Performance
    "Small-context agents (≤12K) auto-trim to ESSENTIAL_TOOLS",
    "/compact shrinks history when you hit the context wall",
    "/stop kills any background tool that hangs",
]


def pick_tip(seed: int | None = None) -> str:
    """Return one tip. Seeded by the wall clock so each launch shows a
    different one, but consecutive instant launches stay stable for an hour."""
    if seed is None:
        seed = int(time.time() // 3600)
    rng = random.Random(seed)
    return rng.choice(TIPS)
