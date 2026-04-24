# CHANGELOG

## v0.4.0 — 05.04 (TUI streaming + UX)
- TUI: real-time streaming — thinking, code, sandbox появляются по мере генерации
- TUI: ThinkingChunk стримит thinking токены в реальном времени (ToggleBlock)
- TUI: SpinnerWidget с live token count и таймером
- TUI: GIL yield после каждого post_message (time.sleep 15ms) — фикс батчинга
- TUI: slash command autocomplete — подсказки при вводе `/`
- TUI: `/resume` восстанавливает визуальную историю чата
- TUI: `/copy` — копирование последнего вывода в буфер обмена
- TUI: multi-line input (1-5 строк, потом скролл)
- TUI: smart scroll — не кидает вниз пока читаешь thinking
- TUI: thinking свёрнут по умолчанию (клик чтобы развернуть)
- TUI: Ctrl+C double-tap (первый раз — подсказка, второй — действие)
- TUI: post_message архитектура с run_id для защиты от stale сообщений
- TUI: refresh(layout=True) после каждого mount

## 05.04 (part 2)
- UI: время выполнения задачи ("Done in 3 iter · 12.4s")
- UI: прогресс-бар итераций при repair [███░░░░░░░] 3/10
- UI: имена файлов жёлтым в wrote/patched/read
- Агент: /history — все задачи за сессию с результатами
- Агент: /chats + /resume — сохранение/загрузка сессий в .iceq/chats/
- Агент: auto-save при любом выходе (Ctrl+C, Ctrl+D, /exit)
- Баннер: "/resume to continue" когда есть сохранённые чаты
- Prompt: JSON format constraint через ollama (format: "json")
- Prompt: запрет non-Lua контента в write_file, запрет эмодзи

## 05.04
- Агент: мультифайловая работа — модель создаёт несколько файлов за одну задачу
- Агент: LUA_PATH настроен для require("src.module") относительно рабочей директории
- Агент: patch_file исправлен для многострочных патчей
- Агент: few-shot примеры в system prompt (write_file + patch_file)
- Агент: /status — итерации, токены, файлы за сессию
- Агент: /undo — откат последнего изменения файла
- Агент: трекинг файлов и undo-стек в context.py
- Модель: переключение на qwen3:4b-thinking-2507-q8_0 (Q8 квантизация, 4.3 GB)
- Агент: нормализация JSON (file->path, implicit write_file без tool ключа)
- Fix: dkjson require работает из любой рабочей директории через package.path prefix

## 04.04
- Модель: Qwen3-4B -> Qwen3-4B-Thinking-2507 (BFCL +9%, LiveCode +7%, контекст 262K)
- Chat template: qwen-2.5-chat -> qwen3-instruct
- Агент: workspace scan при старте (модель видит файлы проекта без list_files)
- Агент: persistent context в REPL (история диалога сохраняется между сообщениями)
- Агент: context compaction — strip thinking, state collapse, log truncation
- Агент: install_package tool (luarocks, whitelist: cjson, luasocket, luafilesystem, lpeg, penlight)
- Агент: dkjson.lua добавлен (чистый Lua JSON, без C-зависимостей)
- Агент: нормализация JSON от модели (method/params, file->path, implicit write_file)
- Агент: авто-обёртка bare Lua-кода в write_file (экономия итераций)
- UI: git-style coloured diff для patch_file
- UI: ~tokens вместо chars в thinking spinner
- UI: Ctrl+C graceful stop, чистый выход без traceback
- UI: подсказки под спиннером (Ctrl+C stop)
- Fix: дублирование ввода (ANSI escape)
- Fix: luacheck/lua54 temp пути заменяются на имя файла
- Fix: unknown tool тихо уходит в контекст, модель исправляется сама
- Fix: num_ctx 32768 для ollama (дефолт 2048 -> 32K)

## 03.04
- Датасет: 4000 синтетических примеров сгенерированы через Claude Code Agent Teams
- HuggingFace датасеты скачаны и очищены (leetcode 1530, stack_lua 37774, raw_pretrain 49691)
- Агент: базовый agent loop (6-state FSM), streaming, luacheck+lua54 sandbox
- UI: Rich TUI с баннером, спиннером, подсветкой кода

## 02.04
- Решение: пишем агент с нуля на Python, не используем фреймворки
- Архитектура: main.py -> agent.py -> llm.py -> tools.py, context.py, ui.py, config.py

## 01.04
- Выбор задачи: LocalScript (MWS Octapi), команда ICEQ
- Регистрация на МТС True Tech Hack 2026
