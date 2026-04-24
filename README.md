# ICEQ

Lua Code Agent powered by a local LLM. Designed for MWS Octapi scripting workflows.

ICEQ takes a natural language task, generates Lua 5.4 code, validates it through luacheck and lua54, and iterates until the code passes. Everything runs locally via Ollama.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com/) running locally
- A Qwen3 4B model (or any Ollama-compatible model):
  ```
  ollama pull qwen3:4b-thinking-2507-q8_0
  ```
- `lua54` and `luacheck` binaries on PATH (or in the project root)

## Installation

```bash
git clone https://github.com/phantom2059/localscript-agent.git
cd localscript-agent
pip install -e .
```

## Usage

```bash
mkdir my_project && cd my_project
iceq
```

Inside the TUI:

```
> Write a request validator that checks required fields and returns structured errors
> Create src/utils.lua with string helpers and src/app.lua that uses them
> /model ICEQ-2507-thinking
> /help
```

### Slash commands

| Command    | Description              |
|------------|--------------------------|
| `/help`    | Show all commands        |
| `/model`   | Show/switch LLM model    |
| `/status`  | Session stats            |
| `/history` | Task history             |
| `/cost`    | Token usage              |
| `/copy`    | Copy last output         |
| `/undo`    | Undo last file change    |
| `/compact` | Compress context         |
| `/resume`  | Resume a saved chat      |
| `/clear`   | Clear context            |
| `/exit`    | Exit ICEQ                |

## Architecture

```
User prompt
    |
    v
[1. LLM_INFERENCE] --- streaming via Ollama API (thinking + content)
    |
    v
[2. PARSE_AND_ROUTE] --- extract JSON tool call from model output
    |
    v
[3. EXECUTE] --- dispatch to one of 8 tools
    |
    v
[4. EVALUATE] --- auto-sandbox: luacheck + lua54
    |                |
    | (pass)         | (fail)
    v                v
[5. EXIT]      [REPAIR LOOP -> back to 1]
```

### Tools

| Tool              | Description                                    |
|-------------------|------------------------------------------------|
| `write_file`      | Create or overwrite a file                     |
| `patch_file`      | Edit specific line ranges                      |
| `read_file`       | Read file contents                             |
| `list_files`      | List directory                                 |
| `run_sandbox`     | Run luacheck + lua54 manually                  |
| `install_package` | Install via luarocks (whitelist)                |
| `message`         | Send a text reply to the user                  |
| `complete_task`   | Signal task completion                         |

### Key features

- **Auto-sandbox**: every `write_file`/`patch_file` for `.lua` triggers luacheck + lua54 automatically
- **Warnings vs errors**: luacheck W-codes (warnings) don't trigger repair loops, only E-codes and runtime errors do
- **Auto path fix**: if the model writes to `solution.lua` instead of the requested path, it auto-renames
- **Require hints**: on `module not found` errors, the agent suggests correct `require()` paths
- **Streaming TUI**: real-time thinking display, collapsible blocks, diff view, spinner with token count
- **Tab completion**: slash commands and Ollama model names autocomplete with ghost text
- **Model hot-swap**: `/model <name>` switches LLM and unloads the previous one from VRAM

## Configuration

All settings are overridable via environment variables:

| Variable          | Default                                          |
|-------------------|--------------------------------------------------|
| `LLM_URL`         | `http://localhost:11434/v1/chat/completions`     |
| `LLM_MODEL`       | `qwen3:4b-thinking-2507-q8_0`                   |
| `LLM_TEMPERATURE` | `0.6`                                            |
| `LLM_MAX_TOKENS`  | `32768`                                          |
| `LLM_CTX_SIZE`    | `32768`                                          |
| `MAX_ITERATIONS`  | `10`                                             |
| `LUA_BINARY`      | auto-detected `lua54`                            |
| `LUACHECK_BINARY` | auto-detected `luacheck`                         |

## Benchmarks

9 scenarios across 3 categories: bugfix (A), multifile (B), business logic / Octapi (C).

### Base model: `qwen3:4b-thinking-2507-q8_0`

| Scenario | Result | Iterations | Time    |
|----------|--------|------------|---------|
| A1       | pass   | 2          | 86.5s   |
| A2       | pass   | 2          | 76.3s   |
| A3       | pass   | 3          | 87.5s   |
| B1       | pass   | 7          | 302.3s  |
| B2       | fail   | 10         | 770.2s  |
| B3       | fail   | 10         | 900.2s  |
| C1       | fail   | 5          | 460.6s  |
| C2       | fail   | 10         | 548.6s  |
| C3       | pass   | 9          | 573.1s  |
| **Total**| **5/9 (56%)** | avg 6.4 | avg 422.8s |

### Fine-tuned model: `ICEQ-2507-thinking`

| Scenario | Result | Iterations | Time    |
|----------|--------|------------|---------|
| A1       | fail   | 6          | 352.5s  |
| A2       | pass   | 7          | 365.4s  |
| A3       | pass   | 3          | 45.8s   |
| B1       | fail   | 10         | 400.8s  |
| B2       | pass   | 5          | 354.4s  |
| B3       | pass   | 6          | 286.9s  |
| C1       | pass   | 3          | 186.4s  |
| C2       | pass   | 9          | 469.1s  |
| C3       | pass   | 2          | 116.7s  |
| **Total**| **7/9 (78%)** | avg 5.7 | avg 286.5s |

Fine-tuned model: **+22% success rate**, **32% faster** on average. Excels at multifile and Octapi scenarios.

## License

MIT
