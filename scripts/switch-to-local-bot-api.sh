#!/usr/bin/env bash
# Log a bot out of the cloud API and verify it through a local Bot API server.
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

[[ $# -eq 1 ]] || die "Usage: scripts/switch-to-local-bot-api.sh <api_url>"
TELEGRAM_API_URL="$1"

if [[ -z "${BOT_TOKEN:-}" && -f .env ]]; then
    BOT_TOKEN="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' .env | tail -n 1)"
fi
[[ -n "${BOT_TOKEN:-}" ]] || die "BOT_TOKEN is required (set it in .env or the environment)"

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

cloud_log_out() {
    TELEGRAM_TOKEN_TO_LOG_OUT="$BOT_TOKEN" python3 - <<'PY'
import json, os, urllib.request
token = os.environ["TELEGRAM_TOKEN_TO_LOG_OUT"]
request = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/logOut", data=b"", method="POST"
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
except Exception as exc:
    raise SystemExit(f"Cloud logOut failed: {exc}")
if not result.get("ok"):
    raise SystemExit("Cloud logOut was rejected by Telegram")
PY
}

echo "Before switching, Telegram must log this bot out of the cloud API. This is one-way: the cloud API cannot be used again for about 10 minutes."
check_telegram_token ""
LOGOUT_CONFIRM="${LOCAL_BOT_API_LOGOUT_CONFIRM:-}"
if [[ -z "$LOGOUT_CONFIRM" && -t 0 ]]; then
    read -r -p "Call cloud logOut and switch to $TELEGRAM_API_URL? [y/N] " LOGOUT_CONFIRM
fi
if [[ ! -t 0 && "$LOGOUT_CONFIRM" != "yes" ]]; then
    die "LOCAL_BOT_API_LOGOUT_CONFIRM=yes is required without a terminal before the irreversible cloud logOut step"
fi
[[ "$LOGOUT_CONFIRM" =~ ^[Yy]([Ee][Ss])?$|^yes$ ]] || die "Local switch cancelled before cloud logOut"
cloud_log_out
check_telegram_token "$TELEGRAM_API_URL"
