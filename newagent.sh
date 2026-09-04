#!/usr/bin/env bash
# ==============================================================================
# iSmart-LA -- provision an ADDITIONAL deployment on this host
#
# One host can run several agents that answer as genuinely different things --
# an infrastructure one holding SSH keys to production, a development one that
# never should. `install.sh` sets up ONE deployment and says so itself:
#
#     "For most setups, create a dedicated non-root user first and re-run as
#      that user."
#
# This automates exactly that, and nothing more. It is deliberately a script the
# operator runs in a shell, NOT a Telegram command: creating a new agent means
# creating a Linux user with shell access and its own credentials, which is a
# strictly larger capability than anything the bot gates behind a PIN today. A
# decision that big belongs where a human already has root, not in a chat.
#
# Why a separate Linux user, and not just a second directory: these are
# $HOME-relative with no per-install override, so two deployments under one user
# still share them --
#
#   ~/.ssh/agent_active               /unlock in one opens write mode for BOTH
#   ~/.ssh/config                     the /addserver block, rewritten by whoever ran last
#   ~/.config/rclone/rclone.conf      connected Drive accounts pooled
#   ~/.gemini, ~/.claude*             the CLI logins -- so two different
#                                     subscription accounts are impossible
#   agy's MCP registry                `agy mcp add` is global to the user
#
# Usage:
#   sudo ./newagent.sh <name> [--repo URL] [--branch NAME] [--no-install]
#
# Example:
#   sudo ./newagent.sh ops        # -> user isla-ops, service lite-agent-ops
#   sudo ./newagent.sh build
# ==============================================================================
set -euo pipefail

BOLD="$(tput bold 2>/dev/null || true)"
DIM="$(tput dim 2>/dev/null || true)"
RESET="$(tput sgr0 2>/dev/null || true)"
GREEN="$(tput setaf 2 2>/dev/null || true)"
YELLOW="$(tput setaf 3 2>/dev/null || true)"
RED="$(tput setaf 1 2>/dev/null || true)"
CYAN="$(tput setaf 6 2>/dev/null || true)"

say()  { echo -e "${CYAN}==>${RESET} $*"; }
warn() { echo -e "${YELLOW}!!${RESET} $*"; }
ok()   { echo -e "${GREEN}ok${RESET} $*"; }
die()  { echo -e "${RED}xx${RESET} $*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAME=""
REPO=""
BRANCH="master"
RUN_INSTALL=1
RESUME=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)      REPO="${2:-}"; shift 2 ;;
    --branch)    BRANCH="${2:-}"; shift 2 ;;
    --no-install) RUN_INSTALL=0; shift ;;
    --resume)    RESUME=1; shift ;;
    -h|--help)   sed -n '2,36p' "$0"; exit 0 ;;
    -*)          die "unknown option: $1" ;;
    *)           [ -n "$NAME" ] && die "give exactly one name"; NAME="$1"; shift ;;
  esac
done

[ -n "$NAME" ] || die "usage: sudo $0 <name> [--repo URL] [--branch NAME] [--no-install]"

# Validated BEFORE the root check, deliberately: a mistyped name should say so
# straight away rather than sending someone to find sudo only to be turned away
# for a different reason once they have it.
#
# Validated rather than quoted, because the name becomes a Linux username, a
# directory and a systemd unit name at once -- anything outside this set has no
# business reaching useradd or a unit path however carefully it is escaped.
case "$NAME" in
  *[!a-z0-9-]*|-*) die "name must be lowercase letters, digits and dashes, not starting with a dash (got: ${NAME})" ;;
esac
[ ${#NAME} -le 20 ] || die "name is too long (max 20 chars, got ${#NAME})"

[ "$(id -u)" -eq 0 ] || die "must run as root -- it creates a user and a systemd unit"

USER_NAME="isla-${NAME}"
SERVICE_NAME="lite-agent-${NAME}"
HOME_DIR="/home/${USER_NAME}"
INSTALL_DIR="${HOME_DIR}/lite-agent"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
SUDOERS_RUNTIME="/etc/sudoers.d/ismart-${NAME}"
SUDOERS_INSTALL="/etc/sudoers.d/ismart-${NAME}-install"

echo ""
echo "${BOLD}iSmart-LA -- new deployment${RESET}"
echo "  name:      ${NAME}"
echo "  user:      ${USER_NAME}"
echo "  directory: ${INSTALL_DIR}"
echo "  service:   ${SERVICE_NAME}"
echo ""

# --- Existing state: refuse, or resume ---------------------------------------
# Never clobber: an existing deployment holds a PIN hash, sessions, SSH keys and
# connected Drive accounts. Re-running this over one would destroy all of it,
# and "provision" is not a word anyone expects to mean that.
#
# --resume exists because install.sh can fail halfway (its Claude Code and agy
# steps download from the internet), and the first version of this script then
# said "left in place so you can retry" while the guard above refused exactly
# that retry. Found on the first real Linux run. Resuming re-runs install.sh
# against the user and checkout that are already there; it still never touches
# an existing UNIT, since reaching that step means the install got far enough
# to have written one.
if [ "$RESUME" -eq 1 ]; then
  id -u "$USER_NAME" >/dev/null 2>&1 || die "--resume, but user ${USER_NAME} does not exist -- run without it to provision from scratch"
  [ -d "$INSTALL_DIR" ] || die "--resume, but ${INSTALL_DIR} does not exist -- run without it to provision from scratch"
  [ "$RUN_INSTALL" -eq 1 ] || die "--resume with --no-install would do nothing"
  say "Resuming: ${USER_NAME} and ${INSTALL_DIR} are already in place"
else
  id -u "$USER_NAME" >/dev/null 2>&1 && die "user ${USER_NAME} already exists -- use --resume to re-run the install against it, or pick another name"
  [ -e "$INSTALL_DIR" ] && die "${INSTALL_DIR} already exists -- use --resume to re-run the install against it, or pick another name"
  [ -e "$UNIT_PATH" ]   && die "${UNIT_PATH} already exists -- refusing to overwrite it"
fi

if [ "$RESUME" -eq 0 ]; then
  if [ -z "$REPO" ]; then
    REPO="$(git -C "$HERE" remote get-url origin 2>/dev/null || true)"
    [ -n "$REPO" ] || die "could not read this checkout's origin -- pass --repo <url>"
    say "Cloning the same repository this checkout came from:"
    echo "    ${DIM}${REPO}${RESET}"
  fi

  command -v git >/dev/null 2>&1 || die "git is not installed"

  # --- 1. the user -----------------------------------------------------------
  say "Creating ${USER_NAME}"
  # A real login shell, not /usr/sbin/nologin: the Gemini and Claude sign-ins run
  # through tmux in a terminal as this user, and /update's git work happens here
  # too. --system would skip creating the home directory the whole design is
  # built around.
  useradd --create-home --home-dir "$HOME_DIR" --shell /bin/bash "$USER_NAME"
  chmod 750 "$HOME_DIR"
  ok "user created, home at ${HOME_DIR} (750)"

  # --- 2. the checkout -------------------------------------------------------
  say "Cloning ${BRANCH}"
  sudo -u "$USER_NAME" git clone --branch "$BRANCH" "$REPO" "$INSTALL_DIR" \
    || die "clone failed"
  ok "cloned into ${INSTALL_DIR}"
fi

# --- 3. sudo rights ----------------------------------------------------------
# Two rules, and the difference between them is the whole security argument of
# this script.
#
# RUNTIME (permanent, narrow): /update ends with
#     sudo -n systemctl restart <SERVICE_NAME>
# and without that the bot updates itself and never comes back. So exactly that
# one command is granted, for exactly this unit.
#
# NOT granted at runtime, deliberately: refresh_systemd_unit() also wants
#     sudo -n cp <tmp> /etc/systemd/system/<SERVICE_NAME>.service
# Granting it would let this service user rewrite its own unit -- including
# `User=root` -- and take the host on the next restart. That would make every
# deployment on this box root-equivalent and turn the per-user isolation above
# into decoration. The bot already treats that write as best-effort: it logs
# "could not write the unit (needs sudo)" and carries on, and does NOT restart,
# so nothing loops. Refreshing the unit after a release that changes the
# template is an operator action -- re-run this script's install step, or
# install.sh, as root.
say "Granting sudo rights"
cat > "$SUDOERS_RUNTIME" <<EOF
# iSmart-LA ${NAME}: /update restarts its own service and nothing else.
# Deliberately NOT granting writes to ${UNIT_PATH} -- that would let this
# user rewrite its own unit as root. See newagent.sh.
${USER_NAME} ALL=(root) NOPASSWD: /usr/bin/systemctl restart ${SERVICE_NAME}, /bin/systemctl restart ${SERVICE_NAME}
EOF
chmod 440 "$SUDOERS_RUNTIME"
visudo -cf "$SUDOERS_RUNTIME" >/dev/null || { rm -f "$SUDOERS_RUNTIME"; die "generated sudoers rule is invalid"; }
ok "runtime rule: restart ${SERVICE_NAME} only"

# INSTALL-TIME (temporary): install.sh needs apt, and it writes the unit itself.
# Removed on EVERY exit path by the trap below -- including Ctrl-C and a failed
# install -- so the broad rights cannot outlive the provisioning that needed
# them. That is the only reason granting them at all is defensible.
cleanup() {
  if [ -e "$SUDOERS_INSTALL" ]; then
    rm -f "$SUDOERS_INSTALL"
    ok "temporary install-time sudo rights removed"
  fi
}
trap cleanup EXIT INT TERM

if [ "$RUN_INSTALL" -eq 1 ]; then
  cat > "$SUDOERS_INSTALL" <<EOF
# TEMPORARY -- removed by newagent.sh on exit. Present only while install.sh runs.
${USER_NAME} ALL=(root) NOPASSWD: ALL
EOF
  chmod 440 "$SUDOERS_INSTALL"
  visudo -cf "$SUDOERS_INSTALL" >/dev/null || die "generated install sudoers rule is invalid"
  warn "granted BROAD sudo to ${USER_NAME} for the install only -- removed when this script exits"

  echo ""
  say "Running install.sh as ${USER_NAME} (it will ask for a bot token and your Telegram user ID)"
  warn "Use a DIFFERENT bot token than any other deployment here: one token polled"
  warn "by two processes gets both rejected by Telegram with 409 Conflict."
  echo ""
  sudo -u "$USER_NAME" env "SERVICE_NAME=${SERVICE_NAME}" \
    bash -c "cd '${INSTALL_DIR}' && ./install.sh" \
    || die "install.sh failed. ${USER_NAME} and ${INSTALL_DIR} were left in place -- re-run the install against them with:
    sudo $0 ${NAME} --resume"
fi

# --- done --------------------------------------------------------------------
echo ""
echo "${BOLD}${GREEN}Provisioned.${RESET}"
echo ""
if [ "$RUN_INSTALL" -eq 1 ]; then
  echo "Start it:"
  echo "  ${CYAN}systemctl enable --now ${SERVICE_NAME}${RESET}"
  echo "  ${CYAN}journalctl -u ${SERVICE_NAME} -f${RESET}"
else
  echo "Finish the install yourself:"
  echo "  ${CYAN}sudo -u ${USER_NAME} env SERVICE_NAME=${SERVICE_NAME} bash -c 'cd ${INSTALL_DIR} && ./install.sh'${RESET}"
fi
echo ""
echo "${BOLD}This deployment is isolated from the others by its Linux user.${RESET}"
echo "  - its own ~/.ssh, so /unlock here never opens write mode anywhere else"
echo "  - its own CLI logins, so it can use different Claude/Gemini accounts"
echo "  - its own rclone config, briefs, memory, PIN and token ledger"
echo ""
echo "${DIM}Give it only the machines it needs: put them in ${HOME_DIR}/.ssh/config${RESET}"
echo "${DIM}(or via /addserver). A development agent should not be able to reach${RESET}"
echo "${DIM}production, and that is enforced by what is absent from this user's config.${RESET}"
echo ""
