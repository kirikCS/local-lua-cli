"""Configuration: paths, URLs, limits, parameters, system prompt."""

import os
import shutil

VERSION = "0.5.0"

# --- Paths ---
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Inference server ---
LLM_URL = os.environ.get("LLM_URL", "http://localhost:11434/api/chat")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:4b-thinking-2507-q8_0")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.6"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "32768"))
LLM_CTX_SIZE = int(os.environ.get("LLM_CTX_SIZE", "32768"))

# --- Agent limits ---
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "10"))
EXECUTE_TIMEOUT = int(os.environ.get("EXECUTE_TIMEOUT", "5"))

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

# --- Tool paths ---
_lua_found = shutil.which("lua54")
_lua_default = os.path.abspath(_lua_found) if _lua_found else os.path.join(_PROJECT_ROOT, "lua54.exe")
LUA_BINARY = os.environ.get("LUA_BINARY", _lua_default)

_lc_found = shutil.which("luacheck")
_lc_default = os.path.abspath(_lc_found) if _lc_found else os.path.join(_PROJECT_ROOT, "luacheck.exe")
LUACHECK_BINARY = os.environ.get("LUACHECK_BINARY", _lc_default)

# Extra Lua search path so require("dkjson") works from any workdir
LUA_LIB_PATH = os.path.join(_PROJECT_ROOT, "?.lua").replace("\\", "/")

# --- System prompt (loaded from prompt.txt for easy editing) ---
_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.txt")
with open(_PROMPT_PATH, "r", encoding="utf-8") as _f:
    SYSTEM_PROMPT = _f.read().strip()
