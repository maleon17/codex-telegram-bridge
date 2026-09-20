#!/usr/bin/env bash
# Install one shared, loopback-only Telegram Bot API server for local projects.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=false
ASSUME_YES=false

TELEGRAM_BOT_API_UNIT="${TELEGRAM_BOT_API_UNIT:-telegram-bot-api}"
TELEGRAM_BOT_API_PORT="${TELEGRAM_BOT_API_PORT:-8081}"
TELEGRAM_BOT_API_DIR="${TELEGRAM_BOT_API_DIR:-$HOME/.local/share/telegram-bot-api}"
TELEGRAM_BOT_API_ENV_FILE="${TELEGRAM_BOT_API_ENV_FILE:-$HOME/.config/telegram-bot-api/env}"
TELEGRAM_BOT_API_BIN="${TELEGRAM_BOT_API_BIN:-$HOME/.local/bin/telegram-bot-api}"
# The upstream project publishes no release tags; this is a pinned official commit.
TELEGRAM_BOT_API_REF="${TELEGRAM_BOT_API_REF:-e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1}"
TELEGRAM_BOT_API_RETENTION_DAYS="${TELEGRAM_BOT_API_RETENTION_DAYS:-7}"

die() { echo "ERROR: $*" >&2; exit 1; }
note() { echo "$*"; }
usage() {
    cat <<'EOF'
Usage: scripts/install-local-bot-api.sh [--dry-run] [--yes]

API credentials are accepted only through TELEGRAM_API_ID and TELEGRAM_API_HASH
or secure interactive prompts; --api-id and --api-hash are deliberately unsupported.
EOF
}
confirm() {
    local prompt="$1" answer=""
    if "$ASSUME_YES"; then return 0; fi
    if [[ ! -t 0 ]]; then die "$prompt requires confirmation; rerun interactively or with --yes"; fi
    read -r -p "$prompt [y/N] " answer
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}
require_number() { [[ "$2" =~ ^[0-9]+$ ]] || die "$1 must be a non-negative integer"; }
run() {
    if "$DRY_RUN"; then
        printf 'PLAN: '
        printf '%q ' "$@"
        printf '\n'
    else
        "$@"
    fi
}
unit_known() { systemctl list-unit-files "${TELEGRAM_BOT_API_UNIT}.service" --no-legend 2>/dev/null | awk 'NF { found=1 } END { exit !found }'; }
installed_unit_port() {
    local unit_text port
    unit_text="$(systemctl cat "${TELEGRAM_BOT_API_UNIT}.service" 2>/dev/null || \
        { [[ -r "/etc/systemd/system/${TELEGRAM_BOT_API_UNIT}.service" ]] && cat "/etc/systemd/system/${TELEGRAM_BOT_API_UNIT}.service"; })" || return 1
    port="$(printf '%s\n' "$unit_text" | sed -n 's/.*--http-port[=[:space:]][[:space:]]*\([0-9][0-9]*\).*/\1/p' | tail -n 1)"
    [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1 && "$port" -le 65535 ]] || return 1
    printf '%s\n' "$port"
}
port_in_use() {
    command -v ss >/dev/null 2>&1 || die "ss is required to check local ports"
    ss -ltnH 2>/dev/null | awk -v port=":$1" '$4 ~ (port "$") { found=1 } END { exit !found }'
}
port_listens_on_loopback() {
    ss -ltnH 2>/dev/null | awk -v port=":$1" '$4 == "127.0.0.1" port || $4 == "[::1]" port { found=1 } END { exit !found }'
}
have_dependencies() {
    local command_name
    for command_name in git cmake gperf g++ make; do
        command -v "$command_name" >/dev/null 2>&1 || return 1
    done
    [[ -f /usr/include/openssl/ssl.h && -f /usr/include/zlib.h ]]
}
dependency_install_command() {
    if command -v apt-get >/dev/null 2>&1; then
        printf '%s\n' 'sudo apt-get update && sudo apt-get install -y git cmake gperf g++ make libssl-dev zlib1g-dev'
    elif command -v dnf >/dev/null 2>&1; then
        printf '%s\n' 'sudo dnf install -y git cmake gperf gcc-c++ make openssl-devel zlib-devel'
    elif command -v pacman >/dev/null 2>&1; then
        printf '%s\n' 'sudo pacman -S --needed git cmake gperf gcc make openssl zlib'
    else
        return 1
    fi
}
install_dependencies() {
    local manager command_text
    if command -v apt-get >/dev/null 2>&1; then
        manager=apt
        command_text="$(dependency_install_command)"
    elif command -v dnf >/dev/null 2>&1; then
        manager=dnf
        command_text="$(dependency_install_command)"
    elif command -v pacman >/dev/null 2>&1; then
        manager=pacman
        command_text="$(dependency_install_command)"
    else
        die "Missing build dependencies. Install git cmake gperf g++ make and OpenSSL/zlib development packages."
    fi
    note "Missing build dependencies. Install them with: $command_text"
    confirm "Install missing dependencies using $manager?" || die "Dependencies were not installed"
    if "$DRY_RUN"; then
        note "PLAN: $command_text"
    else
        case "$manager" in
            apt) sudo apt-get update; sudo apt-get install -y git cmake gperf g++ make libssl-dev zlib1g-dev ;;
            dnf) sudo dnf install -y git cmake gperf gcc-c++ make openssl-devel zlib-devel ;;
            pacman) sudo pacman -S --needed git cmake gperf gcc make openssl zlib ;;
        esac
    fi
}
write_unit() {
    local template="$1" destination="$2" temporary
    temporary="$(mktemp "${TMPDIR:-/tmp}/telegram-bot-api-unit.XXXXXX")"
    trap 'rm -f "$temporary"' RETURN
    sed \
        -e "s|__USER__|$(id -un)|g" \
        -e "s|__ENV_FILE__|${TELEGRAM_BOT_API_ENV_FILE}|g" \
        -e "s|__BIN__|${TELEGRAM_BOT_API_BIN}|g" \
        -e "s|__PORT__|${TELEGRAM_BOT_API_PORT}|g" \
        -e "s|__DATA_DIR__|${TELEGRAM_BOT_API_DIR}|g" \
        -e "s|__TEMP_DIR__|${TELEGRAM_BOT_API_DIR}/tmp|g" \
        -e "s|__RETENTION_DAYS__|${TELEGRAM_BOT_API_RETENTION_DAYS}|g" \
        "$template" > "$temporary"
    run sudo install -m 0644 "$temporary" "$destination"
    rm -f "$temporary"
    trap - RETURN
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --yes) ASSUME_YES=true ;;
        --api-id|--api-hash) die "$1 is unsafe and unsupported; use TELEGRAM_API_ID/TELEGRAM_API_HASH or interactive input" ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
    shift
done

require_number TELEGRAM_BOT_API_PORT "$TELEGRAM_BOT_API_PORT"
require_number TELEGRAM_BOT_API_RETENTION_DAYS "$TELEGRAM_BOT_API_RETENTION_DAYS"
[[ "$TELEGRAM_BOT_API_PORT" -ge 1 && "$TELEGRAM_BOT_API_PORT" -le 65535 ]] || die "TELEGRAM_BOT_API_PORT must be 1..65535"
[[ "$TELEGRAM_BOT_API_UNIT" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "Invalid TELEGRAM_BOT_API_UNIT"

if unit_known; then
    if ! TELEGRAM_BOT_API_PORT="$(installed_unit_port)"; then
        if "$DRY_RUN"; then
            note "PLAN: would refuse: ${TELEGRAM_BOT_API_UNIT}.service exists but its ExecStart has no valid --http-port"
            exit 0
        fi
        die "${TELEGRAM_BOT_API_UNIT}.service exists but its ExecStart has no valid --http-port. It was not overwritten."
    fi
    if systemctl is-active --quiet "${TELEGRAM_BOT_API_UNIT}.service" && \
       port_listens_on_loopback "$TELEGRAM_BOT_API_PORT" && [[ -f "$TELEGRAM_BOT_API_ENV_FILE" ]]; then
        if "$DRY_RUN"; then
            note "PLAN: would reuse the healthy existing Telegram Bot API server; nothing would be changed."
        else
            note "Telegram Bot API server already exists; nothing changed."
        fi
        printf 'LOCAL_BOT_API_URL=http://127.0.0.1:%s\n' "$TELEGRAM_BOT_API_PORT"
        exit 0
    fi
    if "$DRY_RUN"; then
        note "PLAN: would refuse: ${TELEGRAM_BOT_API_UNIT}.service exists but is inactive or unhealthy."
        exit 0
    fi
    die "${TELEGRAM_BOT_API_UNIT}.service already exists but is inactive or unhealthy. Inspect it with: systemctl status ${TELEGRAM_BOT_API_UNIT}.service. It was not overwritten."
fi

while port_in_use "$TELEGRAM_BOT_API_PORT"; do
    note "Port $TELEGRAM_BOT_API_PORT is already in use; leaving that process untouched."
    TELEGRAM_BOT_API_PORT=$((TELEGRAM_BOT_API_PORT + 1))
    [[ "$TELEGRAM_BOT_API_PORT" -le 65535 ]] || die "No free port found"
done

if "$DRY_RUN"; then
    if have_dependencies; then
        note "PLAN: dependencies are present."
    elif dependency_command="$(dependency_install_command)"; then
        note "PLAN: would install missing build dependencies with: $dependency_command"
    else
        note "PLAN: would refuse until git, cmake, gperf, g++/make and OpenSSL/zlib development files are installed."
        exit 0
    fi
    note "PLAN: would build official telegram-bot-api at $TELEGRAM_BOT_API_REF in a temporary directory."
    note "PLAN: would install the binary to $TELEGRAM_BOT_API_BIN and create $TELEGRAM_BOT_API_ENV_FILE (mode 600) if absent."
    note "PLAN: would install ${TELEGRAM_BOT_API_UNIT}.service and its daily cleanup timer on port $TELEGRAM_BOT_API_PORT."
    printf 'LOCAL_BOT_API_URL=http://127.0.0.1:%s\n' "$TELEGRAM_BOT_API_PORT"
    exit 0
fi

if ! have_dependencies; then install_dependencies; fi

if [[ -f "$TELEGRAM_BOT_API_ENV_FILE" ]]; then
    confirm "Credentials file $TELEGRAM_BOT_API_ENV_FILE already exists. Reuse it without overwriting?" || die "Existing credentials file was left unchanged"
    grep -q '^TELEGRAM_API_ID=[0-9][0-9]*$' "$TELEGRAM_BOT_API_ENV_FILE" || die "Existing credentials file has no valid TELEGRAM_API_ID"
    grep -q '^TELEGRAM_API_HASH=.' "$TELEGRAM_BOT_API_ENV_FILE" || die "Existing credentials file has no TELEGRAM_API_HASH"
    chmod 600 "$TELEGRAM_BOT_API_ENV_FILE"
else
    API_ID="${TELEGRAM_API_ID:-}"
    API_HASH="${TELEGRAM_API_HASH:-}"
    # Do not pass caller-provided credentials to git, CMake, or any other child.
    unset TELEGRAM_API_ID TELEGRAM_API_HASH
    if [[ -z "$API_ID" ]]; then
        [[ -t 0 ]] || die "TELEGRAM_API_ID is required without a terminal"
        read -r -p "Telegram api_id: " API_ID
    fi
    if [[ -z "$API_HASH" ]]; then
        [[ -t 0 ]] || die "TELEGRAM_API_HASH is required without a terminal"
        read -r -s -p "Telegram api_hash: " API_HASH
        echo
    fi
    require_number TELEGRAM_API_ID "$API_ID"
    [[ -n "$API_HASH" ]] || die "TELEGRAM_API_HASH must not be empty"
    run mkdir -p -m 700 "$(dirname "$TELEGRAM_BOT_API_ENV_FILE")"
    (umask 077; printf 'TELEGRAM_API_ID=%s\nTELEGRAM_API_HASH=%s\n' "$API_ID" "$API_HASH" > "$TELEGRAM_BOT_API_ENV_FILE")
    chmod 600 "$TELEGRAM_BOT_API_ENV_FILE"
    unset API_ID API_HASH
fi

BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/telegram-bot-api-build.XXXXXX")"
trap 'rm -rf "$BUILD_ROOT"' EXIT
git clone --recursive https://github.com/tdlib/telegram-bot-api.git "$BUILD_ROOT/source"
git -C "$BUILD_ROOT/source" checkout --detach "$TELEGRAM_BOT_API_REF"
git -C "$BUILD_ROOT/source" submodule update --init --recursive
cmake -S "$BUILD_ROOT/source" -B "$BUILD_ROOT/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD_ROOT/build" --target telegram-bot-api -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
"$BUILD_ROOT/build/telegram-bot-api" --version >/dev/null
mkdir -p "$(dirname "$TELEGRAM_BOT_API_BIN")"
install -d -m 700 "$TELEGRAM_BOT_API_DIR" "$TELEGRAM_BOT_API_DIR/tmp"
install -m 0755 "$BUILD_ROOT/build/telegram-bot-api" "$TELEGRAM_BOT_API_BIN"

write_unit "$SCRIPT_DIR/telegram-bot-api.service.example" "/etc/systemd/system/${TELEGRAM_BOT_API_UNIT}.service"
write_unit "$SCRIPT_DIR/telegram-bot-api-cleanup.service.example" "/etc/systemd/system/${TELEGRAM_BOT_API_UNIT}-cleanup.service"
write_unit "$SCRIPT_DIR/telegram-bot-api-cleanup.timer.example" "/etc/systemd/system/${TELEGRAM_BOT_API_UNIT}-cleanup.timer"
sudo systemctl daemon-reload
sudo systemctl enable --now "${TELEGRAM_BOT_API_UNIT}.service" "${TELEGRAM_BOT_API_UNIT}-cleanup.timer"

if ! python3 - "$TELEGRAM_BOT_API_PORT" <<'PY'
import sys
from urllib.error import HTTPError
from urllib.request import urlopen
try:
    with urlopen(f"http://127.0.0.1:{sys.argv[1]}", timeout=10):
        pass
except HTTPError:
    pass  # Any HTTP response proves that the loopback server answered.
except Exception as exc:
    raise SystemExit(exc)
PY
then
    die "Server did not answer. Inspect: journalctl -u ${TELEGRAM_BOT_API_UNIT}.service --no-pager"
fi
printf 'LOCAL_BOT_API_URL=http://127.0.0.1:%s\n' "$TELEGRAM_BOT_API_PORT"
