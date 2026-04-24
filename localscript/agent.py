"""Agent: 6-state FSM for Lua code generation, file manipulation, and repair."""

import os
import re
import time

from localscript import config
from localscript.config import MAX_ITERATIONS

_PATH_RE = re.compile(r'(?:^|\s)((?:src|tests?|lib|modules?)/[\w/.-]+\.lua)\b', re.IGNORECASE)
from localscript.context import Context
from localscript.difficulty import should_think
from localscript.llm import generate, parse_tool_call
from localscript import tools, ui


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
    """Execute a tool call. Returns (tool_name, result_message, path_or_None, old_content_or_None).

    *ctx* is required for tools that read per-session state (e.g. search_memory
    queries the Context's memory store).
    """
    name = tool.get("tool", "")

    if name == "write_file":
        path, content = tool.get("path", ""), tool.get("content", "")
        if not path or content is None:
            return name, "Error: write_file needs 'path' and 'content'.", None, None
        # Save old content for undo
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

    return name, f"Error: unknown tool '{name}'. Available: write_file, patch_file, read_file, list_files, run_sandbox, lookup_docs, search_memory, install_package, message, complete_task.", None, None


def run(user_request: str, ctx: Context | None = None) -> str | None:
    """Run the agent loop. Returns final summary or None.

    If *ctx* is provided, the conversation history is preserved (REPL mode).
    If *ctx* is None, a fresh context is created (one-shot mode).
    """
    if ctx is None:
        ctx = Context()
    ctx.add_user_message(user_request)
    t0 = time.time()
    iteration = 0
    task_files: set[str] = set()

    while iteration < MAX_ITERATIONS:
        iteration += 1
        ctx.total_iterations += 1

        # Determine thinking mode based on config and prompt difficulty
        if iteration == 1:
            mode = config.THINKING_MODE
            if mode == "on":
                thinking = True
            elif mode == "off":
                thinking = False
            else:  # "auto"
                thinking = should_think(user_request)
        else:
            # Always think on repair iterations (errors were found)
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
            raise  # propagate to main.py handler
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
                "lookup_docs, search_memory, install_package, message, or complete_task."
            )
            continue

        # State 5: EXIT via complete_task or message
        if tool.get("tool") == "complete_task":
            summary = tool.get("summary", "Done")
            elapsed = time.time() - t0
            ui.show_success(summary, iteration, elapsed, task_files)
            ctx.add_history(user_request, iteration, elapsed, True)
            return summary

        if tool.get("tool") == "message":
            text = tool.get("text", "Done")
            ui.show_info(text)
            elapsed = time.time() - t0
            ctx.add_history(user_request, iteration, elapsed, True)
            return text

        # State 3: EXECUTE tool
        tool_name, result_msg, changed_path, old_content = _dispatch(tool, ctx)
        if tool_name not in ("write_file", "patch_file", "read_file", "list_files", "run_sandbox", "install_package", "lookup_docs", "search_memory"):
            ctx.add_tool_result(
                "Error: respond with a valid JSON tool call. "
                "Available tools: write_file, patch_file, read_file, list_files, run_sandbox, lookup_docs, search_memory, message, complete_task."
            )
            continue

        # Auto-fix solution.lua → correct path (saves an iteration)
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
                    ctx.compact()
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
                        "Fix the errors. Use write_file to rewrite the entire file."
                    )
                    if iteration < MAX_ITERATIONS:
                        ui.show_repair(iteration + 1, MAX_ITERATIONS)
                    continue
                else:
                    result_msg += f"\n{sandbox_ctx}"

        ctx.add_tool_result(result_msg)

    # State 5: EXIT (exhausted)
    elapsed = time.time() - t0
    ui.show_failure(MAX_ITERATIONS)
    ctx.add_history(user_request, MAX_ITERATIONS, elapsed, False)
    return None
