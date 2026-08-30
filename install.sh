#!/usr/bin/env bash
# ==============================================================================
# iSmart-LA (Lite Agent) installer
#
# Interactive setup for a fresh deployment. Designed to be run directly on a
# Debian/Ubuntu VM with a real terminal (SSH is fine, as long as you have a
# real TTY -- the agy OAuth sign-in step needs one).
#
# The environment brief (SOUL.md / GEMINI.md) is generated for you: step 6 runs
# bootstrap.py, which asks a few plain-language questions about what this agent
# should look after and writes both briefs from your answers. Nothing about it
# is Proxmox-specific -- describe any environment you like.
#
# Scope is deliberately small: system deps, a venv, both CLIs, the bot token,
# the brief, and a systemd service. Everything needing a browser or a human
# decision -- signing in to Gemini and Claude, setting the PIN -- happens in
# Telegram via /start instead, where the operator already is.
#
# What this script does NOT do for you (by design -- see README):
#   - Provide access to whatever you want this agent to manage. Set up your own
#     SSH key + ~/.ssh/config (or kubeconfig, etc) before or after running this.
#   - Complete OAuth logins for you. It offers the Antigravity sign-in here
#     (URL to open, code to paste back) as a convenience, but skipping it is
#     fine -- /start in Telegram does both provider sign-ins properly.
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
echo "${BOLD}iSmart-LA installer${RESET}  ${DIM}(lightweight Telegram bridge to Claude Code + Antigravity CLI)${RESET}"
echo "Install dir: ${INSTALL_DIR}"
echo "Running as:  ${INSTALL_USER}"
echo ""

if [ "$INSTALL_USER" = "root" ]; then
warn "Running as root. This is fine, but the systemd service will run as root too."
warn "For most setups, create a dedicated non-root user first and re-run as that user."
fi

# ------------------------------------------------------------------------------
say "Step 1/7 -- System dependencies"
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
say "Step 2/7 -- Python virtual environment"
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
say "Step 3/7 -- AI providers"
# ------------------------------------------------------------------------------
AGY_BIN_PATH="$HOME/.local/bin/agy"

if command -v claude >/dev/null 2>&1; then
    ok "Claude Code CLI already installed ($(claude --version 2>/dev/null || echo 'version unknown'))."
else
    say "Installing Claude Code CLI (native installer)..."
    curl -fsSL https://claude.ai/install.sh | bash \
      || warn "Automatic install failed -- install manually: https://docs.claude.com/en/docs/claude-code"
fi

if [ -x "$AGY_BIN_PATH" ]; then
    ok "agy already installed ($("$AGY_BIN_PATH" --version 2>/dev/null || echo 'version unknown'))."
else
    say "Installing Antigravity CLI..."
    curl -fsSL https://antigravity.google/cli/install.sh | bash
fi
export PATH="$HOME/.local/bin:$PATH"

echo ""
say "Signing in to Antigravity."
echo "  You'll get a URL to open, then paste back the code it gives you."
echo "  ${DIM}(This drives agy's own login screen for you, so it doesn't take over"
echo "  the terminal mid-install. Skip it and run tools/agy_login.py later.)${RESET}"
ask "Press Enter to sign in now, or type 's' to skip: " AGY_LOGIN_CHOICE
if [ "${AGY_LOGIN_CHOICE:-}" = "s" ]; then
    warn "Skipped. Run ${CYAN}python3 $INSTALL_DIR/tools/agy_login.py${RESET} before starting the service."
else
    python3 "$INSTALL_DIR/tools/agy_login.py" --agy "$AGY_BIN_PATH" \
      || warn "Sign-in didn't complete. Re-run: python3 $INSTALL_DIR/tools/agy_login.py"
fi

mkdir -p "$HOME/.gemini/antigravity-cli"
  AGY_SETTINGS="$HOME/.gemini/antigravity-cli/settings.json"
say "Writing agy permissions (broad allow, same trust level as Claude Code's Bash tool;"
say "plus a deny-list for outright destructive local commands -- see README)."
python3 - "$AGY_SETTINGS" <<'PYEOF'
import json, sys
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}
perms = cfg.setdefault("permissions", {})
perms["allow"] = sorted(set(perms.get("allow", [])) | {
    "command(*)", "read_file(*)", "write_file(*)"
})
# Bare command names only: agy silently ignores wildcard deny patterns
# (command(rm *) let rm through; command(rm) blocked it -- established by
# testing, not assumed). These protect THIS host; anything reaching a managed
# node goes out through ssh and is governed by the node-side guard instead.
perms["deny"] = sorted(set(perms.get("deny", [])) | {
    "command(rm)", "command(dd)", "command(mkfs)",
    "command(shutdown)", "command(reboot)", "command(halt)", "command(poweroff)",
})
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"Updated {path}")
PYEOF

# ------------------------------------------------------------------------------
say "Step 4/7 -- Telegram bot"
# ------------------------------------------------------------------------------
echo "Create a bot with @BotFather on Telegram if you haven't already (/newbot)."
ask "Telegram bot token: " TELEGRAM_BOT_TOKEN_INPUT
echo "To find your own numeric Telegram user ID: message @userinfobot, or start this"
echo "bot after install and send it /chatid (works even before you're authorized)."
ask "Your Telegram user ID (admin -- required, this account can grant group access): " ADMIN_ID_INPUT

# ------------------------------------------------------------------------------
say "Step 5/7 -- Write .env"
# ------------------------------------------------------------------------------
ENV_FILE="$INSTALL_DIR/.env"
cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN_INPUT}
ALLOWED_USER_IDS=${ADMIN_ID_INPUT}
ALLOWED_GROUP_IDS=

AGY_BIN=${AGY_BIN_PATH}

# Everything else has a working default in lite_agent.py -- the models, the
# 4-tier fallback chain, the allowed tools. Set them here ONLY to override.
# See .env.example for the full list with explanations.
#
# Both CLIs sign in to their own subscription directly (that's what /start
# does), so no gateway is needed. To route Claude through one anyway, set
# BOTH of these -- leaving either unset keeps the direct sign-in, and the
# variables are actively stripped from the CLI's environment so an inherited
# shell value can't silently redirect traffic:
# ANTHROPIC_BASE_URL=
# ANTHROPIC_API_KEY=
EOF
chmod 600 "$ENV_FILE"
ok ".env written and locked down (chmod 600)."

# ------------------------------------------------------------------------------
say "Step 6/7 -- Environment brief (SOUL.md / GEMINI.md)"
# ------------------------------------------------------------------------------
# This is the one genuinely per-deployment part of the whole system: what the
# agent is looking after, how it reaches it, and what it must never touch.
# bootstrap.py asks a handful of plain-language questions and writes both briefs,
# so a new server needs no hand-authored config. It is NOT Proxmox-specific --
# describe whatever you want the agent to look after.
if [ -f "$INSTALL_DIR/SOUL.md" ] && [ -f "$INSTALL_DIR/GEMINI.md" ]; then
ok "SOUL.md and GEMINI.md already present -- leaving them alone."
echo "     (Re-run ${CYAN}python3 bootstrap.py${RESET} any time to regenerate them.)"
else
echo "The agent needs to be told what it's looking after. The next few questions"
echo "generate that brief for you -- plain sentences are fine."
echo ""
if "$INSTALL_DIR/venv/bin/python3" "$INSTALL_DIR/bootstrap.py"; then
    ok "Environment brief written."
else
    warn "Bootstrap skipped or cancelled -- falling back to blank templates."
    [ -f "$INSTALL_DIR/SOUL.md" ] || cp "$INSTALL_DIR/SOUL.md.template" "$INSTALL_DIR/SOUL.md"
    [ -f "$INSTALL_DIR/GEMINI.md" ] || cp "$INSTALL_DIR/GEMINI.md.template" "$INSTALL_DIR/GEMINI.md"
    warn "You must fill in SOUL.md and GEMINI.md by hand before the bot is useful."
fi
fi

# ------------------------------------------------------------------------------
say "Step 7/7 -- systemd service"
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
echo "  1. ${BOLD}Review SOUL.md / GEMINI.md${RESET} -- especially the HARD BOUNDARIES section."
echo "     Only the part below the LEARNED_ZONE marker is ever written automatically;"
echo "     everything above it is yours. See examples/proxmox/ for a worked reference."
echo "  2. ${BOLD}Set up access${RESET} from this machine to whatever you want the agent to"
echo "     manage (SSH key + ~/.ssh/config, kubeconfig, etc), matching what you"
echo "     described during the brief step."
echo "  3. If you skipped the Antigravity sign-in above, run:"
echo "     ${CYAN}python3 ${INSTALL_DIR}/tools/agy_login.py${RESET}"
echo ""
echo "Then start it:"
echo "  ${CYAN}sudo systemctl enable --now lite-agent${RESET}"
echo "  ${CYAN}journalctl -u lite-agent -f${RESET}   (watch logs)"
echo ""
echo "Once running, message your bot /help on Telegram for the full command list."
