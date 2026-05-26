# Claude CLI Subscription Adapter (Hardened)

Routes Hermes (or any Anthropic-SDK client) through the **Claude Code CLI** so
you can use your **Pro / Max subscription** without triggering per-token overage
charges.

```
Hermes ──► localhost:8082 (this adapter) ──► claude -p (CLI) ──► Anthropic
```

The adapter speaks the Anthropic Messages API, so no changes to Hermes source
are needed — you only change one config value.

**HARDENED VERSION:** Input validation, rate limiting, error sanitization, regex DoS protection.

---

## Why this exists

Anthropic now routes OAuth subscription tokens through the overage/extra-usage
bucket when tools are present in the request.  The Claude Code CLI manages
quota allocation separately; requests routed through `claude -p` are billed
against your subscription as normal.
See [hermes-agent#29125](https://github.com/NousResearch/hermes-agent/issues/29125).

---

## Prerequisites

* Python 3.10+
* Claude Code CLI installed and authenticated (`claude` is on your `$PATH`,
  `claude -p "hi"` returns a response)
* Hermes Agent 0.14+

---

## Setup

### Automated (recommended)

```bash
git clone https://github.com/RawSmokeTerribilus/claudehermessubscriptionadapter
cd claudehermessubscriptionadapter

# One-command setup: venv + deps + systemd service + Hermes config
bash install.sh
```

This:
- Creates `~/.hermes/proxy/` with isolated venv
- Installs dependencies
- Sets up `hermes-claude-proxy.service` (user-level systemd)
- Configures Hermes to use the adapter
- Starts the service

The proxy will auto-start on login.

### Manual setup

```bash
git clone https://github.com/RawSmokeTerribilus/claudehermessubscriptionadapter
cd claudehermessubscriptionadapter

# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the adapter
python server.py            # listens on 127.0.0.1:8082 by default

# 3. Point Hermes at it (see Configure section below)
```

---

## Configure Hermes to use the adapter

### Option A — environment variable (simplest)

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8082
export ANTHROPIC_API_KEY=dummy   # any non-empty string; the adapter ignores it
hermes
```

### Option B — Hermes `config.yaml`

Open `~/.hermes/config.yaml` (or wherever your config lives) and add / update
the Anthropic provider block:

```yaml
providers:
  anthropic:
    base_url: http://127.0.0.1:8082
    api_key: dummy          # required by the SDK, value is ignored by the adapter
    model: claude-opus-4-7
```

### Option C — `.env` file next to the adapter

```dotenv
ANTHROPIC_BASE_URL=http://127.0.0.1:8082
ANTHROPIC_API_KEY=dummy
```

---

## How it works

1. Hermes sends a normal `POST /v1/messages` to `localhost:8082`.
2. The adapter converts the message list + system prompt + tool definitions into
   a flat Human/Assistant dialogue.
3. It calls `claude -p <dialogue> --system-prompt <system> --tools ""
   --output-format stream-json --no-session-persistence`.
4. It parses the stream-json output, extracts the assistant text, and looks for
   `<tool_call>{…}</tool_call>` blocks in the response.
5. It rebuilds a valid Anthropic API response (or SSE stream) and returns it to
   Hermes.

### Tool use

The adapter converts Anthropic tool definitions into natural-language
instructions appended to the system prompt and teaches the model to emit tool
calls as `<tool_call>{"name": "…", "input": {…}}</tool_call>` blocks.  These
are parsed back into proper `tool_use` content blocks before the response is
returned.  Multi-turn tool loops work because Hermes sends `tool_result`
messages back, which the adapter serialises into the dialogue context.

---

## Security (Hardened Version)

This fork includes several hardening improvements over the original:

### Input Validation
- **Model allowlist**: Only accepts `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`
- **Size limits**: 
  - System prompt: max 20,000 chars (~5k tokens)
  - Full prompt: max 100,000 chars (~25k tokens)
  - Individual message: max 50,000 chars
  - Message count: max 100
  - Tools: max 100
- **Structure validation**: Enforces message format, role types, content blocks

### Rate Limiting
- Max 30 requests per 60 seconds per client IP
- Returns HTTP 429 if limit exceeded

### Error Sanitization
- Stderr/exception details are NOT returned to client
- Generic error messages only; actual errors logged to `journalctl`

### Regex DoS Protection
- Tool call parsing regex limited to 10,000 char matches
- Catastrophic backtracking mitigated

### Access Control
- Bound to `127.0.0.1:8082` by default (localhost only)
- systemd service runs as your user (no privilege escalation)

---

## Limitations

* **No streaming from the CLI** — the adapter waits for the full CLI response,
  then fake-streams it in small chunks.  The user sees text appearing
  progressively, but there is no token-by-token latency improvement.
* **Built-in Claude Code tools are disabled** (`--tools ""`).  Only tools
  Hermes defines are available, via prompt engineering.
* **Token counts** are real when the CLI reports them; otherwise they are 0.
  Hermes should still function correctly — it does not require accurate counts.

---

## Running as a background service

### macOS (launchd)

```xml
<!-- ~/Library/LaunchAgents/com.claude.subscription-adapter.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude.subscription-adapter</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/claudehermessubscriptionadapter/server.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/claude-adapter.log</string>
  <key>StandardErrorPath</key><string>/tmp/claude-adapter.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.claude.subscription-adapter.plist
```

### Linux (systemd)

```ini
# ~/.config/systemd/user/claude-adapter.service
[Unit]
Description=Claude CLI Subscription Adapter

[Service]
ExecStart=/usr/bin/python3 /path/to/claudehermessubscriptionadapter/server.py
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now claude-adapter
```
