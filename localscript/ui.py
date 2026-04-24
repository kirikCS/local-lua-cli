"""TUI: minimal Claude-Code-style terminal output."""

import difflib
import os
import random
import shutil
import time

import pyfiglet
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.markup import escape
from rich import box

from localscript.config import LLM_MODEL, LUA_BINARY, LUACHECK_BINARY, VERSION

console = Console()



# ---------------------------------------------------------------------------
# Thinking spinner with random phrases
# ---------------------------------------------------------------------------

THINKING_PHRASES = [
    "Cooking", "Vibing", "Brewing", "Crunching", "Pondering", "Hacking", "Crafting", "Thinking", "Dreaming", "Loading",
    "Solving", "Building", "Tuning", "Forging", "Conjuring", "Plotting", "Scheming", "Grinding", "Coding", "Debugging",
    "Optimizing", "Refactoring", "Testing", "Documenting", "Formatting", "Packaging", "Deploying", "Architecting"
]


SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class ThinkingSpinner:
    """Animated braille spinner with rotating fun phrases (every 12s)."""

    def __init__(self):
        self._thinking = ""
        self._content_tokens = 0
        self._start = time.time()
        self._tick = 0
        self._order = list(range(len(THINKING_PHRASES)))
        random.shuffle(self._order)
        self._live = Live(self, console=console, refresh_per_second=8, transient=True)

    def __rich__(self):
        self._tick += 1
        elapsed = time.time() - self._start
        frame = SPINNER_FRAMES[self._tick % len(SPINNER_FRAMES)]
        idx = self._order[int(elapsed / 12) % len(self._order)]
        phrase = THINKING_PHRASES[idx]
        toks = f" [dim](~{len(self._thinking) // 4} tokens)[/dim]" if self._thinking else ""
        line1 = f"  [cyan]{frame} {phrase}...[/cyan]{toks}"
        line2 = "  [dim]Ctrl+C stop[/dim]"
        return Text.from_markup(f"{line1}\n{line2}")

    def __enter__(self):
        self._live.__enter__()
        return self

    def __exit__(self, *exc):
        self._live.__exit__(*exc)
        if self._thinking:
            console.print(f"  [dim]Thinking (~{len(self._thinking) // 4} tokens)[/dim]")

    def on_thinking(self, token: str):
        self._thinking += token

    def on_content(self, token: str):
        self._content_tokens += 1


# ---------------------------------------------------------------------------
# Output helpers — minimal style
# ---------------------------------------------------------------------------

def banner():
    cwd = os.path.basename(os.getcwd()) or os.getcwd()
    has_luacheck = os.path.isfile(LUACHECK_BINARY) or shutil.which("luacheck")
    has_lua = os.path.isfile(LUA_BINARY) or shutil.which("lua54")
    tools_parts = [t for t, ok in [("luacheck", has_luacheck), ("lua54", has_lua)] if ok]

    # --- Left column: logo + subtitle ---
    art = pyfiglet.figlet_format("ICEQ", font="ansi_shadow").rstrip("\n")
    logo_lines = [Text(line, style="bold cyan") for line in art.split("\n") if line.strip()]
    left_parts = logo_lines + [
        Text(""),
        Text.from_markup("  [bold white]Lua Code Agent[/bold white]"),
    ]
    left = Text("\n").join(left_parts)

    # --- Right column: tips + info ---
    tools_str = " [white]·[/white] ".join(f"[white]{t}[/white]" for t in tools_parts) or "[dim]none[/dim]"
    has_chats = os.path.isdir(".iceq/chats") and os.listdir(".iceq/chats")
    resume_tip = "\n[white]  · /resume to continue a chat[/white]" if has_chats else ""
    right_text = (
        "[bold white]Tips for getting started[/bold white]\n"
        "[white]  · Type a Lua task to begin[/white]\n"
        "[white]  · /help for all commands[/white]"
        f"{resume_tip}\n"
        "\n"
        f"[dim]Model:[/dim]   [white]{LLM_MODEL}[/white]\n"
        f"[dim]Workdir:[/dim] [white]./{cwd}[/white]\n"
        f"[dim]Tools:[/dim]   {tools_str}"
    )

    # --- Assemble in a table for alignment ---
    table = Table(show_header=False, show_edge=False, box=None, padding=(0, 2), expand=True)
    table.add_column(ratio=1)
    table.add_column(ratio=1)
    table.add_row(left, Text.from_markup(right_text))

    console.print(Panel(
        table,
        title=f"[bold cyan]ICEQ[/bold cyan] [white]v{VERSION}[/white]",
        border_style="cyan",
        padding=(1, 2),
    ))
    console.print()


def show_task(request: str):
    console.print()
    console.print(f" {request} ", style="bold on grey15")
    console.print()


def show_code(code: str, path: str = "", old_content=None):
    syntax = Syntax(code, "lua", theme="monokai", line_numbers=True)
    title = f"[dim]{path}[/dim]" if path else ""
    console.print(Panel(syntax, title=title, border_style="dim", box=box.ROUNDED, padding=(0, 1)))


def show_file_write(path: str, lines: int):
    console.print(f"  [white]wrote [yellow]{path}[/yellow] ({lines} lines)[/white]")


def show_file_patch(path: str):
    console.print(f"  [white]patched [yellow]{path}[/yellow][/white]")


def show_file_read(path: str):
    full = os.path.join(os.getcwd(), os.path.normpath(path))
    lines = ""
    if os.path.isfile(full):
        with open(full, encoding="utf-8") as f:
            n = sum(1 for _ in f)
        lines = f" [dim]({n} lines)[/dim]"
    console.print(f"  [white]Reading [yellow]{path}[/yellow]{lines}[/white]")


def show_diff(old_code: str, new_code: str):
    """Git-style coloured diff: red deletions, green additions, dim context."""
    lines = list(difflib.unified_diff(
        old_code.splitlines(keepends=True),
        new_code.splitlines(keepends=True),
        fromfile="before", tofile="after", n=2,
    ))
    if not lines:
        return
    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("+++") or line.startswith("---"):
            console.print(f"  [bold dim]{line}[/bold dim]")
        elif line.startswith("@@"):
            console.print(f"  [cyan]{line}[/cyan]")
        elif line.startswith("+"):
            console.print(f"  [green]{line}[/green]")
        elif line.startswith("-"):
            console.print(f"  [red]{line}[/red]")
        else:
            console.print(f"  [dim]{line}[/dim]")


def show_sandbox(result):
    """Compact sandbox result — no Panel, just icon lines."""
    if isinstance(result, str):
        if "FAILED" in result or "ERRORS" in result:
            console.print(f"  [red]\u2717 {escape(result.splitlines()[0])}[/red]")
        else:
            console.print(f"  [green]\u2713 {escape(result.splitlines()[0])}[/green]")
        return

    display = result.get("display", "")
    stdout = result.get("stdout", "").rstrip()
    stderr = result.get("stderr", "").rstrip()
    success = result.get("success", True)

    # --- Status line: ✓/✗ luacheck · lua54 ---
    parts = []
    if "ERRORS" in display:
        parts.append("luacheck ERRORS")
    elif "luacheck: OK" in display:
        parts.append("luacheck OK")
    elif "skipped" in display:
        parts.append("luacheck skipped")

    if "ERRORS" not in display:
        parts.append("lua54 OK" if success else "lua54 FAILED")

    status = " \u00b7 ".join(parts)
    if success:
        console.print(f"  [green]\u2713 {status}[/green]")
    else:
        console.print(f"  [red]\u2717 {status}[/red]")

    # --- stderr (red, with │ prefix) ---
    if stderr and not success:
        for sl in stderr.split("\n"):
            sl = sl.strip()
            if sl:
                console.print(f"  [dim]\u2502[/dim] [red]{escape(sl)}[/red]")

    # --- stdout (dim, with │ prefix, max 5 lines) ---
    if stdout:
        stdout_lines = stdout.split("\n")
        for sl in stdout_lines[:5]:
            console.print(f"  [dim]\u2502 {escape(sl)}[/dim]")
        remaining = len(stdout_lines) - 5
        if remaining > 0:
            console.print(f"  [dim]\u2502 ... +{remaining} more lines[/dim]")

    console.print()


def show_files_tree(files: set[str]):
    """Show mini tree of changed files."""
    if not files:
        return
    sorted_files = sorted(files)
    console.print(f"  [bold]Files changed:[/bold]")
    for i, fpath in enumerate(sorted_files):
        is_last = i == len(sorted_files) - 1
        prefix = "\u2514\u2500\u2500" if is_last else "\u251c\u2500\u2500"
        full = os.path.join(os.getcwd(), os.path.normpath(fpath))
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as f:
                n = sum(1 for _ in f)
            console.print(f"  {prefix} [yellow]{fpath}[/yellow] [dim]({n} lines)[/dim]")
        else:
            console.print(f"  {prefix} [yellow]{fpath}[/yellow]")


def show_success(summary: str, iterations: int, elapsed: float = 0, files: set[str] | None = None):
    time_str = f" · {elapsed:.1f}s" if elapsed else ""
    console.print(f"\n  [green]+ Done in {iterations} iteration(s){time_str}[/green]")
    if summary:
        console.print(f"  [white]{summary}[/white]")
    if files:
        show_files_tree(files)


def show_failure(iterations: int):
    console.print(f"\n  [yellow]x Reached max iterations ({iterations})[/yellow]")


def show_repair(iteration: int, max_iter: int):
    filled = int(iteration / max_iter * 10)
    bar = "\u2588" * filled + "\u2591" * (10 - filled)
    console.print(f"  [yellow]~ Repairing [{bar}] {iteration}/{max_iter}[/yellow]")


def show_info(msg: str):
    console.print(f"  [white]{msg}[/white]")


def show_error(msg: str):
    console.print(f"  [red]x {msg}[/red]")


def show_tool_result(name: str, result: str):
    lines = result.strip().split("\n")
    preview = lines[0][:120] if lines else ""
    if len(lines) > 1:
        preview += f" (+{len(lines)-1} lines)"
    console.print(f"  [white]{name}: {preview}[/white]")
