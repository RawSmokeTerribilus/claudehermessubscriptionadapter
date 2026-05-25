"""
Claude CLI Subscription Adapter
================================
Exposes a local Anthropic-compatible HTTP API that routes every request through
`claude -p` (Claude Code CLI).  Point Hermes (or any Anthropic-SDK client) at
http://127.0.0.1:8082 and it will work with your Claude Pro/Max subscription
without burning overage credits.

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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

app = FastAPI(title="Claude CLI Subscription Adapter")

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
        stderr_text = stderr_bytes.decode(errors="replace").strip()
        raise HTTPException(status_code=502, detail=f"claude CLI error: {stderr_text}")

    try:
        result = json.loads(stdout_bytes.decode(errors="replace").strip())
    except json.JSONDecodeError:
        # Fallback: treat raw stdout as plain text
        return stdout_bytes.decode(errors="replace").strip(), 0, 0

    if result.get("is_error"):
        raise HTTPException(status_code=502, detail=f"claude CLI error: {result.get('result', 'unknown error')}")

    text_output = result.get("result", "")
    usage = result.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    return text_output, input_tokens, output_tokens


# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


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
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages: list[dict] = body.get("messages", [])
    system_raw = body.get("system", "")
    tools: list[dict] = body.get("tools", [])
    model: str = body.get("model", "claude-opus-4-7")
    stream: bool = body.get("stream", False)

    system_text = _extract_system_text(system_raw)
    full_system = _build_system_prompt(system_text, tools)
    prompt = _messages_to_prompt(messages)

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
