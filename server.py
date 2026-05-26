"""
Claude CLI Subscription Adapter
================================
Exposes a local Anthropic-compatible HTTP API that routes every request through
`claude -p` (Claude Code CLI).  Point Hermes (or any Anthropic-SDK client) at
http://127.0.0.1:8082 and it will work with your Claude Pro/Max subscription
without burning overage credits.

HARDENED VERSION: input validation, error sanitization, rate limiting, size caps.

Usage:
    python server.py [--port 8082] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from typing import AsyncGenerator, Optional
from collections import defaultdict
from time import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

app = FastAPI(title="Claude CLI Subscription Adapter")

# ---------------------------------------------------------------------------
# Validation & security constants
# ---------------------------------------------------------------------------

ALLOWED_MODELS = {
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
}

MAX_PROMPT_CHARS = 100_000      # ~25k tokens
MAX_SYSTEM_CHARS = 20_000       # ~5k tokens
MAX_TOOL_COUNT = 100
MAX_MESSAGES_COUNT = 100
MAX_MESSAGE_CONTENT_CHARS = 50_000

RATE_LIMIT_REQUESTS = 30        # requests
RATE_LIMIT_WINDOW = 60          # seconds
RATE_LIMIT_STORAGE = defaultdict(lambda: [])  # per-IP: [timestamps]

# Tighten regex to avoid catastrophic backtracking
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.{1,10000}?)\s*</tool_call>", re.DOTALL)

# ---------------------------------------------------------------------------
# Request → CLI helpers
# ---------------------------------------------------------------------------

def _extract_system_text(system) -> str:
    """Accept both a plain string and the list-of-blocks form."""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            block.get("text", "")
            for block in system
            if block.get("type") == "text"
        )
    return ""


def _build_system_prompt(system_text: str, tools: list) -> str:
    parts = []
    if system_text:
        parts.append(system_text)

    if tools:
        descs = []
        for t in tools:
            descs.append(
                f"Tool name: {t['name']}\n"
                f"Description: {t.get('description', '')}\n"
                f"Input schema: {json.dumps(t.get('input_schema', {}))}"
            )
        tool_block = "\n\n".join(descs)
        parts.append(
            "You have access to the following tools. "
            "When you want to call a tool, output ONLY a JSON object wrapped in "
            "<tool_call>…</tool_call> tags, like this:\n\n"
            "<tool_call>\n"
            '{"name": "<tool_name>", "input": {<key>: <value>, ...}}\n'
            "</tool_call>\n\n"
            "Do NOT write anything else on the same line as the tags. "
            "After the tag you may continue your response.\n\n"
            f"Available tools:\n\n{tool_block}"
        )

    return "\n\n".join(parts)


def _messages_to_prompt(messages: list) -> str:
    """
    Flatten the Anthropic messages array into a Human/Assistant dialogue string
    that claude -p can follow.
    """
    lines: list[str] = []

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block["text"])
                elif btype == "tool_use":
                    parts.append(
                        f"<tool_call>\n"
                        f'{json.dumps({"name": block["name"], "input": block["input"]})}\n'
                        f"</tool_call>"
                    )
                elif btype == "tool_result":
                    inner = block.get("content", "")
                    if isinstance(inner, list):
                        inner = " ".join(
                            b.get("text", "") for b in inner if b.get("type") == "text"
                        )
                    parts.append(f"<tool_result id={block.get('tool_use_id', '')}>{inner}</tool_result>")
            text = "\n".join(parts)
        else:
            text = str(content)

        prefix = "Human" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {text}")

    lines.append("Assistant:")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# CLI invocation
# ---------------------------------------------------------------------------

async def _run_claude(
    prompt: str,
    system_prompt: str,
    model: str,
) -> tuple[str, int, int]:
    """
    Call `claude -p` and return (output_text, input_tokens, output_tokens).
    Uses stream-json so we can capture rich metadata; falls back gracefully.
    """
    # Validate model against allowlist
    if model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="Invalid model")

    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--no-session-persistence",
        "--tools", "",          # disable Claude's own built-in tools
    ]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    if model:
        cmd += ["--model", model]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()

    if proc.returncode not in (0, None):
        # Sanitize stderr: don't leak system info, just generic error
        raise HTTPException(status_code=502, detail="Claude CLI execution failed. Check server logs.")

    try:
        result = json.loads(stdout_bytes.decode(errors="replace").strip())
    except json.JSONDecodeError:
        # Fallback: treat raw stdout as plain text
        return stdout_bytes.decode(errors="replace").strip(), 0, 0

    if result.get("is_error"):
        # Sanitize error response
        raise HTTPException(status_code=502, detail="Claude CLI returned an error. Check server logs.")

    text_output = result.get("result", "")
    usage = result.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    return text_output, input_tokens, output_tokens


# ---------------------------------------------------------------------------
# Rate limiting & validation helpers
# ---------------------------------------------------------------------------

def _check_rate_limit(client_ip: str) -> None:
    """Raise HTTPException if client exceeds rate limit."""
    now = time()
    window_start = now - RATE_LIMIT_WINDOW

    timestamps = RATE_LIMIT_STORAGE[client_ip]
    # Keep only recent timestamps
    timestamps[:] = [t for t in timestamps if t > window_start]

    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Max 30 requests per 60 seconds."
        )

    timestamps.append(now)


def _validate_request(body: dict) -> None:
    """Validate request structure and sizes."""
    messages = body.get("messages", [])
    system_raw = body.get("system", "")
    tools = body.get("tools", [])
    model = body.get("model", "claude-opus-4-7")

    # Model allowlist
    if model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model}' not supported. Allowed: {', '.join(ALLOWED_MODELS)}"
        )

    # Message count
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Too many messages. Max {MAX_MESSAGES_COUNT}."
        )

    # Message content validation
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            raise HTTPException(status_code=400, detail=f"Message {i}: invalid structure or role")

        content = msg.get("content", "")
        if isinstance(content, str):
            if len(content) > MAX_MESSAGE_CONTENT_CHARS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Message {i}: content too long. Max {MAX_MESSAGE_CONTENT_CHARS} chars."
                )
        elif isinstance(content, list):
            total = 0
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += len(block.get("text", ""))
            if total > MAX_MESSAGE_CONTENT_CHARS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Message {i}: total content too long."
                )

    # System prompt size
    system_text = _extract_system_text(system_raw)
    if len(system_text) > MAX_SYSTEM_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"System prompt too long. Max {MAX_SYSTEM_CHARS} chars."
        )

    # Tool count
    if not isinstance(tools, list) or len(tools) > MAX_TOOL_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Too many tools. Max {MAX_TOOL_COUNT}."
        )

    for i, tool in enumerate(tools):
        if not isinstance(tool, dict) or "name" not in tool:
            raise HTTPException(status_code=400, detail=f"Tool {i}: missing 'name'")

# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------


def _parse_tool_calls(raw: str) -> tuple[list[dict], str]:
    """
    Extract <tool_call>…</tool_call> blocks from the model output.
    Returns (tool_use_blocks, remaining_text).
    """
    tool_blocks: list[dict] = []
    for match in _TOOL_CALL_RE.finditer(raw):
        try:
            data = json.loads(match.group(1))
            tool_blocks.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": data["name"],
                    "input": data.get("input", {}),
                }
            )
        except (json.JSONDecodeError, KeyError):
            pass

    remaining = _TOOL_CALL_RE.sub("", raw).strip()
    return tool_blocks, remaining


# ---------------------------------------------------------------------------
# Response construction
# ---------------------------------------------------------------------------

def _build_content_blocks(raw_text: str, tools_requested: bool) -> tuple[list[dict], str]:
    """Return (content_blocks, stop_reason)."""
    if not tools_requested:
        return [{"type": "text", "text": raw_text}], "end_turn"

    tool_calls, text = _parse_tool_calls(raw_text)
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(tool_calls)
    stop_reason = "tool_use" if tool_calls else "end_turn"
    return blocks, stop_reason


def _make_response(
    content_blocks: list[dict],
    stop_reason: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> dict:
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


# ---------------------------------------------------------------------------
# SSE streaming helper
# ---------------------------------------------------------------------------

async def _sse_stream(
    content_blocks: list[dict],
    stop_reason: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> AsyncGenerator[str, None]:
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    def _send(event_type: str, data: dict) -> str:
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

    # message_start
    yield _send(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        },
    )

    for i, block in enumerate(content_blocks):
        if block["type"] == "text":
            yield _send(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            text = block["text"]
            chunk_size = 32
            for start in range(0, len(text), chunk_size):
                yield _send(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "text_delta", "text": text[start : start + chunk_size]},
                    },
                )
                await asyncio.sleep(0)  # yield control so the event loop can flush

        elif block["type"] == "tool_use":
            yield _send(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                },
            )
            yield _send(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    },
                },
            )

        yield _send("content_block_stop", {"type": "content_block_stop", "index": i})

    yield _send(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
    )
    yield _send("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def post_messages(request: Request):
    # Rate limit by client IP
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Validate request structure and sizes
    _validate_request(body)

    messages: list[dict] = body.get("messages", [])
    system_raw = body.get("system", "")
    tools: list[dict] = body.get("tools", [])
    model: str = body.get("model", "claude-opus-4-7")
    stream: bool = body.get("stream", False)

    system_text = _extract_system_text(system_raw)
    full_system = _build_system_prompt(system_text, tools)
    prompt = _messages_to_prompt(messages)

    # Cap prompt size
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Prompt too large. Max {MAX_PROMPT_CHARS} chars."
        )

    raw_text, input_tokens, output_tokens = await _run_claude(prompt, full_system, model)

    content_blocks, stop_reason = _build_content_blocks(raw_text, bool(tools))

    if stream:
        return StreamingResponse(
            _sse_stream(content_blocks, stop_reason, model, input_tokens, output_tokens),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return JSONResponse(
        _make_response(content_blocks, stop_reason, model, input_tokens, output_tokens)
    )


@app.get("/v1/models")
async def list_models():
    """Minimal models list so SDK version-checks don't fail."""
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": "claude-opus-4-7", "object": "model"},
                {"id": "claude-sonnet-4-6", "object": "model"},
                {"id": "claude-haiku-4-5-20251001", "object": "model"},
            ],
        }
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Claude CLI Subscription Adapter")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()

    print(f"Starting adapter on http://{args.host}:{args.port}")
    print("Point Hermes at this address by setting ANTHROPIC_BASE_URL or config base_url.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
