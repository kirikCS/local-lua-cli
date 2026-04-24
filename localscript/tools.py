"""Tools: file operations + luacheck/lua54 sandbox.

All path arguments are resolved relative to the working directory (cwd).
"""

import os
import re
import subprocess
import tempfile

from localscript.config import LUACHECK_BINARY, LUA_BINARY, EXECUTE_TIMEOUT, LUA_LIB_PATH


def _lua_path_prefix() -> str:
    """Build package.path prefix: dkjson lib + workdir-relative requires."""
    workdir = os.getcwd()
    paths = [
        LUA_LIB_PATH,
        f"{workdir}/?.lua",
        f"{workdir}/?/init.lua",
    ]
    path_str = ";".join(paths)
    return f'package.path = "{path_str};" .. package.path\n'


def _resolve(path: str) -> str:
    """Resolve a relative path against cwd. Reject absolute / traversal."""
    normed = os.path.normpath(path)
    if os.path.isabs(normed) or normed.startswith(".."):
        raise ValueError(f"Path must be relative and inside workdir: {path}")
    return os.path.join(os.getcwd(), normed)


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

def write_file(path: str, content: str) -> str:
    """Create or overwrite a file. Creates parent directories."""
    full = _resolve(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    lines = len(content.splitlines())
    return f"File written: {path} ({lines} lines)"


def read_file(path: str) -> str:
    """Read a file and return numbered lines."""
    full = _resolve(path)
    if not os.path.isfile(full):
        return f"Error: file not found: {path}"
    with open(full, "r", encoding="utf-8") as f:
        text = f.read()
    numbered = "\n".join(
        f"{i + 1:4d} | {line}" for i, line in enumerate(text.splitlines())
    )
    return f"Contents of {path}:\n{numbered}"


def patch_file(path: str, patches: list[dict]) -> str:
    """Apply line-range patches to an existing file.

    Each patch: {"line_start": int, "line_end": int, "content": str}.
    """
    full = _resolve(path)
    if not os.path.isfile(full):
        return f"Error: file not found: {path}. Use write_file first."

    with open(full, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for patch in sorted(patches, key=lambda p: p["line_start"], reverse=True):
        start = patch["line_start"] - 1
        end = patch["line_end"]
        new = patch["content"]
        if not new.endswith("\n"):
            new += "\n"
        # Split into individual lines so multi-line patches work correctly
        new_lines = new.splitlines(keepends=True)
        lines[start:end] = new_lines

    with open(full, "w", encoding="utf-8") as f:
        f.writelines(lines)

    return f"File patched: {path}"


def list_files(path: str = ".") -> str:
    """Return a tree listing of files in a directory."""
    full = _resolve(path)
    if not os.path.isdir(full):
        return f"Error: not a directory: {path}"

    entries: list[str] = []
    for root, dirs, files in os.walk(full):
        # Skip hidden / cache dirs
        dirs[:] = [d for d in dirs if not d.startswith((".", "__"))]
        level = os.path.relpath(root, full).count(os.sep)
        indent = "  " * level
        rel = os.path.relpath(root, full)
        if rel == ".":
            entries.append(f"{path}/")
        else:
            entries.append(f"{indent}{os.path.basename(root)}/")
        for fname in sorted(files):
            entries.append(f"{indent}  {fname}")
    return "\n".join(entries) if entries else f"{path}/ (empty)"


# ---------------------------------------------------------------------------
# Package management
# ---------------------------------------------------------------------------

ALLOWED_PACKAGES = ["cjson", "luasocket", "luafilesystem", "lpeg", "penlight"]


def lookup_docs(query: str, top_k: int = 3) -> str:
    """Search the bundled Lua 5.4 reference manual.

    Returns the top *top_k* matching chunks formatted for model consumption.
    """
    if not query or not query.strip():
        return "Error: lookup_docs needs a non-empty 'query'."
    try:
        from localscript.docs import search
    except ImportError as e:
        return f"Error: docs module unavailable: {e}"
    try:
        k = max(1, min(int(top_k), 5))
    except (TypeError, ValueError):
        k = 3
    results = search(query, top_k=k)
    if not results:
        return f"No matches found in Lua 5.4 reference for: {query}"
    parts = [f"Lua 5.4 reference — top {len(results)} matches for {query!r}:"]
    for i, r in enumerate(results, 1):
        parts.append(f"\n--- Result {i} (score {r['score']}) ---")
        parts.append(r["text"])
    return "\n".join(parts)


def install_package(package: str) -> str:
    """Install a Lua package via luarocks (whitelist only)."""
    if package not in ALLOWED_PACKAGES:
        return f"Error: package '{package}' not in whitelist. Allowed: {', '.join(ALLOWED_PACKAGES)}"
    try:
        result = subprocess.run(
            ["luarocks", "install", package],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return f"Installed {package} successfully.\n{result.stdout.rstrip()}"
        return f"Error installing {package}:\n{result.stderr.rstrip()}"
    except FileNotFoundError:
        return "Error: luarocks not found on PATH"
    except subprocess.TimeoutExpired:
        return f"Error: luarocks install {package} timed out (60s)"


# ---------------------------------------------------------------------------
# Lua sandbox
# ---------------------------------------------------------------------------

def luacheck(code: str, display_name: str = "file.lua") -> dict:
    """Run luacheck on code string. Returns {success, errors, available}.

    *display_name* replaces the temp file path in error messages so the model
    (and user) see a meaningful filename instead of C:\\Users\\...\\tmp1234.lua.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".lua", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        tmp = f.name
    try:
        result = subprocess.run(
            [LUACHECK_BINARY, tmp, "--no-color", "--codes", "--ranges"],
            capture_output=True, text=True, timeout=10,
        )
        errors = []
        for line in result.stdout.strip().split("\n"):
            if ":" in line and ("W" in line or "E" in line):
                # Replace temp path with display name
                clean = line.strip().replace(tmp, display_name)
                errors.append(clean)
        return {"success": len(errors) == 0, "errors": errors, "available": True}
    except FileNotFoundError:
        return {"success": True, "errors": [], "available": False}
    except subprocess.TimeoutExpired:
        return {"success": False, "errors": ["luacheck timed out"], "available": True}
    finally:
        os.unlink(tmp)


def lua_execute(
    code: str,
    timeout: int = EXECUTE_TIMEOUT,
    display_name: str = "file.lua",
    stdin: str | None = None,
) -> dict:
    """Run Lua code via lua54. Returns {success, stdout, stderr, returncode}.

    If *stdin* is provided, its contents are fed to the Lua process's stdin
    so scripts that use `io.read()` / `io.lines()` can receive test input.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".lua", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(_lua_path_prefix() + code)
        tmp = f.name
    try:
        result = subprocess.run(
            [LUA_BINARY, tmp], capture_output=True, text=True,
            timeout=timeout, cwd=os.getcwd(), input=stdin,
        )
        stderr = result.stderr.replace(tmp, display_name)
        # Fix line numbers offset by _LUA_PATH_PREFIX (1 line)
        stderr = re.sub(
            rf"{re.escape(display_name)}:(\d+)",
            lambda m: f"{display_name}:{int(m.group(1)) - 1}",
            stderr,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": stderr,
            "returncode": result.returncode,
        }
    except FileNotFoundError:
        return {"success": False, "stdout": "", "stderr": f"lua54 not found: {LUA_BINARY}", "returncode": -1}
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": f"TIMEOUT ({timeout}s)", "returncode": -1}
    finally:
        os.unlink(tmp)


# Cap the timeout the agent can request via run_lua so a bad snippet can't
# hang the REPL for minutes.
_RUN_LUA_MAX_TIMEOUT = 30


def run_lua_snippet(code: str, stdin: str = "", timeout: int = EXECUTE_TIMEOUT) -> dict:
    """Execute a Lua snippet (ad-hoc code, not a tracked file) and capture
    its output. Intended for testing existing modules with test inputs.

    Returns {success, stdout, stderr, returncode}. Timeout is clamped to
    _RUN_LUA_MAX_TIMEOUT to prevent runaway snippets.
    """
    try:
        t = max(1, min(int(timeout), _RUN_LUA_MAX_TIMEOUT))
    except (TypeError, ValueError):
        t = EXECUTE_TIMEOUT
    return lua_execute(code, timeout=t, display_name="snippet.lua", stdin=stdin)


def run_sandbox_full(path: str) -> dict:
    """Run luacheck + lua54 on a file. Returns structured result dict.

    Keys: display (str for UI), stdout, stderr, exit_code (int), success (bool).
    """
    full = _resolve(path)
    if not os.path.isfile(full):
        return {
            "display": f"Error: file not found: {path}",
            "stdout": "", "stderr": f"file not found: {path}",
            "exit_code": -1, "success": False, "warnings_only": False,
        }

    with open(full, "r", encoding="utf-8") as f:
        code = f.read()

    display = os.path.basename(path)
    parts: list[str] = []
    lint_warnings_only = False

    # luacheck
    lint = luacheck(code, display_name=display)
    if not lint["available"]:
        parts.append("luacheck: skipped (not installed)")
    elif lint["success"]:
        parts.append("luacheck: OK")
    else:
        # Separate W-codes (warnings) from E-codes (errors)
        lint_warnings_only = all(
            re.search(r'\bW\d+\b', e) and not re.search(r'\bE\d+\b', e)
            for e in lint["errors"]
        )
        if lint_warnings_only:
            parts.append("luacheck warnings:\n" + "\n".join(lint["errors"]))
        else:
            report = "luacheck ERRORS:\n" + "\n".join(lint["errors"])
            return {
                "display": report,
                "stdout": "", "stderr": "\n".join(lint["errors"]),
                "exit_code": 1, "success": False, "warnings_only": False,
            }

    # lua54
    run = lua_execute(code, display_name=display)
    if run["success"]:
        parts.append("lua54: OK")
        if run["stdout"].strip():
            parts.append(f"stdout:\n{run['stdout'].rstrip()}")
    else:
        parts.append(f"lua54 FAILED:\n{run['stderr'].rstrip()}")

    return {
        "display": "\n".join(parts),
        "stdout": run["stdout"],
        "stderr": run["stderr"],
        "exit_code": run.get("returncode", 1 if not run["success"] else 0),
        "success": run["success"] and not lint_warnings_only,
        "warnings_only": lint_warnings_only and run["success"],
    }
