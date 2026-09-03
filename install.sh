#!/usr/bin/env bash
# ==============================================================================
# iSmart-LA (Lite Agent) installer
#
# Interactive setup for a fresh deployment. Designed to be run directly on a
# Debian/Ubuntu VM with a real terminal (SSH is fine, as long as you have a
# real TTY -- the agy OAuth sign-in step needs one).
#
# It asks exactly two things: your Telegram bot token, and your Telegram user ID.
# Nothing else genuinely needs a terminal, so nothing else is asked here.
#
# Everything that needs a browser, a decision, or knowledge of your environment
# happens in Telegram via /start, where the operator already is: signing in to
# Gemini and to Claude, setting the PIN, and saying what this agent looks after.
# Machines it may reach come from /addserver, and what it must never touch from
# /addboundary -- both changeable later without touching the server.
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
  # wkhtmltopdf is here rather than behind a prompt: the brief tells the agent to
  # deliver PDF/JPEG reports, so a missing renderer is a broken feature, not a
  # preference worth interrupting the install for.
  NEEDED_PKGS="python3 python3-venv curl git tmux jq openssh-client wkhtmltopdf"
  MISSING=""
  for p in $NEEDED_PKGS; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
  done
if [ -n "$MISSING" ]; then
    say "Installing missing packages:${MISSING}"
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y $MISSING </dev/null
else
    ok "All required system packages already present."
fi
else
warn "Non-apt system detected. Please make sure these are installed manually:"
warn "  python3 (3.10+), python3-venv, curl, git, tmux, jq, an ssh client"
warn "  wkhtmltopdf (used for PDF/JPEG report delivery)"
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
# Recorded as an ABSOLUTE path throughout: the systemd unit gets a minimal
# PATH that excludes ~/.local/bin, so a bare "claude" never resolves there and
# both Claude tiers would fail to launch.
CLAUDE_BIN_PATH="$HOME/.local/bin/claude"

# Checked by absolute path first, same as agy just below -- NOT `command -v`
# alone. This script's own PATH does not include ~/.local/bin yet (that export
# happens further down), so on a non-interactive re-run against a server that
# already has Claude installed, `command -v claude` doesn't find it and the
# installer runs again for no reason. `command -v` stays as a fallback, for a
# Claude installed somewhere else entirely (a package manager, say) where this
# default path wouldn't apply.
if [ -x "$CLAUDE_BIN_PATH" ] || command -v claude >/dev/null 2>&1; then
    ok "Claude Code CLI already installed ($("$CLAUDE_BIN_PATH" --version 2>/dev/null || claude --version 2>/dev/null || echo 'version unknown'))."
else
    say "Installing Claude Code CLI (native installer)..."
    _cli_tmp="$(mktemp)"
    curl -fsSL https://claude.ai/install.sh -o "$_cli_tmp" \
      && bash "$_cli_tmp" </dev/null \
      || warn "Automatic install failed -- install manually: https://docs.claude.com/en/docs/claude-code"
    rm -f "$_cli_tmp"
fi

if [ -x "$AGY_BIN_PATH" ]; then
    ok "agy already installed ($("$AGY_BIN_PATH" --version 2>/dev/null || echo 'version unknown'))."
else
    say "Installing Antigravity CLI..."
    _cli_tmp="$(mktemp)"
    curl -fsSL https://antigravity.google/cli/install.sh -o "$_cli_tmp"
    bash "$_cli_tmp" </dev/null
    rm -f "$_cli_tmp"
fi
export PATH="$HOME/.local/bin:$PATH"
CLAUDE_BIN_PATH="$(command -v claude || true)"
[ -n "$CLAUDE_BIN_PATH" ] || CLAUDE_BIN_PATH="$HOME/.local/bin/claude"

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
CLAUDE_BIN=${CLAUDE_BIN_PATH}

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
# Deliberately NOT asked here. What this agent looks after is set from /start in
# Telegram (or /setbrief), how it reaches machines comes from /addserver, and what
# it must never touch from /addboundary -- all three where the operator already is,
# and all three changeable later without touching the server.
[ -f "$INSTALL_DIR/SOUL.md" ]   || cp "$INSTALL_DIR/SOUL.md.template"   "$INSTALL_DIR/SOUL.md"
[ -f "$INSTALL_DIR/GEMINI.md" ] || cp "$INSTALL_DIR/GEMINI.md.template" "$INSTALL_DIR/GEMINI.md"
ok "Briefs in place. /start will ask what this agent looks after."

# ------------------------------------------------------------------------------
say "Step 7/8 -- systemd service"
# ------------------------------------------------------------------------------
SERVICE_FILE="/etc/systemd/system/lite-agent.service"
sed \
  -e "s|__INSTALL_USER__|${INSTALL_USER}|g" \
  -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
  -e "s|__INSTALL_HOME__|${HOME}|g" \
  "$INSTALL_DIR/systemd/lite-agent.service.template" | sudo tee "$SERVICE_FILE" >/dev/null
sudo systemctl daemon-reload
ok "systemd unit installed at ${SERVICE_FILE}."

# ------------------------------------------------------------------------------
say "Step 8/8 -- basic hardening"
# ------------------------------------------------------------------------------
# This host is worth more than any single machine it manages: it holds the
# Telegram bot token, the PIN hash, and the SSH keys that reach every managed
# node. So hardening is part of installing, not a page in the docs someone
# gets to later.
#
# The systemd unit written above carries the sandbox half (ProtectSystem,
# MemoryDenyWriteExecute, UMask=0077 and the rest -- each one verified against
# the real workload, see the comments in systemd/lite-agent.service.template).
# What is left is the files already on disk: UMask only governs files created
# from now on, so anything written by an earlier version keeps whatever mode
# it had. On a real deployment that meant sessions.json and spend.jsonl sitting
# at 644 -- every chat's conversation state and token history readable by any
# account on the box.
HARDENED=0
for f in .env pin.json sessions.json sessions.json.bak spend.jsonl          allowed_groups.json servers.json schedules.json snapshots.json          setup_state.json model_overrides.json chat_language.json          gdrive_room_accounts.json gdrive_oauth_client.json \n         mcp_servers.json MEMORY.md OWNER_SCOPE.md; do
  if [ -f "$INSTALL_DIR/$f" ]; then
    chmod 600 "$INSTALL_DIR/$f" 2>/dev/null && HARDENED=$((HARDENED + 1))
  fi
done
[ -d "$INSTALL_DIR/memory" ] && chmod 700 "$INSTALL_DIR/memory" 2>/dev/null
[ -d "$INSTALL_DIR/incoming" ] && chmod 700 "$INSTALL_DIR/incoming" 2>/dev/null
# Credentials the CLIs and rclone keep in $HOME. Never created by this script,
# but a wrong mode here is worth as much to an attacker as the bot's own files.
[ -d "$HOME/.ssh" ] && chmod 700 "$HOME/.ssh" 2>/dev/null
for c in "$HOME/.config/rclone/rclone.conf" "$HOME/.claude.json"; do
  [ -f "$c" ] && chmod 600 "$c" 2>/dev/null
done
ok "Secrets and state locked to owner-only (${HARDENED} file(s) in ${INSTALL_DIR})."
say "  Sandbox: systemd-analyze security lite-agent   (expect ~5.8 MEDIUM, was 9.6 UNSAFE)"
say "  This bot needs NO inbound ports. If this host is reachable from outside,"
say "  a firewall allowing only SSH in is worth the two minutes:"
say "    sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw enable"

echo ""
echo "${BOLD}${GREEN}Done.${RESET} Start it:"
echo ""
echo "  ${CYAN}sudo systemctl enable --now lite-agent${RESET}"
echo "  ${CYAN}journalctl -u lite-agent -f${RESET}   (watch logs)"
echo ""
echo "${BOLD}Then open Telegram and send your bot /start.${RESET} Everything left is there:"
echo "  - sign in to Gemini and to Claude (a URL to open, a code to paste back)"
echo "  - set the 6-digit PIN"
echo "  - say what this agent looks after"
echo ""
echo "After that: ${CYAN}/addserver${RESET} to give it a machine it may reach, and"
echo "${CYAN}/addboundary${RESET} for anything it must never touch. ${CYAN}/help${RESET} lists everything."
