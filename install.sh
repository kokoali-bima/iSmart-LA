#!/usr/bin/env bash
# ==============================================================================
# Ismart-LA (Lite Agent) installer
#
# Interactive setup for a fresh deployment. Designed to be run directly on a
# Debian/Ubuntu VM with a real terminal (SSH is fine, as long as you have a
# real TTY -- the agy/9Router OAuth login steps need one).
#
# What this script does NOT do for you (by design -- see README):
#   - Fill in SOUL.md / GEMINI.md with your actual infrastructure details.
#     It copies the templates and stops there; you edit the content.
#   - Provide SSH access to whatever you want this agent to manage. Set up
#     your own SSH key + ~/.ssh/config for that before or after running this.
#   - Complete OAuth logins for you. It runs `agy` and (if installing 9Router
#     fresh) tells you to open its dashboard -- both need a human to click
#     through a real login flow.
# ==============================================================================
set -euo pipefail

BOLD="$(tput bold 2>/dev/null || true)"
DIM="$(tput dim 2>/dev/null || true)"
RESET="$(tput sgr0 2>/dev/null || true)"
GREEN="$(tput setaf 2 2>/dev/null || true)"
YELLOW="$(tput setaf 3 2>/dev/null || true)"
CYAN="$(tput setaf 6 2>/dev/null || true)"

say()  { echo -e "${CYAN}==>${RESET} $*"; }
warn() { echo -e "${YELLOW}!!${RESET} $*"; }
ok()   { echo -e "${GREEN}ok${RESET} $*"; }
ask()  { read -rp "$(echo -e "${BOLD}$1${RESET}")" "$2"; }

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_USER="$(whoami)"

echo ""
echo "${BOLD}Ismart-LA installer${RESET}  ${DIM}(lightweight Telegram bridge to Claude Code + Antigravity CLI)${RESET}"
echo "Install dir: ${INSTALL_DIR}"
echo "Running as:  ${INSTALL_USER}"
echo ""

if [ "$INSTALL_USER" = "root" ]; then
  warn "Running as root. This is fine, but the systemd service will run as root too."
  warn "For most setups, create a dedicated non-root user first and re-run as that user."
fi

# ------------------------------------------------------------------------------
say "Step 1/8 -- System dependencies"
# ------------------------------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
  NEEDED_PKGS="python3 python3-venv curl git tmux jq openssh-client"
  MISSING=""
  for p in $NEEDED_PKGS; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
  done
  if [ -n "$MISSING" ]; then
    say "Installing missing packages:${MISSING}"
    sudo apt-get update -y
    sudo apt-get install -y $MISSING
  else
    ok "All required system packages already present."
  fi
  if ! command -v wkhtmltopdf >/dev/null 2>&1; then
    ask "Install wkhtmltopdf too? Needed only if you want PDF (not just HTML) reports. [y/N] " INSTALL_PDF
    if [[ "${INSTALL_PDF:-}" =~ ^[Yy]$ ]]; then
      sudo apt-get install -y wkhtmltopdf
    fi
  fi
else
  warn "Non-apt system detected. Please make sure these are installed manually:"
  warn "  python3 (3.10+), python3-venv, curl, git, tmux, jq, an ssh client"
  warn "  (optional) wkhtmltopdf, if you want PDF report generation"
  ask "Press Enter once you've confirmed these are installed... " _dummy
fi

# ------------------------------------------------------------------------------
say "Step 2/8 -- Python virtual environment"
# ------------------------------------------------------------------------------
if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
  ok "Created venv at $INSTALL_DIR/venv"
else
  ok "venv already exists, reusing it."
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "Python dependencies installed."

# ------------------------------------------------------------------------------
say "Step 3/8 -- Claude Code CLI"
# ------------------------------------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code CLI already installed ($(claude --version 2>/dev/null || echo 'version unknown'))."
else
  say "Installing Claude Code CLI (native installer)..."
  curl -fsSL https://claude.ai/install.sh | bash || warn "Automatic install failed -- install manually: https://docs.claude.com/en/docs/claude-code"
fi

# ------------------------------------------------------------------------------
say "Step 4/8 -- Antigravity CLI (agy) + login"
# ------------------------------------------------------------------------------
AGY_BIN_PATH="$HOME/.local/bin/agy"
if [ -x "$AGY_BIN_PATH" ]; then
  ok "agy already installed ($("$AGY_BIN_PATH" --version 2>/dev/null || echo 'version unknown'))."
else
  say "Installing Antigravity CLI..."
  curl -fsSL https://antigravity.google/cli/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
export PATH="$HOME/.local/bin:$PATH"

echo ""
warn "agy needs an interactive OAuth login on a Google AI Pro / Ultra account."
warn "This will launch a full-screen terminal UI. If you're in a plain SSH session"
warn "without a real TTY (e.g. driven by automation), run this in tmux/screen instead."
ask "Press Enter to launch 'agy' and complete login now, or Ctrl+C to skip and do it later... " _dummy
"$AGY_BIN_PATH" || warn "agy exited -- if login wasn't completed, run '$AGY_BIN_PATH' again manually before starting the service."

mkdir -p "$HOME/.gemini/antigravity-cli"
AGY_SETTINGS="$HOME/.gemini/antigravity-cli/settings.json"
say "Writing agy permissions config (allow command/read_file/write_file broadly -- same"
say "trust level as Claude Code's unrestricted Bash tool; see README for why)."
python3 - "$AGY_SETTINGS" <<'PYEOF'
import json, sys
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}
cfg.setdefault("permissions", {})
cfg["permissions"]["allow"] = sorted(set(cfg["permissions"].get("allow", [])) | {
    "command(*)", "read_file(*)", "write_file(*)"
})
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"Updated {path}")
PYEOF

# ------------------------------------------------------------------------------
say "Step 5/8 -- 9Router (Claude side gateway)"
# ------------------------------------------------------------------------------
ask "Do you already have a 9Router instance running that this agent should use? [y/N] " HAVE_9ROUTER
if [[ "${HAVE_9ROUTER:-}" =~ ^[Yy]$ ]]; then
  ask "9Router base URL (e.g. http://127.0.0.1:20128): " ANTHROPIC_BASE_URL_INPUT
  ask "9Router API key: " ANTHROPIC_API_KEY_INPUT
  echo ""
  ok "Using existing 9Router at ${ANTHROPIC_BASE_URL_INPUT}."
  warn "Make sure Claude Code is logged in on 9Router's own dashboard (Providers -> Claude"
  warn "Code -> OAuth) -- this installer cannot do that step for you."
else
  say "Installing 9Router locally..."
  if ! command -v node >/dev/null 2>&1; then
    warn "Node.js not found. 9Router requires it -- installing via NodeSource (LTS)..."
    curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
    sudo apt-get install -y nodejs
  fi
  sudo npm install -g 9router
  say "Starting 9Router (pm2 recommended for persistence across reboots)..."
  if ! command -v pm2 >/dev/null 2>&1; then
    sudo npm install -g pm2
  fi
  pm2 start 9router --name 9router -- --skip-update || pm2 restart 9router
  pm2 save
  echo ""
  warn "9Router is now running, but it still needs manual setup via its own dashboard:"
  warn "  1. Open the 9Router dashboard (default: http://<this-server-ip>:20128)"
  warn "  2. Providers -> add & log in to Claude Code (OAuth) and Antigravity (OAuth)"
  warn "  3. Note the API key 9Router gives you for its native /v1/messages endpoint"
  ANTHROPIC_BASE_URL_INPUT="http://127.0.0.1:20128"
  ask "Paste the 9Router API key once you've completed the dashboard setup above: " ANTHROPIC_API_KEY_INPUT
fi

# ------------------------------------------------------------------------------
say "Step 6/8 -- Telegram bot"
# ------------------------------------------------------------------------------
echo "Create a bot with @BotFather on Telegram if you haven't already (/newbot)."
ask "Telegram bot token: " TELEGRAM_BOT_TOKEN_INPUT
echo "To find your own numeric Telegram user ID: message @userinfobot, or start this"
echo "bot after install and send it /chatid (works even before you're authorized)."
ask "Your Telegram user ID (admin -- required, this account can grant group access): " ADMIN_ID_INPUT

# ------------------------------------------------------------------------------
say "Step 7/8 -- Write .env"
# ------------------------------------------------------------------------------
ENV_FILE="$INSTALL_DIR/.env"
cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN_INPUT}
ALLOWED_USER_IDS=${ADMIN_ID_INPUT}
ALLOWED_GROUP_IDS=

ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL_INPUT}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY_INPUT}
CLAUDE_MODEL_PRIMARY=cc/claude-haiku-4-5-20251001
CLAUDE_MODEL_FALLBACK=cc/claude-sonnet-5

AGY_BIN=${AGY_BIN_PATH}
AGY_MODEL_PRIMARY=gemini-3.7-flash-medium
AGY_MODEL_FALLBACK=gemini-3.1-pro-low
EOF
chmod 600 "$ENV_FILE"
ok ".env written and locked down (chmod 600)."

if [ ! -f "$INSTALL_DIR/SOUL.md" ]; then
  cp "$INSTALL_DIR/SOUL.md.template" "$INSTALL_DIR/SOUL.md"
  ok "Created SOUL.md from template -- ${BOLD}you must edit this before the bot is useful${RESET}."
fi
if [ ! -f "$INSTALL_DIR/GEMINI.md" ]; then
  cp "$INSTALL_DIR/GEMINI.md.template" "$INSTALL_DIR/GEMINI.md"
  ok "Created GEMINI.md from template -- ${BOLD}you must edit this before the bot is useful${RESET}."
fi

# ------------------------------------------------------------------------------
say "Step 8/8 -- systemd service"
# ------------------------------------------------------------------------------
SERVICE_FILE="/etc/systemd/system/lite-agent.service"
sed \
  -e "s|__INSTALL_USER__|${INSTALL_USER}|g" \
  -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
  "$INSTALL_DIR/systemd/lite-agent.service.template" | sudo tee "$SERVICE_FILE" >/dev/null
sudo systemctl daemon-reload
ok "systemd unit installed at ${SERVICE_FILE}."

echo ""
echo "${BOLD}${GREEN}Setup mostly done.${RESET} Before starting the service:"
echo ""
echo "  1. ${BOLD}Edit SOUL.md and GEMINI.md${RESET} -- fill in your actual node/target IPs,"
echo "     hard boundaries, and anything else specific to what you're managing."
echo "     See examples/proxmox/ for a filled-in reference."
echo "  2. ${BOLD}Set up SSH access${RESET} from this machine to whatever you want the agent"
echo "     to manage (key + ~/.ssh/config), matching what you wrote in step 1."
echo "  3. If you skipped agy login above, run: ${AGY_BIN_PATH}"
echo ""
echo "Then start it:"
echo "  ${CYAN}sudo systemctl enable --now lite-agent${RESET}"
echo "  ${CYAN}journalctl -u lite-agent -f${RESET}   (watch logs)"
echo ""
echo "Once running, message your bot /help on Telegram for the full command list."
