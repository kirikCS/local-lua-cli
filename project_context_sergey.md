# LocalScript — Project Context для Сергея (Agent + TUI + Infrastructure)

## Хакатон
**МТС True Tech Hack 2026**, задача №2 — MWS Octapi.
**Команда ICEQ:** Сергей (phantom2059) + Кирилл Маханьков.
**Задача:** автономная агентская система на локальной LLM для генерации и валидации Lua-кода (платформа MWS Octapi).
**Регистрация:** до 9 апреля. Старт: 10 апреля (публикация полного ТЗ). Сдача: 15 апреля. Финал офлайн: 24 апреля в Москве (питч 5 мин + 3 мин Q&A).

---

## Что такое MWS Octapi
Интеграционная платформа МТС. Lua используется как скриптовый язык для логики интеграций: трансформация JSON между API, роутинг запросов, валидация данных, обработка ошибок, бизнес-правила. Аналогия — Kong Gateway / OpenResty. **НЕ gamedev, НЕ Roblox, НЕ Luau.**

---

## Разделение ролей

### Кирилл — Fine-tune pipeline
- Конвертация датасетов в ChatML + JSON tool call формат
- CPT (Continued Pretraining) на сыром Lua-коде
- SFT на instruction-парах
- Экспорт модели в GGUF Q8_0
- Бенчмарк и сравнение с базовой моделью

### Сергей (ты) — Agent + TUI + Infrastructure
- Инференс-сервер (llama.cpp / MLX)
- Агентский цикл (оркестратор)
- Инструменты (luacheck, lua54 sandbox)
- TUI на Rich
- Интеграция файнтюненной модели
- Подготовка демо

**Точка синхронизации:** Кирилл передаёт GGUF-файл, Сергей подменяет модель в llama-server и тестирует.

---

## Модель

**Qwen3-4B-Base** (dense, text-only, 36T токенов обучения, Apache 2.0).
- Thinking/non-thinking режимы встроены — мы используем always thinking (`/think`)
- Файнтюн: LoRA/DoRA через Unsloth на Kaggle H100 (делает Кирилл)
- Инференс: GGUF Q8_0 через llama.cpp (5060 16GB) или MLX BF16 (M4 Max 128GB)
- На M4 Max: ~60-80 tok/s в FP8/BF16 — идеально для демо

**Для разработки (до получения файнтюненной модели):**
Используй ванильную Qwen3-4B. Скачай через `ollama pull qwen3:4b` или GGUF с HuggingFace (`unsloth/Qwen3-4B-GGUF`). Агент не зависит от файнтюна — просто подменишь модель потом.

---

## Архитектура агента

### Структура проекта
```
localscript/
    main.py           — entry point, CLI args, запуск TUI
    agent.py          — конечный автомат (6 состояний), главный цикл
    llm.py            — HTTP client к llama.cpp, парсинг JSON tool calls
    tools.py          — luacheck wrapper + lua54 sandbox runner
    context.py        — управление контекстом, сжатие, история ошибок
    ui.py             — Rich: панели, подсветка Lua, diff, спиннеры
    config.py         — пути, URL модели, лимиты, параметры
```

### Конечный автомат (6 состояний)

```
State 0: INIT
    → Собрать system prompt + user request + environment info
    → Перейти в State 1

State 1: LLM_INFERENCE
    → Отправить контекст в модель (llama.cpp API)
    → Модель генерит <think>рассуждения</think> + JSON tool call
    → Перейти в State 2

State 2: PARSE_AND_ROUTE
    → Парсинг JSON из ответа модели
    → Если JSON невалиден → добавить ошибку парсинга в контекст → State 1
    → Если tool == "write_file" или "patch_file" → применить к файлу → State 3
    → Если tool == "complete_task" → State 5

State 3: VALIDATE (Sandbox)
    → Шаг 1: luacheck (статический анализ) — синтаксис, undefined vars, scoping
    → Шаг 2: lua54 execute (runtime) — с таймаутом 5 сек
    → Перейти в State 4

State 4: EVALUATE
    → Если всё ок (exit code 0, нет ошибок) → добавить подтверждение → State 1
    → Если ошибки:
        a) Выполнить сжатие контекста (Context Compacting)
        b) Добавить лог ошибки в контекст
        c) Проверить счётчик итераций (max 5)
        d) Если лимит → State 5, иначе → State 1

State 5: EXIT
    → Очистка временных файлов
    → Вывод результата (успех или лучший вариант с предупреждением)
```

### JSON Tool Call формат

Модель ВСЕГДА отвечает JSON-ом. Доступные инструменты:

**write_file** — создание нового файла:
```json
{
    "tool": "write_file",
    "path": "solution.lua",
    "content": "local function validate_ip(ip)\n  ...\nend"
}
```

**patch_file** — исправление конкретных строк:
```json
{
    "tool": "patch_file",
    "path": "solution.lua",
    "patches": [
        {"line_start": 5, "line_end": 7, "content": "  if not ip then return false end"}
    ]
}
```

**complete_task** — завершение (модель уверена что код корректен):
```json
{
    "tool": "complete_task",
    "summary": "Function validates IPv4 addresses with proper error handling"
}
```

**Парсинг:** Модель может обернуть JSON в markdown-блоки (```json...```), или добавить текст до/после. Парсер должен уметь извлечь JSON из любого ответа. Fallback: если JSON не найден — считать ошибкой формата, вернуть в State 1 с сообщением об ошибке.

**Grammar constraint (llama.cpp):** Можно использовать GBNF grammar для принудительной генерации валидного JSON. Это минимизирует ошибки парсинга.

### Context Manager (context.py)

```python
class Context:
    # Защищённая часть (неизменяемая)
    system_message: str      # Роль, инструкции, схема инструментов
    user_request: str        # Исходная задача + параметры среды
    
    # Динамическая часть (Working Memory)
    iterations: list         # История: assistant tool call → system response
    current_code: str        # Актуальное состояние кода
    current_file: str        # Путь к текущему файлу
    
    def build_messages(self) -> list:
        """Собрать массив messages для отправки в модель"""
        return [
            {"role": "system", "content": self.system_message},
            {"role": "user", "content": self.user_request},
            *self.iterations  # рабочая память
        ]
    
    def add_tool_call(self, tool_json: str):
        """Добавить вызов инструмента от модели"""
        
    def add_tool_result(self, result: str):
        """Добавить результат выполнения инструмента"""
        
    def compact(self):
        """Сжатие контекста (вызывается при ошибках)"""
```

### Три механизма сжатия контекста

**1. Sliding Window (удаление разрешённых ошибок):**
При новой ошибке — удалить блоки сообщений предыдущей (уже исправленной) ошибки. Оставить только текущую проблему.

**2. State Collapse (схлопывание файловых операций):**
Если накопилось несколько patch_file для одного файла — удалить их все, заменить одним системным сообщением:
`[System: Текущее состояние файла solution.lua:\n<актуальный_код>]`

**3. Log Truncation (обрезка стектрейсов):**
Если stderr > 20 строк — оставить первые 5 + последние 5, середину заменить на `[... truncated ...]`.

### System Prompt

```
You are a Senior Lua Engineer for MWS Octapi integration platform.
You write clean, production-ready Lua 5.4 code for API integrations.

RULES:
1. ALWAYS respond with a JSON tool call. Never respond with plain text.
2. Available tools: write_file, patch_file, complete_task
3. Use standard Lua 5.4 syntax. No Luau, no Roblox APIs.
4. Use pcall/xpcall for error handling.
5. Use cjson for JSON operations.
6. Return structured results: {ok=bool, data=..., error=...}

ENVIRONMENT:
- Lua 5.4 runtime
- cjson library available
- luacheck for static analysis
```

### LLM Client (llm.py)

```python
import requests
import json
import re

LLM_URL = "http://localhost:8080/v1/chat/completions"

def generate(messages: list, temperature=0.6, max_tokens=4096) -> str:
    """Отправить запрос в llama.cpp и получить ответ"""
    response = requests.post(LLM_URL, json={
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    })
    return response.json()["choices"][0]["message"]["content"]

def parse_tool_call(raw_response: str) -> dict | None:
    """Извлечь JSON tool call из ответа модели"""
    # Убрать <think>...</think> блок
    cleaned = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()
    
    # Попробовать прямой JSON парсинг
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    
    # Попробовать извлечь из markdown блока
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', cleaned, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
    
    # Попробовать найти первый {...} в тексте
    brace_match = re.search(r'\{[^{}]*\}', cleaned, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass
    
    return None  # Ошибка парсинга
```

### Tools (tools.py)

```python
import subprocess
import tempfile
import os

def luacheck(code: str) -> dict:
    """Статический анализ через luacheck"""
    with tempfile.NamedTemporaryFile(suffix='.lua', mode='w', delete=False, encoding='utf-8') as f:
        f.write(code)
        f.flush()
        try:
            result = subprocess.run(
                ['luacheck', f.name, '--no-color', '--codes', '--ranges'],
                capture_output=True, text=True, timeout=10
            )
            errors = []
            for line in result.stdout.strip().split('\n'):
                if ':' in line and ('W' in line or 'E' in line):
                    errors.append(line)
            return {"success": len(errors) == 0, "errors": errors, "raw": result.stdout}
        finally:
            os.unlink(f.name)

def lua_execute(code: str, timeout: int = 5) -> dict:
    """Запуск Lua-кода в sandbox"""
    with tempfile.NamedTemporaryFile(suffix='.lua', mode='w', delete=False, encoding='utf-8') as f:
        f.write(code)
        f.flush()
        try:
            result = subprocess.run(
                ['lua54', f.name],  # или полный путь к lua54.exe
                capture_output=True, text=True, timeout=timeout
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "stdout": "", "stderr": "TIMEOUT: execution exceeded 5 seconds", "returncode": -1}
        finally:
            os.unlink(f.name)
```

### TUI (ui.py) — Rich

```python
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.live import Live
from rich.spinner import Spinner

console = Console()

def show_generating():
    console.print(Spinner("dots", text="Generating Lua code..."), style="cyan")

def show_code(code: str, title: str = "Generated Code"):
    syntax = Syntax(code, "lua", theme="monokai", line_numbers=True)
    console.print(Panel(syntax, title=title, border_style="green"))

def show_lint_errors(errors: list):
    console.print(Panel("\n".join(errors), title="luacheck errors", border_style="red"))

def show_runtime_error(stderr: str):
    console.print(Panel(stderr, title="Runtime Error", border_style="red"))

def show_success(code: str, iterations: int):
    console.print(Panel(f"✅ All checks passed in {iterations} iteration(s)", border_style="green"))
    show_code(code, title="Final Code")

def show_repair(iteration: int, max_iter: int):
    console.print(f"[yellow]🔧 Repairing code (iteration {iteration}/{max_iter})...[/yellow]")

def show_diff(old_code: str, new_code: str):
    # Показать diff между версиями
    pass
```

---

## Инференс-сервер

### Вариант A: llama.cpp (для разработки + 5060)
```bash
# Скачать модель
huggingface-cli download unsloth/Qwen3-4B-GGUF --include "*Q8_0*" --local-dir ./models

# Запустить сервер
llama-server -m ./models/qwen3-4b-q8_0.gguf \
    --port 8080 \
    --n-gpu-layers 99 \
    --ctx-size 4096 \
    --threads 8
```

### Вариант B: MLX (для M4 Max, демо на финале)
```bash
pip install mlx-lm
mlx_lm.server --model mlx-community/Qwen3-4B-bf16 --port 8080
```

### Проверка что API работает
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Write hello world in Lua"}],"max_tokens":200}'
```

---

## Интеграция файнтюненной модели

Когда Кирилл передаст GGUF файл:
1. Скачать файл (Google Drive / HuggingFace)
2. Остановить llama-server
3. Запустить с новой моделью: `llama-server -m ./models/localscript-qwen3-4b-q8.gguf ...`
4. Протестировать 20-30 задач из разных категорий
5. Подтюнить промпты если модель ведёт себя иначе

**Формат ответа модели после файнтюна:**
Модель обучена отвечать JSON tool calls. System prompt должен совпадать с тем, на котором обучали (см. раздел System Prompt выше). Если формат расходится — синхронизироваться с Кириллом.

---

## Интерфейс совместимости с Кириллом

### Что Кирилл передаёт Сергею:
- GGUF файл модели (Q8_0, ~4.5GB)
- System prompt, на котором обучали (должен совпадать с тем что в agent)
- Примеры tool call формата (для проверки)
- Результаты бенчмарка (процент корректного JSON, качество кода)

### Что должно совпадать:
- **System prompt** — дословно одинаковый в обучении и в инференсе
- **JSON schema** tool calls — `write_file`, `patch_file`, `complete_task`
- **ChatML формат** — `<|im_start|>system`, `<|im_start|>user`, `<|im_start|>assistant`
- **Thinking mode** — модель обучена с `<think>` блоками, парсер их убирает

---

## Подготовка к демо (финал 24 апреля)

### Killer scenario для питча:
1. Открываешь терминал на M4 Max
2. Пишешь: `localscript "Напиши Lua-скрипт для валидации JWT токенов из входящих API-запросов"`
3. В реальном времени видно: модель думает → генерирует код → luacheck проверяет → lua54 запускает → если ошибка → модель фиксит → повторная проверка → "All checks passed"
4. Итого: 15-30 секунд от запроса до рабочего кода
5. Всё локально, без интернета

### Что показать жюри:
- Итеративный цикл генерация → валидация → фикс (главная фишка)
- Файнтюн на Lua-данных (уникальность решения)
- Красивый TUI с подсветкой кода, diff, спиннерами
- Полная автономность — всё на ноутбуке, без облака

---

## Зависимости

```
# Python
pip install rich requests

# Lua tools
# luacheck: https://github.com/mpeterv/luacheck
# lua54: https://www.lua.org/download.html

# LLM inference
# llama.cpp: https://github.com/ggerganov/llama.cpp
# или MLX: pip install mlx-lm
```

---

## Чеклист

- [ ] Инференс-сервер: llama.cpp + ванильная Qwen3-4B работает
- [ ] tools.py: luacheck вызывается, парсит ошибки
- [ ] tools.py: lua54 execute с таймаутом работает
- [ ] llm.py: HTTP клиент к llama.cpp работает
- [ ] llm.py: парсинг JSON tool calls из ответа модели
- [ ] context.py: build_messages, add_tool_call, add_tool_result
- [ ] context.py: сжатие контекста (sliding window, state collapse, log truncation)
- [ ] agent.py: конечный автомат 6 состояний работает end-to-end
- [ ] ui.py: Rich панели, подсветка кода, спиннеры
- [ ] Интеграция файнтюненной модели от Кирилла
- [ ] Тестирование 20-30 задач из разных категорий
- [ ] Демо-сценарий отработан
- [ ] Регистрация на хакатон (до 9 апреля)
