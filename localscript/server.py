"""HTTP server for the competition benchmark: POST /generate → {code}.

Single-shot Lua code generation endpoint. Does NOT use the full agent loop
(tool calls, repair iterations) — the organizers send one request with
num_predict=256 and expect raw Lua code back.

Usage:
    python -m localscript.server          # starts on :8080
    LLM_MODEL=qwen3:4b python -m localscript.server --port 9000
"""

import argparse
import re

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from localscript import config
from localscript.llm import generate

app = FastAPI(
    title="LocalScript API",
    version="1.0.0",
)

# System prompt tuned for the MWS Octapi LowCode runtime — the competition
# benchmark tests inline Lua snippets that access wf.vars.*, NOT standalone files.
_SYSTEM_PROMPT = (
    "You are a Lua code generator for MWS Octapi LowCode.\n\n"
    "RUNTIME RULES:\n"
    "- All workflow variables are in wf.vars.* (e.g. wf.vars.emails, wf.vars.RESTbody)\n"
    "- Init-time variables (from workflow start) are in wf.initVariables.*\n"
    "- Return the result with `return`. The code is an inline snippet, NOT a file.\n"
    "- To create a new array: _utils.array.new()\n"
    "- To mark an existing table as array: _utils.array.markAsArray(arr)\n"
    "- No require(), no module wrapping, no io/os calls, no file operations.\n"
    "- Use local variables. Standard Lua 5.4 syntax.\n"
    "- Be EXTREMELY concise — you have only 256 tokens.\n"
    "Output ONLY Lua code. No explanations, no markdown fences."
)

# Regex to strip ```lua ... ``` fences if the model wraps output.
_FENCE_RE = re.compile(r"^```(?:lua)?\s*\n?", re.MULTILINE)
_FENCE_END_RE = re.compile(r"\n?```\s*$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences from model output."""
    text = _FENCE_RE.sub("", text)
    text = _FENCE_END_RE.sub("", text)
    return text.strip()


class GenerateRequest(BaseModel):
    prompt: str


class GenerateResponse(BaseModel):
    code: str


@app.post("/generate", response_model=GenerateResponse)
def generate_code(body: GenerateRequest):
    """Generate Lua code from a natural-language prompt.

    Uses the competition benchmark parameters:
      num_ctx=4096, num_predict=256, think=false
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": body.prompt},
    ]
    try:
        raw, _ = generate(
            messages,
            enable_thinking=False,
            response_format=None,   # raw text, not JSON
            max_tokens=256,         # competition hard constraint
        )
        code = _strip_fences(raw)
        return GenerateResponse(code=code)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)},
        )


def main():
    """Entry point for `iceq-server` or `python -m localscript.server`.

    Defaults to the fine-tuned model (config.SFT_MODEL) because the server
    endpoint runs under the competition's num_predict=256 constraint — the
    thinking model wastes that budget on chain-of-thought preamble, so the
    non-thinking fine-tuned model is the only one that produces usable code
    at this limit. Users can override via --model.
    """
    parser = argparse.ArgumentParser(description="ICEQ benchmark API server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8080, help="Port")
    parser.add_argument(
        "--model", default=config.SFT_MODEL,
        help=f"LLM model tag (default: {config.SFT_MODEL})",
    )
    args = parser.parse_args()

    config.LLM_MODEL = args.model
    print(f"Starting ICEQ API server on {args.host}:{args.port}")
    print(f"Model: {config.LLM_MODEL}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
