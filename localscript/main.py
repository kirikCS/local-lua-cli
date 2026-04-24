"""Entry point: CLI with REPL and one-shot modes."""

import sys
import os

# Enable ANSI escape codes in Windows PowerShell / cmd
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        os.system("")

import argparse

if sys.platform == "win32":
    import msvcrt

from localscript import config, agent, ui
from localscript.context import Context


def _flush_stdin():
    """Drain any buffered stdin characters (keypresses during streaming)."""
    if sys.platform == "win32":
        while msvcrt.kbhit():
            msvcrt.getch()
    else:
        import select
        while select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.readline()


SLASH_COMMANDS = {
    "/help":       "Show this help",
    "/status":     "Show session stats",
    "/history":    "Show task history",
    "/cost":       "Show token usage (input/output)",
    "/undo":       "Undo the last prompt (files, context, memory)",
    "/compact":    "Compress context manually",
    "/model":      "Show or switch model",
    "/think":      "Always enable thinking",
    "/no_think":   "Always disable thinking",
    "/auto_think": "Auto-decide thinking by prompt difficulty",
    "/memory":     "Long-term memory: --on, --off, clear",
    "/resume":     "Resume a saved chat",
    "/clear":      "Clear conversation context",
    "/exit":       "Exit ICEQ",
}


def _fmt_ts(ts: str) -> str:
    """Format 20260405_193000 -> 04.05 19:30."""
    if len(ts) >= 13:
        return f"{ts[6:8]}.{ts[4:6]} {ts[9:11]}:{ts[11:13]}"
    return ts


def _handle_slash(cmd: str, ctx: Context) -> Context:
    """Handle a slash command. Returns (possibly new) context."""
    parts = cmd.strip().split(None, 1)
    base = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if base == "/help":
        for name, desc in SLASH_COMMANDS.items():
            ui.console.print(f"  [bold]{name:10s}[/bold] {desc}")
    elif base == "/status":
        ui.console.print(f"  [bold]Iterations:[/bold]  {ctx.total_iterations}")
        ui.console.print(f"  [bold]Tokens:[/bold]      ~{ctx.estimated_tokens()}")
        if ctx.files_touched:
            ui.console.print(f"  [bold]Files:[/bold]       {', '.join(sorted(ctx.files_touched))}")
        else:
            ui.console.print(f"  [bold]Files:[/bold]       (none)")
    elif base == "/cost":
        in_tok = ctx.input_chars // 4
        out_tok = ctx.output_chars // 4
        ui.console.print(f"  [bold]Input:[/bold]   ~{in_tok} tokens ({ctx.input_chars} chars)")
        ui.console.print(f"  [bold]Output:[/bold]  ~{out_tok} tokens ({ctx.output_chars} chars)")
        ui.console.print(f"  [bold]Total:[/bold]   ~{in_tok + out_tok} tokens")
    elif base == "/undo":
        result = ctx.undo_last_turn()
        if not result.get("undone"):
            ui.console.print("  [dim]Nothing to undo[/dim]")
        else:
            req = result["request"].replace("\n", " ")
            if len(req) > 60:
                req = req[:60] + "..."
            ui.console.print(f"  [yellow]\u21a9 Undone:[/yellow] \"{req}\"")
            files = result["files_reverted"]
            if files:
                for action, path in files:
                    color = "yellow" if action in ("restored", "deleted") else "dim"
                    ui.console.print(f"    [{color}]{action} {path}[/{color}]")
            ui.console.print(
                f"  [dim]Context: -{result['iterations_removed']} messages, "
                f"-{result['memory_rows_removed']} memory rows, "
                f"{result['turns_remaining']} prompt(s) left[/dim]"
            )
    elif base == "/history":
        if not ctx.history:
            ui.console.print("  [dim]No tasks yet[/dim]")
        else:
            for i, h in enumerate(ctx.history, 1):
                mark = "[green]+[/green]" if h["success"] else "[red]x[/red]"
                task = h["task"][:50]
                ui.console.print(
                    f"  {mark} {i}. \"{task}\" — {h['iterations']} iter · {h['time']:.1f}s"
                )
    elif base == "/compact":
        result = ctx.compact()
        ui.console.print(
            f"  [white]Compacted: {result['before']} -> {result['after']} messages "
            f"({result['summarized']} summarized, {result['kept']} kept)[/white]"
        )
    elif base == "/model":
        if arg:
            config.LLM_MODEL = arg
            ui.console.print(f"  [white]Model switched to [bold]{arg}[/bold][/white]")
        else:
            ui.console.print(f"  [white]Current model: [bold]{config.LLM_MODEL}[/bold][/white]")
    elif base == "/memory":
        sub = arg.strip()
        if sub == "--on" or sub == "on":
            stats = ctx.memory_enable()
            ui.console.print(
                f"  [green]Memory: ON[/green] "
                f"({stats['rows']} rows, {stats['db_bytes']} bytes "
                f"at .iceq/memory.sqlite)"
            )
        elif sub == "--off" or sub == "off":
            ctx.memory_disable()
            ui.console.print("  [yellow]Memory: OFF[/yellow] (DB preserved on disk)")
        elif sub == "clear":
            stats = ctx.memory_clear()
            ui.console.print(f"  [yellow]Memory cleared[/yellow] ({stats['rows']} rows remain)")
        else:
            stats = ctx.memory_stats()
            state = "[green]ON[/green]" if stats["enabled"] else "[yellow]OFF[/yellow]"
            ui.console.print(f"  [bold]Memory:[/bold] {state}")
            ui.console.print(f"  [bold]Rows:[/bold]     {stats['rows']}")
            ui.console.print(f"  [bold]Sessions:[/bold] {stats['sessions']}")
            ui.console.print(f"  [bold]DB size:[/bold]  {stats['db_bytes']} bytes")
            ui.console.print(
                f"  [bold]Window:[/bold]  {stats['pinned_recent']} pinned recent, "
                f"top-{stats['top_k']} retrieved"
            )
            ui.console.print("  [dim]Subcommands: --on / --off / clear[/dim]")
    elif base == "/resume":
        sessions = Context.list_sessions(20)
        if not sessions:
            ui.console.print("  [dim]No saved chats[/dim]")
        else:
            page_size = 5
            page = 0
            while True:
                start = page * page_size
                chunk = sessions[start:start + page_size]
                if not chunk:
                    break
                ui.console.print("  [bold]Recent sessions:[/bold]")
                for i, s in enumerate(chunk, start + 1):
                    nf = s["files"]
                    fword = "file" if nf == 1 else "files"
                    ui.console.print(
                        f"  {i}. [{_fmt_ts(s['timestamp'])}] "
                        f"\"{s['task']}\" · {s['iters']} iter · {nf} {fword}"
                    )
                has_more = start + page_size < len(sessions)
                if has_more:
                    ui.console.print(f"  [dim][n] Show more[/dim]")
                ui.console.print()
                try:
                    choice = ui.console.input("  [dim]Enter number to resume, or press Enter to cancel:[/dim] ").strip()
                except (EOFError, KeyboardInterrupt):
                    ui.console.print("  [dim]Cancelled[/dim]")
                    break
                if not choice:
                    break
                if choice.lower() == "n" and has_more:
                    page += 1
                    continue
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(sessions):
                        ctx = Context.load_session(sessions[idx]["path"])
                        ui.console.print(f"  [green]Resumed ({ctx.total_iterations} iterations, {len(ctx.files_touched)} files)[/green]")
                    else:
                        ui.console.print("  [red]Invalid choice[/red]")
                except ValueError:
                    ui.console.print("  [dim]Cancelled[/dim]")
                break
    elif base == "/clear":
        ctx.clear()
        ui.console.print("  [white]Context cleared.[/white]")
    elif base == "/think":
        config.THINKING_MODE = "on"
        ui.console.print("  [bold]Thinking mode:[/bold] always on")
    elif base == "/no_think":
        config.THINKING_MODE = "off"
        ui.console.print("  [bold]Thinking mode:[/bold] always off")
    elif base == "/auto_think":
        config.THINKING_MODE = "auto"
        ui.console.print("  [bold]Thinking mode:[/bold] auto (difficulty classifier)")
    else:
        ui.console.print(f"  [red]x Unknown command: {base}[/red]. Type /help")
    return ctx


def _save_and_bye(ctx: Context):
    """Save session and print goodbye."""
    saved = ctx.save_session()
    if saved:
        print(f"  Session saved.")
    print("  Bye!")


def _repl():
    """Interactive REPL loop with persistent context."""
    ui.banner()
    ui.console.print("[white]  Type a task, /help for commands, or /exit to quit.[/white]\n")

    ctx = Context()  # single context for entire REPL session

    try:
        while True:
            _flush_stdin()
            try:
                user_input = ui.console.input("[bold cyan]>[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                _save_and_bye(ctx)
                return

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit", "/exit"):
                _save_and_bye(ctx)
                return

            if user_input.startswith("/"):
                ctx = _handle_slash(user_input, ctx)
                continue

            # Overwrite the raw input line(s) with styled version
            try:
                cols = os.get_terminal_size().columns
            except OSError:
                cols = 80
            # +2 for "> " prompt prefix
            raw_len = len(user_input) + 2
            num_lines = max(1, (raw_len + cols - 1) // cols)
            sys.stdout.write(f"\033[{num_lines}A" + "\033[K\n" * num_lines + f"\033[{num_lines}A")
            sys.stdout.flush()
            ui.console.print(f"  {user_input} ", style="bold on grey15")
            ui.console.print()
            try:
                agent.run(user_input, ctx=ctx)
                ui.console.print()
            except KeyboardInterrupt:
                print("\n  Stopped\n")
    except KeyboardInterrupt:
        print()
        _save_and_bye(ctx)


def _one_shot(task: str):
    """Execute a single task and exit."""
    ui.banner()
    ui.show_task(task)
    result = agent.run(task)
    sys.exit(0 if result else 1)


def main():
    parser = argparse.ArgumentParser(
        prog="iceq",
        description="ICEQ - Lua Code Agent",
    )
    parser.add_argument("task", nargs="*", help="Task (omit for REPL mode)")
    parser.add_argument("--model", default=None, help="Override LLM model name")
    parser.add_argument("--url", default=None, help="Override LLM server URL")
    parser.add_argument("--max-iter", type=int, default=None, help="Max agent iterations")
    parser.add_argument("--workdir", "-w", default=None, help="Working directory for the agent")
    parser.add_argument("--no-tui", action="store_true", help="Use classic Rich-based UI")
    args = parser.parse_args()

    if args.workdir:
        os.makedirs(args.workdir, exist_ok=True)
        os.chdir(args.workdir)

    if args.model:
        config.LLM_MODEL = args.model
    if args.url:
        config.LLM_URL = args.url
    if args.max_iter:
        config.MAX_ITERATIONS = args.max_iter

    try:
        if args.task:
            # One-shot: always Rich UI (output persists after exit)
            _one_shot(" ".join(args.task))
        elif args.no_tui:
            _repl()
        else:
            try:
                from localscript.tui import IceqApp
            except ImportError:
                print("  [TUI] textual not installed, using Rich fallback")
                print("  [TUI] pip install textual")
                print()
                _repl()
            else:
                app = IceqApp()
                app.run()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        import logging
        logging.shutdown()
        os._exit(0)
