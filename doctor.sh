#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

failures=0
check() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "OK   $label"
    else
        echo "FAIL $label"
        failures=$((failures + 1))
    fi
}
check_local_api_port() {
    local url="$1"
    TELEGRAM_API_URL_TO_CHECK="$url" python3 - <<'PY'
import os
import socket
from urllib.parse import urlsplit

parsed = urlsplit(os.environ["TELEGRAM_API_URL_TO_CHECK"])
if parsed.hostname is None:
    raise SystemExit("invalid Telegram API URL")
port = parsed.port or (443 if parsed.scheme == "https" else 80)
with socket.create_connection((parsed.hostname, port), timeout=5):
    pass
PY
}
is_local_api_url() {
    local authority="${1#*://}" host
    authority="${authority%%/*}"
    host="${authority%%:*}"
    [[ "${host,,}" != "api.telegram.org" ]]
}

check "python syntax" python3 -m py_compile bot.py app_server.py telegram_format.py
check "codex CLI" codex --version
check "owner Codex login" codex login status
check ".env exists" test -f .env
if [[ -f .env ]]; then
    mode="$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env)"
    if [[ "$mode" == "600" ]]; then
        echo "OK   .env permissions (600)"
    else
        echo "FAIL .env permissions ($mode, expected 600)"
        failures=$((failures + 1))
    fi
fi
if [[ -f state.json ]]; then
    check "state.json is valid JSON" python3 -c 'import json; json.load(open("state.json"))'
fi
if systemctl list-unit-files codex-telegram-bot.service >/dev/null 2>&1; then
    check "codex-telegram-bot.service active" systemctl is-active --quiet codex-telegram-bot.service
fi
if [[ -f .env ]]; then
    TELEGRAM_API_URL="$(sed -n 's/^TELEGRAM_API_URL=//p' .env | tail -n 1)"
    TELEGRAM_API_URL="${TELEGRAM_API_URL%/}"
    if [[ -n "$TELEGRAM_API_URL" ]] && is_local_api_url "$TELEGRAM_API_URL"; then
        TELEGRAM_BOT_API_UNIT="${TELEGRAM_BOT_API_UNIT:-telegram-bot-api}"
        check "${TELEGRAM_BOT_API_UNIT}.service active (local Bot API)" systemctl is-active --quiet "${TELEGRAM_BOT_API_UNIT}.service"
        check "local Bot API port answers (${TELEGRAM_API_URL})" check_local_api_port "$TELEGRAM_API_URL"
    fi
fi
exit "$failures"
