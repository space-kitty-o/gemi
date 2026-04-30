"""Banner — Buddy startup splash. Claude-Code-style minimal."""
from __future__ import annotations

import subprocess

from rich.console import Console
from rich.text import Text

from ..config import AgentDef
from .theme import get_palette, get_active_theme_name


def _git_branch(workspace) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(workspace), capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def print_banner(
    console: Console,
    agent: AgentDef | None,
    version: str = "0.1.0",
    workspace=None,
) -> None:
    """Minimal startup banner.

    Inspired by Claude Code's compact intro: brand glyph, short tagline,
    one-line agent + workspace summary. No giant ASCII art, no big info table.
    """
    palette = get_palette(get_active_theme_name())
    try:
        from .. import __version__
        version = __version__
    except Exception:
        pass

    console.print()
    line1 = Text()
    line1.append("  ✻ ", style=f"bold {palette.buddy}")
    line1.append("Gemi", style=f"bold {palette.buddy}")
    line1.append(f"  v{version}", style="muted")
    if agent:
        line1.append("  ·  ", style="dim")
        line1.append(agent.short_model, style=palette.text)
        line1.append("  ", style="dim")
        line1.append(agent.quant or "", style=palette.warning)
    console.print(line1)

    if agent:
        line2 = Text("  ")
        proxy_running = agent.is_proxy_running()
        if proxy_running:
            line2.append("●", style=palette.success)
            line2.append(" READY  ", style="muted")
        else:
            line2.append("○", style=palette.error)
            line2.append(" OFFLINE  ", style=f"bold {palette.error}")
        line2.append(f"{agent.slug}", style=palette.info)
        line2.append(f" · {agent.proxy_url}", style="dim")
        console.print(line2)

    if workspace:
        ws = Text("  ")
        ws_str = str(workspace)
        if len(ws_str) > 70:
            ws_str = "…" + ws_str[-68:]
        ws.append("📂  ", style="dim")
        ws.append(ws_str, style="muted")
        branch = _git_branch(workspace)
        if branch:
            ws.append(f"  ⎇ {branch}", style=f"bold {palette.buddy_shimmer}")
        console.print(ws)

    # Rotating tip — Claude-Code-style. One pulled per session-hour so it
    # changes session-to-session but stays stable inside a single session.
    try:
        from .tips import pick_tip
        tip = pick_tip()
        tip_line = Text("  ")
        tip_line.append("✱ ", style=f"bold {palette.buddy_shimmer}")
        tip_line.append("Tip: ", style=f"bold {palette.text_muted}")
        tip_line.append(tip, style="dim")
        console.print(tip_line)
    except Exception:
        pass

    hint = Text()
    hint.append("\n  ")
    hint.append("/help", style=f"bold {palette.buddy}")
    hint.append(" for commands  ·  ", style="dim")
    hint.append("!cmd", style=f"bold {palette.buddy_shimmer}")
    hint.append(" runs shell  ·  ", style="dim")
    hint.append("Ctrl+M", style=f"bold {palette.buddy_shimmer}")
    hint.append(" picks agent", style="dim")
    console.print(hint)
    console.print()
