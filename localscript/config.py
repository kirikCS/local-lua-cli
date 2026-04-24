"""Configuration: paths, URLs, limits, parameters, system prompt."""

import os
import shutil

VERSION = "0.5.0"

# --- Paths ---
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Inference server ---
LLM_URL = os.environ.get("LLM_URL", "http://localhost:11434/api/chat")
# CLI agent default — the thinking variant. Users can switch via `/model`
# slash command or the `--model` CLI flag (iceq --model iceq-sft).
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:4b-thinking-2507-q8_0")
# Fine-tuned non-thinking model used by the API server (iceq-server).
# Produces Lua code directly without chain-of-thought preamble, so it fits
# the competition's num_predict=256 constraint.
SFT_MODEL = os.environ.get("SFT_MODEL", "iceq-sft")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.6"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "256"))
LLM_CTX_SIZE = int(os.environ.get("LLM_CTX_SIZE", "4096"))

# --- Agent limits ---
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "10"))
SUBTASK_MAX_ITERATIONS = int(os.environ.get("SUBTASK_MAX_ITERATIONS", "5"))
EXECUTE_TIMEOUT = int(os.environ.get("EXECUTE_TIMEOUT", "5"))

# --- Auto-compact ---
# Fire auto-compact when the iterations buffer exceeds this many chars.
# Estimates: ~3 chars/token, system prompt ~2000 chars, response ~2400 chars.
# Budget for iterations = (CTX_SIZE * 3 - 2000 - 2400) * 0.75.
# With CTX_SIZE=4096: (12288 - 4400) * 0.75 ≈ 5916 chars.
_ITER_BUDGET = max(LLM_CTX_SIZE * 3 - 4400, 2000)
COMPACT_THRESHOLD = int(os.environ.get(
    "COMPACT_THRESHOLD", str(int(_ITER_BUDGET * 0.75))
))

# --- Thinking mode ---
# "auto" = use difficulty classifier, "on" = always think, "off" = never think
THINKING_MODE = os.environ.get("THINKING_MODE", "auto")

# --- Long-term memory (RAG) ---
# Off by default. Toggled at runtime via /memory --on / --off.
# When enabled, every iteration is appended to a per-project SQLite+FTS5
# store at .iceq/memory.sqlite, and build_messages() composes the model
# context from: system + retrieved memory + last MEMORY_PINNED_RECENT raw turns.
MEMORY_ENABLED = os.environ.get("MEMORY_ENABLED", "0") == "1"
MEMORY_PINNED_RECENT = int(os.environ.get("MEMORY_PINNED_RECENT", "6"))
MEMORY_TOP_K = int(os.environ.get("MEMORY_TOP_K", "8"))

# --- Long-term memory: phase 2 semantic boost ---
# When enabled, every ingested row is also embedded via Ollama, and search
# becomes a two-stage hybrid (BM25 candidates -> dense rerank). If the
# embedding endpoint is unreachable or the model is not pulled, the system
# silently falls back to BM25-only — no errors, no retries.
MEMORY_EMBEDDINGS = os.environ.get("MEMORY_EMBEDDINGS", "1") == "1"
MEMORY_EMBEDDING_MODEL = os.environ.get("MEMORY_EMBEDDING_MODEL", "nomic-embed-text")
MEMORY_EMBEDDING_URL = os.environ.get(
    "MEMORY_EMBEDDING_URL", "http://localhost:11434/api/embed"
)
# Hybrid score = MEMORY_HYBRID_ALPHA * bm25_norm + (1 - alpha) * cosine_norm
# alpha=0 -> dense only, alpha=1 -> lexical only, alpha=0.5 -> equal weight
MEMORY_HYBRID_ALPHA = float(os.environ.get("MEMORY_HYBRID_ALPHA", "0.5"))

# --- Tool paths (macOS) ---
# Lua 5.4 is shipped under several executable names depending on how it was
# installed:
#   * Homebrew `brew install lua`        -> /opt/homebrew/bin/lua  (currently 5.4)
#   * Homebrew `brew install lua@5.4`    -> /opt/homebrew/bin/lua5.4 (kept linked)
#   * Manual builds                       -> often `lua5.4` or `lua54`
# We probe in priority order so a stock `brew install lua` Just Works without
# the user having to set LUA_BINARY. luacheck only ever installs as `luacheck`.
def _find_lua() -> str:
    for name in ("lua5.4", "lua54", "lua"):
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)
    return "lua"  # last-resort: let subprocess fail with a clear "command not found"


def _find_luacheck() -> str:
    found = shutil.which("luacheck")
    return os.path.abspath(found) if found else "luacheck"


LUA_BINARY = os.environ.get("LUA_BINARY") or _find_lua()
LUACHECK_BINARY = os.environ.get("LUACHECK_BINARY") or _find_luacheck()

# Extra Lua search path so require("dkjson") works from any workdir
LUA_LIB_PATH = os.path.join(_PROJECT_ROOT, "?.lua")

# --- System prompt (loaded from prompt.txt for easy editing) ---
_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.txt")
with open(_PROMPT_PATH, "r", encoding="utf-8") as _f:
    SYSTEM_PROMPT = _f.read().strip()
