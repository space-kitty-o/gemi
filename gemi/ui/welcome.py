"""Welcome screen — shown when launching without an agent or with /welcome.

Claude-Code-style: tight title row, gradient logo, dense info block, rotating
tip. No big bordered Panel; everything aligns to a left gutter so the screen
feels less "form" and more "terminal".
"""
from __future__ import annotations

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from .banner import _git_branch
from .theme import get_palette, get_active_theme_name
from . import glyphs
from .tips import pick_tip


def print_welcome(console: Console, workspace=None) -> None:
    from ..config import FLEET
    from ..tools.registry import ALL_TOOLS
    from .. import __version__

    p = get_palette(get_active_theme_name())

    # ── Title row ─────────────────────────────────────────────
    title = Text()
    title.append("  ")
    title.append(glyphs.BUDDY_GLYPH, style=f"bold {p.buddy_shimmer}")
    title.append("  ")
    title.append("Welcome to Gemi", style=f"bold {p.buddy}")
    title.append(f"  v{__version__}", style="dim")
    console.print()
    console.print(title)

    tagline = Text()
    tagline.append("  ")
    tagline.append("Claude-Code-style CLI for your own local LLM fleet", style="dim")
    console.print(tagline)
    console.print()

    # ── Stats grid ────────────────────────────────────────────
    running = sum(1 for a in FLEET if a.is_proxy_running())
    safe = sum(1 for t in ALL_TOOLS if t.read_only)
    write = sum(1 for t in ALL_TOOLS if not t.read_only and not t.dangerous)
    yolo = sum(1 for t in ALL_TOOLS if t.dangerous)

    stats = Table.grid(padding=(0, 1))
    stats.add_column(justify="right", style=f"bold {p.buddy}")
    stats.add_column(style=p.text_muted)
    stats.add_row(glyphs.BUDDY_GLYPH,
                  f"[{p.text}]{len(FLEET)} agents in fleet[/]"
                  f" · "
                  f"[bold {p.success}]{running}[/] online")
    stats.add_row(glyphs.DIAMOND_OPEN,
                  f"[{p.text}]{len(ALL_TOOLS)} tools[/]"
                  f"  ·  "
                  f"[{p.tier_safe}]{safe} SAFE[/] / "
                  f"[{p.tier_write}]{write} WRITE[/] / "
                  f"[{p.tier_yolo}]{yolo} YOLO[/]")
    if workspace is not None:
        ws = str(workspace)
        if len(ws) > 56:
            ws = "…" + ws[-54:]
        branch = _git_branch(workspace) if workspace else ""
        ws_text = f"[{p.text}]{ws}[/]"
        if branch:
            ws_text += f"  [bold {p.buddy_shimmer}]⎇ {branch}[/]"
        stats.add_row("⌂", ws_text)
    console.print(stats)
    console.print()

    # ── Quick start ───────────────────────────────────────────
    qs_header = Text()
    qs_header.append("  ")
    qs_header.append("Quick start", style=f"bold {p.text}")
    console.print(qs_header)

    qs = Table.grid(padding=(0, 2))
    qs.add_column(style=f"bold {p.buddy_shimmer}", no_wrap=True, justify="right")
    qs.add_column(style=p.text_muted)
    qs.add_row(" gemi --status", "show fleet status")
    qs.add_row(" gemi -a <slug>", "launch with a specific agent")
    qs.add_row(" gemi --yolo", "enable dangerous tools")
    qs.add_row(" /help", "see all commands")
    qs.add_row(" /agent", "switch active agent")
    qs.add_row(" /theme", "change color scheme")
    console.print(qs)
    console.print()

    # ── Tip ───────────────────────────────────────────────────
    tip = pick_tip()
    tip_line = Text("  ")
    tip_line.append("✱ ", style=f"bold {p.buddy_shimmer}")
    tip_line.append("Tip: ", style=f"bold {p.text_muted}")
    tip_line.append(tip, style="dim")
    console.print(tip_line)
    console.print()
