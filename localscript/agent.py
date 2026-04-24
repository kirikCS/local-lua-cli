"""Agent: 6-state FSM for Lua code generation, file manipulation, and repair.

Supports two execution modes:
  1. Normal (single-loop): one user prompt → iterate up to MAX_ITERATIONS.
  2. Planned (multi-subtask): decompose into subtasks via a planning LLM call,
     execute each subtask in an isolated context with auto-compact, track
     progress in task_tracker.md, and ingest summaries into long-term memory.
"""

import json as _json
import os
import re
import time

from localscript import config
from localscript.config import MAX_ITERATIONS, SUBTASK_MAX_ITERATIONS
from localscript.context import Context
from localscript.difficulty import should_think
from localscript.llm import generate, parse_tool_call
from localscript import tools, ui


_PATH_RE = re.compile(r'(?:^|\s)((?:src|tests?|lib|modules?)/[\w/.-]+\.lua)\b', re.IGNORECASE)

_TRACKER_FILE = "task_tracker.md"

# System prompt for the planning call — kept short to fit a 4K window.
_PLANNING_PROMPT = (
    "You are a task planner for a Lua coding assistant. "
    "Given the user's request, break it into ordered subtasks.\n\n"
    "RULES:\n"
    "- Each subtask must be completable in 1-2 tool calls "
    "(one write_file or patch_file + auto-sandbox validation).\n"
    "- Order by dependency: create modules BEFORE consumers/tests.\n"
    "- If the task is trivial (single file, no deps), return ONE subtask.\n"
    "- Decide the optimal number of subtasks for the task.\n\n"
    "Output ONLY a JSON object:\n"
    '{"subtasks": [{"title": "short title", "description": "what to do"}]}'
)


# ---------------------------------------------------------------------------
# Helpers (truncation, sandbox, dispatch)
# ---------------------------------------------------------------------------

def _truncate_stdout(text: str, max_lines: int = 50) -> str:
    """Truncate long stdout: first 20 + '...' + last 10."""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    return "\n".join(
        lines[:20]
        + [f"... truncated {len(lines) - 30} lines ..."]
        + lines[-10:]
    )


def _format_sandbox_context(path: str, result: dict) -> str:
    """Format structured sandbox result for model context."""
    stdout = result["stdout"].rstrip()
    stderr = result["stderr"].rstrip()
    exit_code = result["exit_code"]

    parts = [f"SANDBOX RESULT for {path}:", f"Exit code: {exit_code}"]
    parts.append("STDOUT:")
    parts.append(_truncate_stdout(stdout) if stdout else "(empty)")
    if stderr:
        parts.append("STDERR:")
        parts.append(stderr)
    return "\n".join(parts)


def _auto_sandbox(path: str) -> dict | None:
    """If path is a .lua file that exists, run sandbox and return structured result."""
    if not path.endswith(".lua"):
        return None
    full = os.path.join(os.getcwd(), os.path.normpath(path))
    if not os.path.isfile(full):
        return None
    return tools.run_sandbox_full(path)


def _dispatch(tool: dict, ctx: Context) -> tuple[str, str, str | None, str | None]:
    """Execute a tool call. Returns (tool_name, result_message, path_or_None, old_content_or_None)."""
    name = tool.get("tool", "")

    if name == "write_file":
        path, content = tool.get("path", ""), tool.get("content", "")
        if not path or content is None:
            return name, "Error: write_file needs 'path' and 'content'.", None, None
        full = os.path.join(os.getcwd(), os.path.normpath(path))
        old = None
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as f:
                old = f.read()
        result = tools.write_file(path, content)
        ui.show_file_write(path, len(content.splitlines()))
        ui.show_code(content, path=path, old_content=old)
        return name, result, path, old

    if name == "patch_file":
        path, patches = tool.get("path", ""), tool.get("patches", [])
        if not path or not patches:
            return name, "Error: patch_file needs 'path' and 'patches'.", None, None
        full = os.path.join(os.getcwd(), os.path.normpath(path))
        old = None
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as f:
                old = f.read()
        result = tools.patch_file(path, patches)
        ui.show_file_patch(path)
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as f:
                new = f.read()
            ui.show_diff(old or "", new)
        return name, result, path, old

    if name == "read_file":
        path = tool.get("path", "")
        if not path:
            return name, "Error: read_file needs 'path'.", None, None
        result = tools.read_file(path)
        ui.show_file_read(path)
        return name, result, None, None

    if name == "list_files":
        path = tool.get("path", ".")
        result = tools.list_files(path)
        ui.show_tool_result("list_files", result)
        return name, result, None, None

    if name == "run_sandbox":
        path = tool.get("path", "")
        if not path:
            return name, "Error: run_sandbox needs 'path'.", None, None
        sb = tools.run_sandbox_full(path)
        ui.show_sandbox(sb)
        return name, sb["display"], None, None

    if name == "run_lua":
        code = tool.get("code", "")
        if not code or not code.strip():
            return name, "Error: run_lua needs 'code' (a Lua snippet).", None, None
        stdin = tool.get("stdin", "") or ""
        timeout = tool.get("timeout", config.EXECUTE_TIMEOUT)
        result = tools.run_lua_snippet(code, stdin=stdin, timeout=timeout)
        # Display a short preview to the user — full output goes to the model.
        label = f"run_lua ({'ok' if result['success'] else 'failed'})"
        preview = (result["stdout"] or result["stderr"]).strip().splitlines()
        if preview:
            ui.show_tool_result(label, preview[0][:200])
        else:
            ui.show_tool_result(label, "(no output)")
        # Format for the model: exit code, stdout, stderr — truncated.
        parts = [f"run_lua exit={result['returncode']}"]
        stdout = result["stdout"].rstrip()
        stderr = result["stderr"].rstrip()
        if stdout:
            parts.append("STDOUT:")
            parts.append(_truncate_stdout(stdout))
        else:
            parts.append("STDOUT: (empty)")
        if stderr:
            parts.append("STDERR:")
            parts.append(_truncate_stdout(stderr))
        return name, "\n".join(parts), None, None

    if name == "install_package":
        package = tool.get("package", "")
        if not package:
            return name, "Error: install_package needs 'package'.", None, None
        result = tools.install_package(package)
        ui.show_tool_result("install_package", result)
        return name, result, None, None

    if name == "lookup_docs":
        query = tool.get("query", "")
        top_k = tool.get("top_k", 3)
        if not query:
            return name, "Error: lookup_docs needs 'query'.", None, None
        result = tools.lookup_docs(query, top_k)
        ui.show_tool_result("lookup_docs", f"query: {query}")
        return name, result, None, None

    if name == "search_memory":
        query = tool.get("query", "")
        top_k = tool.get("top_k", 5)
        if not query:
            return name, "Error: search_memory needs 'query'.", None, None
        if not ctx.memory_enabled:
            return name, (
                "Error: long-term memory is disabled. The user can enable it "
                "with /memory --on. Do not retry this tool until then."
            ), None, None
        try:
            k = max(1, min(int(top_k), 10))
        except (TypeError, ValueError):
            k = 5
        rows = ctx.memory_search_preview(query, top_k=k)
        ui.show_tool_result("search_memory", f"query: {query}")
        if not rows:
            return name, f"No matches in long-term memory for: {query}", None, None
        parts = [f"Long-term memory \u2014 top {len(rows)} match(es) for {query!r}:"]
        for i, r in enumerate(rows, 1):
            body = r.get("content", "")
            if len(body) > 800:
                body = body[:800] + "...[truncated]"
            parts.append(
                f"\n--- Match {i} (turn {r.get('turn_idx', '?')}, "
                f"{r.get('role', '?')}, score {r.get('score', 0)}) ---"
            )
            parts.append(body)
        return name, "\n".join(parts), None, None

    return name, (
        f"Error: unknown tool '{name}'. Available: write_file, patch_file, "
        "read_file, list_files, run_sandbox, run_lua, lookup_docs, "
        "search_memory, install_package, message, complete_task."
    ), None, None


_VALID_TOOLS = frozenset({
    "write_file", "patch_file", "read_file", "list_files",
    "run_sandbox", "run_lua", "install_package",
    "lookup_docs", "search_memory",
})


# ---------------------------------------------------------------------------
# Task tracker file helpers
# ---------------------------------------------------------------------------

def _write_tracker(request: str, subtasks: list[dict], ctx: Context):
    """Create/overwrite task_tracker.md with the plan.

    Tracks the file via ctx so /undo can revert it.
    """
    old = None
    if os.path.isfile(_TRACKER_FILE):
        with open(_TRACKER_FILE, "r", encoding="utf-8") as f:
            old = f.read()
    lines = ["# Task Plan", ""]
    lines.append(f"## Original request")
    lines.append(request)
    lines.append("")
    lines.append("## Subtasks")
    for i, s in enumerate(subtasks, 1):
        lines.append(f"- [ ] {i}. {s['title']} \u2014 {s['description']}")
    lines.append("")
    lines.append("## Current subtask: 1")
    with open(_TRACKER_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    ctx.track_file(_TRACKER_FILE, old)


def _mark_tracker(index: int, status: str, summary: str, ctx: Context):
    """Mark subtask as done or failed and advance the current pointer.

    Tracks the file via ctx so /undo can revert individual subtask marks.
    """
    if not os.path.isfile(_TRACKER_FILE):
        return
    with open(_TRACKER_FILE, "r", encoding="utf-8") as f:
        old = f.read()

    mark = "x" if status == "done" else "!"
    content = old.replace(f"- [ ] {index}.", f"- [{mark}] {index}.", 1)

    # Insert summary line after the marked subtask
    if summary:
        out_lines = content.split("\n")
        for i, line in enumerate(out_lines):
            if line.lstrip().startswith(f"- [{mark}] {index}."):
                out_lines.insert(i + 1, f"  Summary: {summary}")
                break
        content = "\n".join(out_lines)

    # Advance current subtask pointer
    content = re.sub(
        r"## Current subtask: \d+",
        f"## Current subtask: {index + 1}",
        content,
    )
    with open(_TRACKER_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    ctx.track_file(_TRACKER_FILE, old)


def _read_tracker() -> str:
    """Read the tracker file content, or empty string if not present."""
    if not os.path.isfile(_TRACKER_FILE):
        return ""
    with open(_TRACKER_FILE, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Planning — decompose a request into subtasks
# ---------------------------------------------------------------------------

def _plan_task(request: str) -> list[dict] | None:
    """Ask the model to decompose *request* into subtasks.

    Returns a list of {"title": str, "description": str} dicts, or None if
    the model returns a single subtask (no plan needed) or fails to parse.
    """
    messages = [
        {"role": "system", "content": _PLANNING_PROMPT},
        {"role": "user", "content": request},
    ]
    try:
        content, _ = generate(
            messages,
            enable_thinking=True,
            response_format="json",
            max_tokens=1024,
        )
        data = _json.loads(content.strip())
        subtasks = data.get("subtasks", [])
        if not isinstance(subtasks, list) or not subtasks:
            return None
        result = [
            {
                "title": s.get("title", f"Step {i + 1}"),
                "description": s.get("description", ""),
            }
            for i, s in enumerate(subtasks)
            if isinstance(s, dict)
        ]
        # Single-subtask plan → skip planning overhead
        if len(result) <= 1:
            return None
        return result
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Planned execution — run each subtask in an isolated context
# ---------------------------------------------------------------------------

def _run_planned(
    request: str,
    subtasks: list[dict],
    ctx: Context,
) -> str | None:
    """Execute a planned task by running each subtask with context resets.

    For each subtask:
      1. Reset the iterations buffer (memory survives).
      2. Build a minimal instruction referencing the tracker + subtask.
      3. Run the normal agent loop (max SUBTASK_MAX_ITERATIONS).
      4. Mark the tracker as done (or failed).
      5. Ingest a summary into memory for future subtasks to retrieve.

    Returns the final tracker content as the run summary.
    """
    t0 = time.time()
    results: list[tuple[str, str, str]] = []  # (status, title, summary)

    for i, subtask in enumerate(subtasks, 1):
        ui.show_info(
            f"[bold]\u25b6 Subtask {i}/{len(subtasks)}:[/bold] {subtask['title']}"
        )

        # Build a focused instruction for this subtask. Include the tracker
        # so the model sees what's done, and the subtask description.
        tracker = _read_tracker()
        instruction = (
            f"You are executing subtask {i} of a multi-step plan.\n"
            f"PLAN:\n{tracker}\n\n"
            f"YOUR CURRENT TASK: {subtask['title']}\n"
            f"{subtask['description']}\n\n"
            "Complete ONLY this subtask. When done, call complete_task."
        )

        # Reset iterations for a fresh context window.
        ctx.reset_for_subtask()

        # Run the normal agent loop for this subtask.
        summary = run(
            instruction, ctx,
            _is_subtask=True,
            _max_iterations=SUBTASK_MAX_ITERATIONS,
        )

        if summary and summary != "Plan cancelled":
            _mark_tracker(i, "done", summary, ctx)
            ctx._ingest(
                "subtask_summary",
                f"Subtask {i} done: {subtask['title']}. {summary}",
            )
            results.append(("done", subtask["title"], summary))
            ui.show_info(f"  [green]\u2713 Done[/green]")
        else:
            _mark_tracker(i, "failed", "Max iterations exhausted", ctx)
            ctx._ingest(
                "subtask_summary",
                f"Subtask {i} FAILED: {subtask['title']}. Max iterations exhausted.",
            )
            results.append(("failed", subtask["title"], "Max iterations"))
            ui.show_info(f"  [red]! Failed[/red]")

    elapsed = time.time() - t0
    done = sum(1 for s, _, _ in results if s == "done")
    failed = sum(1 for s, _, _ in results if s == "failed")
    ui.show_info(
        f"Plan complete: {done}/{len(subtasks)} subtasks done, "
        f"{failed} failed ({elapsed:.1f}s)"
    )
    ctx.add_history(request, sum(1 for _ in results), elapsed, failed == 0)
    return _read_tracker() or f"{done}/{len(subtasks)} subtasks done"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(
    user_request: str,
    ctx: Context | None = None,
    on_plan: 'callable | None' = None,
    _is_subtask: bool = False,
    _max_iterations: int | None = None,
) -> str | None:
    """Run the agent loop. Returns final summary or None.

    Parameters
    ----------
    user_request : str
        The user's natural-language task.
    ctx : Context | None
        Conversation context. Created fresh if None (one-shot mode).
    on_plan : callable | None
        Callback ``on_plan(subtasks: list[dict]) -> bool``. Called when the
        agent decides to plan. Receives the subtask list, returns True to
        proceed or False to cancel. If None, plans are executed silently.
        Ignored when ``_is_subtask`` is True (no recursive planning).
    _is_subtask : bool
        True when called from _run_planned (disables planning detection).
    _max_iterations : int | None
        Override for MAX_ITERATIONS (used by subtask loop).
    """
    if ctx is None:
        ctx = Context()

    max_iter = _max_iterations or MAX_ITERATIONS

    # ------------------------------------------------------------------
    # Planning decision — only for top-level requests with thinking on.
    # ------------------------------------------------------------------
    if not _is_subtask:
        mode = config.THINKING_MODE
        thinking_on = (
            mode == "on"
            or (mode == "auto" and should_think(user_request))
        )
        if thinking_on:
            plan = _plan_task(user_request)
            if plan:
                if on_plan is not None:
                    confirmed = on_plan(plan)
                    if not confirmed:
                        return "Plan cancelled"
                # Write the tracker file under the original prompt's turn so
                # /undo can revert it together with all subtask files.
                ctx.add_user_message(user_request)
                _write_tracker(user_request, plan, ctx)
                return _run_planned(user_request, plan, ctx)

    # ------------------------------------------------------------------
    # Normal single-task agent loop
    # ------------------------------------------------------------------
    ctx.add_user_message(user_request)
    t0 = time.time()
    iteration = 0
    task_files: set[str] = set()

    while iteration < max_iter:
        iteration += 1
        ctx.total_iterations += 1

        # Determine thinking mode
        if iteration == 1 and not _is_subtask:
            mode = config.THINKING_MODE
            if mode == "on":
                thinking = True
            elif mode == "off":
                thinking = False
            else:
                thinking = should_think(user_request)
        else:
            thinking = True

        # State 1: LLM_INFERENCE (streaming)
        messages = ctx.build_messages()
        try:
            with ui.ThinkingSpinner() as stream:
                content, _thinking = generate(
                    messages,
                    enable_thinking=thinking,
                    on_thinking=stream.on_thinking,
                    on_content=stream.on_content,
                )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            ui.show_error(f"LLM request failed: {e}")
            return None

        ctx.add_tool_call(content)

        # State 2: PARSE_AND_ROUTE
        if not content.strip():
            ui.show_error("Empty response (all tokens spent on thinking)")
            ctx.add_tool_result(
                "Error: empty response — your thinking consumed all tokens. "
                "Respond with ONLY a short JSON tool call. Do NOT think extensively."
            )
            continue

        tool = parse_tool_call(content)
        if tool is None:
            ui.show_error("Failed to parse JSON tool call")
            ui.show_info(f"Raw: {content[:200]}")
            ctx.add_tool_result(
                "Error: invalid JSON. Respond with exactly one JSON tool call: "
                "write_file, patch_file, read_file, list_files, run_sandbox, "
                "run_lua, lookup_docs, search_memory, install_package, message, "
                "or complete_task."
            )
            continue

        # State 5: EXIT via complete_task or message
        if tool.get("tool") == "complete_task":
            summary = tool.get("summary", "Done")
            elapsed = time.time() - t0
            ui.show_success(summary, iteration, elapsed, task_files)
            if not _is_subtask:
                ctx.add_history(user_request, iteration, elapsed, True)
            return summary

        if tool.get("tool") == "message":
            text = tool.get("text", "Done")
            ui.show_info(text)
            elapsed = time.time() - t0
            if not _is_subtask:
                ctx.add_history(user_request, iteration, elapsed, True)
            return text

        # State 3: EXECUTE tool
        tool_name, result_msg, changed_path, old_content = _dispatch(tool, ctx)
        if tool_name not in _VALID_TOOLS:
            ctx.add_tool_result(
                "Error: respond with a valid JSON tool call. "
                "Available tools: write_file, patch_file, read_file, list_files, "
                "run_sandbox, run_lua, lookup_docs, search_memory, message, "
                "complete_task."
            )
            continue

        # Auto-fix solution.lua -> correct path (saves an iteration)
        if tool_name == "write_file" and changed_path == "solution.lua":
            expected = _PATH_RE.findall(user_request)
            if expected:
                wanted = expected[0]
                src = os.path.join(os.getcwd(), "solution.lua")
                dst = os.path.join(os.getcwd(), os.path.normpath(wanted))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.isfile(src):
                    os.replace(src, dst)
                    if os.path.isfile(dst):
                        os.unlink(src) if os.path.isfile(src) else None
                ui.show_info(f"[yellow]Auto-renamed solution.lua -> {wanted}[/yellow]")
                changed_path = wanted
                tool["path"] = wanted

        # Track file changes for /status and /undo
        if changed_path is not None:
            ctx.track_file(changed_path, old_content)
            task_files.add(changed_path)

        # Refresh workspace listing after file creation
        if tool_name == "write_file":
            ctx.refresh_workspace()

        # State 4: EVALUATE — auto-sandbox for .lua after write/patch
        path = tool.get("path", "")
        if tool_name in ("write_file", "patch_file") and path.endswith(".lua"):
            sandbox = _auto_sandbox(path)
            if sandbox:
                ui.show_sandbox(sandbox)
                sandbox_ctx = _format_sandbox_context(path, sandbox)
                if not sandbox["success"] and not sandbox.get("warnings_only", False):
                    ctx.compact(quick=True)
                    # Include current code in error report
                    full = os.path.join(os.getcwd(), os.path.normpath(path))
                    code = ""
                    if os.path.isfile(full):
                        with open(full, encoding="utf-8") as f:
                            code = f.read()
                    numbered = "\n".join(f"{i+1}: {l}" for i, l in enumerate(code.splitlines()))
                    # Detect require errors and suggest correct paths
                    require_hint = ""
                    stderr = sandbox.get("stderr", "")
                    if "module" in stderr and "not found" in stderr:
                        lua_files = []
                        for root, _dirs, files in os.walk(os.getcwd()):
                            for f in files:
                                if f.endswith(".lua"):
                                    rel = os.path.relpath(os.path.join(root, f), os.getcwd())
                                    mod = rel.replace(os.sep, ".").replace("/", ".").removesuffix(".lua")
                                    lua_files.append((rel, mod))
                        if lua_files:
                            listing = ", ".join(f"require('{mod}') for {rel}" for rel, mod in lua_files)
                            require_hint = f"\nHINT — available modules: {listing}\n"
                    ctx.add_tool_result(
                        f"{result_msg}\n{sandbox_ctx}\n{require_hint}\n"
                        f"Current code ({path}):\n{numbered}\n\n"
                        "Fix the errors. If the error mentions an unknown Lua "
                        "function, field, or metamethod (e.g. 'attempt to call "
                        "nil', 'bad argument', unknown method on a library "
                        "table), OR if you are unsure about the correct API "
                        "for this task, call lookup_docs FIRST with the "
                        "fully-qualified name (e.g. 'string.pack', "
                        "'table.move', '__index') before rewriting — do NOT "
                        "guess. Otherwise use write_file to rewrite the file."
                    )
                    if iteration < max_iter:
                        ui.show_repair(iteration + 1, max_iter)
                    continue
                else:
                    result_msg += f"\n{sandbox_ctx}"

        ctx.add_tool_result(result_msg)

    # State 5: EXIT (exhausted)
    elapsed = time.time() - t0
    ui.show_failure(max_iter)
    if not _is_subtask:
        ctx.add_history(user_request, max_iter, elapsed, False)
    return None
