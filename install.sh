#!/bin/bash
# ctx-vault installer for AI agents (Hermes, Claude Code, etc.)
# Installs ctx-vault and configures it for use with agents

set -e

echo "🚀 Installing ctx-vault..."

# Configuration
REPO="https://github.com/skeehn/ctx.git"
INSTALL_DIR="${HOME}/.ctx-vault"
VAULT_DIR="${HOME}/ai-vault"
CTX_DB="${VAULT_DIR}/vault.db"

# Clone or update repo
if [ -d "$INSTALL_DIR" ]; then
    echo "📦 Updating existing installation..."
    cd "$INSTALL_DIR" && git pull
else
    echo "📦 Cloning ctx-vault..."
    git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# Install Python dependencies
echo "📦 Installing Python dependencies..."
pip3 install -r requirements.txt 2>/dev/null || pip3 install fastapi uvicorn pydantic watchdog sentence-transformers

# Create vault directory
mkdir -p "$VAULT_DIR"

# Initialize database
echo "🗄️  Initializing database..."
python3 indexer.py --vault "$VAULT_DIR" --db "$CTX_DB" --once

# Create systemd/user service for indexer (optional)
if command -v systemctl &> /dev/null; then
    echo "⚙️  Setting up systemd user service..."
    mkdir -p ~/.config/systemd/user
    cat > ~/.config/systemd/user/ctx-indexer.service <<EOF
[Unit]
Description=ctx-vault Indexer
After=network.target

[Service]
Type=simple
ExecStart=$INSTALL_DIR/indexer.py --vault $VAULT_DIR --db $CTX_DB
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now ctx-indexer.service
fi

# Create Hermes integration
HERMES_CONFIG="${HOME}/.hermes/config.yaml"
if [ -f "$HERMES_CONFIG" ]; then
    echo "🔧 Configuring Hermes integration..."
    # Add ctx-vault skill if not present
    if ! grep -q "ctx-vault" "$HERMES_CONFIG"; then
        cat >> "$HERMES_CONFIG" <<EOF

# ctx-vault integration
ctx_vault:
  db_path: "$CTX_DB"
  api_port: 8080
  auto_start: true
EOF
    fi
fi

# Create Claude Code integration
CLAUDE_CONFIG="${HOME}/.config/claude-code/config.json"
if [ -f "$CLAUDE_CONFIG" ]; then
    echo "🔧 Configuring Claude Code integration..."
    # Use jq to update config if available
    if command -v jq &> /dev/null; then
        jq '. + {"ctx_vault": {"db_path": "'$CTX_DB'", "api_port": 8080}}' "$CLAUDE_CONFIG" > "$CLAUDE_CONFIG.tmp" && mv "$CLAUDE_CONFIG.tmp" "$CLAUDE_CONFIG"
    fi
fi

# Create convenience scripts
cat > "${HOME}/.local/bin/ctx-start" <<EOF
#!/bin/bash
export CTX_DB_PATH="$CTX_DB"
export CTX_VAULT_ROOT="$VAULT_DIR"
cd "$INSTALL_DIR"
python3 -m uvicorn api:app --host 127.0.0.1 --port 8080 &
echo "ctx-vault API started on http://127.0.0.1:8080"
EOF
chmod +x "${HOME}/.local/bin/ctx-start"

cat > "${HOME}/.local/bin/ctx-search" <<EOF
#!/bin/bash
curl -s "http://127.0.0.1:8080/search?q=\$1&limit=\${2:-10}" | jq '.'
EOF
chmod +x "${HOME}/.local/bin/ctx-search"

cat > "${HOME}/.local/bin/ctx-index" <<EOF
#!/bin/bash
export CTX_DB_PATH="$CTX_DB"
export CTX_VAULT_ROOT="$VAULT_DIR"
cd "$INSTALL_DIR"
python3 indexer.py --vault "$VAULT_DIR" --db "$CTX_DB" --once
EOF
chmod +x "${HOME}/.local/bin/ctx-index"

echo ""
echo "✅ ctx-vault installed successfully!"
echo ""
echo "📁 Vault location: $VAULT_DIR"
echo "🗄️  Database: $CTX_DB"
echo "📦 Installation: $INSTALL_DIR"
echo ""
echo "🚀 Quick commands:"
echo "  ctx-start    # Start the API server"
echo "  ctx-search \"query\" [limit]  # Search vault"
echo "  ctx-index    # Re-index vault"
echo ""
echo "🔧 Agent Integration:"
echo "  - Hermes: Added to ~/.hermes/config.yaml"
echo "  - Claude Code: Added to ~/.config/claude-code/config.json"
echo ""
echo "📝 Add notes to $VAULT_DIR/*.ctx and they'll be auto-indexed!"