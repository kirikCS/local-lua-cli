"""Prompt difficulty classifier for auto-thinking mode.

Extracts 28 text features from a user prompt and predicts whether the
model should use <think> blocks (complex task) or skip them (trivial task).

Two modes:
  1. ML model: imports _model_generated.py if it exists (trained LightGBM/LogReg)
  2. Rule-based fallback: uses hand-tuned feature thresholds
"""

import re
import os
from typing import Dict

# ---------------------------------------------------------------------------
# Keyword lists (English + Russian)
# ---------------------------------------------------------------------------

_ERROR_KEYWORDS = {
    # English
    "error", "bug", "fix", "traceback", "failed", "crash", "broken",
    "exception", "stack trace", "segfault", "panic", "abort",
    # Russian
    "ошибка", "баг", "исправь", "починить", "сломалось", "не работает",
    "падает", "крашится", "исключение", "сбой",
}

_MULTI_STEP_KEYWORDS = {
    # English
    "then", "after that", "next step", "finally", "first", "second", "third",
    "step 1", "step 2", "step 3", "1)", "2)", "3)", "1.", "2.", "3.",
    # Russian
    "затем", "после этого", "далее", "потом", "шаг", "сначала",
    "во-первых", "во-вторых", "в-третьих",
}

_CONSTRAINT_KEYWORDS = {
    # English
    "must", "should", "validate", "handle", "ensure", "require", "check",
    "verify", "enforce", "guarantee", "constraint", "mandatory",
    # Russian
    "должен", "обязательно", "нужно", "валидировать", "обработать",
    "проверить", "гарантировать", "убедиться", "обязан",
}

_REFACTOR_KEYWORDS = {
    # English
    "refactor", "rewrite", "optimize", "migrate", "restructure", "redesign",
    "improve", "rearchitect", "modernize",
    # Russian
    "рефакторинг", "переписать", "оптимизировать", "мигрировать",
    "перенести", "улучшить", "переделать", "модернизировать",
}

_MODULE_KEYWORDS = {
    # English
    "module", "class", "system", "engine", "framework", "library",
    "service", "middleware", "pipeline", "architecture",
    # Russian
    "модуль", "класс", "система", "движок", "фреймворк", "библиотека",
    "сервис", "мидлвар", "пайплайн", "архитектура",
}

_SIMPLE_FUNCTION_PREFIXES = [
    # English
    "write a lua function that",
    "write a function that",
    "create a function that",
    "implement a function that",
    "write a simple",
    # Russian
    "напиши lua функцию",
    "напиши функцию",
    "создай функцию",
    "реализуй функцию",
    "напиши простую",
]

_API_KEYWORDS = {
    # English
    "api", "endpoint", "webhook", "oauth", "rest", "http", "request",
    "response", "url", "uri", "graphql", "soap",
    # Russian
    "эндпоинт", "вебхук", "запрос", "ответ",
}

_DATA_PROCESSING_KEYWORDS = {
    # English
    "transform", "pipeline", "etl", "aggregate", "batch", "stream",
    "map", "reduce", "filter", "convert", "parse", "serialize",
    # Russian
    "трансформировать", "пайплайн", "агрегировать", "пакетный",
    "преобразовать", "парсить", "сериализовать",
}

_STATEFUL_KEYWORDS = {
    # English
    "state", "session", "cache", "queue", "workflow", "fsm",
    "state machine", "stateful", "persistent", "accumulator",
    # Russian
    "состояние", "сессия", "кэш", "очередь", "воркфлоу",
    "рабочий процесс", "автомат", "конечный автомат",
}

_SECURITY_KEYWORDS = {
    # English
    "auth", "hmac", "encrypt", "signature", "gdpr", "mask", "token",
    "jwt", "certificate", "ssl", "tls", "hash", "salt", "csrf",
    # Russian
    "авторизация", "шифрование", "подпись", "маскирование",
    "токен", "сертификат", "хеш",
}

_CONCURRENCY_KEYWORDS = {
    # English
    "concurrent", "lock", "thread", "async", "rate limit", "throttle",
    "semaphore", "mutex", "parallel", "atomic",
    # Russian
    "конкурентный", "блокировка", "асинхронный", "лимит запросов",
    "параллельный", "атомарный",
}

_LUA_KEYWORDS = {
    "pcall", "xpcall", "require", "metatables", "setmetatable",
    "getmetatable", "__index", "__newindex", "__call", "__tostring",
    "coroutine", "string.find", "string.match", "string.gmatch",
    "table.concat", "table.insert", "table.sort", "io.open",
    "os.time", "os.date", "cjson", "dkjson", "luasocket",
    "local function", "return {",
}

_TECHNICAL_KEYWORDS = {
    # English
    "algorithm", "complexity", "recursive", "iteration", "binary search",
    "hash map", "linked list", "tree", "graph", "sorting", "parsing",
    "regex", "pattern", "protocol", "specification", "schema",
    "idempotent", "deterministic", "polymorphism", "encapsulation",
    # Russian
    "алгоритм", "сложность", "рекурсивный", "итерация",
    "структура данных", "сортировка", "парсинг", "протокол",
    "спецификация", "схема",
}

_FILE_PATH_RE = re.compile(r'(?:[\w/.-]+\.(?:lua|json|csv|xml|txt|log|cfg|ini|yaml|yml))\b')
_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```')
_BULLET_RE = re.compile(r'(?:^|\n)\s*(?:[-*•]|\d+[.)]\s)', re.MULTILINE)
_FUNC_SIG_RE = re.compile(
    r'(?:function\s+\w+|def\s+\w+|func\s+\w+|fn\s+\w+|\w+\s*\([^)]*\)\s*[:{])',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Feature extraction (28 features)
# ---------------------------------------------------------------------------

def _count_keywords(text_lower: str, keywords: set) -> int:
    """Count how many keywords from the set appear in text."""
    return sum(1 for kw in keywords if kw in text_lower)


def extract_features(prompt: str) -> Dict[str, float]:
    """Extract 28 features from a user prompt.

    Returns a dict with named features, values are floats.
    """
    text = prompt.strip()
    text_lower = text.lower()
    words = text.split()
    word_count = max(len(words), 1)

    # Strip code blocks for some text-only analysis
    text_no_code = _CODE_BLOCK_RE.sub("", text)
    code_blocks = _CODE_BLOCK_RE.findall(text)

    # === A. Length & Structure (8) ===
    char_count = len(text)
    sentence_count = max(len(re.split(r'[.!?]+', text_no_code)), 1)
    line_count = text.count("\n") + 1
    avg_word_length = sum(len(w) for w in words) / word_count if words else 0
    code_block_count = len(code_blocks)
    code_block_chars = sum(len(b) for b in code_blocks)
    bullet_count = len(_BULLET_RE.findall(text))

    # === B. Lexical Complexity (6) ===
    unique_words = set(w.lower() for w in words)
    unique_word_ratio = len(unique_words) / word_count if word_count else 0
    technical_keyword_density = _count_keywords(text_lower, _TECHNICAL_KEYWORDS) / word_count
    lua_keyword_count = _count_keywords(text_lower, _LUA_KEYWORDS)
    has_error_context = float(_count_keywords(text_lower, _ERROR_KEYWORDS) > 0)
    has_multi_step_markers = float(_count_keywords(text_lower, _MULTI_STEP_KEYWORDS) > 0)
    constraint_count = _count_keywords(text_lower, _CONSTRAINT_KEYWORDS)

    # === C. Task Type Signals (7) ===
    is_code_repair = float(
        has_error_context and code_block_count > 0
    )
    is_refactor = float(_count_keywords(text_lower, _REFACTOR_KEYWORDS) > 0)
    is_module_request = float(_count_keywords(text_lower, _MODULE_KEYWORDS) > 0)
    is_simple_function = float(
        any(text_lower.startswith(p) for p in _SIMPLE_FUNCTION_PREFIXES)
        and char_count < 200
    )
    file_count = len(_FILE_PATH_RE.findall(text))
    # Heuristic: if prompt mentions fix/patch → likely patch_file
    expected_tool_type = 1.0 if (has_error_context and code_block_count > 0) else 0.0
    has_example_io = float(
        any(marker in text_lower for marker in [
            "e.g.", "example", "input:", "output:", "for instance",
            "например", "пример", "ввод:", "вывод:", "к примеру",
        ])
    )

    # === D. Domain-Specific (5) ===
    api_integration_signals = _count_keywords(text_lower, _API_KEYWORDS)
    data_processing_signals = _count_keywords(text_lower, _DATA_PROCESSING_KEYWORDS)
    stateful_logic_signals = _count_keywords(text_lower, _STATEFUL_KEYWORDS)
    security_signals = _count_keywords(text_lower, _SECURITY_KEYWORDS)
    concurrency_signals = _count_keywords(text_lower, _CONCURRENCY_KEYWORDS)

    # === E. Structural Complexity (2) ===
    # Max nesting depth of brackets
    max_depth = 0
    depth = 0
    for ch in text:
        if ch in "([{":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch in ")]}":
            depth = max(0, depth - 1)
    parenthetical_depth = max_depth

    function_signature_count = len(_FUNC_SIG_RE.findall(text))

    return {
        # A. Length & Structure
        "char_count": float(char_count),
        "word_count": float(word_count),
        "sentence_count": float(sentence_count),
        "line_count": float(line_count),
        "avg_word_length": avg_word_length,
        "code_block_count": float(code_block_count),
        "code_block_chars": float(code_block_chars),
        "bullet_count": float(bullet_count),
        # B. Lexical Complexity
        "unique_word_ratio": unique_word_ratio,
        "technical_keyword_density": technical_keyword_density,
        "lua_keyword_count": float(lua_keyword_count),
        "has_error_context": has_error_context,
        "has_multi_step_markers": has_multi_step_markers,
        "constraint_count": float(constraint_count),
        # C. Task Type Signals
        "is_code_repair": is_code_repair,
        "is_refactor": is_refactor,
        "is_module_request": is_module_request,
        "is_simple_function": is_simple_function,
        "file_count": float(file_count),
        "expected_tool_type": expected_tool_type,
        "has_example_io": has_example_io,
        # D. Domain-Specific
        "api_integration_signals": float(api_integration_signals),
        "data_processing_signals": float(data_processing_signals),
        "stateful_logic_signals": float(stateful_logic_signals),
        "security_signals": float(security_signals),
        "concurrency_signals": float(concurrency_signals),
        # E. Structural Complexity
        "parenthetical_depth": float(parenthetical_depth),
        "function_signature_count": float(function_signature_count),
    }


# Canonical feature order (must match training)
FEATURE_NAMES = list(extract_features("dummy").keys())


# ---------------------------------------------------------------------------
# Rule-based fallback
# ---------------------------------------------------------------------------

def _rule_based_predict(features: Dict[str, float]) -> float:
    """Rule-based fallback when no ML model is available.

    Returns probability of 'think' class (0.0 = definitely easy, 1.0 = definitely hard).
    Conservative: when in doubt, returns > 0.5 (think).
    """
    score = 0.5  # default: think

    # Strong easy signals
    if features["is_simple_function"]:
        score -= 0.4
    if features["char_count"] < 100 and features["code_block_count"] == 0:
        score -= 0.3
    if features["word_count"] < 20:
        score -= 0.2

    # Strong hard signals
    if features["is_code_repair"]:
        score += 0.3
    if features["is_module_request"]:
        score += 0.2
    if features["has_multi_step_markers"]:
        score += 0.15
    if features["constraint_count"] >= 3:
        score += 0.15
    if features["code_block_count"] >= 2:
        score += 0.2
    if features["char_count"] > 500:
        score += 0.15
    if features["is_refactor"]:
        score += 0.2
    if features["stateful_logic_signals"] > 0:
        score += 0.1
    if features["security_signals"] > 0:
        score += 0.1
    if features["api_integration_signals"] >= 2:
        score += 0.1

    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# ML model (loaded dynamically)
# ---------------------------------------------------------------------------

_ml_predict = None

def _load_ml_model():
    """Try to import the generated ML model."""
    global _ml_predict
    try:
        from localscript._model_generated import predict as ml_pred
        _ml_predict = ml_pred
    except ImportError:
        _ml_predict = None

_load_ml_model()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def predict_difficulty(prompt: str) -> float:
    """Predict thinking probability for a prompt.

    Returns float 0.0–1.0 where higher = more likely to need thinking.
    Uses ML model if available, otherwise falls back to rules.
    """
    features = extract_features(prompt)

    if _ml_predict is not None:
        return _ml_predict(features)

    return _rule_based_predict(features)


def should_think(prompt: str, threshold: float = 0.5) -> bool:
    """Determine if the model should use <think> blocks for this prompt.

    Returns True if the prompt is complex enough to warrant thinking.
    """
    return predict_difficulty(prompt) >= threshold
