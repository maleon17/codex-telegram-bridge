#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found"; }
ask_required() {
    local variable="$1" prompt="$2" secret="${3:-false}" value="${!1:-}"
    if [[ -z "$value" ]]; then
        [[ -t 0 ]] || die "$variable is required when setup.sh is run without a terminal"
        if "$secret"; then read -r -s -p "$prompt" value; echo
        else read -r -p "$prompt" value; fi
    fi
    printf -v "$variable" '%s' "$value"
}
ask_default() {
    local variable="$1" prompt="$2" fallback="$3" value="${!1:-}"
    if [[ -z "$value" && -t 0 ]]; then read -r -p "$prompt" value; fi
    printf -v "$variable" '%s' "${value:-$fallback}"
}
normalise_api_url() {
    local value="$1"
    while [[ "$value" == */ ]]; do value="${value%/}"; done
    [[ -z "$value" || "$value" =~ ^https?://[^/]+(:[0-9]+)?$ ]] || die "TELEGRAM_API_URL must be an HTTP(S) base URL without a path"
    printf '%s' "$value"
}
is_local_api_url() {
    local authority="${1#*://}" host
    authority="${authority%%/*}"
    host="${authority%%:*}"
    [[ "${host,,}" != "api.telegram.org" ]]
}
check_telegram_token() {
    local api_base="$1"
    TELEGRAM_TOKEN_TO_CHECK="$BOT_TOKEN" TELEGRAM_API_BASE_TO_CHECK="$api_base" python3 - <<'PY'
import json, os, urllib.request
base = os.environ["TELEGRAM_API_BASE_TO_CHECK"] or "https://api.telegram.org"
token = os.environ["TELEGRAM_TOKEN_TO_CHECK"]
try:
    with urllib.request.urlopen(f"{base}/bot{token}/getMe", timeout=15) as response:
        result = json.load(response)
except Exception as exc:
    raise SystemExit(f"Telegram token check failed: {exc}")
if not result.get("ok"):
    raise SystemExit("Telegram rejected this bot token")
print("Telegram bot token: valid (@%s)" % result["result"].get("username", "unknown"))
PY
}
REINSTALL=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --reinstall) REINSTALL=true ;;
        -h|--help) echo "Usage: ./setup.sh [--reinstall]"; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
    shift
done

if [[ -e .env ]]; then
    if ! "$REINSTALL"; then
        die "installation already exists (.env). Rerun with --reinstall; it will overwrite .env, whitelist permissions and account-directory permissions."
    fi
    echo "Reinstall requested: .env will be overwritten; whitelist.txt and accounts/ permissions will be reset."
fi

echo "== Codex Telegram Bot setup =="
need python3
need systemctl

CODEX_BIN="$(command -v codex || true)"
if [[ -z "$CODEX_BIN" && -x "$HOME/.local/bin/codex" ]]; then
    CODEX_BIN="$HOME/.local/bin/codex"
fi
[[ -n "$CODEX_BIN" ]] || die "Codex CLI not found. Install it, then rerun setup."
echo "Codex: $($CODEX_BIN --version)"

if ! "$CODEX_BIN" login status >/dev/null 2>&1; then
    echo "The owner's Codex account is not logged in."
    echo "Run: codex login"
    exit 1
fi
echo "Owner Codex account: authenticated"

EXISTING_API_URL=""
if [[ -f .env ]]; then
    EXISTING_API_URL="$(sed -n 's/^TELEGRAM_API_URL=//p' .env | tail -n 1)"
fi
ask_required BOT_TOKEN "Telegram bot token (from @BotFather): " true
[[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || die "Bot token format is invalid"
ask_required OWNER_ID "Owner Telegram numeric ID: "
[[ "$OWNER_ID" =~ ^[0-9]+$ ]] || die "OWNER_ID must be numeric"

ask_default CODEX_CWD "Default Codex workspace [$HOME]: " "$HOME"
[[ -d "$CODEX_CWD" ]] || die "Workspace does not exist: $CODEX_CWD"
CODEX_CWD="$(cd "$CODEX_CWD" && pwd)"

ask_default CODEX_SANDBOX "Default sandbox (read-only/workspace-write/danger-full-access) [danger-full-access]: " danger-full-access
case "$CODEX_SANDBOX" in
    read-only|workspace-write|danger-full-access) ;;
    *) die "Unknown sandbox: $CODEX_SANDBOX" ;;
esac

ask_default SERVICE_NAME "systemd service name [codex-telegram-bot]: " codex-telegram-bot
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "Invalid service name"

INSTALL_USER="$(id -un)"
INSTALL_DIR="$SCRIPT_DIR"
UNIT_DESTINATION="/etc/systemd/system/${SERVICE_NAME}.service"
if [[ -f "$UNIT_DESTINATION" ]]; then
    INSTALLED_DIRECTORY="$(sed -n 's/^WorkingDirectory=//p' "$UNIT_DESTINATION" | tail -n 1)"
    if [[ "$INSTALLED_DIRECTORY" != "$INSTALL_DIR" ]]; then
        die "$UNIT_DESTINATION belongs to WorkingDirectory '$INSTALLED_DIRECTORY', not '$INSTALL_DIR'. Choose another service name."
    fi
fi

# A local URL supplied by the caller can't be used before this bot has been
# logged out of the cloud.  A URL persisted in .env is safe to use: it records
# a completed earlier switch.
EXISTING_API_URL="$(normalise_api_url "$EXISTING_API_URL")"
REQUESTED_API_URL="$(normalise_api_url "${TELEGRAM_API_URL:-}")"
TELEGRAM_API_URL="$EXISTING_API_URL"
check_telegram_token "$TELEGRAM_API_URL"
LOCAL_SWITCH_REQUIRED=false
if [[ -z "$EXISTING_API_URL" ]]; then
    TELEGRAM_API_URL="$REQUESTED_API_URL"
fi
if [[ -n "$TELEGRAM_API_URL" && -z "$EXISTING_API_URL" ]] && is_local_api_url "$TELEGRAM_API_URL"; then
    LOCAL_SWITCH_REQUIRED=true
fi

if [[ -z "$TELEGRAM_API_URL" ]]; then
    LOCAL_SETUP="${LOCAL_BOT_API_SETUP:-}"
    if [[ -z "$LOCAL_SETUP" && -t 0 ]]; then
        read -r -p "Local Bot API server (files up to 2 GB; otherwise 20 MB)? api_id/api_hash are available at my.telegram.org. Leave empty to skip [y/N]: " LOCAL_SETUP
    fi
    if [[ "$LOCAL_SETUP" =~ ^[Yy]([Ee][Ss])?$|^1$ ]]; then
        INSTALLER="$SCRIPT_DIR/scripts/install-local-bot-api.sh"
        [[ -x "$INSTALLER" ]] || die "Local Bot API installer is missing or not executable: $INSTALLER"
        if [[ ! -t 0 && "${LOCAL_BOT_API_LOGOUT_CONFIRM:-}" != "yes" ]]; then
            die "LOCAL_BOT_API_LOGOUT_CONFIRM=yes is required without a terminal before the irreversible cloud logOut step"
        fi
        INSTALL_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/local-bot-api-install.XXXXXX")"
        set +e
        if [[ ! -t 0 ]]; then
            "$INSTALLER" --yes | tee "$INSTALL_OUTPUT_FILE"
        else
            "$INSTALLER" | tee "$INSTALL_OUTPUT_FILE"
        fi
        INSTALL_PIPE_STATUS=("${PIPESTATUS[@]}")
        set -e
        if [[ "${INSTALL_PIPE_STATUS[0]}" -ne 0 || "${INSTALL_PIPE_STATUS[1]}" -ne 0 ]]; then
            rm -f "$INSTALL_OUTPUT_FILE"
            die "Local Bot API installer failed"
        fi
        TELEGRAM_API_URL="$(sed -n 's/^LOCAL_BOT_API_URL=//p' "$INSTALL_OUTPUT_FILE" | tail -n 1)"
        rm -f "$INSTALL_OUTPUT_FILE"
        TELEGRAM_API_URL="$(normalise_api_url "$TELEGRAM_API_URL")"
        [[ -n "$TELEGRAM_API_URL" ]] || die "Local Bot API installer did not return a URL"
        LOCAL_SWITCH_REQUIRED=true
    else
        echo "WARNING: without a local Bot API server, the bot can receive files only up to 20 MB."
    fi
fi

if "$LOCAL_SWITCH_REQUIRED"; then
    SWITCHER="$SCRIPT_DIR/scripts/switch-to-local-bot-api.sh"
    [[ -x "$SWITCHER" ]] || die "Local Bot API switcher is missing or not executable: $SWITCHER"
    BOT_TOKEN="$BOT_TOKEN" "$SWITCHER" "$TELEGRAM_API_URL"
fi

umask 077
{
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "$BOT_TOKEN"
    printf 'OWNER_ID=%s\n' "$OWNER_ID"
    printf 'CODEX_CWD=%s\n' "$CODEX_CWD"
    printf 'CODEX_SANDBOX=%s\n' "$CODEX_SANDBOX"
    printf 'CODEX_BOT_STATE_FILE=%s/state.json\n' "$INSTALL_DIR"
    printf 'CODEX_BOT_WHITELIST_FILE=%s/whitelist.txt\n' "$INSTALL_DIR"
    printf 'CODEX_BOT_ACCOUNTS_DIR=%s/accounts\n' "$INSTALL_DIR"
    printf 'CODEX_BOT_RESTART_FILE=%s/restart.request\n' "$INSTALL_DIR"
    printf 'CODEX_BOT_SERVICE_NAME=%s\n' "$SERVICE_NAME"
    [[ -n "$TELEGRAM_API_URL" ]] && printf 'TELEGRAM_API_URL=%s\n' "$TELEGRAM_API_URL"
} > .env
chmod 600 .env
touch whitelist.txt
chmod 600 whitelist.txt
mkdir -p accounts
chmod 700 accounts

UNIT_FILE="$(mktemp "/tmp/${SERVICE_NAME}.service.XXXXXX")"
trap 'rm -f "$UNIT_FILE"' EXIT
sed \
    -e "s|__USER__|${INSTALL_USER}|g" \
    -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    codex-telegram-bot.service.example > "$UNIT_FILE"

python3 -m py_compile bot.py app_server.py telegram_format.py
echo "Installing /etc/systemd/system/${SERVICE_NAME}.service (sudo required)"
sudo install -m 0644 "$UNIT_FILE" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.service"

echo
sudo systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
echo
echo "Installed."
echo "Logs:      journalctl -u ${SERVICE_NAME}.service -f"
echo "Restart:   $INSTALL_DIR/request-restart"
echo "Whitelist: $INSTALL_DIR/whitelist.txt (one Telegram ID per line; no restart needed)"

# --- optional: example personality file -----------------------------------
if [ -t 0 ] && [ -f "$SCRIPT_DIR/personality.example.md" ]; then
    echo
    echo "The bridged assistant has no voice of its own beyond your AGENTS.md."
    echo "personality.example.md in this repo is a starting point you can install."
    read -rp "Install it to  [1] ~/.codex/AGENTS.md  [2] $CODEX_CWD/AGENTS.md  [3] a path you choose  [4] skip : " P_CHOICE || P_CHOICE=3
    case "${P_CHOICE:-4}" in
        1) P_DEST="$HOME/.codex/AGENTS.md" ;;
        2) P_DEST="${CODEX_CWD:-$HOME}/AGENTS.md" ;;
        3) read -rp "Path for the personality file: " P_DEST || P_DEST="" ;;
        *) P_DEST="" ;;
    esac
    if [ -n "${P_DEST:-}" ]; then
        P_REAL="$(readlink -f "$P_DEST" 2>/dev/null || true)"
        echo "Personality destination: $P_DEST"
        if [ -L "$P_DEST" ] || [ -e "$P_DEST" ]; then
            echo "Existing personality target: ${P_REAL:-$P_DEST}"
            read -r -p "Append the example personality to this target? [y/N] " P_CONFIRM || P_CONFIRM=""
            [[ "$P_CONFIRM" =~ ^[Yy]([Ee][Ss])?$ ]] || { echo "Personality installation skipped."; P_DEST=""; }
        else
            echo "Resolved personality target: ${P_REAL:-$P_DEST}"
        fi
    fi
    if [ -n "${P_DEST:-}" ]; then
        mkdir -p "$(dirname "$P_DEST")"
        P_BODY="$(sed '/^<!--/,/-->/d' "$SCRIPT_DIR/personality.example.md")"
        if [ -f "$P_DEST" ] && grep -q 'BEGIN personality.example' "$P_DEST"; then
            echo "$P_DEST already has a personality.example block - left as is."
        else
            {
                [ -f "$P_DEST" ] && printf '\n'
                printf '<!-- BEGIN personality.example -->\n'
                printf '%s\n' "$P_BODY"
                printf '<!-- END personality.example -->\n'
            } >> "$P_DEST"
            echo "Installed the example personality to $P_DEST"
        fi
    fi
fi

# --- optional: graphify code-map -------------------------------------------
# graphify (PyPI package "graphifyy", github.com/Graphify-Labs/graphify) turns
# this repo into a queryable knowledge graph under graphify-out/. Optional.
setup_graphify() {
    local platform="$1"
    if command -v graphify >/dev/null 2>&1; then
        echo "graphify: already on PATH ($(command -v graphify))"
    elif command -v uv >/dev/null 2>&1; then
        uv tool install graphifyy || { echo "graphify: install failed, skipping"; return 0; }
    elif command -v pipx >/dev/null 2>&1; then
        pipx install graphifyy || { echo "graphify: install failed, skipping"; return 0; }
    else
        echo "graphify: needs 'uv' or 'pipx' to install - skipping"
        return 0
    fi
    graphify install --platform "$platform" >/dev/null 2>&1 || true
    graphify update . >/dev/null 2>&1 || true
    echo "graphify: code map built under graphify-out/ (re-run 'graphify update .' after edits;"
    echo "          a post-commit hook keeps it fresh if graphify installed one)"
}

if [ -t 0 ]; then
    read -rp "Set up the graphify code-map for this repo? [y/N] " _SETUP_GRAPHIFY || _SETUP_GRAPHIFY=""
    case "${_SETUP_GRAPHIFY:-}" in
        [Yy]*) setup_graphify "codex" ;;
    esac
fi
