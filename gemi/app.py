"""Main REPL application — the Buddy terminal experience."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, WordCompleter, merge_completers
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from .commands.registry import COMMANDS, handle_command, is_command
from .config import FLEET, AgentDef, get_agent, get_running_agents
from .provider import ProviderError
from .query_engine import QueryEngine
from .session import save_session, load_session
from .ui.banner import print_banner
from .ui.render import render_assistant, render_fleet_table, render_tool_call, render_tool_result
from .ui.theme import GEMI_THEME, get_palette, get_active_theme_name, reload_theme
from .ui.prompt import build_prompt_lines, build_compact_prompt, build_bottom_toolbar
from .ui.picker import show_picker, render_agent_menu
from .ui import glyphs
from . import hooks as hooks_module
from . import logger as logger_module
from . import plugins as plugins_module
from . import mcp as mcp_module
from . import image_input
from . import background as background_module
from . import profiles as profiles_module
from .completion import SmartCompleter
from .workspace_context import discover_context, summarize_context, WorkspaceContext

SYSTEM_PROMPT_TEMPLATE = """You are a coding assistant. Workspace: {workspace}

Read code before modifying it. Make surgical, minimal changes. Verify your work. Fix root causes, not symptoms. Keep responses short and direct."""

PLAN_PROMPT_SUFFIX = """

IMPORTANT: You are in PLAN MODE. Before executing any actions:
1. Analyze the request thoroughly
2. Output a numbered plan of exactly what you will do (files to read, edits to make, commands to run)
3. Wait for the user to approve with "go", "yes", or "ok" before executing
4. Do NOT use any tools until the user approves your plan
If the user says "go" or approves, execute the plan step by step."""

AUTOPILOT_PROMPT_SUFFIX = """

IMPORTANT: You are in AUTOPILOT MODE. Work autonomously without stopping:
- Execute your full plan without waiting for user confirmation
- After completing one task, check if there's more work to do
- If you encounter an error, try to fix it yourself before stopping
- Keep working until the task is fully complete
- Report what you did at the end"""

class _PathCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            return
        words = text.split()
        if not words:
            return
        word = words[-1]
        if len(word) < 2 or word.startswith("-"):
            return
        try:
            from pathlib import Path as _P
            base = _P(word)
            if base.is_dir():
                parent = base
                prefix = ""
            else:
                parent = base.parent
                prefix = base.name
            if not parent.exists():
                return
            for child in sorted(parent.iterdir()):
                name = child.name
                if name.startswith("."):
                    continue
                if prefix and not name.lower().startswith(prefix.lower()):
                    continue
                suffix = str(parent / name) if str(parent) != "." else name
                if child.is_dir():
                    suffix += "/"
                yield Completion(suffix, start_position=-len(word))
        except Exception:
            return


PT_STYLE = PTStyle.from_dict({
    "prompt": "bold ansiwhite",
    "": "ansiwhite",
})


class BuddyApp:
    def __init__(
        self,
        agent: AgentDef | None = None,
        workspace: Path | None = None,
        yolo: bool = False,
        plan_mode: bool = False,
        autopilot: bool = False,
    ):
        # Reload theme from active config (lets /theme persist across sessions)
        reload_theme()
        from .ui.theme import GEMI_THEME as _CURRENT_THEME
        self.console = Console(theme=_CURRENT_THEME)
        self.workspace = workspace or Path.cwd()
        self.engine: QueryEngine | None = None
        self.running = True
        self.session_id: str = ""
        self.yolo = yolo
        self.plan_mode = plan_mode
        self.autopilot = autopilot
        self._session_history = Path.home() / ".gemi" / "history.txt"
        self._session_history.parent.mkdir(parents=True, exist_ok=True)

        # Bootstrap subsystems
        self._hooks_loaded = hooks_module.initialize()
        self._plugins_loaded = plugins_module.discover_and_load()
        self._mcp_summary: dict[str, Any] = {}
        if (Path.home() / ".gemi" / "mcp.json").exists():
            try:
                self._mcp_summary = mcp_module.initialize_all()
            except Exception:
                self._mcp_summary = {}
        profiles_module.seed_default_profiles()
        self.workspace_ctx: WorkspaceContext = discover_context(self.workspace)
        logger_module.log("session.boot",
                          hooks=self._hooks_loaded,
                          plugins=len(self._plugins_loaded),
                          mcp_servers=len(self._mcp_summary))

        if agent:
            self._init_engine(agent)

    def _build_system_prompt(self, agent: AgentDef) -> str:
        # Minimal Claude-Code-style system prompt: just enough context.
        # Tool schemas are passed separately via the API — no need to enumerate
        # them in the system prompt (that just bloats context and confuses
        # smaller models).
        prompt = SYSTEM_PROMPT_TEMPLATE.format(workspace=self.workspace)

        # Auto-loaded workspace context (BUDDY.md / CLAUDE.md / AGENTS.md)
        if self.workspace_ctx and self.workspace_ctx.has_content:
            prompt += "\n\n" + self.workspace_ctx.to_system_block()

        if self.plan_mode:
            prompt += PLAN_PROMPT_SUFFIX
        if self.autopilot:
            prompt += AUTOPILOT_PROMPT_SUFFIX
        return prompt

    def _init_engine(self, agent: AgentDef) -> None:
        # Reserve room for tools (~2K), system prompt, and conversation.
        # max_tokens is the OUTPUT budget; if it's too close to the context
        # window, llama-server rejects with "Invalid request".
        if agent.context <= 8192:
            mt = 1024
        elif agent.context <= 16384:
            mt = 2048
        else:
            mt = 4096
        self.engine = QueryEngine(
            agent=agent,
            workspace=self.workspace,
            system_prompt=self._build_system_prompt(agent),
            max_tokens=mt,
            on_tool_start=self._on_tool_start,
            on_tool_end=self._on_tool_end,
            on_text=None,
            on_text_chunk=self._on_text_chunk,
            on_permission=self._on_permission,
            bypass_permissions=self.yolo,
        )
        self._streaming_started = False

    def _on_tool_start(self, name: str, args: dict[str, Any], tool_id: str) -> None:
        if self._streaming_started:
            self.console.print()
            self._streaming_started = False
        render_tool_call(self.console, name, args, tool_id)

    def _on_tool_end(self, name: str, args: dict[str, Any], output: str) -> None:
        pass

    def _on_text_chunk(self, chunk: str) -> None:
        if not self._streaming_started:
            self.console.print()
            # Print a subtle stream-start glyph
            palette = get_palette(get_active_theme_name())
            buddy_color = palette.buddy_shimmer
            r, g, b = self._parse_rgb(buddy_color)
            esc = f"\033[1;38;2;{r};{g};{b}m" if r is not None else "\033[1;35m"
            self.console.file.write(f"\n  {esc}✻\033[0m  ")
            self.console.file.flush()
            self._streaming_started = True
        self.console.file.write(chunk)
        self.console.file.flush()

    @staticmethod
    def _parse_rgb(rgb: str) -> tuple[int | None, int | None, int | None]:
        if rgb.startswith("rgb("):
            try:
                r, g, b = [int(x) for x in rgb[4:-1].split(",")]
                return r, g, b
            except Exception:
                return None, None, None
        return None, None, None

    def _on_permission(self, name: str, args: dict[str, Any]) -> bool:
        return self.yolo

    def _auto_save_on_exit(self) -> None:
        if self.engine and self.engine.messages and self.engine.turn_count > 0:
            try:
                path = self.save_current_session()
                if path:
                    self.console.print(f"[dim]  Session auto-saved: {path.name}[/dim]")
            except Exception:
                pass

    def _render_usage(self, result: Any) -> None:
        if result.usage.total > 0:
            tools_used = len(result.tool_calls)
            parts = [
                f"[cost]{result.usage.input_tokens:,} in / {result.usage.output_tokens:,} out[/cost]",
                f"[dim]{result.elapsed:.1f}s[/dim]",
            ]
            if tools_used:
                parts.append(f"[dim]{tools_used} tool{'s' if tools_used != 1 else ''}[/dim]")
            cache_hits = getattr(result, "cache_hits", 0)
            if cache_hits:
                parts.append(f"[dim cyan]{cache_hits} cached[/dim cyan]")
            cost_usd = getattr(result, "cost_usd", 0.0)
            if cost_usd > 0.0001:
                parts.append(f"[dim green]~${cost_usd:.4f}[/dim green]")
            self.console.print("\n  " + "  ".join(parts))

    def run_once(self, prompt: str) -> int:
        if not self.engine:
            agent = self._select_agent_interactive()
            if not agent:
                self.console.print("[error]No agent selected.[/error]")
                return 1
            self._init_engine(agent)
        try:
            self._streaming_started = False
            result = self.engine.query(prompt)
            if self._streaming_started:
                self.console.print()
                self._streaming_started = False
            elif result.text:
                self.console.print(result.text)
            if result.error:
                self.console.print(f"[error]{result.error}[/error]")
                return 1
            return 0
        except ProviderError as e:
            self.console.print(f"[error]{e}[/error]")
            return 1
        except Exception as e:
            self.console.print(f"[error]{e}[/error]")
            return 1

    def set_agent(self, agent: AgentDef) -> None:
        if self.engine:
            self.engine.set_agent(agent)
            self.engine.system_prompt = self._build_system_prompt(agent)
            self.engine.bypass_permissions = self.yolo
        else:
            self._init_engine(agent)

    def reload_workspace_context(self) -> int:
        """Re-discover BUDDY.md/CLAUDE.md/AGENTS.md from disk. Returns file count."""
        self.workspace_ctx = discover_context(self.workspace)
        if self.engine:
            self.engine.system_prompt = self._build_system_prompt(self.engine.agent)
        return len(self.workspace_ctx.files)

    def set_yolo(self, enabled: bool) -> None:
        was_off = not self.yolo
        self.yolo = enabled
        if self.engine:
            self.engine.bypass_permissions = enabled
        # First-time YOLO activation in this session: show legal/ethics warning
        if enabled and was_off and not getattr(self, "_yolo_warned", False):
            self._show_yolo_warning()
            self._yolo_warned = True

    def _show_yolo_warning(self) -> None:
        """Display a prominent ethics + legal-use reminder when YOLO turns on."""
        from .ui.theme import get_palette, get_active_theme_name
        palette = get_palette(get_active_theme_name())
        c = self.console
        c.print()
        c.print(f"  [bold {palette.yolo}]⚡ YOLO MODE ACTIVE[/]  "
                f"[muted]all permission gates bypassed, dangerous tools enabled[/]")
        c.print()
        c.print(f"  [bold {palette.warning}]⚠  Authorized use only.[/]  "
                f"[muted]Offensive tools (exploits, recon, websec, api_test,[/]")
        c.print(f"  [muted]bash, cmd, powershell, hash_crack, payload_gen) are for:[/]")
        c.print(f"  [muted]  • systems you own or have written permission to test[/]")
        c.print(f"  [muted]  • bug-bounty programs within published scope[/]")
        c.print(f"  [muted]  • CTFs, educational labs, authorized red-team work[/]")
        c.print()
        c.print(f"  [muted]Unauthorized use violates the CFAA / Computer Misuse Act /[/]")
        c.print(f"  [muted]equivalent statutes in your jurisdiction. You alone bear[/]")
        c.print(f"  [muted]the legal and ethical responsibility for what you do.[/]")
        c.print()
        c.print(f"  [muted]See[/]  [bold {palette.buddy_shimmer}]DISCLAIMER.md[/]  "
                f"[muted]for full terms.[/]")
        c.print()

    def set_plan_mode(self, enabled: bool) -> None:
        self.plan_mode = enabled
        if self.engine:
            self.engine.system_prompt = self._build_system_prompt(self.engine.agent)

    def set_autopilot(self, enabled: bool) -> None:
        self.autopilot = enabled
        if self.engine:
            self.engine.system_prompt = self._build_system_prompt(self.engine.agent)
            self.engine.bypass_permissions = enabled or self.yolo

    @property
    def mode_label(self) -> str:
        parts = []
        if self.yolo:
            parts.append("YOLO")
        if self.plan_mode:
            parts.append("PLAN")
        if self.autopilot:
            parts.append("AUTO")
        return "+".join(parts) if parts else ""

    def _run_quick_shell(self, command: str) -> None:
        """Run a shell one-liner directly without involving the agent."""
        if not command:
            return
        from .tools.registry import get_tool
        from .ui.theme import get_palette, get_active_theme_name
        palette = get_palette(get_active_theme_name())
        tool = get_tool("shell")
        if not tool:
            self.console.print(f"  [bold {palette.error}]✗[/]  shell tool not registered")
            return
        result = tool.execute(self.workspace, command=command, timeout=120)
        prefix = f"  [bold {palette.buddy}]$[/]"
        if result.is_error:
            self.console.print(f"\n{prefix} [bold]{command}[/]")
            for line in (result.error or "").splitlines()[:20]:
                self.console.print(f"      [dim]{line}[/]")
            self.console.print()
        else:
            self.console.print(f"\n{prefix} [bold]{command}[/]")
            for line in result.output.splitlines()[:30]:
                self.console.print(f"      [muted]{line}[/]")
            extra_lines = len(result.output.splitlines()) - 30
            if extra_lines > 0:
                self.console.print(f"      [dim]… ({extra_lines} more lines)[/]")
            self.console.print()

    def _open_picker(self) -> None:
        """Show the agent + mode picker, apply the selected action."""
        if not self.engine:
            return
        result = show_picker(
            self.console, FLEET,
            current_slug=self.engine.agent.slug,
            yolo=self.yolo, plan=self.plan_mode, auto=self.autopilot,
        )
        kind = result.get("kind")
        palette = get_palette(get_active_theme_name())
        if kind == "agent":
            target = FLEET[result["index"]]
            self.set_agent(target)
            glyph = "●" if target.is_proxy_running() else "○"
            self.console.print(
                f"\n  [bold {palette.buddy}]✻[/]  switched to "
                f"[bold {palette.info}]{target.name}[/]  "
                f"[{palette.success if target.is_proxy_running() else palette.error}]{glyph}[/]  "
                f"[muted]{target.quant} · {target.role}[/]\n"
            )
        elif kind == "yolo":
            self.set_yolo(not self.yolo)
            state = "ON" if self.yolo else "OFF"
            self.console.print(f"\n  [bold {palette.yolo}]⚡[/]  YOLO mode: [bold]{state}[/]\n")
        elif kind == "plan":
            self.set_plan_mode(not self.plan_mode)
            state = "ON" if self.plan_mode else "OFF"
            self.console.print(f"\n  [bold {palette.plan}]◇[/]  PLAN mode: [bold]{state}[/]\n")
        elif kind == "auto":
            self.set_autopilot(not self.autopilot)
            state = "ON" if self.autopilot else "OFF"
            self.console.print(f"\n  [bold {palette.auto}]↻[/]  AUTO mode: [bold]{state}[/]\n")
        else:
            self.console.print(f"\n  [muted](picker dismissed)[/]\n")

    def _query_with_blocks(self, blocks: list[dict[str, Any]]) -> Any:
        """Append a structured user message (text+image blocks) and run a turn."""
        from .query_engine import TurnResult
        if not self.engine:
            return TurnResult(text="", error="No engine.")
        # Build a structured user message manually so the QueryEngine can ingest it
        # We bypass the .query() helper since it expects a string; instead we
        # mimic its behavior by appending the message and running the inner loop.
        original_query = self.engine.query
        # Patch: temporarily inject the structured message
        self.engine.messages.append({"role": "user", "content": blocks})
        self.engine.turn_count += 1
        # Use a synthetic prompt to run a turn — easiest reliable path
        # is to call query() with an empty string but that re-appends — instead
        # call the internal loop. We compromise: pop the synthetic add and invoke
        # original_query with the joined text portion.
        text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        n_images = sum(1 for b in blocks if b.get("type") == "image")
        # Pop our synthetic add so query() can do its own
        self.engine.messages.pop()
        self.engine.turn_count = max(0, self.engine.turn_count - 1)
        # Inject image notice into the text so the model knows
        notice = f"\n[attached {n_images} image{'s' if n_images != 1 else ''}]" if n_images else ""
        return original_query(("\n".join(text_parts) + notice).strip())

    def _announce_completed_bg_jobs(self) -> None:
        """Print a one-line notification for any bg jobs that completed since last turn."""
        if not hasattr(self, "_last_seen_bg_jobs"):
            self._last_seen_bg_jobs: set[str] = set()
        palette = get_palette(get_active_theme_name())
        new_completed = []
        for job in background_module.list_jobs(include_done=True):
            if job.state in ("done", "failed", "cancelled") and job.id not in self._last_seen_bg_jobs:
                new_completed.append(job)
                self._last_seen_bg_jobs.add(job.id)
        for job in new_completed:
            if job.state == "done":
                self.console.print(
                    f"  [bold {palette.success}]●[/]  bg [bold]{job.id}[/] done  "
                    f"[muted]{job.title[:50]}[/]  [dim]{job.elapsed:.1f}s[/]"
                )
            elif job.state == "failed":
                self.console.print(
                    f"  [bold {palette.error}]●[/]  bg [bold]{job.id}[/] failed  "
                    f"[muted]{job.title[:50]}[/]  [dim]{job.elapsed:.1f}s[/]"
                )

    def save_current_session(self, session_id: str = "") -> Path | None:
        if not self.engine or not self.engine.messages:
            return None
        path = save_session(
            agent_slug=self.engine.agent.slug,
            messages=self.engine.messages,
            workspace=str(self.workspace),
            session_id=session_id or self.session_id,
            stats=self.engine.get_stats(),
        )
        self.session_id = path.stem
        return path

    def resume_session(self, session_id: str) -> bool:
        data = load_session(session_id)
        if not data:
            return False
        agent = get_agent(data.get("agent", ""))
        if not agent:
            return False
        ws = data.get("workspace", "")
        if ws:
            self.workspace = Path(ws)
        self._init_engine(agent)
        self.engine.messages = data.get("messages", [])
        self.engine.turn_count = data.get("turn_count", 0)
        self.session_id = data.get("id", session_id)
        return True

    def _select_agent_interactive(self) -> AgentDef | None:
        # Default to local-agent-9 if it's running; otherwise first running;
        # otherwise the picker.
        from .config import get_agent
        default_a9 = get_agent("local-agent-9")
        if default_a9 and default_a9.is_proxy_running():
            return default_a9

        from .ui.welcome import print_welcome
        palette = get_palette(get_active_theme_name())
        running = get_running_agents()

        # Show the splash welcome first (now Claude-Code-style with workspace + tip)
        print_welcome(self.console, workspace=self.workspace)

        if running:
            self.console.print(
                f"\n  [bold {palette.success}]●[/]  "
                f"[bold]{len(running)}[/] running agent(s) ready:\n"
            )
            for i, a in enumerate(running, 1):
                tags = " ".join(f"[muted]·[/] [info]{t}[/]" for t in a.capability_tags) if a.capability_tags else ""
                self.console.print(
                    f"    [bold {palette.buddy}]{i}.[/]  "
                    f"[agent.name]{a.name}[/]  "
                    f"[muted]({a.slug})[/]  "
                    f"[dim]{a.role}[/]  "
                    f"[bold {palette.warning}]{a.quant}[/]  "
                    f"[muted]ctx={a.context:,}[/]  {tags}"
                )
            self.console.print(f"    [bold {palette.buddy}]0.[/]  [muted]show full fleet[/]\n")
            try:
                choice = input("  Select agent [1]: ").strip()
                if choice == "0":
                    render_fleet_table(self.console, FLEET)
                    slug = input("  Enter agent slug: ").strip()
                    return get_agent(slug)
                idx = int(choice) - 1 if choice else 0
                if 0 <= idx < len(running):
                    return running[idx]
            except (ValueError, KeyboardInterrupt):
                return None
        else:
            self.console.print(
                f"\n  [bold {palette.warning}]⚠[/]  "
                f"[bold]No agents currently running.[/]\n"
            )
            render_fleet_table(self.console, FLEET)
            self.console.print(
                f"\n  [muted]Start one with:[/]  "
                f"[bold {palette.buddy_shimmer}]agents.ps1 -Start <slug> -Proxy[/]\n"
            )
            slug = input("  Enter agent slug (or press Enter for local-agent-9): ").strip()
            return get_agent(slug or "local-agent-9")

    def _run_autopilot(self, initial_prompt: str) -> None:
        # Use the v2 autopilot loop with subgoal tracking + step budget
        from .autopilot_v2 import run_autopilot, AutopilotBudget
        budget = AutopilotBudget()
        run_autopilot(self, initial_prompt, budget=budget, show_panel=True)
        return

        # legacy v1 loop (kept dead for reference; v2 is exhaustive)
        self.console.print(Panel(
            f"[bold]Task:[/bold] {initial_prompt[:200]}",
            title="[bold cyan]AUTOPILOT[/]",
            border_style="cyan",
        ))
        self.set_autopilot(True)
        round_num = 0
        max_rounds = 50
        current_prompt = initial_prompt

        while self.running and round_num < max_rounds:
            round_num += 1
            self.console.print(f"\n[dim]--- autopilot round {round_num}/{max_rounds} ---[/dim]")
            try:
                self._streaming_started = False
                result = self.engine.query(current_prompt)

                if self._streaming_started:
                    self.console.print()
                    self._streaming_started = False
                    for tc in result.tool_results:
                        render_tool_result(self.console, tc)
                    self._render_usage(result)
                else:
                    render_assistant(self.console, result)

                if result.error:
                    self.console.print(f"[error]Autopilot error: {result.error}[/error]")
                    break

                has_tool_calls = bool(result.tool_calls)
                text_lower = result.text.lower() if result.text else ""
                signals_done = any(w in text_lower for w in [
                    "task complete", "all done", "finished", "nothing left",
                    "no more", "completed all", "everything is done",
                ])

                if signals_done and not has_tool_calls:
                    self.console.print("\n[success]Autopilot: task complete.[/success]")
                    break

                if not has_tool_calls and round_num > 1:
                    current_prompt = (
                        "Continue working on the task. Check if there's anything left to do. "
                        "If everything is complete, say 'task complete'."
                    )
                else:
                    current_prompt = "Continue."

            except ProviderError as e:
                self.console.print(f"\n[error]Autopilot provider error: {e}[/error]")
                break
            except KeyboardInterrupt:
                self.console.print("\n[warning]Autopilot interrupted by user.[/warning]")
                break

        if round_num >= max_rounds:
            self.console.print(f"\n[warning]Autopilot: hit {max_rounds}-round limit.[/warning]")

        self.console.print(f"\n[dim]Autopilot ran {round_num} rounds.[/dim]")

    def run(self) -> None:
        if not self.engine:
            agent = self._select_agent_interactive()
            if not agent:
                self.console.print("[error]No agent selected. Exiting.[/error]")
                return
            self._init_engine(agent)

        print_banner(self.console, self.engine.agent, workspace=self.workspace)

        palette = get_palette(get_active_theme_name())

        # Status announcements (workspace, hooks, modes) — beautified
        announcements: list[str] = []
        if self.workspace_ctx.has_content:
            files = ", ".join(f.relative for f in self.workspace_ctx.files)
            announcements.append(
                f"  [bold {palette.info}]◆[/] [muted]loaded context:[/] "
                f"[bold {palette.info}]{files}[/] "
                f"[dim]({self.workspace_ctx.total_chars:,} chars)[/]"
            )
        if self._hooks_loaded:
            announcements.append(
                f"  [bold {palette.info}]◆[/] [muted]loaded[/] "
                f"[bold]{self._hooks_loaded}[/] [muted]hook(s)[/]"
            )
        if self.yolo:
            announcements.append(
                f"  [bold {palette.yolo}]⚡ YOLO[/]  "
                f"[muted]all permissions bypassed — dangerous tools enabled[/]"
            )
        if self.plan_mode:
            announcements.append(
                f"  [bold {palette.plan}]◇ PLAN[/]  "
                f"[muted]agent plans before executing[/]"
            )
        if self.autopilot:
            announcements.append(
                f"  [bold {palette.auto}]↻ AUTO[/]  "
                f"[muted]non-stop autonomous execution[/]"
            )
        if not self.engine.agent.is_proxy_running():
            announcements.append(
                f"  [bold {palette.warning}]⚠[/]  "
                f"[bold {palette.warning}]proxy offline[/]  "
                f"[muted]start with:[/] "
                f"[bold]agents.ps1 -Start {self.engine.agent.slug} -Proxy[/]"
            )

        for line in announcements:
            self.console.print(line)
        if announcements:
            self.console.print()

        # Fire SessionStart hook
        try:
            hooks_module.fire_session_start(self.engine.agent.slug, str(self.workspace))
        except Exception:
            pass

        completer = SmartCompleter(self)

        # Custom keybindings
        kb = KeyBindings()

        @kb.add("c-l")
        def _clear_screen(event):
            """Ctrl+L → clear the screen but keep the prompt."""
            event.app.renderer.clear()

        @kb.add("c-y")
        def _toggle_yolo(event):
            """Ctrl+Y → toggle YOLO mode."""
            self.set_yolo(not self.yolo)
            event.app.invalidate()

        @kb.add("c-x", "c-a")
        def _next_agent(event):
            """Ctrl+X Ctrl+A → switch to next running agent."""
            from .config import get_running_agents
            running = get_running_agents()
            if not running or not self.engine:
                return
            try:
                idx = next(i for i, a in enumerate(running)
                           if a.slug == self.engine.agent.slug)
                next_idx = (idx + 1) % len(running)
            except StopIteration:
                next_idx = 0
            self.set_agent(running[next_idx])
            event.app.invalidate()

        @kb.add("c-m")
        def _open_picker(event):
            """Ctrl+M → open the agent/model/mode picker."""
            event.app.exit(result="__OPEN_PICKER__")

        def _bottom_toolbar() -> str:
            try:
                if not self.engine:
                    return ""
                a = self.engine.agent
                from .query_engine import _estimate_tokens
                est = _estimate_tokens(self.engine.messages, self.engine.system_prompt)
                pct = est / a.context * 100 if a.context else 0
                cache = self.engine._cache.stats
                return build_bottom_toolbar(
                    agent_slug=a.slug,
                    quant=a.quant,
                    proxy_running=a.is_proxy_running(),
                    mode_label=self.mode_label or "normal",
                    context_pct=pct,
                    turn_count=self.engine.turn_count,
                    cost_usd=self.engine._cost.session.total_usd,
                    cache_hits=cache.hits,
                    cache_total=cache.hits + cache.misses,
                    cwd_basename=self.workspace.name if self.workspace else "",
                    model_display=getattr(a, "short_model", "") or "",
                )
            except Exception:
                return ""

        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(self._session_history)),
            completer=completer,
            style=PT_STYLE,
            multiline=False,
            bottom_toolbar=_bottom_toolbar,
            refresh_interval=2,
            key_bindings=kb,
            enable_history_search=True,   # Up/Down jumps to matching history entries
        )
        self._last_elapsed = 0.0

        while self.running:
            try:
                a = self.engine.agent
                from .query_engine import _estimate_tokens
                try:
                    est = _estimate_tokens(self.engine.messages, self.engine.system_prompt)
                    ctx_pct = est / a.context * 100 if a.context else 0
                except Exception:
                    ctx_pct = 0
                prompt_obj = build_prompt_lines(
                    agent_slug=a.slug,
                    proxy_running=a.is_proxy_running(),
                    mode_label=self.mode_label,
                    turn_count=self.engine.turn_count,
                    context_pct=ctx_pct,
                    cost_usd=self.engine._cost.session.total_usd,
                    cache_hit_rate=self.engine._cache.stats.hit_rate,
                    width=self.console.width,
                )
                user_input = session.prompt(prompt_obj)
            except (KeyboardInterrupt, EOFError):
                self._auto_save_on_exit()
                palette = get_palette(get_active_theme_name())
                self.console.print(f"\n  [bold {palette.buddy}]✻[/]  [muted]Goodbye.[/]\n")
                break

            # Ctrl+M handler — open the picker
            if user_input == "__OPEN_PICKER__":
                self._open_picker()
                continue

            if not user_input.strip():
                continue

            # Quick shell: `!<command>` → run via shell tool (no agent involvement)
            stripped = user_input.strip()
            if stripped.startswith("!") and len(stripped) > 1:
                self._run_quick_shell(stripped[1:].strip())
                continue

            # Quick-switch: /1 .. /9, /0 (10th agent) → switch agents
            if stripped.startswith("/") and len(stripped) == 2 and stripped[1].isdigit():
                idx = int(stripped[1])
                idx = idx - 1 if idx >= 1 else 9
                if 0 <= idx < len(FLEET):
                    target = FLEET[idx]
                    self.set_agent(target)
                    palette = get_palette(get_active_theme_name())
                    glyph = "●" if target.is_proxy_running() else "○"
                    self.console.print(
                        f"  [bold {palette.buddy}]✻[/]  switched to "
                        f"[bold {palette.info}]{target.name}[/]  "
                        f"[{palette.success if target.is_proxy_running() else palette.error}]{glyph}[/]  "
                        f"[muted]{target.quant} · {target.role}[/]"
                    )
                    continue

            if user_input.strip() == "{":
                lines = []
                palette = get_palette(get_active_theme_name())
                self.console.print(
                    f"  [dim]┌─ multi-line mode — enter [/]"
                    f"[bold {palette.warning}]}}[/]"
                    f"[dim] on its own line to send[/]"
                )
                while True:
                    try:
                        line = session.prompt(f"  [bold {palette.buddy}]│[/] ")
                    except (KeyboardInterrupt, EOFError):
                        break
                    if line.strip() == "}":
                        break
                    lines.append(line)
                user_input = "\n".join(lines)
                if not user_input.strip():
                    continue

            if is_command(user_input):
                handle_command(self, user_input)
                continue

            try:
                # Expand image: tokens for vision-capable agents
                processed_input: Any = user_input
                if image_input.has_image_token(user_input) and self.engine:
                    processed_input = image_input.expand(
                        user_input, self.workspace, self.engine.agent,
                    )

                # Notify on completed background jobs since last turn
                self._announce_completed_bg_jobs()

                if self.autopilot:
                    self._run_autopilot(user_input if isinstance(processed_input, str) else user_input)
                else:
                    self._streaming_started = False
                    if isinstance(processed_input, list):
                        # Image-augmented input — bypass simple .query() string path
                        result = self._query_with_blocks(processed_input)
                    else:
                        result = self.engine.query(processed_input)
                    self._last_elapsed = result.elapsed
                    if self._streaming_started:
                        self.console.print()
                        self._streaming_started = False
                        for tc in result.tool_results:
                            render_tool_result(self.console, tc)
                        self._render_usage(result)
                    else:
                        render_assistant(self.console, result)
            except ProviderError as e:
                self.console.print(f"\n[error]Provider error: {e}[/error]\n")
            except KeyboardInterrupt:
                self.console.print("\n[warning]Interrupted.[/warning]\n")
            except Exception as e:
                self.console.print(f"\n[error]Unexpected error: {e}[/error]\n")
