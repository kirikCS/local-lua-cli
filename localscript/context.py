"""Context manager: message history, compaction, file state tracking."""

import json
import os
import re
import time
import uuid

from localscript import config
from localscript.config import SYSTEM_PROMPT
from localscript.memory import Memory

CHATS_DIR = os.path.join(".iceq", "chats")


def _scan_workspace() -> str:
    """Scan cwd for files, return formatted workspace block."""
    entries = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if not d.startswith((".", "__"))]
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            if fpath.startswith("./"):
                fpath = fpath[2:]
            try:
                with open(os.path.join(root, fname), "r", encoding="utf-8") as f:
                    n = sum(1 for _ in f)
                entries.append(f"{fpath} ({n} lines)")
            except (OSError, UnicodeDecodeError):
                entries.append(fpath)
    if not entries:
        return ""
    return "\n[Workspace]\n" + "\n".join(entries)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Max chars of a single file to embed in a compaction summary
_MAX_FILE_CHARS = 4000

# Prefixes that mark a user-role message as a tool result, not a user task
_TOOL_RESULT_PREFIXES = (
    "File written:",
    "File patched:",
    "Contents of ",
    "Error:",
    "SANDBOX RESULT",
    "[Compacted summary",
    "[Conversation summary",
    "luacheck",
    "lua54",
    "{",
)


def _extract_user_tasks(messages: list[dict]) -> list[str]:
    """Pick out user-typed task prompts (not tool results)."""
    tasks = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        c = msg.get("content", "").strip()
        if not c or c.startswith(_TOOL_RESULT_PREFIXES):
            continue
        tasks.append(c)
    return tasks


def _extract_file_paths(messages: list[dict]) -> list[str]:
    """Extract paths from write_file/patch_file tool calls in assistant messages."""
    seen: list[str] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        obj = None
        try:
            obj = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except (json.JSONDecodeError, ValueError):
                    obj = None
        if not isinstance(obj, dict):
            continue
        tool = obj.get("tool") or obj.get("method")
        path = obj.get("path") or obj.get("file")
        if isinstance(path, str) and tool in ("write_file", "patch_file") and path not in seen:
            seen.append(path)
    return seen


def _read_file_states(paths: list[str]) -> list[dict]:
    """Read current content of each path from disk. Marks missing files."""
    states = []
    for p in paths:
        full = os.path.join(os.getcwd(), os.path.normpath(p))
        try:
            with open(full, "r", encoding="utf-8") as f:
                content = f.read()
            total = len(content)
            if total > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS] + f"\n...[truncated, full size {total} chars]"
            states.append({"path": p, "status": "present", "content": content})
        except (OSError, UnicodeDecodeError):
            states.append({"path": p, "status": "missing", "content": ""})
    return states


def _llm_synthesize_errors_state(messages: list[dict]) -> tuple[list[str], str]:
    """Ask the LLM to extract errors+resolutions and current project state.

    Returns (errors_list, state_string). On any failure returns ([], "").
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if len(content) > 600:
            content = content[:600] + "...[truncated]"
        lines.append(f"[{role}]: {content}")
    conversation_text = "\n".join(lines)

    instructions = (
        "Analyze the conversation between a user and a Lua coding assistant. "
        "Output a JSON object with EXACTLY these two keys:\n"
        '  "errors": array of strings — each describes one error encountered '
        'AND how it was resolved (or "unresolved" if it was not fixed).\n'
        '  "state": short string — current state of the project: what works, '
        "what does not, where things stand.\n"
        "Be factual and concise. If there were no errors, use an empty array. "
        "Do NOT include task lists or file lists; only errors and state."
    )

    try:
        from localscript.llm import generate
        out, _ = generate(
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": conversation_text},
            ],
            enable_thinking=False,
            response_format="json",
            max_tokens=1024,
        )
        data = json.loads(out.strip())
        if not isinstance(data, dict):
            return [], ""
        raw_errors = data.get("errors") or []
        if not isinstance(raw_errors, list):
            raw_errors = []
        errors = [str(e).strip() for e in raw_errors if e]
        state = data.get("state") or ""
        if not isinstance(state, str):
            state = str(state)
        return errors, state.strip()
    except Exception:
        return [], ""


def _format_retrieved_block(rows: list[dict]) -> str:
    """Render retrieved memory rows as a single user-role context block."""
    parts = [
        f"[Retrieved from long-term memory \u2014 {len(rows)} relevant excerpt(s) "
        f"from earlier in this session, ordered by relevance]"
    ]
    for i, r in enumerate(rows, 1):
        body = r.get("content", "")
        if len(body) > 800:
            body = body[:800] + "...[truncated]"
        parts.append(
            f"\n--- Excerpt {i} (turn {r.get('turn_idx', '?')}, "
            f"{r.get('role', '?')}, score {r.get('score', 0)}) ---"
        )
        parts.append(body)
    return "\n".join(parts)


def _render_structured_summary(
    tasks: list[str],
    files: list[dict],
    errors: list[str],
    state: str,
) -> str:
    """Render extracted info as a markdown-structured plain-text block."""
    parts: list[str] = []

    parts.append("## Tasks Requested")
    if tasks:
        for i, t in enumerate(tasks, 1):
            t_clean = t.replace("\n", " ").strip()
            if len(t_clean) > 200:
                t_clean = t_clean[:200] + "..."
            parts.append(f"{i}. {t_clean}")
    else:
        parts.append("(none)")
    parts.append("")

    parts.append("## Files (current state on disk)")
    if files:
        for f in files:
            suffix = " (missing from disk)" if f["status"] == "missing" else ""
            parts.append(f"### {f['path']}{suffix}")
            if f["status"] == "present":
                parts.append("```lua")
                parts.append(f["content"])
                parts.append("```")
    else:
        parts.append("(none)")
    parts.append("")

    parts.append("## Errors & Resolutions")
    if errors:
        for e in errors:
            parts.append(f"- {e}")
    else:
        parts.append("(none)")
    parts.append("")

    parts.append("## Current Project State")
    parts.append(state if state else "(see files above)")

    return "\n".join(parts)


class Context:
    """Manages the conversation context sent to the LLM.

    System prompt is always first. Everything else lives in iterations[].
    """

    def __init__(self, scan: bool = True):
        workspace = _scan_workspace() if scan else ""
        self.system_message = SYSTEM_PROMPT + workspace
        self.iterations: list[dict] = []
        # Session stats
        self.total_iterations: int = 0
        self.files_touched: set[str] = set()
        self.undo_stack: list[tuple[str, str | None]] = []  # (path, old_content_or_None)
        self.history: list[dict] = []  # {task, iterations, time, success}
        self.input_chars: int = 0
        self.output_chars: int = 0
        # Long-term memory (RAG). Lazy: only touches disk when enabled.
        self.session_id: str = time.strftime("%Y%m%d_%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self._memory: Memory = Memory()
        self.memory_enabled: bool = bool(config.MEMORY_ENABLED)
        self._mem_turn_idx: int = 0
        # Turn boundaries — one entry per user prompt, used by /undo to
        # revert a whole turn (file changes + iterations + memory rows +
        # history entry) atomically. See undo_last_turn() for the schema.
        self.turns: list[dict] = []

    # ------------------------------------------------------------------
    # Append path — every iteration also goes to the memory store when on
    # ------------------------------------------------------------------

    def _ingest(self, role: str, content: str) -> int | None:
        """Append one row to the memory store, if enabled.

        Returns the inserted row id (so the caller can record it as part of
        a turn boundary), or None when memory is disabled, the content is
        empty, or the write failed for any reason.
        """
        if not self.memory_enabled or not content or not content.strip():
            return None
        try:
            self._mem_turn_idx += 1
            row_id = self._memory.add(self.session_id, self._mem_turn_idx, role, content)
            return row_id if row_id and row_id > 0 else None
        except Exception:
            # Memory is best-effort; never break the agent loop on a write
            # failure (locked DB, disk full, etc.)
            return None

    def add_user_message(self, text: str):
        # Capture turn-boundary metadata BEFORE we mutate any state, so /undo
        # can revert exactly to this point.
        turn = {
            "request": text,
            "iter_start": len(self.iterations),
            "undo_first": len(self.undo_stack),
            "mem_turn_idx_first": self._mem_turn_idx,
            "mem_first_id": None,
        }
        self.iterations.append({"role": "user", "content": text})
        row_id = self._ingest("user", text)
        if row_id is not None:
            turn["mem_first_id"] = row_id
        self.turns.append(turn)

    def _auto_compact_if_needed(self):
        """Fire compact automatically when iterations are about to overflow
        the model's context window. Uses COMPACT_THRESHOLD from config which
        is calibrated to 75% of the estimated token budget for iterations."""
        total = sum(len(m.get("content", "")) for m in self.iterations)
        total += len(self.system_message)
        if total > int(config.COMPACT_THRESHOLD) and len(self.iterations) > 4:
            self.compact(quick=True)

    def build_messages(self) -> list[dict]:
        self._auto_compact_if_needed()
        if self.memory_enabled:
            msgs = self._build_messages_with_memory()
        else:
            msgs = [{"role": "system", "content": self.system_message}] + self.iterations
        self.input_chars += sum(len(m.get("content", "")) for m in msgs)
        return msgs

    def _build_messages_with_memory(self) -> list[dict]:
        """Compose context: system + retrieved memory + pinned recent.

        The pinned recent window is the last MEMORY_PINNED_RECENT raw
        iterations (preserves short-term coherence). Retrieved memory is
        the top-k BM25 hits for the most recent user message, with the
        pinned window's row ids excluded so we never duplicate.
        """
        pinned_n = max(0, int(config.MEMORY_PINNED_RECENT))
        top_k = max(0, int(config.MEMORY_TOP_K))

        pinned = self.iterations[-pinned_n:] if pinned_n > 0 else []

        # Retrieval query: the most recent user message in the iterations buffer.
        query = ""
        for msg in reversed(self.iterations):
            if msg.get("role") == "user" and msg.get("content"):
                query = msg["content"]
                break

        retrieved: list[dict] = []
        if query and top_k > 0:
            try:
                # Skip the pinned window so we don't echo it back.
                exclude = set(self._memory.recent_ids(self.session_id, len(pinned)))
                retrieved = self._memory.search(
                    query, top_k=top_k, exclude_ids=exclude,
                )
            except Exception:
                retrieved = []

        msgs: list[dict] = [{"role": "system", "content": self.system_message}]
        if retrieved:
            msgs.append({
                "role": "user",
                "content": _format_retrieved_block(retrieved),
            })
        msgs.extend(pinned)
        return msgs

    def add_tool_call(self, content: str):
        """Store assistant response with <think> blocks stripped."""
        self.output_chars += len(content)
        clean = _THINK_RE.sub("", content).strip()
        self.iterations.append({"role": "assistant", "content": clean})
        self._ingest("assistant", clean)

    def add_tool_result(self, result: str):
        self.iterations.append({"role": "user", "content": result})
        self._ingest("tool_result", result)

    # ------------------------------------------------------------------
    # Memory admin — used by /memory slash command
    # ------------------------------------------------------------------

    def memory_enable(self) -> dict:
        """Turn memory on. Backfills the current iterations buffer AND embeds
        any pre-existing rows that have NULL embedding (phase 2 upgrade path).

        If anything goes wrong (DB creation fails, permissions, etc.), the
        flag is rolled back to False so /memory correctly reports OFF.
        """
        try:
            self.memory_enabled = True
            config.MEMORY_ENABLED = True
            # Backfill current buffer so /memory --on mid-session is useful.
            items: list[tuple[int, str, str]] = []
            for msg in self.iterations:
                self._mem_turn_idx += 1
                role = msg.get("role", "user")
                items.append((self._mem_turn_idx, role, msg.get("content", "")))
            if items:
                self._memory.add_many(self.session_id, items)
            # Phase 2: embed any pre-existing rows without a vector.
            backfill: dict = {"backfilled": 0, "remaining": 0, "skipped": True}
            try:
                backfill = self._memory.backfill_embeddings()
            except Exception:
                pass
            stats = self._memory.stats()
            stats["backfill"] = backfill
            return stats
        except Exception:
            # Roll back so status correctly reports OFF.
            self.memory_enabled = False
            config.MEMORY_ENABLED = False
            raise

    def memory_disable(self) -> dict:
        """Turn memory off. The DB file is left intact."""
        self.memory_enabled = False
        config.MEMORY_ENABLED = False
        return self._memory.stats()

    def memory_stats(self) -> dict:
        s = self._memory.stats()
        s["enabled"] = self.memory_enabled
        s["session_id"] = self.session_id
        s["pinned_recent"] = int(config.MEMORY_PINNED_RECENT)
        s["top_k"] = int(config.MEMORY_TOP_K)
        s["hybrid_alpha"] = float(config.MEMORY_HYBRID_ALPHA)
        return s

    def memory_search_preview(self, query: str, top_k: int = 5) -> list[dict]:
        try:
            return self._memory.search(query, top_k=top_k)
        except Exception:
            return []

    def memory_clear(self) -> dict:
        try:
            self._memory.clear()
        except Exception:
            pass
        # Also reset turn counter so future inserts start at 1 again.
        self._mem_turn_idx = 0
        return self._memory.stats()

    def add_history(self, task: str, iterations: int, elapsed: float, success: bool):
        self.history.append({
            "task": task, "iterations": iterations,
            "time": elapsed, "success": success,
        })

    def track_file(self, path: str, old_content: str | None):
        """Record a file change for /status and /undo."""
        self.files_touched.add(path)
        self.undo_stack.append((path, old_content))

    def undo_last_turn(self) -> dict:
        """Revert the most recent user prompt and everything it produced.

        A "turn" is one user prompt plus all of the agent's responses,
        tool calls, tool results, and file changes that followed before
        the next prompt. /undo pops one turn record off self.turns and:

          1. Reverts each tracked file change in reverse order. Files
             that didn't exist before the turn are deleted; files that
             existed are restored to their pre-turn content.
          2. Truncates the iterations buffer back to where the turn began,
             so the live LLM context no longer references the undone work.
          3. Deletes the corresponding rows from the long-term memory
             store (FTS5 index is updated automatically by the ad trigger).
          4. Rolls back self._mem_turn_idx so future inserts continue from
             the value it held before this turn started.
          5. Drops the matching history entry if any (agent.run appends
             history at the end of the turn, so the latest entry is the
             one being undone).

        Returns a dict with what was undone for the slash command to display.
        """
        if not self.turns:
            return {"undone": False, "reason": "no turns to undo"}

        turn = self.turns.pop()
        iter_start = turn["iter_start"]
        undo_first = turn["undo_first"]
        mem_first_id = turn.get("mem_first_id")
        mem_turn_idx_first = turn.get("mem_turn_idx_first", 0)

        # 1. Revert file changes (in reverse order so multiple writes to
        #    the same file unwind cleanly to the pre-turn state).
        reverted: list[tuple[str, str]] = []  # (action, path)
        while len(self.undo_stack) > undo_first:
            path, old_content = self.undo_stack.pop()
            full = os.path.join(os.getcwd(), os.path.normpath(path))
            if old_content is None:
                # File didn't exist before — delete it now if it still does.
                if os.path.isfile(full):
                    try:
                        os.unlink(full)
                        reverted.append(("deleted", path))
                    except OSError:
                        reverted.append(("delete-failed", path))
                else:
                    reverted.append(("already-gone", path))
            else:
                try:
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(old_content)
                    reverted.append(("restored", path))
                except OSError:
                    reverted.append(("restore-failed", path))
            # files_touched is a session-cumulative set; leave it alone so
            # /status still reflects what was touched at any point.

        # 2. Truncate the live iterations buffer.
        removed_iters = len(self.iterations) - iter_start
        if removed_iters > 0:
            del self.iterations[iter_start:]

        # 3+4. Delete memory rows for this turn and roll back the turn index.
        deleted_mem = 0
        if mem_first_id is not None:
            try:
                deleted_mem = self._memory.delete_from(self.session_id, mem_first_id)
            except Exception:
                deleted_mem = 0
        # Always restore _mem_turn_idx to its pre-turn value, even when memory
        # is currently disabled — the user may toggle it back on, and we don't
        # want gaps or collisions in turn_idx assignments.
        self._mem_turn_idx = mem_turn_idx_first

        # 5. Drop the matching history entry. agent.run appends to history at
        #    the end of every turn (success OR exhausted), so the latest entry
        #    corresponds to the turn we're popping. If the turn was aborted
        #    before history was written, len(history) will be one short and
        #    this pop is a no-op.
        if self.history:
            self.history.pop()

        return {
            "undone": True,
            "request": turn["request"],
            "files_reverted": reverted,
            "iterations_removed": removed_iters,
            "memory_rows_removed": deleted_mem,
            "turns_remaining": len(self.turns),
        }

    def estimated_tokens(self) -> int:
        """Rough token estimate: total chars in iterations / 4."""
        return sum(len(m.get("content", "")) for m in self.iterations) // 4

    def refresh_workspace(self):
        """Re-scan workspace and update system prompt."""
        base = SYSTEM_PROMPT
        workspace = _scan_workspace()
        self.system_message = base + workspace

    def save_session(self):
        """Save session to .iceq/chats/{timestamp}.json."""
        if not self.history and not self.iterations:
            return None
        os.makedirs(CHATS_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(CHATS_DIR, f"{ts}.json")
        data = {
            "timestamp": ts,
            "iterations": self.iterations,
            "history": self.history,
            "files_touched": sorted(self.files_touched),
            "total_iterations": self.total_iterations,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load_session(cls, path: str) -> "Context":
        """Load a saved session."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ctx = cls(scan=True)
        ctx.iterations = data.get("iterations", [])
        ctx.history = data.get("history", [])
        ctx.files_touched = set(data.get("files_touched", []))
        ctx.total_iterations = data.get("total_iterations", 0)
        return ctx

    @staticmethod
    def list_sessions(limit: int = 5) -> list[dict]:
        """List recent saved sessions."""
        if not os.path.isdir(CHATS_DIR):
            return []
        files = sorted(
            [f for f in os.listdir(CHATS_DIR) if f.endswith(".json")],
            reverse=True,
        )[:limit]
        sessions = []
        for fname in files:
            fpath = os.path.join(CHATS_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                first_task = ""
                for h in data.get("history", []):
                    first_task = h.get("task", "")[:60]
                    break
                if not first_task:
                    # Fallback: first user message
                    for m in data.get("iterations", []):
                        if m.get("role") == "user":
                            first_task = m["content"][:60]
                            break
                ts = data.get("timestamp", fname.replace(".json", ""))
                sessions.append({
                    "path": fpath,
                    "timestamp": ts,
                    "task": first_task,
                    "files": len(data.get("files_touched", [])),
                    "iters": data.get("total_iterations", 0),
                })
            except (json.JSONDecodeError, OSError):
                continue
        return sessions

    def clear(self):
        self.iterations.clear()
        # Drop turn metadata and the undo stack — they reference iterations
        # that no longer exist. Memory rows are intentionally left alone
        # (use /memory clear to wipe those).
        self.turns.clear()
        self.undo_stack.clear()

    def reset_for_subtask(self):
        """Clear the iterations buffer between planning subtasks.

        Unlike clear(), this preserves:
          - turns (so /undo can rewind subtask-by-subtask)
          - undo_stack (file changes survive subtask boundaries)
          - memory store (the bridge between subtask contexts)
          - session_id, history, files_touched, stats
        Only the iterations[] buffer is emptied so the next subtask starts
        with a fresh context (system prompt + retrieved memory only).
        """
        self.iterations.clear()

    # ------------------------------------------------------------------
    # Compaction — summarize old context, keep recent messages
    # ------------------------------------------------------------------

    def compact(self, quick: bool = False) -> dict:
        """Compact context: summarize older messages to free token budget.

        Handles every size:
          0 messages → nothing to compact
          1 message that's already a summary → nothing to compact
          1-4 messages → summarize ALL into one message, keep nothing
          >4 messages → summarize first 70%, keep last 30%

        When *quick* is True (auto-compact), the LLM synthesis step is
        skipped — deterministic extraction only. Much faster, never overflows.

        Returns {"before": int, "after": int, "summarized": int, "kept": int}.
        """
        n = len(self.iterations)

        if n == 0:
            return {"before": 0, "after": 0, "summarized": 0, "kept": 0}

        # A single message that is itself already a summary → nothing to do.
        if n == 1 and "[Compacted summary" in self.iterations[0].get("content", ""):
            return {"before": 1, "after": 1, "summarized": 0, "kept": 1}

        # Small context (1-4): summarize everything, keep nothing.
        if n <= 4:
            to_summarize = self.iterations
            to_keep: list[dict] = []
        else:
            # Normal case: first 70% summarized, last 30% stays.
            split = max(1, int(n * 0.7))
            to_summarize = self.iterations[:split]
            to_keep = self.iterations[split:]

        tasks = _extract_user_tasks(to_summarize)
        file_paths = _extract_file_paths(to_summarize)
        file_states = _read_file_states(file_paths)
        if quick:
            errors, state = [], ""
        else:
            errors, state = _llm_synthesize_errors_state(to_summarize)

        summary_text = _render_structured_summary(
            tasks=tasks,
            files=file_states,
            errors=errors,
            state=state,
        )

        summary_msg = {
            "role": "user",
            "content": (
                f"[Compacted summary of {len(to_summarize)} earlier messages]\n\n"
                f"{summary_text}"
            ),
        }
        self.iterations = [summary_msg] + to_keep

        # Ingest the summary into long-term memory so it's retrievable after
        # the iterations buffer is eventually overwritten by further compaction
        # or reset between planning subtasks.
        self._ingest("compact_summary", summary_text)

        return {
            "before": n,
            "after": len(self.iterations),
            "summarized": len(to_summarize),
            "kept": len(to_keep),
        }

