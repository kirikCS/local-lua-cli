# CHANGELOG

## v0.7.0 — 12.04 (MWS Octapi + бенчмарк)

- **API-сервер** (`iceq-server`): новый пайплайн — FastAPI эндпоинт `POST /generate` для однократной генерации Lua-кода
- **MWS Octapi domain knowledge**: системный промпт сервера и агента обновлён — `wf.vars.*`, `wf.initVariables.*`, `_utils.array.new()`, inline-сниппеты с `return`
- **Бенчмарк-параметры**: `num_predict=256`, `num_ctx=4096` — дефолты config.py и Modelfile приведены к требованиям конкурса
- **README на русском**: полная документация с двумя пайплайнами (CLI-агент + API-сервер)

## v0.6.3 — 12.04 (UX-фиксы)

- **Кнопка остановки**: кликабельный `■` в TUI рядом с полем ввода, прерывает текущую операцию модели
- **Hover**: только центральный символ подсвечивается белым при наведении
- **/memory --on баг**: `call_from_thread` вместо `AgentBlock` — результат больше не теряется при быстром вводе следующего промпта
- **SQLite cross-thread**: `check_same_thread=False` — фикс ошибки при включении памяти из TUI worker thread
- **memory_enable rollback**: при ошибке создания БД флаг сбрасывается обратно в False
- **/compact для малых контекстов**: 1-4 сообщения теперь компактируются (раньше были no-op), уже сжатое единственное сообщение → «Нечего сжимать»

## v0.6.2 — 12.04 (Inline self-tests + run_lua)

- **Inline self-tests**: модель пишет тесты прямо в файле через `if arg then assert(...) end` — проверка корректности без отдельных файлов
- **run_lua tool**: выполнение произвольного Lua-сниппета с optional stdin и timeout (для ручного тестирования по запросу пользователя)
- **Repair loop hint**: при ошибке sandbox модель получает подсказку использовать `lookup_docs` перед попыткой исправления
- **Промпт**: полная секция `WHEN TO USE EACH TOOL` с описанием каждого из 11 инструментов и правилами экономии токенов

## v0.6.1 — 12.04 (4K контекст + планирование)

- **Авто-compact**: перед каждым `build_messages()` проверяется порог (~5916 chars при 4K окне), при превышении — автоматическое сжатие (quick-mode, без LLM)
- **Планирование задач**: в режиме мышления сложные задачи декомпозируются на подзадачи через отдельный LLM-вызов
- **task_tracker.md**: файл-артефакт с планом, статусами `[x]`/`[!]`/`[ ]` и саммари каждой подзадачи
- **Изолированный контекст подзадач**: `reset_for_subtask()` — итерации сбрасываются, память сохраняется
- **on_plan callback**: REPL показывает план и спрашивает подтверждение, TUI показывает и выполняет
- **/plan**: новая slash-команда для просмотра текущего плана
- **SUBTASK_MAX_ITERATIONS=5**: отдельный лимит итераций для подзадач
- **LLM_CTX_SIZE=4096**: дефолт изменён с 32768 на 4096

## v0.6.0 — 07.04 (macOS)

- **macOS-only ветка**: удалены `lua54.exe`, `luacheck.exe` и весь Windows-conditional код
- **Homebrew авто-детект**: `_find_lua()` пробует `lua5.4` → `lua54` → `lua` в PATH
- **Очистка**: убрана нормализация `\\` → `/` в путях (мёртвый код на macOS)
- **Удалён `import msvcrt`**, Windows ANSI escape setup, `sys.stdout.reconfigure`

## v0.5.6 — 07.04 (Dead code cleanup)

- **Удалён `tools.send_message`** (3 строки, никогда не вызывался)
- **Удалён `tools.run_sandbox`** (string-версия, ~30 строк — используется только `run_sandbox_full`)
- **Удалён `Context.update_code`** + поля `current_code`/`current_file` (никогда не читались)
- **Удалён неиспользуемый импорт `Columns`** из ui.py
- **Дедупликация констант**: `THINKING_PHRASES` и `SPINNER_FRAMES` теперь определены в ui.py, tui.py импортирует
- **Фикс drift в tui.py**: `SLASH_COMMANDS` dict, `/help` и `_handle_slash` синхронизированы (было 3 разных списка)
- **Фикс tool list**: ошибка парсинга JSON теперь перечисляет все 10 инструментов

## v0.5.5 — 07.04 (Turn-level /undo)

- **/undo переработан**: откат целого промпта (все файлы + контекст + память) за одно нажатие
- **Context.turns**: список границ промптов с метаданными для атомарного отката
- **Memory.delete_from**: удаление строк по session_id + min_id
- **_ingest возвращает row_id**: для привязки к turn boundary
- **/clear**: теперь также очищает turns и undo_stack

## v0.5.4 — 07.04 (FTS noise cleanup)

- **content_indexed column**: JSON tool-call bodies стрипятся из FTS5 индекса — BM25 больше не матчит код внутри `write_file`
- **Схема v3**: миграция v2→v3 с DROP+RECREATE FTS5, бэкфилл content_indexed
- **_strip_index_noise**: извлекает только tool name + path + query + summary из JSON

## v0.5.3 — 07.04 (Phase 2 memory — embeddings)

- **Гибридный поиск**: BM25 кандидаты → embed query → cosine rerank → α·bm25 + (1-α)·cosine
- **Ollama /api/embed**: nomic-embed-text, pre-normalized unit vectors, float32 BLOB storage
- **Backfill**: автоматическое встраивание существующих строк при `/memory --on`
- **Graceful degradation**: если модель эмбеддингов недоступна — тихий откат к BM25-only
- **Схема v2**: миграция v1→v2, embedding BLOB column

## v0.5.2 — 07.04 (Phase 1 memory — SQLite+FTS5)

- **Долговременная память**: SQLite + FTS5 BM25, per-project `.iceq/memory.sqlite`
- **/memory --on/--off/clear**: управление памятью из slash-команд
- **build_messages() composer**: system + retrieved memory + last 6 pinned
- **Backfill on enable**: текущий буфер итераций сразу записывается в БД
- **MEMORY_ENABLED=0** по умолчанию, переключается в runtime

## v0.5.1 — 07.04 (Structured /compact + Lua docs)

- **/compact**: структурированное саммари (Tasks, Files, Errors, State) вместо free-form
- **lookup_docs tool**: офлайн BM25-поиск по справочнику Lua 5.4 (493 секции)
- **Title boost**: заголовки секций удвоены в индексе для точного ранкинга API-функций
- **llm.generate**: новый параметр `response_format` для управления JSON/plain text

## v0.5.0 — 06.04 (Auto-thinking)

- **Difficulty classifier**: LightGBM на 4000 примерах, экспортирован как чистый Python
- **should_think(prompt)**: авто-решение включать ли `<think>` блок
- **/think, /no_think, /auto_think**: ручное переключение режима мышления
- **Ollama native API**: переключение с `/v1/chat/completions` на `/api/chat` для контроля `think` параметра
- **Modelfile fix**: `<think>` блок теперь условный через `$.IsThinkSet`

## v0.4.4 — 05.04 (Slash autocomplete + sandbox)

- Slash autocomplete с ghost text подсказками
- Warnings-only sandbox: W-коды luacheck не блокируют repair loop
- /model hot-swap с выгрузкой предыдущей модели из VRAM

## v0.4.0 — 05.04 (TUI streaming + UX)

- TUI: real-time streaming thinking/code/sandbox
- TUI: ThinkingChunk стримит thinking токены (ToggleBlock)
- TUI: SpinnerWidget с live token count и таймером
- TUI: slash command autocomplete
- TUI: /resume восстанавливает визуальную историю
- TUI: /copy — копирование в буфер обмена
- TUI: multi-line input, smart scroll, thinking свёрнут по умолчанию
- TUI: Ctrl+C double-tap, post_message архитектура с run_id

## 05.04 (часть 2)

- Время выполнения задачи, прогресс итераций при repair
- /history — все задачи за сессию с результатами
- /chats + /resume — сохранение/загрузка сессий в `.iceq/chats/`
- Auto-save при любом выходе
- JSON format constraint через Ollama

## 05.04

- Мультифайловая работа, LUA_PATH для require, patch_file fix
- Few-shot примеры, /status, /undo, трекинг файлов
- Переключение на qwen3:4b-thinking-2507-q8_0 (Q8, 4.3 GB)

## 04.04

- Qwen3-4B → Qwen3-4B-Thinking-2507, chat template qwen3-instruct
- Workspace scan, persistent context, context compaction
- install_package, dkjson.lua, нормализация JSON, авто-обёртка bare Lua
- Git-style coloured diff, Ctrl+C graceful stop

## 03.04

- Датасет: 4000 синтетических примеров (Claude Code Agent Teams)
- HuggingFace датасеты (leetcode, stack_lua, raw_pretrain)
- Базовый agent loop (6-state FSM), streaming, luacheck+lua54 sandbox

## 02.04

- Архитектура с нуля на Python: main → agent → llm → tools, context, ui, config

## 01.04

- Выбор задачи: LocalScript (MWS Octapi), команда ICEQ
- Регистрация на МТС True Tech Hack 2026
