# locallua-iceq: AI-агент для генерации Lua-кода 

Локальный AI-агент для генерации, валидации и итеративного улучшения Lua-кода в среде MWS Octapi LowCode. Работает полностью офлайн через Ollama, без обращения к внешним AI-сервисам.

## Два режима работы

locallua-iceq предоставляет **два независимых пайплайна**, каждый со своей точкой входа:

| | CLI-агент (`locallua-iceq`) | API-сервер (`locallua-iceq-server`) |
|---|---|---|
| **Назначение** | Интерактивная разработка с пользователем | Бенчмарк / программная интеграция |
| **Интерфейс** | TUI (Textual) или Rich REPL | HTTP REST API (`POST /generate`) |
| **Режим работы** | Мульти-итерационный: генерация -> валидация -> исправление -> повтор | Однократная генерация: запрос -> код |
| **Валидация** | luacheck + lua5.4 + inline self-tests + цикл исправлений (до 10 итераций) | Нет (single-shot) |
| **Инструменты** | 11 tool calls (write_file, patch_file, read_file, lookup_docs, search_memory и др.) | Нет |
| **Память (RAG)** | SQLite + FTS5 + гибридный BM25/cosine rerank | Нет |
| **Планирование** | Автоматическая декомпозиция сложных задач на подзадачи | Нет |
| **Контекст** | Авто-компакт при переполнении 4K окна | 256 токенов на ответ |

## Требования

- macOS (Apple Silicon или Intel)
- Python 3.11+
- [Homebrew](https://brew.sh/)
- [Ollama](https://ollama.com/)
- Lua 5.4 и luacheck (для CLI-агента)

## Установка

```bash
# 1. Системные зависимости
brew install lua luacheck

# 2. Модели Ollama
ollama pull qwen3:4b-thinking-2507-q8_0
# Опционально: модель эмбеддингов для гибридного memory RAG
ollama pull nomic-embed-text

# 3. Установка locallua-iceq
git clone https://github.com/phantom2059/localscript-agent.git
cd localscript-agent
git checkout macOS
pip install -e .
```

Lua-бинарник определяется автоматически: `lua5.4` -> `lua54` -> `lua` в `PATH`.

### Запуск в Docker

Два независимых образа — сервер и CLI собираются из разных Dockerfile:

```bash
# Собрать оба образа
docker compose build

# Запустить API-сервер (на host.docker.internal:11434 должна быть Ollama)
docker compose up server
# → http://localhost:8080/generate

# Запустить CLI (интерактивный)
docker compose run --rm -it cli
docker compose run --rm -it cli --model locallua-iceq-sft   # с fine-tuned моделью
```

Ollama запускается **на хосте** (`ollama serve`), а не в контейнере — иначе при каждом старте пришлось бы заново тянуть 4+ ГБ весов. Контейнеры достучатся до хоста через `host.docker.internal` (Docker Desktop добавляет этот DNS автоматически, для Linux в compose уже прописан `extra_hosts: host-gateway`).

Модель `locallua-iceq-sft` (fine-tuned) уже импортирована в Ollama через `ollama create locallua-iceq-sft -f Modelfile` из `models/model-Q8_0.gguf`. Если на другой машине — повторите этот шаг.

---

## Пайплайн 1: API-сервер (`locallua-iceq-server`)

Однократная генерация Lua-кода по запросу на естественном языке. Для бенчмарков и программной интеграции.

### Запуск

```bash
locallua-iceq-server                                          # модель по умолчанию: locallua-iceq-sft (fine-tuned)
# или: python -m localscript.server
# или: locallua-iceq-server --model qwen3:4b-thinking-2507-q8_0   # переключение на thinking-модель
# или: locallua-iceq-server --port 9000
```

API-сервер по умолчанию использует **fine-tuned модель `locallua-iceq-sft`** — это `Qwen3-4B-Instruct-2507` (non-thinking) с LoRA-адаптером, обученным на датасете MWS Octapi LowCode. В отличие от thinking-варианта, она не тратит бюджет токенов на chain-of-thought и помещается в `num_predict=256`.

### Эндпоинт

```
POST /generate  {"prompt": "..."} -> {"code": "..."}
```

### Пример

```bash
curl -s -X POST http://localhost:8080/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Из полученного списка email получи последний"}' \
  | python3 -m json.tool
```

### Параметры бенчмарка

| Параметр | Значение |
|---|---|
| `num_ctx` | 4096 |
| `num_predict` | 256 |
| Модель (API) | `locallua-iceq-sft` (Qwen3-4B-Instruct-2507 + LoRA) |
| Модель (CLI) | `qwen3:4b-thinking-2507-q8_0` (переключается через `/model`) |
| Квантизация | Q8_0 |
| Пиковый VRAM | < 8 ГБ |

### Системный промпт API

Сервер использует короткий промпт (+-150 токенов), заточенный под MWS Octapi LowCode:
- Переменные через `wf.vars.*` и `wf.initVariables.*`
- Массивы через `_utils.array.new()` и `_utils.array.markAsArray()`
- Результат возвращается через `return` (inline-сниппет, не файл)
- Без `require()`, без обёрток в модули, без файловых операций

---

## Пайплайн 2: CLI-агент (`locallua-iceq`)

Полноценный интерактивный агент с инструментами, валидацией, памятью и планированием задач.

### Запуск

```bash
mkdir my_project && cd my_project
locallua-iceq                                       # по умолчанию: qwen3:4b-thinking-2507-q8_0
locallua-iceq --model locallua-iceq-sft                      # fine-tuned модель (быстрее, без thinking)
locallua-iceq --no-tui                              # Rich REPL вместо Textual TUI
```

Переключение модели внутри сессии: `/model <name>`. Tab-автодополнение подтягивает список из `ollama list`, так что `locallua-iceq-sft` и `qwen3:4b-thinking-2507-q8_0` появятся автоматически.

### Пример сессии

```
> Напиши валидатор запросов с проверкой обязательных полей
> Создай src/utils.lua со строковыми хелперами и src/app.lua, который их использует
> /memory --on
> /undo
> /help
```

### Архитектура агента

```
Запрос пользователя
    │
    ▼
[Планирование?] ── thinking=on -> декомпозиция на подзадачи
    │                              │
    │ нет                          │ да
    ▼                              ▼
[Обычный цикл]          [Для каждой подзадачи:]
    │                     ├── сброс контекста (память сохраняется)
    ▼                     ├── agent loop (до 5 итераций)
[1. LLM]                 ├── отметка в task_tracker.md
    │                     ├── сохранение результата в память
    ▼                     └── следующая подзадача
[2. Парсинг JSON tool call]
    │
    ▼
[3. Исполнение инструмента]
    │
    ▼
[4. Авто-sandbox: luacheck + lua5.4 + self-tests]
    │                │
    │ pass           │ fail
    ▼                ▼
[5. Готово]    [Цикл исправлений -> назад к 1]
```

### Инструменты агента

| Инструмент | Описание |
|---|---|
| `write_file` | Создать или перезаписать файл |
| `patch_file` | Редактировать диапазон строк |
| `read_file` | Прочитать содержимое файла |
| `list_files` | Список файлов в директории |
| `run_sandbox` | Запустить luacheck + lua5.4 вручную |
| `run_lua` | Выполнить произвольный Lua-сниппет с тестовыми данными |
| `lookup_docs` | Поиск по встроенному справочнику Lua 5.4 |
| `search_memory` | Поиск по долговременной памяти проекта |
| `install_package` | Установка через luarocks (whitelist) |
| `message` | Текстовый ответ пользователю |
| `complete_task` | Сигнал завершения задачи |

### Slash-команды

| Команда | Описание |
|---|---|
| `/help` | Список всех команд |
| `/status` | Статистика сессии |
| `/history` | История задач |
| `/cost` | Потребление токенов |
| `/copy` | Скопировать последний вывод (только TUI) |
| `/undo` | Откатить последний промпт (файлы, контекст, память) |
| `/compact` | Сжать контекст вручную |
| `/memory` | Долговременная память: `--on`, `--off`, `clear` |
| `/plan` | Показать текущий план задачи |
| `/model` | Показать/сменить модель |
| `/think` | Всегда включать режим мышления |
| `/no_think` | Всегда выключать режим мышления |
| `/auto_think` | Авто-выбор по сложности промпта |
| `/resume` | Возобновить сохранённый чат |
| `/clear` | Очистить контекст |
| `/exit` | Выход |

### Ключевые возможности

- **Авто-sandbox**: каждый `write_file`/`patch_file` для `.lua` автоматически запускает luacheck + lua5.4
- **Inline self-tests**: модель пишет тесты прямо в файле через `if arg then assert(...) end` — проверка корректности без отдельных тестовых файлов
- **Авто-compact**: при приближении к лимиту 4K контекста автоматически сжимает историю
- **Планирование задач**: в режиме мышления сложные задачи декомпозируются на подзадачи с изолированным контекстом
- **Долговременная память**: SQLite + FTS5 + опциональный dense rerank через `nomic-embed-text`
- **Гибридный поиск**: BM25 (лексический) + cosine (семантический) с двухступенчатым reranking
- **Turn-level /undo**: один `/undo` откатывает файлы, контекст и память атомарно
- **Кнопка остановки**: кликабельная кнопка прерывания в TUI
- **Lookup docs**: встроенный справочник Lua 5.4 (493 секции, офлайн BM25-поиск)
- **Tab completion**: автодополнение slash-команд и моделей Ollama
- **Hot-swap модели**: `/model <name>` переключает модель и выгружает предыдущую из VRAM

## MWS Octapi LowCode

Системный промпт агента содержит знания о runtime MWS Octapi:

- Переменные хранятся в `wf.vars.*` и `wf.initVariables.*`
- Массивы создаются через `_utils.array.new()`
- Код — inline Lua-сниппеты с `return` (не standalone файлы)
- Без JsonPath — прямое обращение к данным: `wf.vars.RESTbody.result[1].ID`

## Конфигурация

Все параметры переопределяются через переменные окружения:

| Переменная | По умолчанию |
|---|---|
| `LLM_URL` | `http://localhost:11434/api/chat` |
| `LLM_MODEL` | `qwen3:4b-thinking-2507-q8_0` |
| `LLM_TEMPERATURE` | `0.6` |
| `LLM_MAX_TOKENS` | `256` |
| `LLM_CTX_SIZE` | `4096` |
| `MAX_ITERATIONS` | `10` |
| `THINKING_MODE` | `auto` (`on`, `off`) |
| `LUA_BINARY` | авто: `lua5.4` -> `lua54` -> `lua` |
| `LUACHECK_BINARY` | авто: `luacheck` |
| `MEMORY_ENABLED` | `0` (переключается через `/memory --on`) |
| `MEMORY_PINNED_RECENT` | `6` |
| `MEMORY_TOP_K` | `8` |
| `MEMORY_EMBEDDINGS` | `1` |
| `MEMORY_EMBEDDING_MODEL` | `nomic-embed-text` |
| `MEMORY_EMBEDDING_URL` | `http://localhost:11434/api/embed` |
| `MEMORY_HYBRID_ALPHA` | `0.5` |

## Лицензия

Apache 2.0
