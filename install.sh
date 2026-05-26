#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_HOME="$HOME/.hermes/proxy"

echo "Installing Claude CLI Subscription Adapter..."

# 1. Create proxy home and copy files
echo "[1/5] Setting up proxy directory..."
mkdir -p "$PROXY_HOME"
cp "$SCRIPT_DIR/server.py" "$PROXY_HOME/"
cp "$SCRIPT_DIR/requirements.txt" "$PROXY_HOME/"

# 2. Create virtual environment
echo "[2/5] Creating virtual environment..."
python3 -m venv "$PROXY_HOME/venv"
"$PROXY_HOME/venv/bin/pip" install --upgrade pip setuptools wheel
"$PROXY_HOME/venv/bin/pip" install -r "$PROXY_HOME/requirements.txt"

# 3. Install systemd service
echo "[3/5] Installing systemd service..."
mkdir -p "$HOME/.config/systemd/user"
ESCAPED_HOME=$(echo "$HOME" | sed 's/[&/\]/\\&/g')
sed "s|%h|$ESCAPED_HOME|g" "$SCRIPT_DIR/hermes-claude-proxy.service" \
    > "$HOME/.config/systemd/user/hermes-claude-proxy.service"
systemctl --user daemon-reload
systemctl --user enable hermes-claude-proxy

# 4. Configure Hermes
echo "[4/5] Configuring Hermes..."
if ! grep -q "anthropic_base_url" ~/.hermes/config.yaml 2>/dev/null; then
    echo "" >> ~/.hermes/config.yaml
    echo "# Claude CLI Subscription Adapter proxy" >> ~/.hermes/config.yaml
    echo "anthropic_base_url: http://127.0.0.1:8082" >> ~/.hermes/config.yaml
    echo "Hermes config updated (anthropic_base_url = http://127.0.0.1:8082)"
else
    echo "Hermes already configured. Verify config:"
    grep "anthropic_base_url" ~/.hermes/config.yaml || echo "anthropic_base_url not found"
fi

# 5. Start the service
echo "[5/5] Starting service..."
systemctl --user restart hermes-claude-proxy
sleep 2

# Verify
if systemctl --user is-active --quiet hermes-claude-proxy; then
    echo ""
    echo "✓ Installation complete!"
    echo ""
    echo "Service status:"
    systemctl --user status hermes-claude-proxy --no-pager
    echo ""
    echo "Usage:"
    echo "  - Start:   systemctl --user start hermes-claude-proxy"
    echo "  - Stop:    systemctl --user stop hermes-claude-proxy"
    echo "  - Logs:    journalctl --user -u hermes-claude-proxy -f"
    echo "  - Health:  curl http://127.0.0.1:8082/health"
else
    echo ""
    echo "✗ Service failed to start!"
    echo "Check logs: journalctl --user -u hermes-claude-proxy -f"
    exit 1
fi
