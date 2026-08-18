#!/bin/bash
# Enhanced ctx-vault installer for AI agents (Hermes, Claude Code, etc.)
# Features: Auto-detection, cross-platform, better UX, validation

set -euo pipefail

# Colors for better UX
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Show banner
show_banner() {
    echo -e "${BLUE}"
    echo "╔═════════════════════════════════════════════════════════════════════════════╗"
    echo "║                                                                              ║"
    echo "║                    🚀 CTX-VAULT INSTALLER v2.0 🚀                         ║"
    echo "║              The AI-Optimized Knowledge Vault System                        ║"
    echo "║                                                                              ║"
    echo "╚═════════════════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# Check dependencies
check_dependencies() {
    log_info "Checking system dependencies..."
    
    # Check Python
    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 is required but not found. Please install Python 3.8+"
        exit 1
    fi
    
    PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1-2)
    if (( $(echo "$PYTHON_VERSION < 3.8" | bc -l) )); then
        log_error "Python 3.8+ required, found $PYTHON_VERSION"
        exit 1
    fi
    
    # Check Git
    if ! command -v git &> /dev/null; then
        log_error "Git is required but not found. Please install Git"
        exit 1
    fi
    
    # Check pip
    if ! command -v pip3 &> /dev/null; then
        log_error "pip3 is required but not found. Please install pip3"
        exit 1
    fi
    
    log_success "All dependencies found: Python $PYTHON_VERSION, Git, pip3"
}

# Detect platform and set appropriate paths
detect_platform() {
    log_info "Detecting platform..."
    
    case "$(uname -s)" in
        Darwin*)
            PLATFORM="macos"
            CONFIG_DIR="${HOME}/.config"
            BIN_DIR="${HOME}/.local/bin"
            ;;
        Linux*)
            PLATFORM="linux"
            CONFIG_DIR="${HOME}/.config"
            BIN_DIR="${HOME}/.local/bin"
            ;;
        *)
            log_warning "Unsupported platform: $(uname -s). Using Linux defaults."
            PLATFORM="linux"
            CONFIG_DIR="${HOME}/.config"
            BIN_DIR="${HOME}/.local/bin"
            ;;
    esac
    
    log_success "Platform detected: $PLATFORM"
    log_info "Config dir: $CONFIG_DIR"
    log_info "Bin dir: $BIN_DIR"
}

# Get user preferences with smart defaults
get_user_preferences() {
    log_info "Configuring installation preferences..."
    
    # Installation directory
    read -p "Installation directory [${HOME}/.ctx-vault]: " INSTALL_DIR_INPUT
    INSTALL_DIR="${INSTALL_DIR_INPUT:-${HOME}/.ctx-vault}"
    
    # Vault directory
    read -p "Vault directory [${HOME}/ai-vault]: " VAULT_DIR_INPUT
    VAULT_DIR="${VAULT_DIR_INPUT:-${HOME}/ai-vault}"
    
    # Database file
    CTX_DB="${VAULT_DIR}/vault.db"
    
    # Service installation
    read -p "Install systemd service for auto-start? [y/N]: " INSTALL_SERVICE
    INSTALL_SERVICE="${INSTALL_SERVICE:-N}"
    INSTALL_SERVICE="${INSTALL_SERVICE^^}" # Convert to uppercase
    
    # Agent integrations
    read -p "Configure Hermes integration? [Y/n]: " CONFIG_HERMES
    CONFIG_HERMES="${CONFIG_HERMES:-Y}"
    CONFIG_HERMES="${CONFIG_HERMES^^}"
    
    read -p "Configure Claude Code integration? [Y/n]: " CONFIG_CLAUDE
    CONFIG_CLAUDE="${CONFIG_CLAUDE:-Y}"
    CONFIG_CLAUDE="${CONFIG_CLAUDE^^}"
    
    log_success "Preferences collected"
}

# Show configuration summary
show_summary() {
    echo -e "${BLUE}══════════════════════════════════════════════════════════════════════════════${NC}"
    echo -e "${YELLOW}Installation Summary:${NC}"
    echo -e "  Installation dir: ${INSTALL_DIR}"
    echo -e "  Vault directory:  ${VAULT_DIR}"
    echo -e "  Database file:    ${CTX_DB}"
    echo -e "  Platform:         ${PLATFORM}"
    echo -e "  Systemd service:  ${INSTALL_SERVICE}"
    echo -e "  Hermes config:    ${CONFIG_HERMES}"
    echo -e "  Claude Code cfg:  ${CONFIG_CLAUDE}"
    echo -e "${BLUE}══════════════════════════════════════════════════════════════════════════════${NC}"
    
    read -p "Proceed with installation? [Y/n]: " CONFIRM
    CONFIRM="${CONFIRM:-Y}"
    CONFIRM="${CONFIRM^^}"
    
    if [[ "$CONFIRM" != "Y" ]]; then
        log_info "Installation cancelled by user"
        exit 0
    fi
}

# Install Python dependencies with fallback
install_dependencies() {
    log_info "Installing Python dependencies..."
    
    # Try requirements.txt first
    if pip3 install -r requirements.txt; then
        log_success "Dependencies installed from requirements.txt"
    else
        log_warning "Falling back to manual dependency installation"
        pip3 install fastapi uvicorn[standard] pydantic watchdog sentence-transformers tiktoken
        log_success "Dependencies installed manually"
    fi
}

# Create directory structure
create_directories() {
    log_info "Creating directory structure..."
    
    mkdir -p "${INSTALL_DIR}"
    mkdir -p "${VAULT_DIR}"
    mkdir -p "${HOME}/.local/bin"
    
    # Platform-specific directories
    if [[ "$PLATFORM" == "linux" || "$PLATFORM" == "macos" ]]; then
        mkdir -p "${HOME}/.config/systemd/user"
    fi
    
    log_success "Directories created"
}

# Clone or update repository
setup_repository() {
    log_info "Setting up ctx-vault repository..."
    
    if [ -d "${INSTALL_DIR}" ]; then
        log_info "Updating existing installation..."
        cd "${INSTALL_DIR}"
        git fetch origin
        git reset --hard origin/main
    else
        log_info "Cloning ctx-vault repository..."
        git clone "https://github.com/skeehn/ctx.git" "${INSTALL_DIR}"
    fi
    
    cd "${INSTALL_DIR}"
    log_success "Repository ready at ${INSTALL_DIR}"
}

# Initialize database with progress indication
initialize_database() {
    log_info "Initializing database (this may take a moment)..."
    
    # Show progress spinner while indexer runs
    (
        python3 indexer.py --vault "${VAULT_DIR}" --db "${CTX_DB}" --once &
        INDEXER_PID=$!
        
        # Spinner
        spin='-\|/'
        i=0
        while kill -0 $INDEXER_PID 2>/dev/null; do
            i=$(( (i+1) %4 ))
            printf "\r[%c] Initializing database... " "${spin:$i:1}"
            sleep .1
        done
        
        # Wait for completion and get result
        wait $INDEXER_PID
        INDEXER_RESULT=$?
        
        if [ $INDEXER_RESULT -ne 0 ]; then
            echo -e "\r${RED}[ERROR]${NC} Database initialization failed! "
            exit 1
        fi
        
        echo -e "\r${GREEN}[SUCCESS]${NC} Database initialized successfully!     "
    )
    
    # Verify database
    if [ ! -f "${CTX_DB}" ]; then
        log_error "Database file not created at ${CTX_DB}"
        exit 1
    fi
    
    DB_SIZE=$(du -h "${CTX_DB}" | cut -f1)
    log_info "Database size: ${DB_SIZE}"
}

# Create systemd service (Linux only)
setup_systemd_service() {
    if [[ "$INSTALL_SERVICE" == "Y" && ("$PLATFORM" == "linux" || "$PLATFORM" == "macos") ]]; then
        log_info "Setting up systemd user service..."
        
        mkdir -p "${HOME}/.config/systemd/user"
        
        cat > "${HOME}/.config/systemd/user/ctx-indexer.service" <<EOF
[Unit]
Description=ctx-vault Indexer - Watches for .ctx file changes and updates database
After=network.target

[Service]
Type=simple
ExecStart=${INSTALL_DIR}/indexer.py --vault ${VAULT_DIR} --db ${CTX_DB}
Restart=on-failure
RestartSec=5
Environment=CTX_DB_PATH=${CTX_DB}
Environment=CTX_VAULT_ROOT=${VAULT_DIR}

[Install]
WantedBy=default.target
EOF
        
        # Reload and enable service
        systemctl --user daemon-reload
        systemctl --user enable --now ctx-indexer.service
        
        log_success "Systemd service installed and enabled"
        log_info "To check status: systemctl --user status ctx-indexer.service"
        log_info "To view logs: journalctl --user-unit ctx-indexer.service -f"
    elif [[ "$INSTALL_SERVICE" == "Y" ]]; then
        log_warning "Systemd service only available on Linux/macOS. Skipping on ${PLATFORM}"
    fi
}

# Configure agent integrations
setup_agent_integrations() {
    # Hermes integration
    if [[ "$CONFIG_HERMES" == "Y" ]]; then
        log_info "Configuring Hermes integration..."
        
        HERMES_CONFIG="${HOME}/.hermes/config.yaml"
        mkdir -p "$(dirname "${HERMES_CONFIG}")"
        
        # Check if ctx-vault section already exists
        if grep -q "ctx_vault:" "${HERMES_CONFIG}" 2>/dev/null || true; then
            log_info "Hermes ctx-vault section already exists, updating..."
            # Replace existing section (simplified approach)
            sed -i '/^ctx_vault:/,/^$/d' "${HERMES_CONFIG}"
        fi
        
        # Append new configuration
        cat >> "${HERMES_CONFIG}" <<EOF

# ctx-vault integration
ctx_vault:
  db_path: "${CTX_DB}"
  api_port: 8080
  auto_start: true
EOF
        
        log_success "Hermes integration configured"
    fi
    
    # Claude Code integration
    if [[ "$CONFIG_CLAUDE" == "Y" ]]; then
        log_info "Configuring Claude Code integration..."
        
        CLAUDE_CONFIG_DIR="${HOME}/.config/claude-code"
        CLAUDE_CONFIG="${CLAUDE_CONFIG_DIR}/config.json"
        mkdir -p "${CLAUDE_CONFIG_DIR}"
        
        # Create or update config
        if [ -f "${CLAUDE_CONFIG}" ]; then
            # Use jq if available, otherwise create new
            if command -v jq &> /dev/null; then
                if jq -e '.ctx_vault' "${CLAUDE_CONFIG}" >/dev/null 2>&1; then
                    # Update existing
                    jq --arg db_path "${CTX_DB}" --argjson api_port 8080 \
                       '.ctx_vault.db_path = $db_path | .ctx_vault.api_port = $api_port' \
                       "${CLAUDE_CONFIG}" > "${CLAUDE_CONFIG}.tmp" && mv "${CLAUDE_CONFIG}.tmp" "${CLAUDE_CONFIG}"
                else
                    # Add new section
                    jq --arg db_path "${CTX_DB}" --argjson api_port 8080 \
                       '.ctx_vault = {db_path: $db_path, api_port: $api_port}' \
                       "${CLAUDE_CONFIG}" > "${CLAUDE_CONFIG}.tmp" && mv "${CLAUDE_CONFIG}.tmp" "${CLAUDE_CONFIG}"
                fi
            else
                # Fallback: replace entire file
                cat > "${CLAUDE_CONFIG}" <<EOF
{
  "ctx_vault": {
    "db_path": "${CTX_DB}",
    "api_port": 8080
  }
}
EOF
            fi
        else
            # Create new config file
            cat > "${CLAUDE_CONFIG}" <<EOF
{
  "ctx_vault": {
    "db_path": "${CTX_DB}",
    "api_port": 8080
  }
}
EOF
        fi
        
        log_success "Claude Code integration configured"
    fi
}

# Create convenience scripts
create_convenience_scripts() {
    log_info "Creating convenience scripts..."
    
    # ctx-start - Start API server
    cat > "${HOME}/.local/bin/ctx-start" <<'EOF'
#!/bin/bash
# Start ctx-vault API server

# Auto-detect paths if not set
export CTX_DB_PATH="${CTX_DB_PATH:-${HOME}/ai-vault/vault.db}"
export CTX_VAULT_ROOT="${CTX_VAULT_ROOT:-${HOME}/ai-vault}"

# Get script directory for fallback
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${CTX_DB_PATH}" ] || [ ! -f "${CTX_DB_PATH}" ]; then
    if [ -f "${SCRIPT_DIR}/../vault.db" ]; then
        export CTX_DB_PATH="${SCRIPT_DIR}/../vault.db"
        export CTX_VAULT_ROOT="${SCRIPT_DIR}/.."
    fi
fi

echo "🚀 Starting ctx-vault API server..."
echo "📁 Vault: ${CTX_VAULT_ROOT}"
echo "🗄️  Database: ${CTX_DB_PATH}"
echo "🌐 API will be available at: http://127.0.0.1:8080"
echo "📚 API docs: http://127.0.0.1:8080/docs"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Change to install directory if we can find it
if [ -n "${SCRIPT_DIR}" ] && [ -d "${SCRIPT_DIR}/.." ] && [ -f "${SCRIPT_DIR}/../indexer.py" ]; then
    cd "${SCRIPT_DIR}/.."
fi

# Start the server
exec python3 -m uvicorn api:app --host 127.0.0.1 --port 8080
EOF
    chmod +x "${HOME}/.local/bin/ctx-start"
    
    # ctx-search - Search vault
    cat > "${HOME}/.local/bin/ctx-search" <<'EOF'
#!/bin/bash
# Search ctx-vault

if [ $# -eq 0 ]; then
    echo "Usage: ctx-search \"query\" [limit]"
    echo "Example: ctx-search \"machine learning\" 10"
    exit 1
fi

QUERY="$1"
LIMIT="${2:-10}"

# Try to find the API server
API_URL="http://127.0.0.1:8080/search"

# Try curl first, fallback to wget
if command -v curl &> /dev/null; then
    curl -s "${API_URL}?q=${QUERY}&limit=${LIMIT}" | jq -r '.[] | "\(.title) (\(.path))\n  \(.snippet)\n"' 2>/dev/null || \
    curl -s "${API_URL}?q=${QUERY}&limit=${LIMIT}" | python3 -m json.tool 2>/dev/null || \
    curl -s "${API_URL}?q=${QUERY}&limit=${LIMIT}"
elif command -v wget &> /dev/null; then
    wget -qO- "${API_URL}?q=${QUERY}&limit=${LIMIT}" | jq -r '.[] | "\(.title) (\(.path))\n  \(.snippet)\n"' 2>/dev/null || \
    wget -qO- "${API_URL}?q=${QUERY}&limit=${LIMIT}" | python3 -m json.tool 2>/dev/null || \
    wget -qO- "${API_URL}?q=${QUERY}&limit=${LIMIT}"
else
    echo "Error: Neither curl nor wget found. Please install one of them."
    exit 1
fi
EOF
    chmod +x "${HOME}/.local/bin/ctx-search"
    
    # ctx-index - Reindex vault
    cat > "${HOME}/.local/bin/ctx-index" <<'EOF'
#!/bin/bash
# Reindex ctx-vault vault

# Auto-detect paths
export CTX_DB_PATH="${CTX_DB_PATH:-${HOME}/ai-vault/vault.db}"
export CTX_VAULT_ROOT="${CTX_VAULT_ROOT:-${HOME}/ai-vault}"

# Get script directory for fallback
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${CTX_DB_PATH}" ] || [ ! -f "${CTX_DB_PATH}" ]; then
    if [ -f "${SCRIPT_DIR}/../vault.db" ]; then
        export CTX_DB_PATH="${SCRIPT_DIR}/../vault.db"
        export CTX_VAULT_ROOT="${SCRIPT_DIR}/.."
    fi
fi

echo "🔄 Reindexing ctx-vault vault..."
echo "📁 Vault: ${CTX_VAULT_ROOT}"
echo "🗄️  Database: ${CTX_DB_PATH}"

# Change to install directory if we can find it
if [ -n "${SCRIPT_DIR}" ] && [ -d "${SCRIPT_DIR}/.." ] && [ -f "${SCRIPT_DIR}/../indexer.py" ]; then
    cd "${SCRIPT_DIR}/.."
fi

# Run indexer once
exec python3 indexer.py --vault "${CTX_VAULT_ROOT}" --db "${CTX_DB_PATH}" --once
EOF
    chmod +x "${HOME}/.local/bin/ctx-index"
    
    # ctx-status - Check system status
    cat > "${HOME}/.local/bin/ctx-status" <<'EOF'
#!/bin/bash
# Check ctx-vault system status

echo "📊 ctx-vault System Status"
echo "========================"

# Check if API is running
if curl -s http://127.0.0.1:8080/stats > /dev/null 2>&1; then
    echo -e "🟢 API Server: \e[32mRunning\e[0m"
    curl -s http://127.0.0.1:8080/stats | jq -r 'to_entries[] | "  \(.key): \(.value)"' 2>/dev/null || \
    curl -s http://127.0.0.1:8080/stats
else
    echo -e "🔴 API Server: \e[31mNot running\e[0m"
    echo "   Try: ctx-start"
fi

# Check database
if [ -n "${CTX_DB_PATH:-}" ] && [ -f "${CTX_DB_PATH}" ]; then
    SIZE=$(du -h "${CTX_DB_PATH}" | cut -f1)
    COUNT=$(sqlite3 "${CTX_DB_PATH}" "SELECT COUNT(*) FROM files;" 2>/dev/null || echo "Error")
    echo -e "🗄️  Database: \e[32m${SIZE} (${COUNT} files)\e[0m"
elif [ -f "${HOME}/ai-vault/vault.db" ]; then
    SIZE=$(du -h "${HOME}/ai-vault/vault.db" | cut -f1)
    COUNT=$(sqlite3 "${HOME}/ai-vault/vault.db" "SELECT COUNT(*) FROM files;" 2>/dev/null || echo "Error")
    echo -e "🗄️  Database: \e[32m${SIZE} (${COUNT} files)\e[0m (default location)"
else
    echo -e "🗄️  Database: \e[31mNot found\e[0m"
fi

# Check systemd service (Linux/macOS only)
if command -v systemctl &> /dev/null; then
    if systemctl --user is-active --quiet ctx-indexer.service 2>/dev/null; then
        echo -e "🟡 Indexer Service: \e[32mActive\e[0m"
    else
        echo -e "🟡 Indexer Service: \e[33mInactive\e[0m (try: systemctl --user start ctx-indexer.service)"
    fi
else
    echo -e "🟡 Indexer Service: \e[33mN/A (platform doesn't support systemd)\e[0m"
fi

echo ""
echo "💡 Tips:"
echo "   • Use 'ctx-search \"your query\"' to search the vault"
echo "   • Use 'ctx-index' to reindex after manual file changes"
echo "   • Add .ctx files to your vault directory to auto-index them"
EOF
    chmod +x "${HOME}/.local/bin/ctx-status"
    
    log_success "Convenience scripts created in ${HOME}/.local/bin"
}

# Final verification and summary
final_verification() {
    log_info "Performing final verification..."
    
    # Check key files exist
    if [ ! -f "${INSTALL_DIR}/indexer.py" ]; then
        log_error "Installation incomplete: indexer.py missing"
        exit 1
    fi
    
    if [ ! -f "${INSTALL_DIR}/api.py" ]; then
        log_error "Installation incomplete: api.py missing"
        exit 1
    fi
    
    if [ ! -f "${VAULT_DIR}/vault.db" ]; then
        log_error "Installation incomplete: database not created"
        exit 1
    fi
    
    # Test Python imports
    if ! python3 -c "import fastapi, uvicorn, pydantic, watchdog, sentence_transformers, tiktoken" 2>/dev/null; then
        log_warning "Some Python dependencies may not be installed correctly"
    else
        log_success "Python dependencies verified"
    fi
    
    # Test basic functionality
    if [ -f "${INSTALL_DIR}/indexer.py" ]; then
        log_success "Installation verified successfully!"
    fi
}

# Main installation process
main() {
    show_banner
    check_dependencies
    detect_platform
    get_user_preferences
    show_summary
    
    # Installation steps
    install_dependencies
    create_directories
    setup_repository
    initialize_database
    setup_systemd_service
    setup_agent_integrations
    create_convenience_scripts
    final_verification
    
    # Final message
    echo -e "${GREEN}"
    echo "╔═════════════════════════════════════════════════════════════════════════════╗"
    echo "║                                                                              ║"
    echo "║                    🎉 INSTALLATION COMPLETE! 🎉                           ║"
    echo "║                                                                              ║"
    echo "║  Your ctx-vault is now ready to use!                                       ║"
    echo "║                                                                              ║"
    echo "║  Next steps:                                                               ║"
    echo "║  1. Add your .ctx files to: ${VAULT_DIR}        ║"
    echo "║  2. Start the API with: ctx-start                                       ║"
    echo "║  3. Search with: ctx-search \"your query\"                              ║"
    echo "║  4. Check status with: ctx-status                                     ║"
    echo "║                                                                              ║"
    echo "║  For agent integration:                                                    ║"
    echo "║  • Hermes: Check ~/.hermes/config.yaml                                   ║"
    echo "║  • Claude Code: Check ~/.config/claude-code/config.json                 ║"
    echo "║                                                                              ║"
    echo "║  Documentation: https://github.com/skeehn/ctx                            ║"
    echo "║                                                                              ║"
    echo "╚═════════════════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# Run installation
main "$@"