"""TUI: Textual-based interactive terminal UI for ICEQ."""

import ctypes
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import time
import threading
import urllib.request

import pyperclip

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll, Horizontal
from textual.message import Message
from textual.widgets import Static, Input, TextArea
from textual.reactive import reactive
from textual import work

from rich.console import Group
from rich.panel import Panel as RichPanel
from rich.syntax import Syntax
from rich.table import Table as RichTable
from rich.text import Text
from rich.markup import escape
from rich import box

try:
    import pyfiglet
except ImportError:
    pyfiglet = None

from localscript.context import Context
from localscript import config, agent
from localscript.ui import THINKING_PHRASES, SPINNER_FRAMES
import localscript.ui as ui_module


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLASH_COMMANDS = {
    "/help":       "Show this help",
    "/copy":       "Copy last output to clipboard",
    "/status":     "Session stats",
    "/history":    "Task history",
    "/cost":       "Token usage",
    "/undo":       "Undo the last prompt (files, context, memory)",
    "/compact":    "Compress context",
    "/memory":     "Long-term memory: --on, --off, clear",
    "/plan":       "Show the current task plan",
    "/model":      "Show/switch model",
    "/think":      "Always enable thinking",
    "/no_think":   "Always disable thinking",
    "/auto_think": "Auto-decide by difficulty",
    "/resume":     "Resume a saved chat",
    "/clear":      "Clear context",
    "/exit":       "Exit ICEQ",
}


# ---------------------------------------------------------------------------
# Messages — posted from worker thread, handled on main thread
# ---------------------------------------------------------------------------

class _AgentMsg(Message):
    """Base for all messages from agent worker."""
    def __init__(self, run_id: int):
        super().__init__()
        self.run_id = run_id


class AgentBlock(_AgentMsg):
    def __init__(self, run_id: int, markup: str):
        super().__init__(run_id)
        self.markup = markup


class AgentCodeWrite(_AgentMsg):
    def __init__(self, run_id: int, content: str, path: str,
                 lines: int, old_content: str | None):
        super().__init__(run_id)
        self.content = content
        self.path = path
        self.lines = lines
        self.old_content = old_content


class AgentCodeRaw(_AgentMsg):
    def __init__(self, run_id: int, content: str, path: str):
        super().__init__(run_id)
        self.content = content
        self.path = path


class AgentDiff(_AgentMsg):
    def __init__(self, run_id: int, old: str, new: str, path: str):
        super().__init__(run_id)
        self.old = old
        self.new = new
        self.path = path


class AgentSandbox(_AgentMsg):
    def __init__(self, run_id: int, result):
        super().__init__(run_id)
        self.result = result


class AgentSuccess(_AgentMsg):
    def __init__(self, run_id: int, summary: str, iterations: int,
                 elapsed: float, files: list | None):
        super().__init__(run_id)
        self.summary = summary
        self.iterations = iterations
        self.elapsed = elapsed
        self.files = files


class AgentReplaceSpinner(_AgentMsg):
    def __init__(self, run_id: int, thinking: str, tokens: int, elapsed: float):
        super().__init__(run_id)
        self.thinking = thinking
        self.tokens = tokens
        self.elapsed = elapsed


class ThinkingChunk(_AgentMsg):
    """Incremental thinking text update — streamed during LLM inference."""
    def __init__(self, run_id: int, text: str, tokens: int, elapsed: float):
        super().__init__(run_id)
        self.text = text
        self.tokens = tokens
        self.elapsed = elapsed


class AgentFinished(_AgentMsg):
    """Agent worker run completed (finally block)."""
    pass


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class ToggleBlock(Static):
    """Collapsible block: click or Enter to toggle."""

    collapsed = reactive(True)
    can_focus = True

    def __init__(self, header: str, body, default_collapsed: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._header = header
        self._body = body
        self.collapsed = default_collapsed

    def render(self):
        arrow = "\u25b8" if self.collapsed else "\u25be"
        header_text = Text.from_markup(f"{arrow} {self._header}")
        if self.collapsed:
            return header_text
        if isinstance(self._body, (str, Text)):
            body = Text.from_markup(self._body) if isinstance(self._body, str) else self._body
        else:
            body = self._body
        return Group(header_text, body)

    def watch_collapsed(self, _collapsed: bool) -> None:
        self.refresh(layout=True)

    def on_click(self):
        self.collapsed = not self.collapsed

    def key_enter(self):
        self.collapsed = not self.collapsed


class SpinnerWidget(Static):
    """Animated braille spinner with random phrases and live token count."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tick = 0
        self._start = time.time()
        self._tokens = 0
        self._order = list(range(len(THINKING_PHRASES)))
        random.shuffle(self._order)

    def on_mount(self):
        self.set_interval(1 / 12, self._animate)

    def _animate(self):
        self._tick += 1
        elapsed = time.time() - self._start
        frame = SPINNER_FRAMES[self._tick % len(SPINNER_FRAMES)]
        idx = self._order[int(elapsed / 12) % len(self._order)]
        phrase = THINKING_PHRASES[idx]
        tok_info = f" {self._tokens} tok \u00b7" if self._tokens else ""
        self.update(Text.from_markup(
            f"  [cyan]{frame} {phrase}...[/cyan]  "
            f"[dim]{elapsed:.0f}s \u00b7{tok_info} Ctrl+C \u00d72 stop[/dim]"
        ))


class PromptInput(TextArea):
    """Prompt that starts as 1 line, grows to max 5, then scrolls."""

    class Submitted(Message):
        def __init__(self, value: str):
            super().__init__()
            self.value = value

    def __init__(self):
        super().__init__(
            id="prompt", language=None, show_line_numbers=False,
            tab_behavior="focus", soft_wrap=True,
        )
        self._suggestion = ""
        self._ollama_models: list[str] = []
        self._models_fetched = False

    def _get_ollama_models(self) -> list[str]:
        """Fetch available models from Ollama API (cached)."""
        if not self._models_fetched:
            self._models_fetched = True
            try:
                req = urllib.request.Request("http://localhost:11434/api/tags")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read())
                    self._ollama_models = sorted(
                        m["name"] for m in data.get("models", [])
                    )
            except Exception:
                pass
        return self._ollama_models

    def invalidate_model_cache(self):
        """Force re-fetch of Ollama models on next completion."""
        self._models_fetched = False
        self._ollama_models = []

    def _on_key(self, event) -> None:
        if event.key == "tab":
            if self._suggestion:
                event.prevent_default()
                event.stop()
                self.insert(self._suggestion)
                self._suggestion = ""
                return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                self.clear()
                self.post_message(self.Submitted(text))

    def on_text_area_changed(self, event) -> None:
        try:
            visual = self.wrapped_document.height
        except Exception:
            visual = self.document.line_count
        self.styles.height = min(visual + 2, 7)  # +2 for border

        text = self.text.strip()
        self._suggestion = ""
        try:
            hints = self.app.query_one("#hints")
        except Exception:
            return

        if not text.startswith("/") or "\n" in text:
            hints.display = False
            return

        parts = text.split(None, 1)
        cmd = parts[0].lower()

        # --- Model name completion: /model <partial> ---
        if cmd == "/model" and len(parts) == 2:
            arg = parts[1]
            models = self._get_ollama_models()
            matches = [m for m in models if m.startswith(arg)]
            if len(matches) == 1 and matches[0] != arg:
                self._suggestion = matches[0][len(arg):]
                hints.update(Text.from_markup(
                    f"  [dim]{escape(text)}[/dim][dim italic]{escape(self._suggestion)}[/dim italic]"
                    f"  [dim]Tab[/dim]"
                ))
                hints.display = True
            elif matches:
                parts_h = [f"[bold]{escape(m)}[/bold]" for m in matches[:10]]
                if len(matches) > 10:
                    parts_h.append(f"[dim]+{len(matches)-10} more[/dim]")
                hints.update(Text.from_markup("  ".join(parts_h)))
                hints.display = True
            else:
                hints.display = False
            return

        # --- Show model list after "/model " with no arg ---
        if cmd == "/model" and text.endswith(" ") and len(parts) == 1:
            models = self._get_ollama_models()
            if models:
                parts_h = [f"[bold]{escape(m)}[/bold]" for m in models[:10]]
                if len(models) > 10:
                    parts_h.append(f"[dim]+{len(models)-10} more[/dim]")
                hints.update(Text.from_markup("  ".join(parts_h)))
                hints.display = True
            else:
                hints.display = False
            return

        # --- Command name completion ---
        cmd_matches = [c for c in SLASH_COMMANDS if c.startswith(text)]
        if not cmd_matches or text in SLASH_COMMANDS:
            hints.display = False
            return

        if len(cmd_matches) == 1:
            self._suggestion = cmd_matches[0][len(text):]
            hints.update(Text.from_markup(
                f"  [dim]{escape(text)}[/dim][dim italic]{escape(self._suggestion)}[/dim italic]"
                f"  [dim]{SLASH_COMMANDS[cmd_matches[0]]} \u00b7 Tab[/dim]"
            ))
            hints.display = True
        else:
            parts_h = [
                f"[bold]{c}[/bold] [dim]{d}[/dim]"
                for c, d in SLASH_COMMANDS.items()
                if c.startswith(text)
            ]
            hints.update(Text.from_markup("  ".join(parts_h)))
            hints.display = True


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class IceqApp(App):
    """ICEQ Textual TUI application."""

    TITLE = "ICEQ"

    CSS = """
    Screen {
        background: $background;
    }
    #messages {
        height: 1fr;
        padding: 0 1;
    }
    #prompt-bar {
        dock: bottom;
        height: auto;
        min-height: 3;
        max-height: 7;
        margin: 0 1;
        padding: 0 3 0 0;
    }
    #prompt {
        width: 1fr;
        height: auto;
        min-height: 3;
        max-height: 7;
    }
    #stop-btn {
        width: 0;
        height: 0;
        display: none;
        visibility: hidden;
        content-align: center middle;
        background: $background;
        color: $text-muted;
        margin: 0 0 0 1;
    }
    #stop-btn.active {
        width: 5;
        height: 3;
        display: block;
        visibility: visible;
        border: round cyan;
    }
    #stop-btn.active:hover {
        color: white;
        border: round white;
    }
    .block {
        margin: 0 0 0 2;
    }
    .user-msg {
        background: $surface;
        padding: 0 1;
        margin: 1 0 0 0;
    }
    .result {
        margin: 1 0 0 2;
    }
    #hints {
        dock: bottom;
        height: auto;
        max-height: 4;
        margin: 0 1;
        padding: 0 1;
        background: $surface;
        display: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel", "Stop", priority=True),
        Binding("ctrl+d", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self._ctx = Context()
        self._agent_active = False
        self._spinner_n = 0
        self._agent_thread_id: int | None = None
        self._agent_run_id: int = 0
        self._pending_write: tuple[str, int] | None = None
        self._pending_patch_path: str | None = None
        self._originals: dict = {}
        self._current_spinner_id: str | None = None
        self._last_ctrl_c: float = 0
        self._last_output: str = ""

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="messages")
        yield Static("", id="hints")
        with Horizontal(id="prompt-bar"):
            yield PromptInput()
            yield Static("\u25a0", id="stop-btn")

    def on_mount(self):
        self._show_banner()
        self.query_one("#prompt").focus()

    # ------------------------------------------------------------------
    # Banner
    # ------------------------------------------------------------------

    def _show_banner(self):
        cwd = os.path.basename(os.getcwd()) or os.getcwd()
        lc_bin = config.LUACHECK_BINARY
        lua_bin = config.LUA_BINARY
        has_lc = os.path.isfile(lc_bin) or shutil.which("luacheck")
        has_lua = os.path.isfile(lua_bin) or shutil.which("lua54")
        tools_list = [t for t, ok in [("luacheck", has_lc), ("lua54", has_lua)] if ok]
        tools_str = " [white]\u00b7[/white] ".join(
            f"[white]{t}[/white]" for t in tools_list
        ) or "[dim]none[/dim]"

        if pyfiglet:
            art = pyfiglet.figlet_format("ICEQ", font="ansi_shadow").rstrip("\n")
            logo_lines = [Text(ln, style="bold cyan") for ln in art.split("\n") if ln.strip()]
        else:
            logo_lines = [Text("ICEQ", style="bold cyan")]
        left_parts = logo_lines + [Text(""), Text.from_markup("  [bold white]Lua Code Agent[/bold white]")]
        left = Text("\n").join(left_parts)

        has_chats = os.path.isdir(".iceq/chats") and os.listdir(".iceq/chats")
        resume_tip = "\n[white]  \u00b7 /resume to continue a chat[/white]" if has_chats else ""
        right_text = (
            "[bold white]Tips for getting started[/bold white]\n"
            "[white]  \u00b7 Type a Lua task to begin[/white]\n"
            "[white]  \u00b7 /help for all commands[/white]"
            f"{resume_tip}\n\n"
            f"[dim]Model:[/dim]   [white]{escape(config.LLM_MODEL)}[/white]\n"
            f"[dim]Workdir:[/dim] [white]./{escape(cwd)}[/white]\n"
            f"[dim]Tools:[/dim]   {tools_str}"
        )

        table = RichTable(show_header=False, show_edge=False, box=None, padding=(0, 2), expand=True)
        table.add_column(ratio=1)
        table.add_column(ratio=1)
        table.add_row(left, Text.from_markup(right_text))

        panel = RichPanel(
            table,
            title=f"[bold cyan]ICEQ[/bold cyan] [white]v{config.VERSION}[/white]",
            border_style="cyan",
            padding=(1, 2),
        )
        self._mount_widget(Static(panel))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _scroll_if_at_bottom(self):
        """Auto-scroll only if user hasn't scrolled up to read."""
        msgs = self.query_one("#messages")
        if msgs.max_scroll_y == 0 or msgs.scroll_y >= msgs.max_scroll_y - 3:
            msgs.scroll_end(animate=False)

    def _mount_widget(self, widget: Static):
        msgs = self.query_one("#messages")
        msgs.mount(widget)
        msgs.refresh(layout=True)
        self._scroll_if_at_bottom()

    def _mount_block(self, markup: str):
        self._mount_widget(Static(Text.from_markup(f"  {markup}"), classes="block"))

    def _restore_chat_history(self):
        """Replay saved chat history into the UI after /resume."""
        shown = 0
        # Show user messages from iterations
        for msg in self._ctx.iterations:
            if msg.get("role") == "user":
                task = msg.get("content", "")[:200]
                if task:
                    self._mount_widget(Static(
                        Text.from_markup(f"  {escape(task)} "),
                        classes="user-msg",
                    ))
                    shown += 1
        # Show task results from history
        for h in self._ctx.history:
            icon = "\u2713" if h["success"] else "\u2717"
            color = "green" if h["success"] else "red"
            time_str = f" \u00b7 {h['time']:.1f}s" if h.get("time") else ""
            self._mount_block(
                f"[{color}]{icon} \"{escape(h['task'][:60])}\" "
                f"\u2014 {h['iterations']} iter{time_str}[/{color}]"
            )
        files = len(self._ctx.files_touched)
        self._mount_block(
            f"[green]Resumed: {shown} messages, "
            f"{self._ctx.total_iterations} iter, {files} files[/green]"
        )

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def on_prompt_input_submitted(self, event: PromptInput.Submitted):
        text = event.value
        if not text:
            return
        if text.lower() in ("exit", "quit", "/exit"):
            self._quit()
            return
        if text.startswith("/"):
            self._handle_slash(text)
            return
        if self._agent_active:
            self._cancel_agent()
            self._finish_agent()
        self._start_task(text)

    def _start_task(self, task: str):
        self._mount_widget(Static(
            Text.from_markup(f"  {escape(task)} "), classes="user-msg",
        ))
        self._agent_active = True
        self._agent_run_id += 1
        self._current_spinner_id = self._add_spinner()
        self._patch_ui()
        self._show_stop_button(True)
        self._run_agent(task)

    def _show_stop_button(self, visible: bool):
        """Toggle the interrupt button visibility."""
        try:
            btn = self.query_one("#stop-btn")
            if visible:
                btn.add_class("active")
            else:
                btn.remove_class("active")
        except Exception:
            pass

    def on_click(self, event):
        """Catch clicks on the stop button (Static doesn't emit Pressed)."""
        target = getattr(event, "widget", None)
        if target is not None and getattr(target, "id", None) == "stop-btn":
            if self._agent_active:
                self._cancel_agent()
                self._finish_agent()
                self._mount_block("[yellow]Stopped[/yellow]")

    # ------------------------------------------------------------------
    # Agent worker
    # ------------------------------------------------------------------

    @work(thread=True)
    def _run_agent(self, task: str):
        run_id = self._agent_run_id
        self._agent_thread_id = threading.current_thread().ident

        def _on_plan(subtasks):
            """Show the task plan in the TUI and proceed immediately.

            In the TUI we can't easily prompt for confirmation from a worker
            thread, so we display the plan and auto-proceed. The user can
            /undo if they don't like it.
            """
            lines = ["[bold]Task Plan:[/bold]"]
            for i, s in enumerate(subtasks, 1):
                lines.append(
                    f"  {i}. {s['title']} \u2014 {s['description']}"
                )
            self._post(AgentBlock(run_id, "\n".join(lines)))
            return True

        try:
            agent.run(task, ctx=self._ctx, on_plan=_on_plan)
        except KeyboardInterrupt:
            pass  # Ctrl+C handled by action_cancel, don't duplicate
        except Exception as e:
            self._post(
                AgentBlock(run_id, f"[red]\u2717 {escape(str(e))}[/red]")
            )
        finally:
            self._agent_thread_id = None
            self._post(AgentFinished(run_id))

    @work(thread=True)
    def _run_compact(self):
        """Run context compaction in a background thread.

        Uses call_from_thread to mount the result directly — compact is
        NOT an agent run, so it must not go through AgentBlock which gets
        filtered by _is_current_run and silently dropped if the user
        starts a task before the compact finishes.
        """
        try:
            result = self._ctx.compact()
            if result["summarized"] == 0:
                markup = "[dim]Nothing to compact[/dim]"
            else:
                markup = (
                    f"Compacted: {result['before']} \u2192 {result['after']} messages "
                    f"({result['summarized']} summarized, {result['kept']} kept)"
                )
        except Exception as e:
            markup = f"[red]Compact failed: {escape(str(e))}[/red]"
        self.call_from_thread(self._mount_block, markup)

    @work(thread=True)
    def _run_memory_enable(self):
        """Enable long-term memory in a background thread.

        Uses call_from_thread instead of AgentBlock to avoid being
        silently dropped by the _is_current_run filter if the user starts
        a task before the enable finishes.
        """
        try:
            stats = self._ctx.memory_enable()
            markup = (
                f"[green]Memory: ON[/green] "
                f"({stats['rows']} rows, {stats['db_bytes']} bytes "
                f"at .iceq/memory.sqlite)"
            )
        except Exception as e:
            markup = f"[red]Enable failed: {escape(str(e))}[/red]"
        self.call_from_thread(self._mount_block, markup)

    def _finish_agent(self):
        if not self._agent_active:
            return
        self._agent_active = False
        self._last_ctrl_c = 0
        self._unpatch_ui()
        if self._current_spinner_id:
            try:
                self.query_one(f"#{self._current_spinner_id}").remove()
            except Exception:
                pass
            self._current_spinner_id = None
        self._show_stop_button(False)
        self.query_one("#prompt").focus()

    # ------------------------------------------------------------------
    # Message handlers — main thread, driven by Textual event loop
    # ------------------------------------------------------------------

    def _is_current_run(self, event: _AgentMsg) -> bool:
        return event.run_id == self._agent_run_id

    def on_agent_block(self, event: AgentBlock):
        if not self._is_current_run(event):
            return
        self._mount_block(event.markup)

    def on_agent_code_raw(self, event: AgentCodeRaw):
        if not self._is_current_run(event):
            return
        self._mount_code_raw(event.content, event.path)

    def on_agent_diff(self, event: AgentDiff):
        if not self._is_current_run(event):
            return
        self._mount_diff(event.old, event.new, event.path)

    def on_agent_sandbox(self, event: AgentSandbox):
        if not self._is_current_run(event):
            return
        self._mount_sandbox(event.result)

    def on_agent_success(self, event: AgentSuccess):
        if not self._is_current_run(event):
            return
        self._last_output = event.summary or ""
        self._mount_success(event.summary, event.iterations, event.elapsed, event.files)

    def on_thinking_chunk(self, event: ThinkingChunk):
        if not self._is_current_run(event):
            return
        # Update spinner token count
        if self._current_spinner_id:
            try:
                self.query_one(f"#{self._current_spinner_id}")._tokens = event.tokens
            except Exception:
                pass
        # Create or update live thinking ToggleBlock
        header = f"[dim]Thinking ({event.tokens} tokens) \u00b7 {event.elapsed:.1f}s[/dim]"
        body = Text(event.text, style="dim")
        try:
            widget = self.query_one("#thinking-stream")
            widget._header = header
            widget._body = body
            widget.refresh(layout=True)
            self._scroll_if_at_bottom()
        except Exception:
            self._mount_widget(ToggleBlock(
                header=header, body=body, default_collapsed=True,
                id="thinking-stream", classes="block",
            ))

    def on_agent_code_write(self, event: AgentCodeWrite):
        if not self._is_current_run(event):
            return
        self._last_output = event.content
        self._mount_code_write(event.content, event.path, event.lines, event.old_content)

    def on_agent_replace_spinner(self, event: AgentReplaceSpinner):
        if not self._is_current_run(event):
            return
        # Remove live thinking stream widget
        try:
            self.query_one("#thinking-stream").remove()
        except Exception:
            pass
        # Replace spinner with final collapsible ThinkingBlock
        if self._current_spinner_id:
            self._remove_spinner(
                self._current_spinner_id,
                event.thinking, event.tokens, event.elapsed,
            )
            self._current_spinner_id = None
        # New spinner for next iteration (if agent still running)
        if self._agent_active:
            self._current_spinner_id = self._add_spinner()

    def on_agent_finished(self, event: AgentFinished):
        if not self._is_current_run(event):
            return
        self._finish_agent()

    # ------------------------------------------------------------------
    # Cancel / Quit
    # ------------------------------------------------------------------

    def _cancel_agent(self):
        """Stop the running agent thread."""
        tid = self._agent_thread_id
        if tid:
            self._agent_thread_id = None
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(tid), ctypes.py_object(KeyboardInterrupt),
            )

    def action_cancel(self):
        now = time.time()

        # First press: try to copy selected text
        if now - self._last_ctrl_c > 2.0:
            selected = ""
            try:
                prompt = self.query_one("#prompt", PromptInput)
                selected = prompt.selected_text or ""
            except Exception:
                pass
            if selected:
                pyperclip.copy(selected)
                self.notify("Copied!", timeout=1)
                return

            self._last_ctrl_c = now
            if self._agent_active:
                self.notify("Ctrl+C again to cancel", timeout=2)
            else:
                self.notify("Ctrl+C again to quit", timeout=2)
            return

        # Second press within 2s: stop/quit
        self._last_ctrl_c = 0
        if self._agent_active:
            self._cancel_agent()
            self._finish_agent()
            self._mount_block("[yellow]Stopped[/yellow]")
        else:
            self._quit()

    def _quit(self):
        self._ctx.save_session()
        self.exit()

    def action_quit(self):
        self._quit()

    # ------------------------------------------------------------------
    # UI monkey-patching
    # ------------------------------------------------------------------

    _PATCH_NAMES = [
        "show_file_write", "show_code", "show_file_patch", "show_diff",
        "show_sandbox", "show_success", "show_failure", "show_repair",
        "show_error", "show_info", "show_tool_result", "show_file_read",
        "show_files_tree",
    ]

    def _patch_ui(self):
        for n in self._PATCH_NAMES:
            self._originals[n] = getattr(ui_module, n)
            setattr(ui_module, n, getattr(self, f"_ui_{n}"))
        self._originals["ThinkingSpinner"] = ui_module.ThinkingSpinner
        app_ref = self
        run_id = self._agent_run_id

        class _Spinner:
            """Streams thinking chunks to UI, replaces spinner on exit."""

            def __init__(self):
                self._thinking = ""
                self._start = time.time()
                self._tok_n = 0

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                tokens = len(self._thinking) // 4
                elapsed = time.time() - self._start
                app_ref._post(AgentReplaceSpinner(
                    run_id, self._thinking, tokens, elapsed,
                ))

            def on_thinking(self, tok):
                self._thinking += tok
                self._tok_n += 1
                if self._tok_n % 5 == 0:
                    elapsed = time.time() - self._start
                    app_ref._post(ThinkingChunk(
                        run_id, self._thinking, self._tok_n, elapsed,
                    ))

            def on_content(self, _tok):
                pass

        ui_module.ThinkingSpinner = _Spinner

    def _unpatch_ui(self):
        for n, fn in self._originals.items():
            setattr(ui_module, n, fn)
        self._originals.clear()

    # ------------------------------------------------------------------
    # Spinner
    # ------------------------------------------------------------------

    def _add_spinner(self) -> str:
        sid = f"spinner-{self._spinner_n}"
        self._spinner_n += 1
        self._mount_widget(SpinnerWidget(id=sid, classes="block"))
        return sid

    def _remove_spinner(self, sid: str, thinking: str, tokens: int, elapsed: float):
        try:
            self.query_one(f"#{sid}").remove()
        except Exception:
            pass
        thinking = re.sub(r'\n{3,}', '\n\n', thinking).strip()
        if thinking:
            self._mount_widget(ToggleBlock(
                header=f"[dim]Thinking ({tokens} tokens) \u00b7 {elapsed:.1f}s[/dim]",
                body=Text(thinking, style="dim"),
                default_collapsed=True,
                classes="block",
            ))

    # ------------------------------------------------------------------
    # UI adapter — called from worker thread, posts messages
    # ------------------------------------------------------------------

    def _post(self, msg: Message):
        """Post message and yield GIL so main thread can process + render."""
        self.post_message(msg)
        time.sleep(0.015)

    def _ui_show_file_write(self, path, lines):
        self._pending_write = (path, lines)

    def _ui_show_code(self, content, path="", old_content=None):
        run_id = self._agent_run_id
        pw = self._pending_write
        if pw:
            self._pending_write = None
            self._post(AgentCodeWrite(run_id, content, pw[0], pw[1], old_content))
        else:
            self._post(AgentCodeRaw(run_id, content, path))

    def _ui_show_file_patch(self, path):
        self._pending_patch_path = path

    def _ui_show_diff(self, old, new):
        run_id = self._agent_run_id
        path = self._pending_patch_path or ""
        self._pending_patch_path = None
        self._post(AgentDiff(run_id, old, new, path))

    def _ui_show_sandbox(self, result):
        self._post(AgentSandbox(self._agent_run_id, result))

    def _ui_show_success(self, summary, iterations, elapsed=0, files=None):
        self._post(AgentSuccess(self._agent_run_id, summary, iterations, elapsed, files))

    def _ui_show_failure(self, iterations):
        self._post(AgentBlock(
            self._agent_run_id,
            f"[yellow]\u2717 Reached max iterations ({iterations})[/yellow]",
        ))

    def _ui_show_repair(self, iteration, max_iter):
        filled = int(iteration / max_iter * 10)
        bar = "\u2588" * filled + "\u2591" * (10 - filled)
        self._post(AgentBlock(
            self._agent_run_id,
            f"[yellow]~ Repairing [{bar}] {iteration}/{max_iter}[/yellow]",
        ))

    def _ui_show_error(self, msg):
        self._post(AgentBlock(
            self._agent_run_id, f"[red]\u2717 {escape(msg)}[/red]",
        ))

    def _ui_show_info(self, msg):
        self._post(AgentBlock(self._agent_run_id, msg))

    def _ui_show_tool_result(self, name, result):
        lines = result.strip().split("\n")
        preview = escape(lines[0][:120]) if lines else ""
        if len(lines) > 1:
            preview += f" (+{len(lines)-1} lines)"
        self._post(AgentBlock(
            self._agent_run_id, f"[dim]{escape(name)}: {preview}[/dim]",
        ))

    def _ui_show_file_read(self, path):
        full = os.path.join(os.getcwd(), os.path.normpath(path))
        extra = ""
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as f:
                n = sum(1 for _ in f)
            extra = f" [dim]({n} lines)[/dim]"
        self._post(AgentBlock(
            self._agent_run_id,
            f"Reading [yellow]{escape(path)}[/yellow]{extra}",
        ))

    def _ui_show_files_tree(self, _files):
        pass

    # ------------------------------------------------------------------
    # Mount helpers — main thread
    # ------------------------------------------------------------------

    def _mount_code_write(self, content: str, path: str, lines_count: int, old_content):
        if old_content is not None:
            self._mount_diff(old_content, content, path, header_prefix="Wrote")
            return
        code_lines = content.split("\n")
        parts = []
        for i, line in enumerate(code_lines, 1):
            parts.append(
                f"[green]+[/green] [dim]{i:>3}[/dim]  [green]{escape(line)}[/green]"
            )
        self._mount_widget(ToggleBlock(
            header=f"Wrote [yellow]{escape(path)}[/yellow] ({lines_count} lines)",
            body=Text.from_markup("\n".join(parts)),
            default_collapsed=False,
            classes="block",
        ))

    def _mount_code_raw(self, content: str, path: str):
        self._mount_widget(ToggleBlock(
            header=f"[yellow]{escape(path)}[/yellow]",
            body=Syntax(content, "lua", theme="monokai", line_numbers=True),
            default_collapsed=False,
            classes="block",
        ))

    def _mount_diff(self, old: str, new: str, path: str = "",
                    header_prefix: str = "Patched"):
        diff = list(difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            n=3,
        ))
        if not diff:
            return
        parts: list[str] = []
        old_ln = new_ln = 0
        for line in diff:
            line = line.rstrip("\n")
            if line.startswith("---") or line.startswith("+++"):
                continue
            m = re.match(r"^@@ -(\d+),?\d* \+(\d+),?\d* @@", line)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(2))
                parts.append(f"[cyan]{escape(line)}[/cyan]")
                continue
            if line.startswith("-"):
                parts.append(
                    f"[red]{old_ln:>4} - {escape(line[1:])}[/red]"
                )
                old_ln += 1
            elif line.startswith("+"):
                parts.append(
                    f"[green]{new_ln:>4} + {escape(line[1:])}[/green]"
                )
                new_ln += 1
            else:
                txt = line[1:] if line.startswith(" ") else line
                parts.append(f"[dim]{new_ln:>4}   {escape(txt)}[/dim]")
                old_ln += 1
                new_ln += 1
        header_path = f" [yellow]{escape(path)}[/yellow]" if path else ""
        self._mount_widget(ToggleBlock(
            header=f"{header_prefix}{header_path}",
            body=Text.from_markup("\n".join(parts)),
            default_collapsed=False,
            classes="block",
        ))

    def _mount_sandbox(self, result):
        if isinstance(result, str):
            clr = "red" if ("FAILED" in result or "ERRORS" in result) else "green"
            self._mount_block(f"[{clr}]{escape(result.splitlines()[0])}[/{clr}]")
            return

        display = result.get("display", "")
        stdout = result.get("stdout", "").rstrip()
        stderr = result.get("stderr", "").rstrip()
        success = result.get("success", True)

        status_parts = []
        if "ERRORS" in display:
            status_parts.append("luacheck ERRORS")
        elif "luacheck: OK" in display:
            status_parts.append("luacheck OK")
        elif "skipped" in display:
            status_parts.append("luacheck skipped")
        if "ERRORS" not in display:
            status_parts.append("lua54 OK" if success else "lua54 FAILED")

        status = " \u00b7 ".join(status_parts)
        icon = "\u2713" if success else "\u2717"
        color = "green" if success else "red"
        header = f"[{color}]{icon} {status}[/{color}]"

        body_lines: list[str] = []
        if stderr and not success:
            for sl in stderr.split("\n"):
                sl = sl.strip()
                if sl:
                    body_lines.append(f"  [red]\u2502 {escape(sl)}[/red]")
        if stdout:
            for sl in stdout.split("\n")[:5]:
                body_lines.append(f"  [dim]\u2502 {escape(sl)}[/dim]")
            extra = len(stdout.split("\n")) - 5
            if extra > 0:
                body_lines.append(f"  [dim]\u2502 ... +{extra} more lines[/dim]")

        if body_lines:
            self._mount_widget(ToggleBlock(
                header=header,
                body=Text.from_markup("\n".join(body_lines)),
                default_collapsed=success,
                classes="block",
            ))
        else:
            self._mount_block(header)

    def _mount_success(self, summary, iterations, elapsed, files):
        time_str = f" \u00b7 {elapsed:.1f}s" if elapsed else ""
        parts = [f"[green]\u2713 Done in {iterations} iteration(s){time_str}[/green]"]
        if summary:
            parts.append(f"  {escape(summary)}")
        if files:
            parts.append("  [bold]Files changed:[/bold]")
            sorted_f = sorted(files)
            for i, fp in enumerate(sorted_f):
                last = i == len(sorted_f) - 1
                pfx = "\u2514\u2500\u2500" if last else "\u251c\u2500\u2500"
                full = os.path.join(os.getcwd(), os.path.normpath(fp))
                n_info = ""
                if os.path.isfile(full):
                    with open(full, encoding="utf-8") as f:
                        n = sum(1 for _ in f)
                    n_info = f" [dim]({n} lines)[/dim]"
                parts.append(f"  {pfx} [yellow]{escape(fp)}[/yellow]{n_info}")
        self._mount_widget(Static(Text.from_markup("\n".join(parts)), classes="result"))

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def _handle_slash(self, cmd: str):
        parts = cmd.strip().split(None, 1)
        base = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if base == "/help":
            lines = ["[bold]Commands:[/bold]"]
            for name, desc in SLASH_COMMANDS.items():
                lines.append(f"  [bold]{name:11s}[/bold] {desc}")
            self._mount_block("\n".join(lines))

        elif base == "/copy":
            if not self._last_output:
                self._mount_block("[dim]Nothing to copy[/dim]")
            else:
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["clip"], input=self._last_output.encode("utf-8"),
                            check=True, creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        subprocess.run(
                            ["xclip", "-selection", "clipboard"],
                            input=self._last_output.encode("utf-8"), check=True,
                        )
                    n = len(self._last_output)
                    self._mount_block(f"[green]Copied {n} chars to clipboard[/green]")
                except Exception as e:
                    self._mount_block(f"[red]Copy failed: {escape(str(e))}[/red]")

        elif base == "/status":
            files = ", ".join(sorted(self._ctx.files_touched)) or "(none)"
            lines = [
                f"[bold]Iterations:[/bold]  {self._ctx.total_iterations}",
                f"[bold]Tokens:[/bold]      ~{self._ctx.estimated_tokens()}",
                f"[bold]Files:[/bold]       {files}",
            ]
            self._mount_block("\n".join(lines))

        elif base == "/cost":
            it = self._ctx.input_chars // 4
            ot = self._ctx.output_chars // 4
            lines = [
                f"[bold]Input:[/bold]   ~{it} tokens ({self._ctx.input_chars} chars)",
                f"[bold]Output:[/bold]  ~{ot} tokens ({self._ctx.output_chars} chars)",
                f"[bold]Total:[/bold]   ~{it + ot} tokens",
            ]
            self._mount_block("\n".join(lines))

        elif base == "/undo":
            result = self._ctx.undo_last_turn()
            if not result.get("undone"):
                self._mount_block("[dim]Nothing to undo[/dim]")
            else:
                req = result["request"].replace("\n", " ")
                if len(req) > 60:
                    req = req[:60] + "..."
                lines = [f"[yellow]\u21a9 Undone:[/yellow] \"{escape(req)}\""]
                for action, path in result["files_reverted"]:
                    color = "yellow" if action in ("restored", "deleted") else "dim"
                    lines.append(f"  [{color}]{action} {escape(path)}[/{color}]")
                lines.append(
                    f"[dim]Context: -{result['iterations_removed']} messages, "
                    f"-{result['memory_rows_removed']} memory rows, "
                    f"{result['turns_remaining']} prompt(s) left[/dim]"
                )
                self._mount_block("\n".join(lines))

        elif base == "/history":
            if not self._ctx.history:
                self._mount_block("[dim]No tasks yet[/dim]")
            else:
                lines = []
                for i, h in enumerate(self._ctx.history, 1):
                    mark = "[green]+[/green]" if h["success"] else "[red]x[/red]"
                    task = escape(h["task"][:50])
                    lines.append(
                        f"{mark} {i}. \"{task}\" \u2014 "
                        f"{h['iterations']} iter \u00b7 {h['time']:.1f}s"
                    )
                self._mount_block("\n".join(lines))

        elif base == "/compact":
            self._mount_block("[dim]Compacting context...[/dim]")
            self._run_compact()

        elif base == "/model":
            if arg:
                old_model = config.LLM_MODEL
                config.LLM_MODEL = arg
                self._mount_block(f"Model switched to [bold]{escape(arg)}[/bold]")
                # Unload old model from VRAM in background
                def _unload(model_name):
                    try:
                        payload = json.dumps(
                            {"model": model_name, "keep_alive": 0}
                        ).encode()
                        req = urllib.request.Request(
                            "http://localhost:11434/api/generate",
                            data=payload,
                            headers={"Content-Type": "application/json"},
                        )
                        urllib.request.urlopen(req, timeout=5)
                    except Exception:
                        pass
                threading.Thread(
                    target=_unload, args=(old_model,), daemon=True,
                ).start()
                # Invalidate model cache so new models show up
                self.query_one("#prompt", PromptInput).invalidate_model_cache()
            else:
                prompt_w = self.query_one("#prompt", PromptInput)
                models = prompt_w._get_ollama_models()
                lines = [
                    f"Current model: [bold]{escape(config.LLM_MODEL)}[/bold]",
                ]
                if models:
                    lines.append("[bold]Available:[/bold]")
                    for m in models:
                        marker = " *" if m == config.LLM_MODEL else ""
                        lines.append(f"  {escape(m)}{marker}")
                self._mount_block("\n".join(lines))

        elif base == "/plan":
            from localscript.agent import _read_tracker
            tracker = _read_tracker()
            if tracker:
                self._mount_block(escape(tracker))
            else:
                self._mount_block("[dim]No active task plan[/dim]")

        elif base == "/memory":
            sub = arg.strip()
            if sub == "--on" or sub == "on":
                # memory_enable() can be slow when phase 2 embeddings are
                # active — it embeds the iterations buffer and backfills any
                # pre-existing rows. Run it on a worker thread so the TUI
                # stays responsive; the result is mounted via _post() when
                # the embed calls return.
                self._mount_block("[dim]Enabling memory...[/dim]")
                self._run_memory_enable()
            elif sub == "--off" or sub == "off":
                self._ctx.memory_disable()
                self._mount_block("[yellow]Memory: OFF[/yellow] (DB preserved on disk)")
            elif sub == "clear":
                stats = self._ctx.memory_clear()
                self._mount_block(
                    f"[yellow]Memory cleared[/yellow] ({stats['rows']} rows remain)"
                )
            else:
                stats = self._ctx.memory_stats()
                state = "[green]ON[/green]" if stats["enabled"] else "[yellow]OFF[/yellow]"
                lines = [
                    f"[bold]Memory:[/bold] {state}",
                    f"[bold]Rows:[/bold]     {stats['rows']}",
                    f"[bold]Sessions:[/bold] {stats['sessions']}",
                    f"[bold]DB size:[/bold]  {stats['db_bytes']} bytes",
                    f"[bold]Window:[/bold]  {stats['pinned_recent']} pinned recent, "
                    f"top-{stats['top_k']} retrieved",
                    "[dim]Subcommands: --on / --off / clear[/dim]",
                ]
                self._mount_block("\n".join(lines))

        elif base == "/resume":
            sessions = Context.list_sessions(10)
            if not sessions:
                self._mount_block("[dim]No saved chats[/dim]")
                return
            if arg:
                try:
                    idx = int(arg) - 1
                    if not (0 <= idx < len(sessions)):
                        self._mount_block("[red]Invalid choice[/red]")
                        return
                    self._ctx = Context.load_session(sessions[idx]["path"])
                    for child in list(self.query_one("#messages").children):
                        child.remove()
                    self._show_banner()
                    self._restore_chat_history()
                except ValueError:
                    self._mount_block("[red]Usage: /resume N[/red]")
                except Exception as e:
                    self._mount_block(f"[red]Resume failed: {escape(str(e))}[/red]")
                return
            lines = ["[bold]Recent sessions:[/bold]"]
            for i, s in enumerate(sessions, 1):
                ts = s["timestamp"]
                fmt = (f"{ts[6:8]}.{ts[4:6]} {ts[9:11]}:{ts[11:13]}"
                       if len(ts) >= 13 else ts)
                nf = s["files"]
                fw = "file" if nf == 1 else "files"
                lines.append(
                    f"  {i}. [{fmt}] \"{escape(s['task'])}\" \u00b7 "
                    f"{s['iters']} iter \u00b7 {nf} {fw}"
                )
            lines.append("\n  [dim]/resume N to load[/dim]")
            self._mount_block("\n".join(lines))

        elif base == "/clear":
            self._ctx.clear()
            for child in list(self.query_one("#messages").children):
                child.remove()
            self._show_banner()
            self._mount_block("Context cleared.")

        elif base == "/think":
            config.THINKING_MODE = "on"
            self._mount_block("[bold]Thinking mode:[/bold] always on")

        elif base == "/no_think":
            config.THINKING_MODE = "off"
            self._mount_block("[bold]Thinking mode:[/bold] always off")

        elif base == "/auto_think":
            config.THINKING_MODE = "auto"
            self._mount_block("[bold]Thinking mode:[/bold] auto (difficulty classifier)")

        else:
            self._mount_block(
                f"[red]Unknown command: {escape(base)}[/red]. Type /help"
            )
