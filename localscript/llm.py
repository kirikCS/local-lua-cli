"""HTTP client to llama.cpp / ollama with streaming + JSON tool call parser."""

import json
import re
from typing import Callable

import requests

from localscript import config


# ---------------------------------------------------------------------------
# Streaming parser — handles both ollama (reasoning field) and llama.cpp
# (<think> tags embedded in content)
# ---------------------------------------------------------------------------

class _StreamParser:

    def __init__(self, on_thinking: Callable[[str], None] | None = None,
                 on_content: Callable[[str], None] | None = None):
        self._on_thinking = on_thinking
        self._on_content = on_content
        self._thinking: list[str] = []
        self._content: list[str] = []
        self._uses_reasoning = False
        self._phase = "detect"  # detect → thinking → content
        self._buf = ""

    def feed(self, delta: dict):
        reasoning = delta.get("reasoning") or ""
        content = delta.get("content") or ""
        if reasoning:
            self._uses_reasoning = True
            self._emit_thinking(reasoning)
        if content:
            if self._uses_reasoning:
                self._emit_content(content)
            else:
                self._parse_tags(content)

    def finish(self) -> tuple[str, str | None]:
        if self._buf:
            (self._emit_thinking if self._phase == "thinking" else self._emit_content)(self._buf)
            self._buf = ""
        return "".join(self._content), "".join(self._thinking) or None

    def _emit_thinking(self, t: str):
        self._thinking.append(t)
        if self._on_thinking:
            self._on_thinking(t)

    def _emit_content(self, t: str):
        self._content.append(t)
        if self._on_content:
            self._on_content(t)

    def _parse_tags(self, token: str):
        self._buf += token
        if self._phase == "detect":
            if "<think>" in self._buf:
                self._phase = "thinking"
                _, after = self._buf.split("<think>", 1)
                self._buf = after
                self._drain_thinking()
            elif len(self._buf) > 20 or "{" in self._buf:
                self._phase = "content"
                self._emit_content(self._buf)
                self._buf = ""
        elif self._phase == "thinking":
            self._drain_thinking()
        elif self._phase == "content":
            self._emit_content(token)
            self._buf = ""

    def _drain_thinking(self):
        if "</think>" in self._buf:
            before, after = self._buf.split("</think>", 1)
            if before:
                self._emit_thinking(before)
            self._phase = "content"
            self._buf = ""
            if after.strip():
                self._emit_content(after)
        else:
            safe = max(0, len(self._buf) - 9)
            if safe:
                self._emit_thinking(self._buf[:safe])
                self._buf = self._buf[safe:]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(messages: list,
             temperature: float | None = None,
             max_tokens: int | None = None,
             enable_thinking: bool | None = None,
             response_format: str | None = "json",
             on_thinking: Callable[[str], None] | None = None,
             on_content: Callable[[str], None] | None = None,
             ) -> tuple[str, str | None]:
    """Stream a chat completion. Returns (content, thinking | None).

    Uses Ollama native /api/chat endpoint for proper think control.
    """
    body = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": temperature if temperature is not None else config.LLM_TEMPERATURE,
            "num_predict": max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS,
            "num_ctx": config.LLM_CTX_SIZE,
        },
    }

    if response_format:
        body["format"] = response_format

    # Ollama native: 'think' at top level
    if enable_thinking is not None:
        body["think"] = enable_thinking

    resp = requests.post(config.LLM_URL, json=body, stream=True, timeout=180)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    parser = _StreamParser(on_thinking=on_thinking, on_content=on_content)

    # Detect response format:
    #   - Ollama native /api/chat: NDJSON (one JSON object per line)
    #   - OpenAI compat /v1/...:   SSE   (data: {json}\n)
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        # SSE format (OpenAI-compatible)
        if raw.startswith("data: "):
            data = raw[6:]
            if data.strip() == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0].get("delta", {})
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            parser.feed(delta)
        # NDJSON format (Ollama native)
        else:
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if chunk.get("done"):
                break
            msg = chunk.get("message", {})
            # Ollama native uses "content" and "thinking" (not "reasoning")
            delta = {}
            if msg.get("thinking"):
                delta["reasoning"] = msg["thinking"]
            if msg.get("content"):
                delta["content"] = msg["content"]
            if delta:
                parser.feed(delta)
    return parser.finish()


# ---------------------------------------------------------------------------
# JSON tool call extraction
# ---------------------------------------------------------------------------

def _try_parse(text: str) -> dict | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _find_balanced_json(text: str) -> dict | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return _try_parse(text[start:i + 1])
    return None


def _normalize(d: dict) -> dict:
    """Normalize alternative JSON formats to canonical {tool, ...}."""
    if "method" in d and "tool" not in d:
        d["tool"] = d.pop("method")
    if "params" in d and isinstance(d["params"], dict):
        d.update(d.pop("params"))
    if "file" in d and "path" not in d:
        d["path"] = d.pop("file")
    if "content" in d and "path" in d and "tool" not in d:
        d["tool"] = "write_file"
    return d


def parse_tool_call(text: str) -> dict | None:
    """Extract a JSON tool call dict from model output.

    Fallback: if model wrote bare Lua code (in ```lua block or plain),
    auto-wrap it in a write_file tool call so no iteration is wasted.
    """
    text = text.strip()
    if not text:
        return None

    # 1) Direct JSON
    r = _try_parse(text)
    if isinstance(r, dict):
        r = _normalize(r)
        if r.get("tool"):
            return r

    # 2) JSON inside ```json block
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        r = _try_parse(m.group(1))
        if isinstance(r, dict):
            r = _normalize(r)
            if r.get("tool"):
                return r

    # 3) Balanced brace extraction
    r = _find_balanced_json(text)
    if isinstance(r, dict):
        r = _normalize(r)
        if r.get("tool"):
            return r

    # 4) Fallback: bare Lua code in ```lua block → auto-wrap as write_file
    m = re.search(r"```lua\s*\n(.*?)```", text, re.DOTALL)
    if m:
        code = m.group(1).strip()
        if code:
            return {"tool": "write_file", "path": "solution.lua", "content": code}

    # 5) Fallback: if text looks like Lua code (has common Lua keywords), wrap it
    if not text.startswith("{") and re.search(
        r"\b(local |function |return |print\(|for |if |end)\b", text
    ):
        return {"tool": "write_file", "path": "solution.lua", "content": text}

    return None
