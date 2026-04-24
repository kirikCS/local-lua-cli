"""Lua reference manual lookup — BM25-lite search over bundled docs.

Loads `data/lua_docs.json` once on first call, builds an in-memory index,
and answers `search(query, top_k)` with the most relevant chunks.
"""

import json
import math
import os
import re

_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lua_docs.json")

# Minimal stopword set — keeps Lua identifiers and operators searchable.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "be", "as", "by", "with", "that", "this", "it", "its", "from", "at", "if",
    "any", "all", "can", "you", "your", "we", "us", "our", "i", "me", "my",
    "do", "does", "how", "what", "when", "which", "who", "why", "where",
    "into", "than", "then", "but", "not", "no", "yes", "so", "only",
})

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
# Qualified identifier: foo.bar or foo.bar.baz — capture as a single rare token.
_QUALIFIED_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+")

# BM25 parameters (standard defaults)
_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    """Tokenize for indexing.

    Emits both bare identifiers (`format`) AND qualified identifiers
    (`string.format`) so exact API lookups outrank substring noise.
    """
    tokens: list[str] = []
    for m in _QUALIFIED_RE.findall(text):
        tokens.append(m.lower())
    for t in _TOKEN_RE.findall(text):
        low = t.lower()
        if low in _STOPWORDS or len(low) <= 1:
            continue
        tokens.append(low)
    return tokens


def _parse_doc_file(path: str) -> list[str]:
    """Parse the file as a stream of concatenated JSON objects."""
    with open(path, "r", encoding="utf-8") as f:
        data = f.read()
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    i = 0
    n = len(data)
    while i < n:
        while i < n and data[i] in " \t\n\r":
            i += 1
        if i >= n:
            break
        try:
            obj, end = decoder.raw_decode(data, i)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict) and isinstance(obj.get("text"), str):
            chunks.append(obj["text"])
        i = end
    return chunks


class _Index:
    """Lazy BM25 index over the bundled Lua reference manual."""

    def __init__(self):
        self._loaded = False
        self.chunks: list[str] = []
        self.doc_tokens: list[list[str]] = []
        self.doc_len: list[int] = []
        self.avgdl: float = 0.0
        self.df: dict[str, int] = {}
        self.idf: dict[str, float] = {}

    def load(self):
        if self._loaded:
            return
        if not os.path.isfile(_DATA_PATH):
            self._loaded = True
            return
        self.chunks = _parse_doc_file(_DATA_PATH)
        # Boost: duplicate title tokens twice so the heading dominates ranking.
        # A chunk that *defines* `string.format` outranks one that merely links to it.
        self.doc_tokens = []
        for c in self.chunks:
            title = c.split("\n", 1)[0]
            body_tokens = _tokenize(c)
            title_tokens = _tokenize(title)
            self.doc_tokens.append(body_tokens + title_tokens * 2)
        self.doc_len = [len(toks) for toks in self.doc_tokens]
        self.avgdl = sum(self.doc_len) / len(self.doc_len) if self.doc_len else 0.0
        df: dict[str, int] = {}
        for toks in self.doc_tokens:
            for term in set(toks):
                df[term] = df.get(term, 0) + 1
        self.df = df
        n = len(self.chunks)
        self.idf = {
            term: math.log(1 + (n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }
        self._loaded = True

    def search(self, query: str, top_k: int = 3) -> list[tuple[float, str]]:
        self.load()
        if not self.chunks:
            return []
        q_terms = _tokenize(query)
        if not q_terms:
            return []
        scores: list[tuple[float, int]] = []
        for idx, toks in enumerate(self.doc_tokens):
            if not toks:
                continue
            dl = self.doc_len[idx]
            score = 0.0
            tf_local: dict[str, int] = {}
            for t in toks:
                tf_local[t] = tf_local.get(t, 0) + 1
            for term in q_terms:
                tf = tf_local.get(term, 0)
                if tf == 0:
                    continue
                idf = self.idf.get(term, 0.0)
                denom = tf + _K1 * (1 - _B + _B * dl / self.avgdl)
                score += idf * (tf * (_K1 + 1)) / denom
            if score > 0:
                scores.append((score, idx))
        scores.sort(key=lambda x: x[0], reverse=True)
        return [(s, self.chunks[i]) for s, i in scores[:top_k]]


_INDEX = _Index()


def search(query: str, top_k: int = 3) -> list[dict]:
    """Search the Lua reference manual.

    Returns a list of {"score": float, "text": str} dicts, ordered by relevance.
    """
    return [{"score": round(s, 3), "text": t} for s, t in _INDEX.search(query, top_k)]
