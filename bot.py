#!/usr/bin/env python3
"""Single-owner Telegram frontend for persistent Codex CLI conversations."""

import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from app_server import AppServerClient, AppServerError
from telegram_format import escape_mdv2, rich_message_to_markdown, strip_mdv2


EDIT_THROTTLE_S = 1.3
BATCH_DEBOUNCE_S = 1.5
BATCH_RETRY_S = 0.2
MAX_MESSAGE_LEN = 4000
RICH_MAX_CHARS = 30000
HTTP_TIMEOUT_S = 20
LOCAL_GET_FILE_TIMEOUT_S = 600
LOCAL_SEND_TIMEOUT_S = 600
TELEGRAM_CLOUD_FILE_MAX_BYTES = 20 * 1024 * 1024
IDLE_TIMEOUT_S = 300
TOTAL_TIMEOUT_S = 1800
LOCAL_BOT_API_STATUS_EDIT_MIN_INTERVAL_S = 1.0
COMMANDS = [
    ("new", "Начать новую Codex-сессию"),
    ("sessions", "Список последних сессий"),
    ("resume", "Продолжить сессию по id"),
    ("status", "Сессия, модель, sandbox и workspace"),
    ("stop", "Прервать текущий запрос"),
    ("usage", "Токены последнего запроса"),
    ("compact", "Сжать контекст текущей сессии"),
    ("model", "Выбрать модель Codex"),
    ("effort", "Выбрать мощность модели"),
    ("mode", "Sandbox: read-only/workspace-write/full"),
    ("workspace", "Рабочая директория"),
    ("account", "Состояние аккаунта Codex"),
    ("login", "Подключить свой аккаунт Codex"),
    ("persona", "Показать или сбросить персону владельца"),
    ("restart", "Перезапустить Codex-бота"),
    ("update", "Обновить бота из git и перезапустить"),
]


def log(message):
    print(message, file=sys.stderr, flush=True)


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"codex-telegram-bot: required environment variable {name} is not set")
    return value


def env_positive_int(name, default):
    """Read a positive integer environment setting without accepting nonsense."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log(f"Ignoring invalid {name}; using {default}")
        return default
    if value <= 0:
        log(f"Ignoring non-positive {name}; using {default}")
        return default
    return value


def telegram_api_config(value):
    """Return the normalized API root and whether it is a local Bot API."""
    root = (value or "https://api.telegram.org").strip().rstrip("/")
    parsed = urlsplit(root)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("codex-telegram-bot: TELEGRAM_API_URL must be an absolute URL")
    hostname = (parsed.hostname or "").lower()
    is_local = bool(value and hostname != "api.telegram.org")
    return root, is_local


BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
try:
    OWNER_ID = int(require_env("OWNER_ID"))
except ValueError as exc:
    raise SystemExit("codex-telegram-bot: OWNER_ID must be an integer") from exc

CODEX_CWD = os.environ.get("CODEX_CWD", "/home/mishin")
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "danger-full-access")
STATE_FILE = Path(
    os.environ.get("CODEX_BOT_STATE_FILE", Path(__file__).with_name("state.json"))
).expanduser()
STATE_INSTANCE_NAME = STATE_FILE.stem
RESTART_SIGNAL_FILE = Path(os.environ.get(
    "CODEX_BOT_RESTART_FILE", Path(__file__).with_name("restart.request")
)).expanduser()
BOT_ENV_FILE = Path(__file__).with_name(".env")
TELEGRAM_BOT_API_UNIT = os.environ.get("TELEGRAM_BOT_API_UNIT", "telegram-bot-api")
TELEGRAM_BOT_API_PORT = env_positive_int("TELEGRAM_BOT_API_PORT", 8081)
TELEGRAM_BOT_API_ENV_FILE = Path(os.environ.get(
    "TELEGRAM_BOT_API_ENV_FILE", "~/.config/telegram-bot-api/env"
)).expanduser()
LOCAL_BOT_API_INSTALL_SCRIPT = Path(__file__).parent / "scripts/install-local-bot-api.sh"
LOCAL_BOT_API_SWITCH_SCRIPT = Path(__file__).parent / "scripts/switch-to-local-bot-api.sh"
TELEGRAM_API_URL, LOCAL_BOT_API = telegram_api_config(os.environ.get("TELEGRAM_API_URL"))
API_BASE = f"{TELEGRAM_API_URL}/bot{BOT_TOKEN}"
FILE_API_BASE = f"{TELEGRAM_API_URL}/file/bot{BOT_TOKEN}"
WHITELIST_FILE = Path(os.environ.get(
    "CODEX_BOT_WHITELIST_FILE", Path(__file__).with_name("whitelist.txt")
)).expanduser()
ACCOUNTS_DIR = Path(os.environ.get(
    "CODEX_BOT_ACCOUNTS_DIR", Path(__file__).with_name("accounts")
)).expanduser()
EXTERNAL_REQUEST_FILE = Path(os.environ.get(
    "CODEX_BOT_EXTERNAL_REQUEST_FILE", Path(__file__).with_name("external_request.json")
)).expanduser()
CROSS_DELEGATE_QUEUE_DIR = Path(__file__).with_name("cross_delegate_queue")
CROSS_DELEGATE_RESULT_DIR = Path(__file__).with_name("cross_delegate_result")
FILE_SEND_QUEUE_DIR = Path(__file__).with_name("file_send_queue")
FILE_SEND_RESULT_DIR = Path(__file__).with_name("file_send_result")
FILE_SEND_MAX_BYTES = env_positive_int(
    "FILE_SEND_MAX_BYTES", 2000 * 1024 * 1024 if LOCAL_BOT_API else 50 * 1024 * 1024,
)
FILE_SEND_MAX_CAPTION_CHARS = 1024
PHOTO_SEND_MAX_BYTES = 10 * 1024 * 1024
INCOMING_FILE_MAX_BYTES = env_positive_int(
    "INCOMING_FILE_MAX_BYTES", 2000 * 1024 * 1024 if LOCAL_BOT_API else 50 * 1024 * 1024,
)
GET_FILE_TIMEOUT_S = env_positive_int(
    "TELEGRAM_GET_FILE_TIMEOUT_S", LOCAL_GET_FILE_TIMEOUT_S if LOCAL_BOT_API else HTTP_TIMEOUT_S,
)
SEND_TIMEOUT_S = env_positive_int(
    "TELEGRAM_SEND_TIMEOUT_S", LOCAL_SEND_TIMEOUT_S if LOCAL_BOT_API else HTTP_TIMEOUT_S,
)
TEXT_DOCUMENT_MAX_BYTES = 1024 * 1024
TURN_STOP_WAIT_S = 10

state_lock = threading.RLock()
process_lock = threading.RLock()
telegram_lock = threading.Lock()
rate_limit_until = 0.0
restart_draining = False


DELEGATE_KEY_PREFIX = "delegate:"
FILE_SEND_AGENTS_MARKER = "## Отправка файлов в Telegram"
FILE_SEND_AGENTS_SECTION = """
## Отправка файлов в Telegram

Чтобы отправить пользователю готовый документ, сначала создай или скопируй его
в каталог из переменной `CODEX_TELEGRAM_OUTBOX`, затем вызови MCP-тул
`send_telegram_file` с абсолютным путём к файлу и, при необходимости, `caption`.
Не пытайся искать или использовать токен Telegram: этот тул отправляет файл
только в текущий чат и не раскрывает секреты бота.
""".strip()


def delegate_key(chat_id):
    """Return the stable state/process key for a delegated tenant."""
    return f"{DELEGATE_KEY_PREFIX}{int(chat_id)}"


def real_chat_id(state_key):
    """Return the Telegram chat id represented by a state key."""
    value = str(state_key)
    if value.startswith(DELEGATE_KEY_PREFIX):
        value = value[len(DELEGATE_KEY_PREFIX):]
    return int(value)


class TenantRuntime:
    def __init__(self, chat_id, state_key=None):
        self.chat_id = int(chat_id)
        self.state_key = str(state_key if state_key is not None else self.chat_id)
        self.busy = False
        self.app_server = None
        self.loaded_thread_id = None
        self.loaded_server_pid = None
        self.active_view = None
        self.active_done = None
        self.active_turn_id = None
        self.active_thread_id = None
        self.active_error = None
        self.active_stopped = False
        self.active_last_event_at = None
        self.active_media_paths = []
        self.last_rate_limits = None
        self.login_id = None
        # Rapid Telegram updates (most visibly a multi-message forward) are
        # held briefly and dispatched as one prompt instead of starting one
        # Codex turn per update.
        self.pending_batch = []
        self.batch_timer = None
        self.batch_generation = 0
        self.pending_env = None
        self.close_app_server_after_turn = False
        self.cancel_requested = False
        self.worker_done = threading.Event()
        self.worker_done.set()
        # The local-mode "📥 Загружаю вложение…" status message (if any) for
        # the burst currently landing, handed to the next turn's TurnView so
        # it becomes the Thinking card in place instead of a second message.
        self.pending_progress_msg_id = None
        # These values deliberately never enter state.json. In particular,
        # api_hash is used in one call and never stored on this object.
        self.update_flow_stage = None
        self.update_flow_api_id = None


tenants = {}
tenants_delegate = {}
# A local getFile call may wait while telegram-bot-api fetches a large upload.
# Each chat gets its own worker so that later updates cannot overtake that
# download, while independent chats continue immediately.
local_message_queues = {}
# chat_id -> message ids produced by /persona.  This is not a conversational
# stage: a reply remains valid whenever it references one of these snapshots.
persona_message_ids = {}


def get_tenant(chat_id):
    chat_id = int(chat_id)
    with process_lock:
        runtime = tenants.get(chat_id)
        if runtime is None:
            runtime = TenantRuntime(chat_id)
            tenants[chat_id] = runtime
        return runtime


def get_delegate_tenant(chat_id):
    """Return the persistent delegated runtime for a real Telegram chat."""
    chat_id = int(chat_id)
    with process_lock:
        runtime = tenants_delegate.get(chat_id)
        if runtime is None:
            runtime = TenantRuntime(chat_id, state_key=delegate_key(chat_id))
            tenants_delegate[chat_id] = runtime
        return runtime


def active_delegate_tenant(chat_id):
    """Return a delegate that must receive the owner's next input."""
    chat_id = int(chat_id)
    with process_lock:
        runtime = tenants_delegate.get(chat_id)
        active = runtime is not None and (runtime.busy or runtime.pending_batch)
    if runtime is not None and (active or chat_state(runtime.state_key).get("resume_selected", False)):
        return runtime
    return None


def load_whitelist():
    result = {str(OWNER_ID)}
    try:
        raw = WHITELIST_FILE.read_text(encoding="utf-8")
        result.update(part.strip() for part in raw.replace("\n", ",").split(",") if part.strip())
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"Could not read whitelist: {exc}")
    return result


def default_codex_home():
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def _ensure_tenant_mcp_server(tenant_dir, chat_id, name, server_filename):
    config_path = tenant_dir / "config.toml"
    section_header = f"[mcp_servers.{name}]"
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        config_text = ""
    if any(
        line.partition("#")[0].strip() == section_header
        for line in config_text.splitlines()
    ):
        return

    server_path = Path(__file__).with_name(server_filename).resolve()
    result = subprocess.run(
        [
            "codex", "mcp", "add", name,
            "--env", f"CHAT_ID={int(chat_id)}",
            "--", sys.executable, str(server_path),
        ],
        env={**os.environ, "CODEX_HOME": str(tenant_dir)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Could not seed {name} MCP server: "
            + (detail or f"codex exited with status {result.returncode}")
        )


def _ensure_tenant_mcp_config(tenant_dir, chat_id):
    _ensure_tenant_mcp_server(
        tenant_dir, chat_id, "delegate-to-claude", "delegate_to_claude_mcp.py",
    )
    _ensure_tenant_mcp_server(
        tenant_dir, chat_id, "send-telegram-file", "send_telegram_file_mcp.py",
    )


def tenant_file_outbox(chat_id):
    """Return the only directory whose files a tenant may send to Telegram."""
    path = ACCOUNTS_DIR / str(int(chat_id)) / "outbox"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _ensure_tenant_file_send_instructions(agents_path):
    try:
        content = agents_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    if FILE_SEND_AGENTS_MARKER in content:
        return
    agents_path.write_text(
        content.rstrip() + "\n\n" + FILE_SEND_AGENTS_SECTION + "\n",
        encoding="utf-8",
    )


def tenant_codex_home(chat_id, state_key=None):
    """Return a tenant home; delegates share auth/config, not session files."""
    chat_id = int(chat_id)
    delegated = state_key is not None and str(state_key) != str(chat_id)
    if delegated:
        path = ACCOUNTS_DIR / "delegated" / str(chat_id)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        shared_home = tenant_codex_home(chat_id)
        # Only these two files are shared. Sessions and every other file stay
        # physically inside the delegated home above.
        for filename in ("auth.json", "config.toml"):
            link = path / filename
            target = (shared_home / filename).absolute()
            if link.is_symlink():
                if os.path.realpath(link) == os.path.realpath(target):
                    continue
                link.unlink()
            elif link.exists():
                link.unlink()
            link.symlink_to(target)
        return path
    path = ACCOUNTS_DIR / str(chat_id)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    agents_path = path / "AGENTS.md"
    owner = chat_id == OWNER_ID
    if not agents_path.exists():
        persona_source = (
            default_codex_home() / "AGENTS.md"
            if owner else Path(__file__).with_name("personality.example.md")
        )
        if persona_source.exists():
            shutil.copyfile(persona_source, agents_path)
        else:
            agents_path.touch(mode=0o600)
        if owner:
            source_config = default_codex_home() / "config.toml"
            if source_config.exists():
                shutil.copyfile(source_config, path / "config.toml")
        else:
            shutil.copyfile(Path(__file__).with_name("HANDOFF.md"), path / "handoff.md")
    if owner:
        link = path / "auth.json"
        target = (default_codex_home() / "auth.json").absolute()
        if link.is_symlink():
            if os.path.realpath(link) != os.path.realpath(target):
                link.unlink()
        elif link.exists():
            link.unlink()
        if not link.is_symlink():
            link.symlink_to(target)
    # Idempotent (marker-guarded, append-only if missing) for everyone,
    # owner included -- without it, the owner's migrated AGENTS.md has no
    # working knowledge of the send-telegram-file MCP tool it was just
    # given access to. This does not touch the rest of a /persona rewrite.
    _ensure_tenant_file_send_instructions(agents_path)
    try:
        _ensure_tenant_mcp_config(path, chat_id)
    except Exception as exc:
        # Cross-delegation is a nice-to-have on top of an otherwise-working
        # tenant; a seeding hiccup (PATH, transient codex-cli failure, a
        # concurrent seed race) must never break this tenant's ordinary
        # chat, which is what calling tenant_codex_home() usually means.
        log(f"tenant={chat_id} could not seed delegate-to-claude MCP server: {exc}")
    return path


def _rate_limited(result):
    return result.get("error_code") == 429


def tg_call(method, params=None, timeout=HTTP_TIMEOUT_S):
    """Call Telegram once; never retry, or call at all during a known 429 ban.

    `telegram_lock` guards only the tiny rate_limit_until read/write -- NOT
    the network call itself. main()'s own getUpdates is a 30-40s long poll
    that also goes through this function; holding a lock across the actual
    HTTP request would serialize every other thread's sendMessage/edit
    behind that poll for its entire duration, which is exactly what
    happened here (confirmed live via py-spy: run_turn's first live-progress
    edit sat blocked on this lock while the main loop held it inside
    urlopen()). The lock only needs to protect the shared counter.
    """
    global rate_limit_until
    with telegram_lock:
        now = time.monotonic()
        if now < rate_limit_until and method != "getUpdates":
            remaining = max(1, int(rate_limit_until - now + 0.999))
            result = {
                "ok": False,
                "error_code": 429,
                "description": "locally suppressed during Telegram rate limit",
                "parameters": {"retry_after": remaining},
            }
            log(f"Telegram {method} suppressed: rate-limited for ~{remaining} more seconds")
            return result

    request = urllib.request.Request(
        f"{API_BASE}/{method}",
        data=json.dumps(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {"ok": False, "error": str(exc)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    if not result.get("ok"):
        if _rate_limited(result):
            retry_after = (result.get("parameters") or {}).get("retry_after")
            try:
                delay = max(1.0, float(retry_after))
            except (TypeError, ValueError):
                delay = 1.0
            with telegram_lock:
                rate_limit_until = max(rate_limit_until, time.monotonic() + delay)
            log(
                f"Telegram {method} not ok: 429 Too Many Requests; "
                f"bot rate-limited for {retry_after} seconds: {result}"
            )
        else:
            log(f"Telegram {method} not ok: {result}")
    return result


def tenant_upload_dir(chat_id):
    path = ACCOUNTS_DIR / str(chat_id) / "uploads"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


class UnsupportedAttachmentError(RuntimeError):
    pass


class AttachmentDownloadError(UnsupportedAttachmentError):
    """A download failure with a stable machine-readable reason for callers."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _download_error_from_exception(exc):
    if isinstance(exc, TimeoutError):
        return AttachmentDownloadError(
            "timeout", "Файл не скачан: Telegram слишком долго готовил файл. Попробуй ещё раз или дай ссылку."
        )
    if isinstance(exc, PermissionError):
        return AttachmentDownloadError(
            "local_file_access", "Файл не скачан: нет доступа к файлу локального Bot API. Проверь права сервера и бота."
        )
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return AttachmentDownloadError(
            "no_space", "Файл не скачан: на диске нет места. Освободи место и отправь файл снова."
        )
    if isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
        return AttachmentDownloadError(
            "network", "Файл не скачан: ошибка связи с Telegram. Попробуй ещё раз или дай ссылку."
        )
    return AttachmentDownloadError(
        "download_failed", "Файл не скачан: Telegram не дал получить файл. Попробуй ещё раз или дай ссылку."
    )


def _get_file_error(result):
    detail = str(result.get("error") or result.get("description") or "").lower()
    if not LOCAL_BOT_API and "file is too big" in detail:
        return AttachmentDownloadError(
            "too_big_for_cloud",
            "Файл не скачан: размер файла превышает облачный лимит Telegram 20 МБ. "
            "Включи локальный Bot API сервер в setup.sh или дай ссылку.",
        )
    if "timed out" in detail or "timeout" in detail:
        return AttachmentDownloadError(
            "timeout", "Файл не скачан: Telegram слишком долго готовил файл. Попробуй ещё раз или дай ссылку."
        )
    if result.get("error"):
        return AttachmentDownloadError(
            "network", "Файл не скачан: ошибка связи с Telegram. Попробуй ещё раз или дай ссылку."
        )
    return AttachmentDownloadError(
        "get_file_failed", "Файл не скачан: Telegram отказал в выдаче файла. Отправь его снова или дай ссылку."
    )


def download_telegram_file(file_id, suggested_name="image.jpg", chat_id=None,
                           max_bytes=INCOMING_FILE_MAX_BYTES, file_size=None):
    """Download an owner-sent Telegram file for App Server localImage input."""
    if not LOCAL_BOT_API and isinstance(file_size, int) and file_size > TELEGRAM_CLOUD_FILE_MAX_BYTES:
        size_mb = file_size / (1024 * 1024)
        raise AttachmentDownloadError(
            "too_big_for_cloud",
            "Файл не скачан: размер "
            f"{size_mb:.1f} МБ превышает облачный лимит Telegram 20 МБ. "
            "Включи локальный Bot API сервер в setup.sh или дай ссылку.",
        )
    result = tg_call("getFile", {"file_id": file_id}, timeout=GET_FILE_TIMEOUT_S)
    if not result.get("ok"):
        raise _get_file_error(result)
    remote_path = (result.get("result") or {}).get("file_path") if result.get("ok") else None
    if not remote_path:
        raise AttachmentDownloadError(
            "get_file_failed", "Файл не скачан: Telegram не вернул путь к файлу. Попробуй ещё раз или дай ссылку."
        )
    suffix = Path(suggested_name).suffix or Path(remote_path).suffix or ".jpg"
    media_dir = tenant_upload_dir(chat_id) if chat_id is not None else (
        Path(tempfile.gettempdir()) / "codex-telegram-bot-media"
    )
    local_path = None
    try:
        media_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, local_path = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=media_dir)
        os.close(fd)
        source = Path(remote_path)
        if LOCAL_BOT_API and source.is_absolute():
            try:
                source_stat = source.stat()
            except FileNotFoundError:
                raise AttachmentDownloadError(
                    "local_file_missing",
                    "Файл не скачан: локальный Bot API больше не видит этот файл. Отправь его снова или дай ссылку.",
                )
            except PermissionError:
                raise AttachmentDownloadError(
                    "local_file_access",
                    "Файл не скачан: нет доступа к файлу локального Bot API. Проверь права сервера и бота.",
                )
            if not source.is_file():
                raise AttachmentDownloadError(
                    "local_file_access",
                    "Файл не скачан: путь локального Bot API не является доступным файлом. Проверь права сервера и бота.",
                )
            if source_stat.st_size > max_bytes:
                raise AttachmentDownloadError(
                    "too_big", "Файл не скачан: размер превышает разрешённый лимит. Отправь меньший файл или дай ссылку."
                )
            shutil.move(str(source), local_path)
            return local_path
        with urllib.request.urlopen(
            f"{FILE_API_BASE}/{remote_path}",
            timeout=HTTP_TIMEOUT_S,
        ) as response, open(local_path, "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                if handle.tell() + len(chunk) > max_bytes:
                    raise AttachmentDownloadError(
                        "too_big", "Файл не скачан: размер превышает разрешённый лимит. Отправь меньший файл или дай ссылку."
                    )
                handle.write(chunk)
        return local_path
    except AttachmentDownloadError:
        if local_path is not None:
            try:
                os.unlink(local_path)
            except FileNotFoundError:
                pass
        raise
    except Exception as exc:
        if local_path is not None:
            try:
                os.unlink(local_path)
            except FileNotFoundError:
                pass
        raise _download_error_from_exception(exc) from exc


FORWARD_FIELDS = (
    "forward_origin", "forward_from", "forward_from_chat", "forward_sender_name",
    "is_automatic_forward",
)


def is_forwarded_message(message):
    """Return whether Telegram marked this update as a forwarded message."""
    return any(message.get(field) for field in FORWARD_FIELDS)


def _peer_label(peer, fallback="неизвестный источник"):
    if not isinstance(peer, dict):
        return fallback
    name = " ".join(
        str(peer.get(key)).strip()
        for key in ("first_name", "last_name", "title")
        if peer.get(key)
    ).strip()
    username = str(peer.get("username") or "").strip().lstrip("@")
    if name and username:
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else fallback)


def forwarded_origin_label(message):
    """Render Bot API forward metadata without dumping its raw JSON."""
    origin = message.get("forward_origin")
    if isinstance(origin, dict):
        origin_type = origin.get("type")
        if origin_type == "user":
            return _peer_label(origin.get("sender_user"), "пользователь")
        if origin_type == "hidden_user":
            return str(origin.get("sender_user_name") or "скрытый отправитель")
        if origin_type in ("chat", "channel"):
            return _peer_label(origin.get("sender_chat") or origin.get("chat"), "чат/канал")
        return (
            _peer_label(origin.get("sender_user") or origin.get("sender_chat"), "")
            or str(origin.get("author_signature") or "").strip()
            or "источник"
        )

    if message.get("forward_sender_name"):
        return str(message["forward_sender_name"])
    if message.get("forward_from"):
        return _peer_label(message["forward_from"], "пользователь")
    if message.get("forward_from_chat"):
        return _peer_label(message["forward_from_chat"], "чат/канал")
    return "неизвестный источник"


def forwarded_origin_note(message):
    if not is_forwarded_message(message):
        return ""
    return f"[Пересланное сообщение от {forwarded_origin_label(message)}]"


def message_attachment_note(message):
    """Describe non-text Telegram content when App Server cannot attach it."""
    notes = []
    if message.get("photo"):
        notes.append("фото")

    document = message.get("document") or {}
    if document:
        filename = str(document.get("file_name") or "документ")
        mime = str(document.get("mime_type") or "").strip()
        notes.append(f"документ «{filename}»" + (f" ({mime})" if mime else ""))

    for field, label in (
        ("animation", "анимация/GIF"),
        ("video", "видео"),
        ("video_note", "видеосообщение"),
        ("voice", "голосовое сообщение"),
        ("audio", "аудио"),
        ("sticker", "стикер"),
        ("poll", "опрос"),
        ("contact", "контакт"),
        ("location", "геопозиция"),
        ("venue", "место"),
        ("dice", "кубик"),
        ("game", "игра"),
    ):
        if message.get(field):
            notes.append(label)
    return ", ".join(notes)


def add_attachment_failure(inputs, failures, exc):
    """Keep a failed attachment visible both to the user and to the agent."""
    failures.append(exc)
    detail = str(exc).removeprefix("Файл не скачан: ")
    inputs.append({
        "type": "text",
        "text": f"[Файл не скачан: {detail}. Не ищи его на диске.]",
    })


def message_inputs(message):
    """Build App Server inputs from text, forwards and image media.

    Bot API puts the original content of a forward in the same top-level
    ``text``/``caption``/media fields as an ordinary message.  The forward
    metadata is separate, so preserve a compact source note instead of
    treating a forward as an empty update.  For media that App Server cannot
    attach directly, pass a useful description so the bot still acknowledges
    the message rather than silently dropping it.
    """
    raw_text = (
        message.get("text")
        or message.get("caption")
        or rich_message_to_markdown(message.get("rich_message"))
        or ""
    )
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    origin_note = forwarded_origin_note(message)
    attachment_note = message_attachment_note(message)
    photo = message.get("photo")
    document = message.get("document") or {}
    has_image = bool(photo) or str(document.get("mime_type", "")).startswith("image/")

    text_parts = [part for part in (origin_note, text) if part]
    if attachment_note and not has_image:
        text_parts.append(f"[Вложение: {attachment_note}]")

    inputs = []
    paths = []
    failures = []
    if text_parts:
        inputs.append({"type": "text", "text": "\n\n".join(text_parts)})
    chat_id = message.get("chat", {}).get("id")
    if isinstance(photo, list) and photo:
        try:
            path = download_telegram_file(
                photo[-1]["file_id"], "photo.jpg", chat_id=chat_id,
                file_size=photo[-1].get("file_size"),
            )
            paths.append(path)
            inputs.append({"type": "localImage", "path": path})
        except AttachmentDownloadError as exc:
            add_attachment_failure(inputs, failures, exc)
    if str(document.get("mime_type", "")).startswith("image/") and document.get("file_id"):
        try:
            path = download_telegram_file(
                document["file_id"], document.get("file_name") or "image", chat_id=chat_id,
                file_size=document.get("file_size"),
            )
            paths.append(path)
            inputs.append({"type": "localImage", "path": path})
        except AttachmentDownloadError as exc:
            add_attachment_failure(inputs, failures, exc)

    if document and not has_image:
        mime = str(document.get("mime_type") or "").lower()
        name = str(document.get("file_name") or "document")
        if not document.get("file_id"):
            raise UnsupportedAttachmentError("Файл не прочитан: Telegram не передал его идентификатор.")
        # No format allowlist: the mime/suffix check below only decides HOW
        # a document is handed to the model (inlined as text vs. left as a
        # local path for Codex to read itself with its own tools), never
        # WHETHER it's accepted. A format Codex can't make sense of is its
        # own problem to report, same as any other unexpected input -- not
        # something to pre-reject here. The actual safety boundary for what
        # Codex can DO with an attachment is its sandbox mode, not this gate.
        textual = (mime.startswith("text/") or mime in {
            "application/json", "application/xml", "application/javascript",
            "application/x-javascript", "application/yaml", "text/markdown",
        } or Path(name).suffix.lower() in {
            ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".py", ".js",
            ".ts", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".sh", ".sql",
        })
        try:
            path = download_telegram_file(
                document["file_id"], name, chat_id=chat_id, file_size=document.get("file_size"),
            )
        except AttachmentDownloadError as exc:
            add_attachment_failure(inputs, failures, exc)
            path = None
        if path is None:
            return inputs, paths, failures
        paths.append(path)
        inlined = False
        if textual and Path(path).stat().st_size <= TEXT_DOCUMENT_MAX_BYTES:
            try:
                contents = Path(path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                contents = None
            if contents is not None:
                inputs.append({"type": "text", "text": f"[Документ {name}]\n{contents}"})
                inlined = True
        if not inlined:
            # Covers every non-textual format, plus a textual file too big
            # or too oddly encoded to inline -- Codex reads it itself
            # instead of the attachment being rejected outright.
            inputs.append({"type": "text", "text": (
                f"[Прикреплён файл {name}; локальный путь: {path}. "
                "Используй этот путь для чтения вложения.]"
            )})

    voice = message.get("voice") or message.get("audio")
    if voice:
        if not voice.get("file_id"):
            # Synthetic/legacy forwarded metadata can describe audio without
            # a downloadable file.  Preserve the attachment note already
            # added above rather than pretending it was transcribed.
            return inputs, paths, failures
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise UnsupportedAttachmentError(
                "Голосовые сообщения не поддерживаются на этой установке."
            ) from exc
        try:
            path = download_telegram_file(
                voice["file_id"], "voice.ogg", chat_id=chat_id, file_size=voice.get("file_size"),
            )
        except AttachmentDownloadError as exc:
            add_attachment_failure(inputs, failures, exc)
            return inputs, paths, failures
        paths.append(path)
        transcript = transcribe_voice(path)
        if not transcript:
            raise UnsupportedAttachmentError("Голосовое сообщение не удалось распознать и оно не прочитано.")
        inputs.append({"type": "text", "text": f"[Расшифровка голосового сообщения]\n{transcript}"})

    if not inputs and attachment_note:
        inputs.append({"type": "text", "text": "[Вложение: " + attachment_note + "]"})
    if not inputs and message.get("rich_message"):
        inputs.append({"type": "text", "text": "[Rich-сообщение без распознаваемого текста]"})
    if paths and not any(item.get("type") == "text" for item in inputs):
        prompt = "Посмотри на это изображение и ответь по контексту."
        if origin_note:
            prompt = f"{origin_note}\n\n{prompt}"
        inputs.insert(0, {"type": "text", "text": prompt})
    return inputs, paths, failures


def load_state():
    if not STATE_FILE.exists():
        return {"version": 2, "chats": {}, "runtime": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        if data.get("version") == 2 and isinstance(data.get("chats"), dict):
            data.setdefault("runtime", {})
            return data
        # One-time, lossless migration: the old global state belonged to the
        # owner. Additional users start with clean isolated entries.
        runtime_keys = ("restart_completed_chat_id", "restart_message_id")
        runtime = {key: data.pop(key) for key in runtime_keys if key in data}
        return {"version": 2, "chats": {str(OWNER_ID): data}, "runtime": runtime}
    except Exception as exc:
        raise SystemExit(f"codex-telegram-bot: cannot read state file {STATE_FILE}: {exc}") from exc


state_db = load_state()


def chat_state(chat_id):
    with state_lock:
        entry = state_db["chats"].setdefault(str(chat_id), {})
        entry.setdefault("thread_id", None)
        entry.setdefault("model", None)
        entry.setdefault("effort", None)
        entry.setdefault("sandbox", CODEX_SANDBOX)
        entry.setdefault("workspace", CODEX_CWD)
        entry.setdefault("last_usage", None)
        entry.setdefault("session_usage", None)
        entry.setdefault("context_window", None)
        entry.setdefault("account_status", "ready" if real_chat_id(chat_id) == OWNER_ID else None)
        entry.setdefault("pending_delegator_session_id", None)
        return entry


def update_state(chat_id=OWNER_ID, **values):
    with state_lock:
        state_db["chats"].setdefault(str(chat_id), {}).update(values)
        _save_state_locked()


def update_runtime_state(**values):
    with state_lock:
        state_db.setdefault("runtime", {}).update(values)
        _save_state_locked()


def write_last_turn(chat_id, text, delegated=False, ok=None):
    """Signal a completed turn's final text via a plain file instead of
    Telegram's getUpdates -- this process already owns that bot token's
    getUpdates stream exclusively (only one consumer can ever see a given
    update), so an external script polling the same API would just starve.
    Ordinary and delegated turns deliberately use different signal files;
    bridge_exec.py polls the delegated one."""
    filename = (
        f"last_turn_{STATE_INSTANCE_NAME}_delegate_{chat_id}.json"
        if delegated else f"last_turn_{chat_id}.json"
    )
    path = STATE_FILE.with_name(filename)
    tmp = path.with_suffix(".tmp")
    try:
        signal = {"text": text, "ts": time.time()}
        if ok is not None:
            signal["ok"] = bool(ok)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(signal, f)
        os.replace(tmp, path)
    except OSError as exc:
        log(f"write_last_turn failed: {exc}")


DELIVERY_MAX_ATTEMPTS = 8
DELIVERY_FIRST_RETRY_S = 30
_delivery_guard = threading.Lock()
_delivering_keys = set()


def _set_pending_delivery(state_key, chat_id, answer, delegated):
    # The turn itself makes the first attempt right away; the background
    # retry loop only picks the entry up once next_retry_at has passed.
    update_state(state_key, pending_delivery={
        "chat_id": int(chat_id), "text": answer, "delegated": bool(delegated),
        "parts": split_rich_text(answer), "next_part": 0,
        "attempts": 0, "next_retry_at": time.time() + DELIVERY_FIRST_RETRY_S,
    })


def _confirm_delivery(state_key, chat_id, answer, delegated):
    update_state(state_key, pending_delivery=None)
    write_last_turn(chat_id, answer, delegated=delegated, ok=True)


def _schedule_delivery_retry(state_key, delivery, parts, next_part):
    attempts = int(delivery.get("attempts") or 0) + 1
    chat_id, answer = delivery.get("chat_id"), delivery.get("text")
    delegated = bool(delivery.get("delegated"))
    if attempts >= DELIVERY_MAX_ATTEMPTS:
        log(f"Giving up delivering final answer for {state_key} after {attempts} attempts")
        update_state(state_key, pending_delivery=None)
        write_last_turn(chat_id, answer, delegated=delegated, ok=False)
        return
    update_state(state_key, pending_delivery=dict(
        delivery, parts=parts, next_part=next_part, attempts=attempts,
        next_retry_at=time.time() + min(300, 2 ** attempts),
    ))


def _deliver_pending(state_key, delivery, progress_message_id=None):
    """Deliver from the first unacknowledged rich chunk, never re-send an acked one.

    Only one delivery per state key runs at a time, so the background retry
    loop cannot duplicate an attempt the finishing turn is still making.
    """
    with _delivery_guard:
        if state_key in _delivering_keys:
            return {"ok": False, "in_flight": True, "description": "delivery already in progress"}
        _delivering_keys.add(state_key)
    try:
        return _deliver_pending_exclusive(state_key, delivery, progress_message_id)
    finally:
        with _delivery_guard:
            _delivering_keys.discard(state_key)


def _deliver_pending_exclusive(state_key, delivery, progress_message_id):
    chat_id, answer = delivery.get("chat_id"), delivery.get("text")
    if not isinstance(chat_id, int) or not isinstance(answer, str):
        update_state(state_key, pending_delivery=None)
        return {"ok": False, "description": "invalid pending delivery"}
    parts = delivery.get("parts")
    if not isinstance(parts, list) or not all(isinstance(part, str) for part in parts):
        parts = split_rich_text(answer)
    next_part = int(delivery.get("next_part") or 0)
    for index in range(next_part, len(parts)):
        if index == 0 and progress_message_id is not None:
            # The first chunk replaces the live progress card so it never
            # stays stuck showing an in-progress state.
            result = edit_rich(chat_id, progress_message_id, parts[0])
        else:
            result = send_rich(chat_id, parts[index])
        if not result.get("ok"):
            _schedule_delivery_retry(state_key, delivery, parts, index)
            return result
        delivery = dict(delivery, parts=parts, next_part=index + 1)
        if index + 1 < len(parts):
            update_state(state_key, pending_delivery=delivery)
    _confirm_delivery(state_key, chat_id, answer, bool(delivery.get("delegated")))
    return {"ok": True}


def retry_pending_deliveries():
    """Retry due finals that have never received a successful Telegram ack."""
    now = time.time()
    with state_lock:
        pending = [
            (key, dict(value.get("pending_delivery") or {}))
            for key, value in state_db.get("chats", {}).items()
            if isinstance(value.get("pending_delivery"), dict)
        ]
    for state_key, delivery in pending:
        if float(delivery.get("next_retry_at") or 0) > now:
            continue
        _deliver_pending(state_key, delivery)


def pending_delivery_watcher():
    while True:
        retry_pending_deliveries()
        time.sleep(1)


def _save_state_locked():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=STATE_FILE.name + ".", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state_db, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_FILE)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def save_thread_id(chat_id, thread_id):
    update_state(chat_id, thread_id=thread_id)


def compact(value, limit=1000):
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def mdv2_code_block(value):
    """Render safe MarkdownV2 pre content (backslash/backtick are special)."""
    content = str(value).replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{content}\n```"


def pretty_tool_value(value, limit=1200):
    """Human-sized tool input; never serialize an entire protocol envelope."""
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return compact(value, limit)
    try:
        return compact(json.dumps(value, ensure_ascii=False, indent=2), limit)
    except (TypeError, ValueError):
        return compact(str(value), limit)


def protocol_text(value):
    """Extract readable text from App Server text/summary values."""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, str):
                text = part
            elif isinstance(part, dict):
                text = part.get("text") or part.get("summary") or part.get("content") or ""
            else:
                text = str(part)
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return str(value)


def tool_result_text(value, limit=1600):
    """Extract useful text from MCP/dynamic output without IDs and metadata."""
    if not isinstance(value, dict):
        return pretty_tool_value(value, limit)
    content = value.get("content") or value.get("contentItems") or []
    texts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("outputText")
                if text:
                    texts.append(str(text))
    if texts:
        return compact("\n".join(texts), limit)
    error = value.get("error")
    if error:
        return pretty_tool_value(error, limit)
    # Do not fall back to the full result object: it commonly contains
    # structuredContent duplicates, resources, opaque IDs and base64 data.
    return "результат получен"


def truncate_mdv2(text, limit=MAX_MESSAGE_LEN):
    """Truncate without ever leaving a Telegram MarkdownV2 pre block open."""
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    if clipped.count("```") % 2:
        clipped = clipped[: max(0, limit - 4)].rstrip() + "\n```"
    return clipped


def item_label_and_blocks(item):
    item_type = item.get("type", "unknown")
    if item_type == "agent_message":
        return "💬 Ответ", protocol_text(item.get("text", "")), []
    if item_type == "reasoning":
        return "🧠 Размышление", protocol_text(
            item.get("text", item.get("summary", ""))
        ), []
    if item_type == "command_execution":
        command = item.get("command") or item.get("commandLine") or ""
        exit_code = item.get("exit_code")
        output = item.get("aggregated_output")
        results = []
        if output not in (None, ""):
            results.append(("📤 Результат", compact(str(output), 1800)))
        if exit_code is not None:
            try:
                succeeded = int(exit_code) == 0
            except (TypeError, ValueError):
                succeeded = False
            results.append(("✅ Код завершения" if succeeded else "❌ Код завершения", str(exit_code)))
        # Match Claude's live renderer: identify the concrete tool instead of
        # exposing a generic "Выполняю" status.
        return "🔧 Bash", compact(command, 1400), results
    if item_type == "file_change":
        # App Server includes the complete patch in changes[*].kind.diff.  A
        # Progress is a user-facing summary, not a debug console: exposing that payload can
        # fill the whole chat with escaped JSON and partially rendered code.
        changes = item.get("changes") or []
        if not isinstance(changes, list):
            changes = [changes]
        summaries = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("path")
            kind = change.get("kind")
            if isinstance(kind, dict):
                kind = kind.get("type")
            labels = {"add": "создан", "delete": "удалён", "update": "изменён"}
            if path:
                summaries.append(f"{path} — {labels.get(kind, kind or 'изменён')}")
        content = "\n".join(summaries) or str(item.get("path") or "файл изменён")
        return "📝 Изменение файла", compact(content, 1200), []
    if item_type == "web_search":
        query = item.get("query") or "поиск"
        action = item.get("action") or {}
        action_type = action.get("type") if isinstance(action, dict) else None
        labels = {"openPage": "открываю страницу", "findInPage": "ищу на странице",
                  "search": "ищу в интернете"}
        suffix = labels.get(action_type)
        content = f"{suffix}: {query}" if suffix else str(query)
        return "🔎 Поиск", compact(content, 1000), []
    if item_type == "mcp_tool_call":
        name = ".".join(filter(None, (item.get("server"), item.get("tool")))) or "MCP"
        arguments = pretty_tool_value(item.get("arguments"))
        results = []
        if item.get("error"):
            results.append(("❌ Ошибка", pretty_tool_value(item["error"], 1200)))
        elif item.get("result") is not None:
            results.append(("📤 Результат", tool_result_text(item["result"])))
        return f"🔧 {name}", arguments, results
    if item_type == "dynamic_tool_call":
        name = ".".join(filter(None, (item.get("namespace"), item.get("tool")))) or "инструмент"
        arguments = pretty_tool_value(item.get("arguments"))
        results = []
        if item.get("contentItems") is not None:
            results.append(("📤 Результат", tool_result_text(
                {"contentItems": item.get("contentItems")}
            )))
        return f"🔧 {name}", arguments, results
    if item_type in ("collab_agent_tool_call", "sub_agent_activity"):
        tool = item.get("tool") or item.get("kind") or "работа агента"
        prompt = item.get("prompt")
        states = item.get("agentsStates") or {}
        state_text = ", ".join(
            str(value.get("status") if isinstance(value, dict) else value)
            for value in states.values()
        )
        content = pretty_tool_value(prompt, 1000) or state_text or str(tool)
        return f"🤖 Агент · {tool}", content, []
    if item_type == "image_view":
        return "🖼 Просмотр изображения", compact(item.get("path") or "изображение", 1000), []
    if item_type == "image_generation":
        failure = item.get("failure")
        return "🎨 Генерация изображения", (
            pretty_tool_value(failure, 1000) if failure else "изображение создаётся"
        ), []
    if item_type == "context_compaction":
        return "🗜 Сжатие контекста", "контекст сессии сжат", []
    if item_type == "plan":
        return "📋 План", compact(item.get("text") or "план обновлён", 1400), []
    if item_type == "sleep":
        seconds = (item.get("durationMs") or 0) / 1000
        return "⏳ Ожидание", f"{seconds:g} с", []
    if item_type in ("entered_review_mode", "exited_review_mode"):
        text = "режим проверки включён" if item_type.startswith("entered") else "режим проверки завершён"
        return "🔍 Проверка", text, []
    # Future App Server item types must degrade to a short label. Never put
    # the complete protocol object into Telegram: it may contain huge output,
    # patches, base64 media, internal IDs or other implementation details.
    # Unknown/future protocol items still get a useful, neutral label.  Do
    # not leak the product name or invent a fake "action"/"in progress"
    # payload when the protocol did not provide one.
    return "🔧 Инструмент", str(item_type).replace("_", " "), []


def normalize_app_item(item):
    """Convert App Server camelCase thread items to the existing renderer shape."""
    if not isinstance(item, dict):
        return {"type": "unknown", "value": item}
    result = dict(item)
    type_map = {
        "agentMessage": "agent_message",
        "commandExecution": "command_execution",
        "fileChange": "file_change",
        "mcpToolCall": "mcp_tool_call",
        "dynamicToolCall": "dynamic_tool_call",
        "webSearch": "web_search",
        "imageView": "image_view",
        "collabAgentToolCall": "collab_agent_tool_call",
        "contextCompaction": "context_compaction",
        "subAgentActivity": "sub_agent_activity",
        "imageGeneration": "image_generation",
        "enteredReviewMode": "entered_review_mode",
        "exitedReviewMode": "exited_review_mode",
    }
    result["type"] = type_map.get(result.get("type"), result.get("type", "unknown"))
    if result["type"] == "reasoning":
        summary = result.get("summary") or result.get("content") or []
        result["text"] = protocol_text(summary)
    result["aggregated_output"] = result.get("aggregatedOutput")
    result["exit_code"] = result.get("exitCode")
    if result["type"] == "file_change":
        result["changes"] = result.get("changes", [])
    return result


def user_facing_codex_error(error):
    if not isinstance(error, dict):
        text = str(error)
        info = None
    else:
        text = str(error.get("message") or "Неизвестная ошибка Codex")
        info = error.get("codexErrorInfo")
    combined = f"{info or ''} {text}".lower()
    if "contextwindowexceeded" in combined or "context window" in combined:
        return (
            "Контекст текущей сессии исчерпан. Используй /compact, чтобы сжать "
            "историю и продолжить, либо /new для новой сессии."
        )
    if "sessionbudgetexceeded" in combined:
        return "Бюджет этой сессии исчерпан. Начни новую через /new."
    if "usagelimitexceeded" in combined:
        return usage_limit_exceeded_message()
    return compact(text, 1000)


def usage_limit_exceeded_message(runtime=None):
    with process_lock:
        limits = dict((runtime.last_rate_limits if runtime else None) or {})
    windows = [
        ("5-часовой лимит", limits.get("primary")),
        ("недельный лимит", limits.get("secondary")),
    ]
    reached = [(label, window) for label, window in windows
               if isinstance(window, dict) and int(window.get("usedPercent") or 0) >= 100]
    if not reached:
        available = [(label, window) for label, window in windows if isinstance(window, dict)]
        reached = sorted(
            available, key=lambda pair: int(pair[1].get("usedPercent") or 0), reverse=True
        )[:1]
    if not reached:
        return "Лимит сессии Codex исчерпан. Попробуй позже; актуальное состояние — /usage."
    details = "; ".join(
        f"{label}, сброс {format_reset_time(window.get('resetsAt'))}"
        for label, window in reached
    )
    return f"⏳ Лимит сессии Codex исчерпан: {details}. После сброса можно продолжить этот же тред."


def render_process_item(item):
    label, content, results = item_label_and_blocks(item)
    if item.get("type") in ("reasoning", "agent_message") and not str(content).strip():
        return ""
    lines = [f"{label}:", mdv2_code_block(content)]
    for result_label, result_content in results:
        lines.extend((f"{result_label}:", mdv2_code_block(result_content)))
    return "\n".join(lines)


def format_usage(usage):
    if not isinstance(usage, dict):
        return compact(usage, 300)
    parts = []
    for key, label in (("input_tokens", "in"), ("cached_input_tokens", "cached"),
                       ("output_tokens", "out"),
                       ("reasoning_output_tokens", "reasoning")):
        if key in usage:
            parts.append(f"{label}: {usage[key]}")
    return ", ".join(parts) if parts else compact(usage, 300)


def send_plain(chat_id, text):
    text = text or "(пусто)"
    last = None
    while text:
        part, text = text[:MAX_MESSAGE_LEN], text[MAX_MESSAGE_LEN:]
        last = tg_call("sendMessage", {"chat_id": chat_id, "text": part})
        if _rate_limited(last):
            break
    return last


_whisper_model = None
_whisper_lock = threading.Lock()


def transcribe_voice(path):
    """Lazily transcribe voice only when faster-whisper is installed."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _info = _whisper_model.transcribe(path, beam_size=5)
        return " ".join(segment.text.strip() for segment in segments).strip()


def edit_plain(chat_id, message_id, text):
    return tg_call("editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": text[:MAX_MESSAGE_LEN]
    })


def split_rich_text(markdown_text, limit=RICH_MAX_CHARS):
    """Split final rich text at paragraphs/lines while balancing fenced code."""
    text = str(markdown_text or "")
    if len(text) <= limit:
        return [text]
    parts, current, in_fence = [], "", False
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            pieces = [line[index:index + max(1, limit - 8)]
                      for index in range(0, len(line), max(1, limit - 8))]
        else:
            pieces = [line]
        for piece in pieces:
            closing = "\n```" if in_fence else ""
            opening = "```\n" if in_fence else ""
            if current and len(current) + len(piece) + len(closing) > limit:
                parts.append((current.rstrip() + closing).rstrip())
                current = opening
            current += piece
            if piece.count("```") % 2:
                in_fence = not in_fence
    if current:
        parts.append((current.rstrip() + ("\n```" if in_fence else "")).rstrip())
    return parts


def send_rich(chat_id, markdown_text):
    last = {"ok": True}
    for text in split_rich_text(markdown_text):
        last = tg_call("sendRichMessage", {
            "chat_id": chat_id, "rich_message": {"markdown": text}
        })
        if not last.get("ok"):
            if not _rate_limited(last):
                return send_plain(chat_id, text)
            return last
    return last


def send_document(chat_id, path, caption=""):
    """Upload one local document through the bridge-owned Telegram token."""
    global rate_limit_until
    source = Path(path)
    size = source.stat().st_size
    if size > FILE_SEND_MAX_BYTES:
        return {
            "ok": False,
            "description": f"Файл больше лимита {FILE_SEND_MAX_BYTES} байт.",
        }
    if LOCAL_BOT_API:
        local_result = tg_call("sendDocument", {
            "chat_id": chat_id,
            "caption": caption[:FILE_SEND_MAX_CAPTION_CHARS],
            "document": source.resolve().as_uri(),
        }, timeout=SEND_TIMEOUT_S)
        if local_result.get("ok"):
            return local_result
        # Retry only a confirmed Bot API rejection. A timeout or other
        # transport failure leaves the outcome unknown: the local server may
        # still finish sending the file, so retrying could duplicate it.
        locally_suppressed = (
            local_result.get("description") == "locally suppressed during Telegram rate limit"
        )
        if ("description" not in local_result and "error_code" not in local_result) or locally_suppressed:
            if locally_suppressed:
                return local_result
            detail = str(local_result.get("error") or "неизвестная ошибка связи")
            return {
                "ok": False,
                "error": local_result.get("error"),
                "description": (
                    "Не удалось подтвердить отправку файла: ошибка связи или таймаут "
                    f"({detail}). Файл не был отправлен повторно, чтобы избежать дубля."
                ),
            }
        # A local Bot API can reject file:// when the bot and server do not
        # share a mount. Its documented fallback is the normal upload.
        log(f"Telegram local sendDocument rejected file://; retrying multipart: {local_result}")
    with telegram_lock:
        now = time.monotonic()
        if now < rate_limit_until:
            remaining = max(1, int(rate_limit_until - now + 0.999))
            return {
                "ok": False,
                "error_code": 429,
                "description": "locally suppressed during Telegram rate limit",
                "parameters": {"retry_after": remaining},
            }

    boundary = f"----CodexTelegram{time.time_ns()}"
    body = bytearray()
    for key, value in (("chat_id", str(chat_id)), ("caption", caption[:FILE_SEND_MAX_CAPTION_CHARS])):
        if not value:
            continue
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    filename = source.name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
    )
    body.extend(f"Content-Type: {mime}\r\n\r\n".encode())
    with source.open("rb") as handle:
        body.extend(handle.read())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"{API_BASE}/sendDocument",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=SEND_TIMEOUT_S if LOCAL_BOT_API else HTTP_TIMEOUT_S,
        ) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {"ok": False, "error": str(exc)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    if not result.get("ok"):
        if _rate_limited(result):
            retry_after = (result.get("parameters") or {}).get("retry_after")
            try:
                delay = max(1.0, float(retry_after))
            except (TypeError, ValueError):
                delay = 1.0
            with telegram_lock:
                rate_limit_until = max(rate_limit_until, time.monotonic() + delay)
        log(f"Telegram sendDocument not ok: {result}")
    return result


def _persona_path(chat_id):
    return tenant_codex_home(chat_id) / "AGENTS.md"


def _write_persona(path, contents):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".persona.", dir=path.parent, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _remember_persona_message(chat_id, result):
    if result.get("ok"):
        message_id = (result.get("result") or {}).get("message_id")
        if isinstance(message_id, int):
            persona_message_ids.setdefault(int(chat_id), set()).add(message_id)


def send_persona(chat_id):
    """Send a one-message persona snapshot, or a Markdown document if needed."""
    contents = _persona_path(chat_id).read_text(encoding="utf-8")
    if len(contents) <= MAX_MESSAGE_LEN:
        result = send_plain(chat_id, contents)
    else:
        fd, temporary = tempfile.mkstemp(prefix="persona-", suffix=".md", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(contents)
            result = send_document(chat_id, temporary, caption="Текущая персона")
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    _remember_persona_message(chat_id, result)
    return result


def handle_persona_reply(message):
    """Consume a reply to a /persona snapshot before normal Codex routing."""
    chat_id = message.get("chat", {}).get("id")
    if chat_id != OWNER_ID:
        return False
    reply = message.get("reply_to_message") or {}
    reply_id = reply.get("message_id") or message.get("reply_to_message_id")
    if reply_id not in persona_message_ids.get(chat_id, set()):
        return False
    document = message.get("document") or {}
    if document:
        try:
            local_path = download_telegram_file(
                document["file_id"], document.get("file_name") or "persona.md",
                chat_id=chat_id, file_size=document.get("file_size"),
            )
            contents = Path(local_path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            send_plain(chat_id, "Файл персоны должен быть текстовым UTF-8 Markdown-файлом.")
            return True
        except Exception as exc:
            send_plain(chat_id, f"Не удалось прочитать файл персоны: {compact(str(exc), 500)}")
            return True
    else:
        contents = message.get("text")
        if not isinstance(contents, str):
            send_plain(chat_id, "Пришли текст персоны или UTF-8 Markdown-файл ответом на сообщение.")
            return True
    if not contents.strip():
        send_plain(chat_id, "Пустая персона не сохранена.")
        return True
    _write_persona(_persona_path(chat_id), contents)
    send_plain(chat_id, "✅ Персона обновлена.")
    return True


def send_photo(chat_id, path, caption=""):
    """Upload an App Server-generated image as a Telegram photo.

    Telegram previews photos inline, which is the expected result of the
    image-generation tool. Oversized outputs still arrive as documents
    rather than disappearing.
    """
    source = Path(path)
    if not source.is_file():
        return {"ok": False, "description": "Файл изображения не найден."}
    if source.stat().st_size > PHOTO_SEND_MAX_BYTES:
        return send_document(chat_id, source, caption)

    boundary = f"----CodexTelegramPhoto{time.time_ns()}"
    body = bytearray()
    for key, value in (("chat_id", str(chat_id)), ("caption", caption[:FILE_SEND_MAX_CAPTION_CHARS])):
        if not value:
            continue
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    filename = source.name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    mime = mimetypes.guess_type(filename)[0] or "image/png"
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode())
    body.extend(f"Content-Type: {mime}\r\n\r\n".encode())
    with source.open("rb") as handle:
        body.extend(handle.read())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"{API_BASE}/sendPhoto", data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {"ok": False, "error": str(exc)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    if not result.get("ok"):
        log(f"Telegram sendPhoto not ok: {result}")
    return result


def _write_file_send_result(request_id, ok, text):
    FILE_SEND_RESULT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = FILE_SEND_RESULT_DIR / f"{request_id}.json"
    temporary = FILE_SEND_RESULT_DIR / f".{request_id}.{os.getpid()}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump({"done": True, "ok": bool(ok), "text": text}, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _file_in_tenant_outbox(chat_id, path):
    try:
        source = Path(path).resolve(strict=True)
        source.relative_to(tenant_file_outbox(chat_id).resolve())
    except (OSError, ValueError):
        return None
    return source if source.is_file() else None


def process_file_send_queue():
    """Send approved outbox files requested through tenant MCP processes."""
    FILE_SEND_QUEUE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for request_path in sorted(FILE_SEND_QUEUE_DIR.glob("*.json")):
        request_id = request_path.stem
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception as exc:
            log(f"Could not read file-send request {request_id}: {exc}")
            request = None
        try:
            request_path.unlink()
        except FileNotFoundError:
            pass

        ok = False
        if not isinstance(request, dict):
            message = "Отклонено: повреждённый запрос отправки файла."
        else:
            chat_id = request.get("chat_id")
            path = request.get("path")
            caption = request.get("caption", "")
            if not isinstance(chat_id, int) or isinstance(chat_id, bool):
                message = "Отклонено: некорректный Telegram chat_id."
            elif str(chat_id) not in load_whitelist():
                message = "Отклонено: Telegram ID отсутствует в whitelist."
            elif not isinstance(path, str) or not isinstance(caption, str):
                message = "Отклонено: некорректный путь или подпись."
            elif len(caption) > FILE_SEND_MAX_CAPTION_CHARS:
                message = "Отклонено: подпись длиннее лимита Telegram."
            else:
                source = _file_in_tenant_outbox(chat_id, path)
                if source is None:
                    message = "Отклонено: файл должен быть обычным файлом из CODEX_TELEGRAM_OUTBOX."
                elif source.stat().st_size > FILE_SEND_MAX_BYTES:
                    message = f"Отклонено: файл больше {FILE_SEND_MAX_BYTES} байт."
                else:
                    result = send_document(chat_id, source, caption)
                    ok = bool(result.get("ok"))
                    if ok:
                        message = f"Файл «{source.name}» отправлен в Telegram."
                    else:
                        message = "Telegram не принял файл: " + compact(
                            str(result.get("description") or result.get("error") or result), 500,
                        )
        try:
            _write_file_send_result(request_id, ok, message)
        except Exception as exc:
            log(f"Could not write file-send result {request_id}: {exc}")


def file_send_queue_watcher():
    """Keep MCP file delivery responsive while Telegram is long-polling."""
    while True:
        process_file_send_queue()
        time.sleep(1)


def edit_rich(chat_id, message_id, markdown_text):
    chunks = split_rich_text(markdown_text)
    text = chunks[0]
    params = {
        "chat_id": chat_id, "message_id": message_id,
        "rich_message": {"markdown": text},
    }
    result = tg_call("editMessageText", params)
    # A live progress edit can race Telegram's per-message rate limit. Wait
    # for the server-provided window and retry the SAME edit before allowing
    # the caller to use its last-resort send path; otherwise a transient 429
    # leaves the old Thinking card and creates a duplicate final message.
    if _rate_limited(result):
        retry_after = (result.get("parameters") or {}).get("retry_after")
        try:
            time.sleep(min(60.0, max(1.0, float(retry_after))))
        except (TypeError, ValueError):
            time.sleep(1.0)
        result = tg_call("editMessageText", params)
    if not result.get("ok") and not _rate_limited(result):
        description = str(result.get("description", "")).lower()
        if "not modified" not in description:
            return edit_plain(chat_id, message_id, text)
    if result.get("ok"):
        for chunk in chunks[1:]:
            result = send_rich(chat_id, chunk)
            if not result.get("ok"):
                return result
    return result


class TurnView:
    def __init__(self, chat_id, state_key=None, progress_msg_id=None):
        self.chat_id = int(chat_id)
        self.state_key = str(state_key if state_key is not None else self.chat_id)
        self.delegated = self.state_key != str(self.chat_id)
        self.items = []
        self.process_items = []
        self.generated_image_paths = []
        self.current_thought = None
        self.current_thought_id = None
        self.current_tool = None
        self.last_edit_at = 0.0
        self.usage = None
        self.completed = False
        self.context_notice = None
        # A real Telegram message is the live process card.  It is edited in
        # place as App Server events arrive, like the Claude bridge; this is
        # deliberately not Telegram's ephemeral sendMessageDraft API.
        # A caller may hand in an already-sent message (the local-mode
        # "Загружаю вложение…" status bubble) to edit into place instead of
        # sending a fresh one -- see run_turn/flush_pending_batch.
        self.progress_msg_id = progress_msg_id
        self.progress_attempted = progress_msg_id is not None
        self.progress_lock = threading.Lock()

    @staticmethod
    def _thought_id(item):
        return item.get("id") or item.get("itemId") or item.get("item_id")

    def _set_thought(self, item):
        text = protocol_text(item.get("text", item.get("summary", "")))
        if not text.strip():
            return False
        self.current_thought = text
        self.current_thought_id = self._thought_id(item)
        # A new thought starts the next model phase.  Until this point the
        # previous thought remains visible while a following tool runs.
        self.current_tool = None
        return True

    def add_thought_delta(self, delta, item_id=None):
        delta = protocol_text(delta)
        if not delta:
            return
        # App Server normally supplies itemId.  If an older server omits it,
        # the presence of a tool is enough to identify this as the next
        # thought phase.  Subsequent deltas then append to the same thought.
        if item_id and item_id != self.current_thought_id:
            self.current_thought = ""
            self.current_tool = None
            self.current_thought_id = item_id
        elif self.current_tool is not None:
            self.current_thought = ""
            self.current_tool = None
            self.current_thought_id = item_id
        self.current_thought = (self.current_thought or "") + delta

    def add_event(self, event):
        event_type = event.get("type", "unknown")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                save_thread_id(self.state_key, thread_id)
        elif event_type == "turn.started":
            pass
        elif event_type in ("item.started", "item.updated"):
            item = event.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in ("agent_message", "reasoning"):
                    self._set_thought(item)
                else:
                    self.current_tool = item
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                self.items.append(item)
                item_type = item.get("type")
                if item_type in ("agent_message", "reasoning"):
                    self._set_thought(item)
                else:
                    self.current_tool = item
                self.process_items.append(item)
                if item_type == "image_generation":
                    # App Server calls this savedPath; accept snake_case too
                    # so the bridge remains compatible with older servers.
                    image_path = item.get("savedPath") or item.get("saved_path")
                    if isinstance(image_path, str) and image_path not in self.generated_image_paths:
                        self.generated_image_paths.append(image_path)
        elif event_type == "turn.completed":
            self.completed = True
            self.usage = event.get("usage")
            if isinstance(self.usage, dict):
                update_state(self.state_key, last_usage=self.usage)

    def live_text(self):
        lines = []
        if self.current_thought:
            lines.append(escape_mdv2(self.current_thought))
        if self.current_tool:
            label, content, results = item_label_and_blocks(self.current_tool)
            lines.append(escape_mdv2(f"{label}:"))
            if content:
                lines.append(mdv2_code_block(content))
            for result_label, result_content in results:
                lines.extend((escape_mdv2(f"{result_label}:"),
                              mdv2_code_block(result_content)))
        body = "\n".join(lines) if lines else "Думаю"
        return f"🤔 {body}"

    def _send_or_edit_live(self, text, force=False):
        """Publish one persistent process message and update it in place."""
        with self.progress_lock:
            now = time.monotonic()
            if (
                self.progress_msg_id is not None
                and not force
                and now - self.last_edit_at < EDIT_THROTTLE_S
            ):
                return
            if self.progress_msg_id is None:
                # A failed first send must not cause a Telegram request for
                # every token delta.  A later forced flush can retry once.
                if self.progress_attempted and not force:
                    return
                self.progress_attempted = True
                result = tg_call("sendMessage", {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                })
                if not result.get("ok") and not _rate_limited(result):
                    result = tg_call("sendMessage", {
                        "chat_id": self.chat_id,
                        "text": strip_mdv2(text).replace("```", ""),
                    })
                if result.get("ok"):
                    self.progress_msg_id = (result.get("result") or {}).get("message_id")
            else:
                params = {
                    "chat_id": self.chat_id,
                    "message_id": self.progress_msg_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                }
                result = tg_call("editMessageText", params)
                if not result.get("ok"):
                    description = str(result.get("description", "")).lower()
                    if "not modified" not in description and not _rate_limited(result):
                        params["text"] = strip_mdv2(text).replace("```", "")
                        params.pop("parse_mode", None)
                        tg_call("editMessageText", params)
            self.last_edit_at = now

    def flush(self, force=False):
        """Render the current thought/tool snapshot into one live message."""
        now = time.monotonic()
        if not force and self.progress_msg_id is not None:
            if now - self.last_edit_at < EDIT_THROTTLE_S:
                return
        try:
            self._send_or_edit_live(truncate_mdv2(self.live_text()), force=force)
        except Exception as exc:
            # Telegram hiccups must never stop App Server event consumption.
            log(f"Live progress update failed: {exc}")

    def replace_progress(self, text):
        """Replace the live process card in place, returning success."""
        with self.progress_lock:
            if self.progress_msg_id is None:
                return False
            result = edit_rich(self.chat_id, self.progress_msg_id, text)
            return bool(result and result.get("ok"))

    def deliver(self, stopped=False, error=None):
        final_index = next(
            (i for i in range(len(self.items) - 1, -1, -1)
             if self.items[i].get("type") == "agent_message"), None
        )
        final_text = (
            protocol_text(self.items[final_index].get("text", ""))
            if final_index is not None else ""
        )

        process_items = list(self.process_items)
        if final_index is not None:
            final_item = self.items[final_index]
            for i in range(len(process_items) - 1, -1, -1):
                if process_items[i] is final_item:
                    del process_items[i]
                    break

        if stopped:
            answer = "⏹ Выполнение остановлено."
        elif error:
            answer = f"⚠️ Ошибка Codex: {error}"
        else:
            answer = final_text or "(нет ответа — смотри процесс выше)"
        if self.usage is not None:
            answer += f"\n\nТокены: {format_usage(self.usage)}"
        if self.context_notice:
            answer += f"\n\n{self.context_notice}"
        with state_lock:
            thread_id = chat_state(self.state_key).get("thread_id")
            prior_thread_id = chat_state(self.state_key).get("pending_delegator_session_id")
        # Delegation-only footer, not a per-message one (per the owner
        # directly): only present when this turn was actually kicked off
        # via external_request_watcher(), and one-shot -- cleared right
        # after use so it doesn't leak onto the next, ordinary turn.
        # prior_thread_id is the OWNER's own thread from before this
        # delegated call touched anything (empty string "" if there wasn't
        # one) -- so they can return to their own conversation, separate
        # from `thread_id` below, which is whatever this delegated task
        # itself ended up using.
        if prior_thread_id is not None and thread_id:
            if prior_thread_id:
                answer += (
                    f"\n\nТвой session id (до делегации): `{prior_thread_id[:8]}`. "
                    f"Продолжить делегированную: `/resume {thread_id[:8]}`"
                )
            else:
                answer += f"\n\nПродолжить делегированную сессию: `/resume {thread_id[:8]}`"
            # The old owner-key delegation path is live only for this one
            # turn -- preserve c130528's rollback so the next ordinary
            # Telegram message continues the owner's own conversation.
            # A delegate has its own state key, so it keeps its own thread.
            if self.delegated:
                update_state(self.state_key, pending_delegator_session_id=None)
            else:
                update_state(
                    self.state_key,
                    pending_delegator_session_id=None,
                    thread_id=prior_thread_id or None,
                    last_usage=None,
                    session_usage=None,
                    context_window=None,
                )
        _set_pending_delivery(self.state_key, self.chat_id, answer, self.delegated)
        with state_lock:
            delivery = dict(chat_state(self.state_key).get("pending_delivery") or {})

        if process_items:
            process_steps = [
                step for step in (render_process_item(item) for item in process_items)
                if step
            ]
        else:
            process_steps = []
        if process_steps:
            closing_reserve = 100
            visible = []
            used = 0
            for step in reversed(process_steps):
                cost = len(step) + 1
                if visible and used + cost > RICH_MAX_CHARS - closing_reserve:
                    break
                visible.append(step[: RICH_MAX_CHARS - closing_reserve])
                used += cost
            visible.reverse()
            hidden = len(process_steps) - len(visible)
            if hidden:
                visible.insert(0, f"…и ещё {hidden} шагов выше…")
            body = "\n".join(visible)
            rich = f"<details><summary>🔧 Процесс ({len(process_steps)})</summary>\n{body}\n</details>"
            if not self.replace_progress(rich):
                send_rich(self.chat_id, rich)
            # A genuinely NEW message here is deliberate: an edit doesn't
            # push a Telegram notification, a fresh send does. Reverted
            # 2026-08-30 -- merging the process block and answer into one
            # edited card (matching a since-reverted change on the Claude
            # bridge side) silently killed the "your answer is ready"
            # notification for every tool-using turn.
            result = _deliver_pending(self.state_key, delivery)
        elif self.progress_msg_id is not None:
            result = _deliver_pending(self.state_key, delivery, self.progress_msg_id)
        else:
            result = _deliver_pending(self.state_key, delivery)
        if result.get("ok"):
            for image_path in self.generated_image_paths:
                image_result = send_photo(self.chat_id, image_path)
                if not image_result.get("ok"):
                    log(f"Could not deliver generated image {image_path}: {image_result}")
        if not result.get("ok") and not result.get("in_flight"):
            write_last_turn(self.chat_id, answer, delegated=self.delegated, ok=False)


def codex_process_env(runtime, extra_env=None):
    # Keep the system/Codex environment intact, but never inherit bot secrets
    # or control-plane variables from the bridge process.
    env = {
        key: value for key, value in os.environ.items()
        if key != "TELEGRAM_BOT_TOKEN" and not key.startswith("CODEX_BOT_")
    }
    codex_home = tenant_codex_home(runtime.chat_id, state_key=runtime.state_key)
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    env["CODEX_TELEGRAM_OUTBOX"] = str(tenant_file_outbox(runtime.chat_id))
    if extra_env:
        env.update(extra_env)
        if codex_home is not None:
            env["CODEX_HOME"] = str(codex_home)
    return env


def get_app_server(runtime):
    with process_lock:
        if runtime.app_server is None:
            extra_env = runtime.pending_env
            runtime.pending_env = None
            env = codex_process_env(runtime, extra_env=extra_env)
            runtime.close_app_server_after_turn = bool(extra_env)
            runtime.app_server = AppServerClient(
                lambda method, params: handle_app_notification(runtime, method, params),
                lambda message: log(f"tenant={runtime.chat_id} {message}"),
                env=env,
            )
        return runtime.app_server


def _usage_breakdown(usage):
    return {
        "input_tokens": usage.get("inputTokens", 0),
        "cached_input_tokens": usage.get("cachedInputTokens", 0),
        "cache_write_input_tokens": usage.get("cacheWriteInputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "reasoning_output_tokens": usage.get("reasoningOutputTokens", 0),
        "total_tokens": usage.get("totalTokens", 0),
    }


def _usage_for_renderer(token_usage):
    return _usage_breakdown((token_usage or {}).get("last") or {})


def handle_app_notification(runtime, method, params):
    if method == "account/rateLimits/updated":
        update = params.get("rateLimits") or {}
        with process_lock:
            merged = dict(runtime.last_rate_limits or {})
            for key, value in update.items():
                if value is not None:
                    merged[key] = value
            runtime.last_rate_limits = merged
        return
    if method == "account/login/completed":
        success = bool(params.get("success"))
        if success and runtime.chat_id != OWNER_ID:
            update_state(runtime.state_key, account_status="awaiting_display_name")
            send_plain(
                runtime.chat_id,
                "✅ Вход в аккаунт Codex завершён.\n\nКак к тебе обращаться?",
            )
        elif success:
            update_state(runtime.state_key, account_status="ready")
            send_plain(runtime.chat_id, "✅ Вход в аккаунт Codex завершён.")
        else:
            update_state(runtime.state_key, account_status="login_failed")
            send_plain(runtime.chat_id, f"❌ Вход не завершён: {params.get('error') or 'неизвестная ошибка'}")
        return
    with process_lock:
        view = runtime.active_view
        done = runtime.active_done
        if view is None:
            return
        event_turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if runtime.active_turn_id and event_turn_id and event_turn_id != runtime.active_turn_id:
            return
        runtime.active_last_event_at = time.monotonic()
        if event_turn_id:
            runtime.active_turn_id = event_turn_id
        if params.get("threadId"):
            runtime.active_thread_id = params["threadId"]

    force = False
    if method in ("item/started", "item/updated", "item/completed"):
        item = normalize_app_item(params.get("item"))
        # User messages and internal hook prompts are protocol bookkeeping,
        # not model actions. Rendering them exposed raw JSON after a mid-turn
        # message was injected.
        if item.get("type") not in ("userMessage", "hookPrompt"):
            event_type = {
                "item/started": "item.started",
                "item/updated": "item.updated",
                "item/completed": "item.completed",
            }[method]
            view.add_event({"type": event_type, "item": item})
            if item.get("type") == "context_compaction" and method.endswith("completed"):
                view.context_notice = "🗜 Контекст сессии автоматически сжат."
    elif method == "item/agentMessage/delta":
        view.add_thought_delta(
            params.get("delta", ""),
            params.get("itemId") or params.get("item_id") or params.get("id"),
        )
    elif method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage") or {}
        view.usage = _usage_for_renderer(token_usage)
        context_window = token_usage.get("modelContextWindow")
        context_tokens = view.usage.get("input_tokens") or 0
        if context_window and context_tokens:
            ratio = context_tokens / context_window
            if ratio >= 0.9:
                view.context_notice = (
                    f"⚠️ Контекст заполнен на {ratio:.0%}. Рекомендуется /compact или /new."
                )
            elif ratio >= 0.8:
                view.context_notice = (
                    f"⚠️ Контекст заполнен на {ratio:.0%}; скоро понадобится /compact."
                )
        update_state(
            runtime.state_key,
            last_usage=view.usage,
            session_usage=_usage_breakdown(token_usage.get("total") or {}),
            context_window=token_usage.get("modelContextWindow"),
        )
    elif method == "error" and not params.get("willRetry"):
        with process_lock:
            error = params.get("error", "неизвестная ошибка")
            if isinstance(error, dict) and error.get("codexErrorInfo") == "usageLimitExceeded":
                runtime.active_error = usage_limit_exceeded_message(runtime)
            else:
                runtime.active_error = user_facing_codex_error(error)
    elif method == "thread/compacted":
        view.context_notice = "🗜 Контекст сессии сжат."
        force = True
        if done is not None:
            done.set()
    elif method == "turn/completed":
        turn = params.get("turn") or {}
        if turn.get("error"):
            with process_lock:
                turn_error = turn["error"]
                if (isinstance(turn_error, dict)
                        and turn_error.get("codexErrorInfo") == "usageLimitExceeded"):
                    runtime.active_error = usage_limit_exceeded_message(runtime)
                else:
                    runtime.active_error = user_facing_codex_error(turn_error)
        view.completed = True
        force = True
        if done is not None:
            done.set()
    view.flush(force=force)


def _thread_params(runtime, thread_id=None):
    with state_lock:
        snapshot = dict(chat_state(runtime.state_key))
    params = {
        "cwd": snapshot.get("workspace") or CODEX_CWD,
        "sandbox": snapshot.get("sandbox") or CODEX_SANDBOX,
        "approvalPolicy": "never",
    }
    if snapshot.get("model"):
        params["model"] = snapshot["model"]
    if thread_id:
        params["threadId"] = thread_id
    return params


def available_models(runtime):
    """Return the visible live model catalog advertised by App Server."""
    result = get_app_server(runtime).request(
        "model/list", {"limit": 100, "includeHidden": False}, timeout=30,
    ) or {}
    return [model for model in result.get("data", [])
            if isinstance(model, dict) and not model.get("hidden")]


def model_key(model):
    return str(model.get("model") or model.get("id") or "")


def resolve_model_choice(models, value):
    wanted = str(value or "").strip().lower()
    if not wanted:
        return None
    return next((model for model in models if wanted in {
        model_key(model).lower(),
        str(model.get("id") or "").lower(),
        str(model.get("displayName") or "").lower(),
    }), None)


def effort_options(model):
    return [option for option in model.get("supportedReasoningEfforts", [])
            if isinstance(option, dict) and option.get("reasoningEffort")]


def supported_reasoning_efforts(model):
    return [option.get("reasoningEffort") for option in
            model.get("supportedReasoningEfforts", []) if isinstance(option, dict)]


def resolve_effort_choice(model, value):
    wanted = str(value or "").strip().lower()
    if not wanted:
        return None
    return next((effort for effort in supported_reasoning_efforts(model)
                 if effort and str(effort).lower() == wanted), None)


def select_model_from_catalog(models, current=None):
    chosen = next((model for model in models if model_key(model) == current), None)
    if chosen is None:
        chosen = next((model for model in models if model.get("isDefault")), None)
    if chosen is None and models:
        chosen = models[0]
    return chosen


def selected_model(runtime, models=None, persist=True):
    models = models if models is not None else available_models(runtime)
    with state_lock:
        snapshot = dict(chat_state(runtime.state_key))
    current = snapshot.get("model")
    chosen = select_model_from_catalog(models, current)
    if chosen and persist:
        updates = {}
        key = model_key(chosen)
        if current != key:
            updates["model"] = key
        supported = supported_reasoning_efforts(chosen)
        if snapshot.get("effort") not in supported:
            updates["effort"] = chosen.get("defaultReasoningEffort") or (supported[0] if supported else None)
        if updates:
            update_state(runtime.state_key, **updates)
    return chosen


def resolve_delegate_settings(runtime, current_model=None, current_effort=None,
                              requested_model=None, requested_effort=None):
    """Resolve explicit model settings against the delegate's live catalog."""
    models = available_models(runtime)
    if requested_model is not None:
        chosen = resolve_model_choice(models, requested_model)
        if chosen is None:
            raise ValueError(f"Модель «{requested_model}» недоступна.")
    else:
        chosen = select_model_from_catalog(models, current_model)
    if not chosen:
        raise ValueError("Codex не вернул доступных моделей.")

    supported = supported_reasoning_efforts(chosen)
    if requested_effort is not None:
        effort = resolve_effort_choice(chosen, requested_effort)
        if effort is None:
            raise ValueError(
                f"Мощность «{requested_effort}» недоступна для {model_key(chosen)}."
            )
    else:
        effort = (current_effort if current_effort in supported else
                  chosen.get("defaultReasoningEffort") or
                  (supported[0] if supported else None))
    return model_key(chosen), effort


def render_model_picker(runtime):
    models = available_models(runtime)
    chosen = selected_model(runtime, models)
    current = model_key(chosen) if chosen else None
    lines = ["🧠 Доступные модели:"]
    for model in models:
        key = model_key(model)
        name = model.get("displayName") or key
        lines.append(f"{'●' if key == current else '○'} {name} — `/model {key}`")
    if not models:
        lines.append("Список моделей пуст.")
    return "  \n".join(lines)


def render_effort_picker(runtime):
    models = available_models(runtime)
    chosen = selected_model(runtime, models)
    if not chosen:
        return "Codex не вернул доступных моделей."
    current = chat_state(runtime.state_key).get("effort")
    lines = [f"⚡ Мощность модели {chosen.get('displayName') or model_key(chosen)}:"]
    for option in effort_options(chosen):
        effort = option["reasoningEffort"]
        description = option.get("description")
        line = f"{'●' if effort == current else '○'} {effort} — `/effort {effort}`"
        if description:
            line += f"  \n   {description}"
        lines.append(line)
    return "  \n".join(lines)


def ensure_thread(runtime, client, requested_thread_id):
    process_pid = client.process.pid if client.process is not None else None
    with process_lock:
        if (requested_thread_id and requested_thread_id == runtime.loaded_thread_id
                and process_pid == runtime.loaded_server_pid):
            return requested_thread_id
    method = "thread/resume" if requested_thread_id else "thread/start"
    try:
        result = client.request(method, _thread_params(runtime, requested_thread_id), timeout=60)
    except AppServerError as exc:
        if not requested_thread_id:
            raise
        message = str(exc).lower()
        missing = any(marker in message for marker in (
            # Exact wording emitted by codex app-server for a missing thread.
            "thread not found", "no rollout found for thread id",
            "no rollout found for conversation id", "invalid thread id",
        ))
        if not missing:
            raise
        log(f"Thread {requested_thread_id} no longer exists; starting a new thread")
        result = client.request("thread/start", _thread_params(runtime), timeout=60)
        send_plain(runtime.chat_id, "Прежняя сессия не найдена; начата новая сессия Codex.")
    thread_id = ((result or {}).get("thread") or {}).get("id")
    if not thread_id:
        raise AppServerError(f"{method} returned no thread id")
    with process_lock:
        runtime.loaded_thread_id = thread_id
        runtime.loaded_server_pid = process_pid
    save_thread_id(runtime.state_key, thread_id)
    return thread_id


def sandbox_policy(name, workspace):
    if name == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    if name == "workspace-write":
        return {"type": "workspaceWrite", "writableRoots": [workspace],
                "networkAccess": True}
    return {"type": "dangerFullAccess"}


def run_turn(runtime, inputs, thread_id, media_paths=None, progress_msg_id=None):
    chat_id = runtime.chat_id
    view = TurnView(chat_id, state_key=runtime.state_key, progress_msg_id=progress_msg_id)
    done = threading.Event()
    error = None
    stopped = False
    paths = []
    close_after_turn_client = None
    try:
        with process_lock:
            runtime.worker_done.clear()
            cancelled = runtime.cancel_requested
        if cancelled:
            view.deliver(stopped=True)
            return
        client = get_app_server(runtime)
        client.start_if_needed()
        with process_lock:
            if runtime.cancel_requested:
                view.deliver(stopped=True)
                return
        with state_lock:
            needs_model_settings = not (
                chat_state(runtime.state_key).get("model")
                and chat_state(runtime.state_key).get("effort")
            )
        if needs_model_settings:
            selected_model(runtime)
        with process_lock:
            if runtime.cancel_requested:
                view.deliver(stopped=True)
                return
        server_thread_id = ensure_thread(runtime, client, thread_id)
        with state_lock:
            snapshot = dict(chat_state(runtime.state_key))
        with process_lock:
            runtime.active_view = view
            runtime.active_done = done
            runtime.active_turn_id = None
            runtime.active_thread_id = server_thread_id
            runtime.active_error = None
            runtime.active_stopped = False
            runtime.active_last_event_at = time.monotonic()
            runtime.active_media_paths = list(media_paths or [])
        view.flush(force=True)
        with process_lock:
            if runtime.cancel_requested:
                view.deliver(stopped=True)
                return
        params = {
            "threadId": server_thread_id,
            "input": inputs,
            "cwd": snapshot.get("workspace") or CODEX_CWD,
            "approvalPolicy": "never",
            "sandboxPolicy": sandbox_policy(
                snapshot.get("sandbox") or CODEX_SANDBOX,
                snapshot.get("workspace") or CODEX_CWD,
            ),
        }
        if snapshot.get("model"):
            params["model"] = snapshot["model"]
        if snapshot.get("effort"):
            params["effort"] = snapshot["effort"]
        result = client.request("turn/start", params, timeout=60)
        turn_id = ((result or {}).get("turn") or {}).get("id")
        with process_lock:
            runtime.active_turn_id = runtime.active_turn_id or turn_id
            cancelled = runtime.cancel_requested
        if cancelled:
            stop_current_process(runtime)
        started_at = time.monotonic()
        while not done.wait(1):
            with process_lock:
                last_event = runtime.active_last_event_at or started_at
                process = client.process
            now = time.monotonic()
            if process is None or process.poll() is not None:
                error = "постоянный процесс Codex неожиданно завершился"
                break
            if now - last_event > IDLE_TIMEOUT_S or now - started_at > TOTAL_TIMEOUT_S:
                error = "Codex остановлен по таймауту"
                stop_current_process(runtime)
                break
        with process_lock:
            error = error or runtime.active_error
            stopped = runtime.active_stopped
        view.deliver(stopped=stopped, error=error)
    except Exception as exc:
        log(f"Codex worker failed: {exc}")
        view.deliver(error=compact(str(exc), 1000))
    finally:
        with process_lock:
            if runtime.close_app_server_after_turn:
                close_after_turn_client = runtime.app_server
                runtime.app_server = None
                runtime.loaded_thread_id = None
                runtime.loaded_server_pid = None
                if close_after_turn_client is not None:
                    close_after_turn_client.close()
            runtime.close_app_server_after_turn = False
            runtime.pending_env = None
            runtime.active_view = None
            runtime.active_done = None
            runtime.active_turn_id = None
            runtime.active_thread_id = None
            runtime.active_error = None
            runtime.active_stopped = False
            runtime.active_last_event_at = None
            paths = runtime.active_media_paths
            runtime.active_media_paths = []
            runtime.busy = False
            runtime.cancel_requested = False
            runtime.worker_done.set()
        for path in paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def run_compaction(runtime, thread_id):
    chat_id = runtime.chat_id
    view = TurnView(chat_id, state_key=runtime.state_key)
    done = threading.Event()
    error = None
    try:
        client = get_app_server(runtime)
        client.start_if_needed()
        server_thread_id = ensure_thread(runtime, client, thread_id)
        with process_lock:
            runtime.active_view = view
            runtime.active_done = done
            runtime.active_turn_id = None
            runtime.active_thread_id = server_thread_id
            runtime.active_error = None
            runtime.active_stopped = False
            runtime.active_last_event_at = time.monotonic()
        client.request("thread/compact/start", {"threadId": server_thread_id}, timeout=60)
        if not done.wait(TOTAL_TIMEOUT_S):
            error = "Сжатие контекста не завершилось за отведённое время."
        with process_lock:
            error = error or runtime.active_error
        if error:
            message = f"🗜 Не удалось сжать контекст: {error}"
            send_plain(chat_id, message)
        else:
            update_state(
                runtime.state_key,
                last_usage=None,
                session_usage=None,
                context_window=None,
            )
            message = "🗜 Контекст сессии сжат. Можно продолжать."
            send_plain(chat_id, message)
    except Exception as exc:
        message = f"🗜 Не удалось сжать контекст: {user_facing_codex_error(exc)}"
        send_plain(chat_id, message)
    finally:
        with process_lock:
            runtime.active_view = None
            runtime.active_done = None
            runtime.active_turn_id = None
            runtime.active_thread_id = None
            runtime.active_error = None
            runtime.active_stopped = False
            runtime.active_last_event_at = None
            runtime.busy = False


def session_files(runtime_or_chat_id=OWNER_ID, state_key=None):
    if isinstance(runtime_or_chat_id, TenantRuntime):
        chat_id = runtime_or_chat_id.chat_id
        state_key = runtime_or_chat_id.state_key
    else:
        chat_id = int(runtime_or_chat_id)
    codex_home = tenant_codex_home(chat_id, state_key=state_key)
    root = (codex_home or default_codex_home()) / "sessions"
    return sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def session_info(path):
    sid, preview = path.stem, ""
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                obj = json.loads(line)
                payload = obj.get("payload", {})
                if obj.get("type") == "session_meta":
                    sid = payload.get("id", sid)
                elif obj.get("type") == "response_item" and payload.get("role") == "user":
                    texts = [part.get("text", "") for part in payload.get("content", [])
                             if part.get("type") == "input_text"]
                    candidate = " ".join(texts).strip()
                    if candidate and not candidate.startswith("<recommended_plugins>"):
                        preview = compact(candidate, 55)
    except Exception:
        pass
    return sid, preview


def session_message_count(runtime_or_chat_id, thread_id, state_key=None):
    if not thread_id:
        return None
    for path in session_files(runtime_or_chat_id, state_key=state_key):
        sid, _ = session_info(path)
        if sid != thread_id:
            continue
        count = 0
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    obj = json.loads(line)
                    payload = obj.get("payload") or {}
                    if obj.get("type") != "response_item" or payload.get("role") != "user":
                        continue
                    text = " ".join(
                        part.get("text", "") for part in payload.get("content", [])
                        if part.get("type") == "input_text"
                    ).strip()
                    if text and not text.startswith((
                        "<recommended_plugins>", "<environment_context>",
                    )):
                        count += 1
            return count
        except Exception:
            return None
    return None


def fmt_number(value):
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def format_reset_time(timestamp):
    if not timestamp:
        return "—"
    try:
        reset = datetime.fromtimestamp(int(timestamp)).astimezone()
        remaining = max(0, int(timestamp) - int(time.time()))
        if remaining < 3600:
            relative = f"через {max(1, remaining // 60)} мин"
        elif remaining < 86400:
            relative = f"через {remaining // 3600} ч {remaining % 3600 // 60} мин"
        else:
            relative = f"через {remaining // 86400} д {remaining % 86400 // 3600} ч"
        return f"{reset:%d.%m %H:%M} ({relative})"
    except (TypeError, ValueError, OSError):
        return "—"


def rate_limit_line(label, window):
    if not isinstance(window, dict):
        return f"{label}: нет данных"
    used = int(window.get("usedPercent") or 0)
    return (
        f"{label}: использовано {used}% · осталось {max(0, 100 - used)}% · "
        f"сброс {format_reset_time(window.get('resetsAt'))}"
    )


def build_usage_report(runtime):
    chat_id = runtime.chat_id
    try:
        if not chat_state(runtime.state_key).get("model"):
            selected_model(runtime)
    except Exception as exc:
        log(f"Could not resolve model for usage: {exc}")
    with state_lock:
        snapshot = dict(chat_state(runtime.state_key))
    thread_id = snapshot.get("thread_id")
    last = snapshot.get("last_usage") or {}
    total = snapshot.get("session_usage") or last
    client = get_app_server(runtime)
    limits_result = usage_result = None
    limits_error = usage_error = None
    try:
        limits_result = client.request("account/rateLimits/read", timeout=30)
        with process_lock:
            runtime.last_rate_limits = (limits_result or {}).get("rateLimits") or runtime.last_rate_limits
    except Exception as exc:
        limits_error = compact(str(exc), 300)
    try:
        usage_result = client.request(
            "account/usage/read", {"threadId": thread_id} if thread_id else {}, timeout=30,
        )
    except Exception as exc:
        usage_error = compact(str(exc), 300)

    messages = session_message_count(runtime, thread_id)
    context_tokens = last.get("input_tokens")
    context_window = snapshot.get("context_window")
    context = f"~{fmt_number(context_tokens)} tokens" if context_tokens else "нет данных"
    if context_tokens and context_window:
        context += f" / {fmt_number(context_window)} ({context_tokens / context_window:.1%})"
    lines = [
        "📊 Session",
        f"{(thread_id or 'нет активной')[:8]}  •  Model: {snapshot.get('model') or 'не определена'}"
        f"  •  Effort: {snapshot.get('effort') or 'не определён'}",
        f"Messages: {messages if messages is not None else '—'}",
        f"Context: {context}",
        "",
        "🔢 Tokens (this session)",
        f"in {fmt_number(total.get('input_tokens'))}  ·  out {fmt_number(total.get('output_tokens'))}  ·  "
        f"cache-r {fmt_number(total.get('cached_input_tokens'))}  ·  "
        f"cache-w {fmt_number(total.get('cache_write_input_tokens'))}",
    ]
    thread_usage = (usage_result or {}).get("threadUsage") or {}
    usd_micros = thread_usage.get("estimatedUsageUsdMicros")
    if usd_micros is not None:
        lines.append(f"(~${usd_micros / 1_000_000:.4f} эквивалент по API-тарифу)")
    elif usage_error:
        lines.append(f"Стоимость: не удалось получить ({usage_error})")
    else:
        lines.append("Стоимость: недоступна для текущего subscription-маршрута")

    lines.extend(("", "📈 Account limits (subscription, not credits)"))
    limits = (limits_result or {}).get("rateLimits") or {}
    if limits:
        plan = limits.get("planType")
        if plan:
            lines.append(f"Plan: {str(plan).replace('_', ' ').title()}")
        lines.append(rate_limit_line("5-hour", limits.get("primary")))
        lines.append(rate_limit_line("Weekly", limits.get("secondary")))
        credits = limits.get("credits") or {}
        if credits.get("hasCredits") or credits.get("unlimited"):
            balance = "unlimited" if credits.get("unlimited") else credits.get("balance")
            lines.append(f"Credits: {balance}")
    else:
        lines.append(f"Не удалось получить: {limits_error or 'нет данных'}")
    return "\n".join(lines)


def refresh_rate_limits(runtime):
    try:
        result = get_app_server(runtime).request("account/rateLimits/read", timeout=30)
        with process_lock:
            runtime.last_rate_limits = (result or {}).get("rateLimits") or runtime.last_rate_limits
    except Exception as exc:
        log(f"Could not preload Codex rate limits: {exc}")


def request_restart(chat_id):
    """Persist a restart request; the watcher executes it only between turns."""
    RESTART_SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=RESTART_SIGNAL_FILE.name + ".", dir=RESTART_SIGNAL_FILE.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"chat_id": chat_id, "requested_at": time.time()}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, RESTART_SIGNAL_FILE)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def restart_watcher():
    global restart_draining
    while True:
        time.sleep(0.5)
        if not RESTART_SIGNAL_FILE.exists():
            continue
        with process_lock:
            runtimes = list(tenants.values()) + list(tenants_delegate.values())
            if any(runtime.busy or runtime.pending_batch for runtime in runtimes):
                continue
            restart_draining = True
        try:
            request = json.loads(RESTART_SIGNAL_FILE.read_text(encoding="utf-8"))
            chat_id = (request.get("chat_id") if isinstance(request, dict) else None) or OWNER_ID
        except Exception:
            chat_id = OWNER_ID
        try:
            RESTART_SIGNAL_FILE.unlink()
        except FileNotFoundError:
            pass
        result = send_plain(chat_id, "🔄 Текущий ход завершён. Перезапускаю Codex-бота…")
        message_id = (result.get("result") or {}).get("message_id") if result else None
        update_runtime_state(
            restart_completed_chat_id=chat_id,
            restart_message_id=message_id if isinstance(message_id, int) else None,
        )
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
        return


def external_request_watcher():
    """Local, non-Telegram input channel for bridge_exec.py. A bot can
    never see its own outgoing messages via getUpdates -- Telegram simply
    does not deliver them back to the sender, confirmed live 2026-09-01,
    not something fixable at the code level. Since this whole product is
    code we control, the actual fix is to skip Telegram for this leg
    entirely: bridge_exec.py writes a request file here instead of
    pretending to be an incoming message. Every request gets the separate
    persistent delegate tenant for its real Telegram chat; it never reuses
    or steers the owner's tenant unless the owner explicitly sends a real
    Telegram message while the delegate is busy."""
    while True:
        time.sleep(1)
        if not EXTERNAL_REQUEST_FILE.exists():
            continue
        try:
            request = json.loads(EXTERNAL_REQUEST_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"Could not read external request: {exc}")
            request = None
        try:
            EXTERNAL_REQUEST_FILE.unlink()
        except FileNotFoundError:
            pass
        if not isinstance(request, dict):
            continue
        try:
            chat_id = int(request.get("chat_id") or OWNER_ID)
        except (TypeError, ValueError):
            log(f"Ignoring external request with invalid chat_id: {request.get('chat_id')!r}")
            continue
        text = request.get("text")
        if not text:
            continue
        start_delegate_turn(
            chat_id,
            text,
            resume_thread_id=request.get("resume_thread_id"),
            workspace=request.get("workspace"),
            model=request.get("model"),
            effort=request.get("effort"),
            env=request.get("env"),
        )


def _write_cross_delegate_result(request_id, ok, text):
    CROSS_DELEGATE_RESULT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    result_path = CROSS_DELEGATE_RESULT_DIR / f"{request_id}.json"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{request_id}.", suffix=".tmp", dir=CROSS_DELEGATE_RESULT_DIR,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"done": True, "ok": bool(ok), "text": text},
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, result_path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def cross_delegate_watcher():
    """Accept per-user Claude-to-Codex requests from the dedicated queue."""
    CROSS_DELEGATE_QUEUE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    CROSS_DELEGATE_RESULT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    while True:
        time.sleep(0.5)
        for request_path in sorted(CROSS_DELEGATE_QUEUE_DIR.glob("*.json")):
            request_id = request_path.stem
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except Exception as exc:
                log(f"Could not read cross-delegate request {request_id}: {exc}")
                request = None
            try:
                request_path.unlink()
            except FileNotFoundError:
                pass

            ok = False
            if not isinstance(request, dict):
                result_text = "Отклонено: повреждённый формат запроса делегации."
            else:
                chat_id = request.get("chat_id")
                text = request.get("text")
                if not isinstance(chat_id, int) or isinstance(chat_id, bool):
                    result_text = "Отклонено: некорректный Telegram chat_id."
                elif not isinstance(text, str) or not text.strip():
                    result_text = "Отклонено: пустой текст задачи."
                elif str(chat_id) not in load_whitelist():
                    result_text = (
                        "Отклонено: этот Telegram ID отсутствует в whitelist Codex-бота."
                    )
                else:
                    with state_lock:
                        account = state_db.get("chats", {}).get(str(chat_id), {})
                        account_status = (
                            account.get("account_status") if isinstance(account, dict) else None
                        )
                    if account_status != "ready":
                        result_text = (
                            "Отклонено: Codex-аккаунт для этого Telegram ID не готов. "
                            "Сначала заверши /login в Codex-боте."
                        )
                    else:
                        ok = start_delegate_turn(chat_id, text)
                        if ok:
                            result_text = (
                                "Принято: Codex-бот запустил задачу. Результат придёт "
                                "в этот же Telegram-чат от Codex-бота."
                            )
                        else:
                            runtime = get_delegate_tenant(chat_id)
                            with process_lock:
                                busy = runtime.busy or bool(runtime.pending_batch)
                            result_text = (
                                "Отклонено: уже выполняется предыдущая делегированная задача."
                                if busy else
                                "Отклонено: Codex-бот не смог запустить делегированную задачу."
                            )
            try:
                _write_cross_delegate_result(request_id, ok, result_text)
            except Exception as exc:
                log(f"Could not write cross-delegate result {request_id}: {exc}")


def stop_current_process(runtime):
    with process_lock:
        client = runtime.app_server
        thread_id = runtime.active_thread_id
        turn_id = runtime.active_turn_id
        if not runtime.busy:
            return False
        runtime.active_stopped = True
        runtime.cancel_requested = True
        done = runtime.active_done
        if client is None or not thread_id or not turn_id:
            return True
    try:
        client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return True
    except Exception as exc:
        log(f"Could not interrupt turn: {exc}")
        # A failed interrupt leaves the tenant's old runtime untrustworthy.
        with process_lock:
            if runtime.app_server is client:
                runtime.app_server = None
                runtime.loaded_thread_id = None
                runtime.loaded_server_pid = None
        client.close()
        if done is not None:
            done.set()
        return True


def stop_and_wait_for_worker(runtime):
    """Prevent /new and /resume from overlapping the previous worker cleanup."""
    running = stop_current_process(runtime)
    with process_lock:
        finished = runtime.worker_done
    if running and not finished.wait(TURN_STOP_WAIT_S):
        return False
    return True


def steer_current_turn(runtime, inputs, media_paths=None):
    with process_lock:
        client = runtime.app_server
        thread_id = runtime.active_thread_id
        turn_id = runtime.active_turn_id
        if client is None or not thread_id or not turn_id:
            return False, "активный ход ещё не успел получить ID — повтори через секунду"
        runtime.active_media_paths.extend(media_paths or [])
    try:
        client.request("turn/steer", {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": inputs,
        })
        return True, None
    except Exception as exc:
        with process_lock:
            for path in media_paths or []:
                try:
                    runtime.active_media_paths.remove(path)
                except ValueError:
                    pass
        return False, compact(str(exc), 500)


def cleanup_media_paths(paths):
    for path in paths or []:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def combine_input_batch(entries):
    """Combine rapid Telegram messages into one ordered App Server input.

    Text messages are separated visibly for the model.  Images and other
    local inputs stay in their original order, so a forwarded caption/image
    pair remains associated with the message that supplied it.
    """
    combined = []
    media_paths = []
    for index, (inputs, paths) in enumerate(entries):
        if index:
            combined.append({"type": "text", "text": "\n\n---\n\n"})
        combined.extend(inputs or [])
        media_paths.extend(paths or [])
    return combined, media_paths


def cancel_pending_batch(runtime):
    """Cancel an idle debounce batch and remove any downloaded media."""
    with process_lock:
        timer = runtime.batch_timer
        runtime.batch_timer = None
        runtime.batch_generation += 1
        entries = runtime.pending_batch
        runtime.pending_batch = []
    if timer is not None:
        timer.cancel()
    cleanup_media_paths(
        path for _, paths in entries for path in (paths or [])
    )


def _schedule_batch_timer(runtime, delay=BATCH_DEBOUNCE_S):
    with process_lock:
        old_timer = runtime.batch_timer
        runtime.batch_generation += 1
        generation = runtime.batch_generation
        timer = None

        def fire():
            flush_pending_batch(runtime, timer, generation)

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        runtime.batch_timer = timer
    if old_timer is not None:
        old_timer.cancel()
    timer.start()


def _requeue_batch(runtime, entries, delay=BATCH_RETRY_S):
    """Put a batch back when a live turn is between startup/completion."""
    with process_lock:
        runtime.pending_batch = list(entries) + runtime.pending_batch
    _schedule_batch_timer(runtime, delay)


def flush_pending_batch(runtime, timer, generation):
    """Start the one turn represented by the current debounce window."""
    with process_lock:
        # A canceled timer can still wake up concurrently.  Only the newest
        # timer that is still registered for this chat may consume the batch.
        if runtime.batch_timer is not timer or runtime.batch_generation != generation:
            return
        runtime.batch_timer = None
        entries = runtime.pending_batch
        runtime.pending_batch = []
        if not entries:
            return
        # A local-mode download for this chat can still be running well past
        # the debounce window (large file, GET_FILE_TIMEOUT_S up to minutes).
        # The debounce timer only tracks "quiet since the last item that
        # FINISHED downloading" -- it has no idea a slower sibling is still
        # in flight in the same FIFO, so it can and does fire early. Flushing
        # now would start (or steer) a turn permanently missing that
        # attachment; wait for the whole burst to finish landing instead.
        still_downloading = LOCAL_BOT_API and runtime.chat_id in local_message_queues
        if not still_downloading:
            already_busy = runtime.busy
            if not already_busy:
                runtime.busy = True
                runtime.cancel_requested = False
                runtime.worker_done.clear()
    if still_downloading:
        _requeue_batch(runtime, entries)
        return

    inputs, media_paths = combine_input_batch(entries)
    if already_busy:
        steered, _ = steer_current_turn(runtime, inputs, media_paths)
        if steered:
            return
        with process_lock:
            still_busy = runtime.busy
        if still_busy:
            # The first updates can arrive while turn/start is still waiting
            # for its ID.  Do not throw those messages away or emit one error
            # per update; retry the single combined batch shortly.
            _requeue_batch(runtime, entries)
            return
        # The old turn ended between the snapshot above and turn/steer.  Let
        # this batch become the next ordinary turn instead of losing it.
        with process_lock:
            if runtime.busy:
                _requeue_batch(runtime, entries)
                return
            runtime.busy = True
            runtime.cancel_requested = False
            runtime.worker_done.clear()

    with state_lock:
        thread_id = chat_state(runtime.state_key).get("thread_id")
    with process_lock:
        progress_msg_id = runtime.pending_progress_msg_id
        runtime.pending_progress_msg_id = None
    threading.Thread(
        target=run_turn,
        args=(runtime, inputs, thread_id, media_paths, progress_msg_id),
        daemon=True,
    ).start()


def queue_message(runtime, inputs, media_paths=None):
    """Debounce every message, including follow-ups during a live turn."""
    with process_lock:
        runtime.pending_batch.append((inputs or [], list(media_paths or [])))
    _schedule_batch_timer(runtime)


def _delegate_error(chat_id, message):
    send_plain(chat_id, message)
    write_last_turn(chat_id, message, delegated=True, ok=False)


def start_delegate_turn(chat_id, text, resume_thread_id=None, workspace=None,
                        model=None, effort=None, env=None):
    """Start a delegated turn in the real chat's isolated delegate tenant."""
    chat_id = int(chat_id)
    runtime = get_delegate_tenant(chat_id)
    requested_thread_id = str(resume_thread_id or "").strip() or None
    requested_env = dict(env or {})
    delegate_state_key = runtime.state_key

    with state_lock:
        owner_state = dict(chat_state(chat_id))
        delegate_state = dict(chat_state(delegate_state_key))
    owner_thread_id = owner_state.get("thread_id")
    delegate_thread_id = delegate_state.get("thread_id")

    if requested_thread_id and requested_env:
        _delegate_error(chat_id, "Нельзя использовать --env вместе с --resume.")
        return False

    if requested_thread_id:
        valid_delegate_thread = bool(
            delegate_thread_id
            and (
                delegate_thread_id == requested_thread_id
                or delegate_thread_id.startswith(requested_thread_id)
            )
        )
        owner_thread_conflict = bool(
            owner_thread_id
            and (
                delegate_thread_id == owner_thread_id
                or owner_thread_id.startswith(requested_thread_id)
            )
        )
        if not valid_delegate_thread or owner_thread_conflict:
            _delegate_error(
                chat_id,
                "Нельзя продолжить эту делегацию: resume_thread_id не совпадает "
                "с последним делегированным тредом.",
            )
            return False

    with process_lock:
        delegate_busy = runtime.busy or bool(runtime.pending_batch)
        if not delegate_busy:
            cancel_pending_batch(runtime)
            runtime.busy = True
            runtime.cancel_requested = False
            runtime.worker_done.clear()
            if not requested_thread_id:
                if runtime.app_server is not None:
                    # A fresh delegation must not inherit background work from
                    # a previous delegate turn. Do not reuse this client after
                    # close(): its old reader thread may still be unwinding.
                    old_client = runtime.app_server
                    runtime.app_server = None
                    old_client.close()
                runtime.loaded_thread_id = None
                runtime.loaded_server_pid = None
    if delegate_busy:
        _delegate_error(chat_id, "Уже выполняю предыдущую делегированную задачу.")
        return False

    requested_settings = None
    if model is not None or effort is not None:
        current_state = delegate_state if requested_thread_id else owner_state
        try:
            requested_settings = resolve_delegate_settings(
                runtime,
                current_model=current_state.get("model"),
                current_effort=current_state.get("effort"),
                requested_model=model,
                requested_effort=effort,
            )
        except ValueError as exc:
            with process_lock:
                runtime.busy = False
            _delegate_error(chat_id, str(exc))
            return False
        except Exception as exc:
            with process_lock:
                runtime.busy = False
            _delegate_error(
                chat_id,
                f"Не удалось проверить настройки делегации: {compact(str(exc), 500)}",
            )
            return False

    updates = {"pending_delegator_session_id": owner_thread_id or ""}
    if requested_thread_id:
        # Keep the full persisted ID; the caller may pass the short prefix
        # shown in the footer, but App Server's resume endpoint needs the ID.
        updates["thread_id"] = delegate_thread_id
        if workspace:
            updates["workspace"] = workspace
        thread_id = delegate_thread_id
    else:
        updates.update(
            thread_id=None,
            last_usage=None,
            session_usage=None,
            context_window=None,
            model=owner_state.get("model"),
            effort=owner_state.get("effort"),
            sandbox=owner_state.get("sandbox"),
            workspace=workspace or owner_state.get("workspace"),
        )
        thread_id = None
    if requested_settings is not None:
        updates["model"], updates["effort"] = requested_settings
    if requested_env:
        with process_lock:
            old_client = runtime.app_server
            runtime.app_server = None
            runtime.loaded_thread_id = None
            runtime.loaded_server_pid = None
        if old_client is not None:
            old_client.close()
    update_state(delegate_state_key, **updates)
    if requested_env:
        with process_lock:
            runtime.pending_env = requested_env

    try:
        threading.Thread(
            target=run_turn,
            args=(runtime, [{"type": "text", "text": text}], thread_id, []),
            daemon=True,
        ).start()
    except Exception as exc:
        with process_lock:
            runtime.busy = False
            runtime.pending_env = None
            runtime.close_app_server_after_turn = False
        _delegate_error(chat_id, f"Не удалось запустить делегированную задачу: {compact(str(exc), 500)}")
        return False
    return True


def start_account_login(runtime):
    if runtime.chat_id == OWNER_ID:
        send_plain(runtime.chat_id, "Владелец использует основной аккаунт ~/.codex; отдельный вход не требуется.")
        return
    try:
        client = get_app_server(runtime)
        client.start_if_needed()
        result = client.request(
            "account/login/start", {"type": "chatgptDeviceCode"}, timeout=60,
        ) or {}
        runtime.login_id = result.get("loginId")
        update_state(runtime.state_key, account_status="awaiting_login")
        send_plain(
            runtime.chat_id,
            "🔐 Подключение отдельного аккаунта Codex\n\n"
            f"1. Открой: {result.get('verificationUrl')}\n"
            f"2. Введи код: {result.get('userCode')}\n\n"
            "Токены сохранятся только в твоём изолированном CODEX_HOME. "
            "Бот сообщит, когда вход завершится.",
        )
    except Exception as exc:
        update_state(runtime.state_key, account_status="login_failed")
        send_plain(runtime.chat_id, f"Не удалось начать вход в Codex: {compact(str(exc), 500)}")


def account_status_report(runtime):
    try:
        result = get_app_server(runtime).request("account/read", {"refreshToken": False}, timeout=30) or {}
        account = result.get("account") or {}
        if not account:
            update_state(runtime.state_key, account_status=None)
            return "Аккаунт Codex не подключён. Используй /login."
        update_state(runtime.state_key, account_status="ready")
        label = account.get("email") or account.get("type") or "подключён"
        plan = account.get("planType") or account.get("plan_type")
        return f"Аккаунт: {label}" + (f"\nПлан: {plan}" if plan else "")
    except Exception as exc:
        return f"Не удалось прочитать аккаунт: {compact(str(exc), 500)}"


def account_is_ready(runtime):
    if runtime.chat_id == OWNER_ID:
        return True
    try:
        result = get_app_server(runtime).request(
            "account/read", {"refreshToken": False}, timeout=30,
        ) or {}
        ready = bool(result.get("account"))
        update_state(runtime.state_key, account_status="ready" if ready else None)
        return ready
    except Exception as exc:
        log(f"tenant={runtime.chat_id} account readiness check failed: {exc}")
        return False


def _env_file_value(path, name):
    """Read the last simple KEY=value entry without importing it into env."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    except Exception as exc:
        log(f"Could not read {path}: {exc}")
        return ""
    prefix = f"{name}="
    values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    return values[-1].strip() if values else ""


def _configured_local_bot_api_url():
    return _env_file_value(BOT_ENV_FILE, "TELEGRAM_API_URL")


def _local_bot_api_dependencies_available():
    """Keep --yes from making bot.py install packages through sudo."""
    return (
        all(shutil.which(name) for name in ("git", "cmake", "gperf", "g++", "make"))
        and Path("/usr/include/openssl/ssl.h").is_file()
        and Path("/usr/include/zlib.h").is_file()
    )


def _send_local_bot_api_status(chat_id, text):
    result = tg_call("sendMessage", {"chat_id": chat_id, "text": text})
    if result.get("ok"):
        return (result.get("result") or {}).get("message_id")
    return None


def _update_local_bot_api_status(status, text, force=False):
    now = time.monotonic()
    if (not force and now - status.get("last_edit_at", 0.0)
            < LOCAL_BOT_API_STATUS_EDIT_MIN_INTERVAL_S):
        return
    message_id = status.get("message_id")
    if message_id is None:
        status["message_id"] = _send_local_bot_api_status(status["chat_id"], text)
    else:
        edit_plain(status["chat_id"], message_id, text)
    status["last_edit_at"] = now


def _finish_local_bot_api_failure(runtime, status, details):
    text = f"⚠️ Не удалось включить локальный сервер: {compact(details, 500)}"
    try:
        _update_local_bot_api_status(status, text, force=True)
    except Exception as exc:
        log(f"Could not report local Bot API failure: {exc}")
    with process_lock:
        runtime.update_flow_stage = None
        runtime.update_flow_api_id = None
    # This is only ever reached from the /update flow, after update.sh has
    # already pulled new code -- a failed local-server switch must not also
    # leave that update unapplied. .env is untouched on this path (still
    # cloud mode, exactly as before), so restarting is safe either way.
    send_plain(runtime.chat_id, "🔁 Перезапускаю бота, чтобы обновление вступило в силу…")
    request_restart(OWNER_ID)


def _write_telegram_api_url(url):
    """Replace TELEGRAM_API_URL in the protected deployment .env file."""
    if not BOT_ENV_FILE.exists():
        raise RuntimeError(f"Не найден {BOT_ENV_FILE}")
    if "\n" in url or "\r" in url:
        raise RuntimeError("Установщик вернул недопустимый адрес локального сервера")
    lines = BOT_ENV_FILE.read_text(encoding="utf-8").splitlines()
    prefix = "TELEGRAM_API_URL="
    lines = [line for line in lines if not line.startswith(prefix)]
    lines.append(prefix + url)
    old_umask = os.umask(0o077)
    try:
        # The file already exists and is mode 600; opening it this way does
        # not alter its mode while the umask protects an unexpected create.
        BOT_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    finally:
        os.umask(old_umask)


def _run_local_bot_api_install(runtime, api_id=None, api_hash=None):
    """Worker for the build/reuse and irreversible API endpoint switch."""
    status = {
        "chat_id": runtime.chat_id,
        "message_id": None,
        "last_edit_at": 0.0,
    }
    output_tail = deque(maxlen=20)
    stage_text = {
        "dependencies": "⏳ Проверяю зависимости сборки…",
        "build": "⏳ Собираю локальный Bot API сервер (может занять ~15 минут)…",
        "install": "⏳ Устанавливаю сервис…",
        "done": "✅ Локальный сервер собран.",
    }
    try:
        _update_local_bot_api_status(status, "⏳ Готовлю локальный Bot API сервер…", force=True)
        env = dict(os.environ)
        if api_id is not None:
            env["TELEGRAM_API_ID"] = api_id
        if api_hash is not None:
            env["TELEGRAM_API_HASH"] = api_hash
        process = subprocess.Popen(
            [str(LOCAL_BOT_API_INSTALL_SCRIPT), "--yes"],
            cwd=str(Path(__file__).parent), env=env, text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        # api_hash was supplied only to Popen's environment and is not kept
        # on runtime (or any persistent state) after this point.
        api_hash = None
        local_url = None
        for line in process.stdout:
            line = line.rstrip("\n")
            output_tail.append(line)
            if line.startswith("STAGE:"):
                stage = line[len("STAGE:"):]
                if stage in stage_text:
                    _update_local_bot_api_status(status, stage_text[stage], force=True)
            elif line.startswith("REUSE:existing"):
                _update_local_bot_api_status(
                    status,
                    "✅ Найден уже настроенный локальный сервер, пересобирать не нужно.",
                    force=True,
                )
            elif line.startswith("LOCAL_BOT_API_URL="):
                local_url = line[len("LOCAL_BOT_API_URL="):].strip()
        exit_code = process.wait()
        if exit_code != 0:
            tail = "\n".join(output_tail)[-500:]
            raise RuntimeError(tail or f"Установщик завершился с кодом {exit_code}")
        if not local_url:
            raise RuntimeError("Установщик не сообщил адрес локального сервера")
        switch = subprocess.run(
            [str(LOCAL_BOT_API_SWITCH_SCRIPT), local_url],
            cwd=str(Path(__file__).parent),
            env={**os.environ, "LOCAL_BOT_API_LOGOUT_CONFIRM": "yes"},
            capture_output=True, text=True, timeout=60, check=False,
        )
        if switch.returncode != 0:
            raise RuntimeError((switch.stderr or switch.stdout or
                                f"Переключатель завершился с кодом {switch.returncode}")[-500:])
        _write_telegram_api_url(local_url)
        _update_local_bot_api_status(status, "✅ Готово. Перезапускаю бота…", force=True)
        request_restart(OWNER_ID)
    except Exception as exc:
        _finish_local_bot_api_failure(runtime, status, str(exc))


def start_local_bot_api_install(runtime, api_id=None, api_hash=None):
    """Start one installer worker, never a second concurrent installer."""
    # install-local-bot-api.sh only ever builds from source when NO unit is
    # registered yet (an existing one, even unhealthy, is left alone rather
    # than rebuilt -- see repair_local_bot_api_service's own comment). Build
    # tooling is therefore only actually needed in that one case; requiring
    # it here too would block a plain reuse of an already-built shared
    # server on a host that happens to be missing them.
    if not _systemctl_unit_exists(TELEGRAM_BOT_API_UNIT) and not _local_bot_api_dependencies_available():
        status = {"chat_id": runtime.chat_id, "message_id": None, "last_edit_at": 0.0}
        _finish_local_bot_api_failure(
            runtime, status,
            "Не хватает зависимостей сборки. Прогони scripts/install-local-bot-api.sh из терминала вручную.",
        )
        return False
    with process_lock:
        if runtime.update_flow_stage == "installing":
            return False
        runtime.update_flow_stage = "installing"
    threading.Thread(
        target=_run_local_bot_api_install,
        args=(runtime, api_id, api_hash), daemon=True,
    ).start()
    return True


def _systemctl_is_active(unit):
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", f"{unit}.service"],
            capture_output=True, text=True, timeout=15, check=False,
        ).returncode == 0
    except OSError as exc:
        log(f"Could not check {unit}.service: {exc}")
        return False


def _systemctl_unit_exists(unit):
    try:
        result = subprocess.run(
            ["systemctl", "list-unit-files", f"{unit}.service", "--no-legend"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return bool((result.stdout or "").strip())
    except OSError as exc:
        log(f"Could not list {unit}.service: {exc}")
        return False


def _local_bot_api_port_ready():
    try:
        with socket.create_connection(("127.0.0.1", TELEGRAM_BOT_API_PORT), timeout=5):
            return True
    except OSError:
        return False


def repair_local_bot_api_service():
    """Repair only an existing configured unit; never install one from bot.py."""
    unit = TELEGRAM_BOT_API_UNIT
    if _systemctl_is_active(unit):
        return None
    if not _systemctl_unit_exists(unit):
        return ("⚠️ Локальный сервер сконфигурирован, но не установлен на этом хосте — "
                "прогони scripts/install-local-bot-api.sh из терминала вручную.")
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", f"{unit}.service"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except OSError as exc:
        return f"⚠️ Не смог перезапустить локальный сервер: {compact(str(exc), 300)}"
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        permission_markers = (
            "password is required", "a password is required", "not allowed to run sudo",
            "no tty present", "permission denied",
        )
        if any(marker in details.lower() for marker in permission_markers):
            return "⚠️ Нет прав перезапустить сервер, нужен sudoers-грант."
        return f"⚠️ Не смог перезапустить локальный сервер: {compact(details, 300)}"
    time.sleep(2)
    if _systemctl_is_active(unit) and _local_bot_api_port_ready():
        return "🔧 Локальный сервер был неактивен, перезапустил."
    return "⚠️ Не смог перезапустить локальный сервер: сервис или его порт не стал активен."


def handle_update_flow_message(chat_id, text, runtime):
    """Consume owner-only local-Bot-API setup replies before Codex sees them."""
    if chat_id != OWNER_ID:
        return False
    with process_lock:
        stage = runtime.update_flow_stage
    if stage is None:
        return False
    if stage == "installing":
        command = text.split(None, 1)[0].split("@", 1)[0].lower() if text else ""
        if command == "/update":
            send_plain(chat_id, "Установка уже идёт, дождись.")
        return True
    if stage == "awaiting_yes_no":
        answer = text.strip().lower()
        if answer in ("да", "yes", "ага"):
            if TELEGRAM_BOT_API_ENV_FILE.exists():
                start_local_bot_api_install(runtime)
            else:
                with process_lock:
                    runtime.update_flow_stage = "awaiting_api_id"
                send_plain(chat_id, "Пришли api_id")
            return True
        with process_lock:
            runtime.update_flow_stage = None
            runtime.update_flow_api_id = None
        # The code update.sh already pulled is still waiting to be applied --
        # declining the local-server offer must not silently swallow that,
        # the same restart /update always does when nothing local-bot-api-
        # related comes up at all.
        send_plain(chat_id, "Ладно. 🔁 Перезапускаю бота, чтобы обновление вступило в силу…")
        request_restart(chat_id)
        return True
    if stage == "awaiting_api_id":
        if not re.fullmatch(r"[0-9]+", text.strip()):
            send_plain(chat_id, "Пришли api_id")
            return True
        with process_lock:
            runtime.update_flow_api_id = text.strip()
            runtime.update_flow_stage = "awaiting_api_hash"
        send_plain(chat_id, "Пришли api_hash")
        return True
    if stage == "awaiting_api_hash":
        api_hash = text.strip()
        if not api_hash:
            send_plain(chat_id, "Пришли api_hash")
            return True
        with process_lock:
            api_id = runtime.update_flow_api_id
            runtime.update_flow_api_id = None
        start_local_bot_api_install(runtime, api_id=api_id, api_hash=api_hash)
        return True
    return False


def handle_command(chat_id, command, runtime=None):
    runtime = runtime or get_tenant(chat_id)
    state_key = runtime.state_key
    raw_cmd, _, arg = command.partition(" ")
    cmd = raw_cmd.split("@", 1)[0].lower().lstrip("/.")
    arg = arg.strip()
    if cmd in ("start", "help"):
        send_plain(chat_id, "Codex Telegram bridge. Команды доступны в меню бота.")
        return True
    if cmd == "persona":
        if chat_id != OWNER_ID:
            send_plain(chat_id, "Персона доступна только владельцу в личном чате.")
            return True
        if arg.lower() == "reset":
            _write_persona(
                _persona_path(chat_id),
                Path(__file__).with_name("personality.example.md").read_text(encoding="utf-8"),
            )
            send_plain(chat_id, "✅ Персона сброшена к шаблону по умолчанию.")
        elif arg:
            send_plain(chat_id, "Использование: /persona или /persona reset")
        else:
            send_persona(chat_id)
        return True
    if cmd == "login":
        threading.Thread(target=start_account_login, args=(runtime,), daemon=True).start()
        return True
    if cmd == "account":
        send_plain(chat_id, account_status_report(runtime))
        return True
    if cmd == "new":
        if not stop_and_wait_for_worker(runtime):
            send_plain(chat_id, "Предыдущий ход ещё завершается; /new пока не выполнен.")
            return True
        cancel_pending_batch(runtime)
        update_state(state_key, thread_id=None, last_usage=None, session_usage=None, context_window=None)
        send_plain(chat_id, "🆕 Текущий Codex-тред сброшен. Следующее сообщение начнёт новый.")
        return True
    if cmd == "sessions":
        current = chat_state(state_key).get("thread_id")
        rows = []
        for path in session_files(runtime)[:10]:
            sid, preview = session_info(path)
            rows.append(f"{sid[:8]}{' ← текущая' if sid == current else ''}  {preview}")
        send_plain(chat_id, "Последние сессии:\n" + ("\n".join(rows) or "не найдены"))
        return True
    if cmd == "resume":
        # Delegated turns deliberately use a separate CODEX_HOME so their
        # session files cannot pollute the owner's normal conversation.
        # The delegation footer nevertheless offers /resume, so search both
        # stores and remember which runtime owns the selected session.
        owner_runtime = get_tenant(chat_id)
        delegate_runtime = get_delegate_tenant(chat_id)
        matches = []
        for candidate_runtime in (owner_runtime, delegate_runtime):
            matches.extend(
                (candidate_runtime, sid)
                for path in session_files(candidate_runtime)
                for sid, _ in [session_info(path)]
                if arg and sid.startswith(arg)
            )
        if len(matches) != 1:
            send_plain(chat_id, "Укажи однозначный id/префикс: /resume <id>" if matches else "Сессия не найдена.")
        else:
            target_runtime, thread_id = matches[0]
            if not stop_and_wait_for_worker(target_runtime):
                send_plain(chat_id, "Предыдущий ход ещё завершается; /resume пока не выполнен.")
                return True
            cancel_pending_batch(target_runtime)
            update_state(
                target_runtime.state_key, thread_id=thread_id, last_usage=None,
                session_usage=None, context_window=None,
            )
            update_state(
                delegate_runtime.state_key,
                resume_selected=(target_runtime is delegate_runtime),
            )
            kind = "делегированную " if target_runtime is delegate_runtime else ""
            send_plain(chat_id, f"Продолжаю {kind}сессию {thread_id[:8]}.")
        return True
    if cmd == "status":
        try:
            selected_model(runtime)
        except Exception as exc:
            log(f"Could not resolve model for status: {exc}")
        with state_lock:
            snapshot = dict(chat_state(state_key))
        send_plain(chat_id, "ℹ️ Статус\n"
                   f"Сессия: {(snapshot.get('thread_id') or 'нет')[:8]}\n"
                   f"Модель: {snapshot.get('model') or 'не определена'}\n"
                   f"Мощность: {snapshot.get('effort') or 'не определена'}\n"
                   f"Sandbox: {snapshot.get('sandbox')}\n"
                   f"Workspace: {snapshot.get('workspace')}\n"
                   f"Занят: {'да' if (runtime.busy or runtime.pending_batch) else 'нет'}\n"
                   f"Аккаунт: {snapshot.get('account_status') or 'не подключён'}")
        return True
    if cmd == "usage":
        send_plain(chat_id, build_usage_report(runtime))
        return True
    if cmd == "compact":
        with state_lock:
            thread_id = chat_state(state_key).get("thread_id")
        if not thread_id:
            send_plain(chat_id, "Нет активной сессии для сжатия.")
            return True
        with process_lock:
            if runtime.busy:
                already_busy = True
            else:
                runtime.busy = True
                already_busy = False
        if already_busy:
            send_plain(chat_id, "Сначала дождись завершения текущего хода или используй /stop.")
        else:
            threading.Thread(
                target=run_compaction, args=(runtime, thread_id), daemon=True
            ).start()
        return True
    if cmd == "model":
        try:
            models = available_models(runtime)
            if not arg:
                send_rich(chat_id, render_model_picker(runtime))
                return True
            chosen = resolve_model_choice(models, arg)
            if chosen is None:
                send_rich(chat_id, f"Модель «{arg}» недоступна.\n\n{render_model_picker(runtime)}")
                return True
            supported = supported_reasoning_efforts(chosen)
            current_effort = chat_state(state_key).get("effort")
            effort = (current_effort if current_effort in supported else
                      chosen.get("defaultReasoningEffort") or (supported[0] if supported else None))
            if (chat_state(state_key).get("model") != model_key(chosen)
                    or chat_state(state_key).get("effort") != effort):
                cancel_pending_batch(runtime)
            update_state(state_key, model=model_key(chosen), effort=effort)
            send_plain(chat_id, f"🧠 Модель: {chosen.get('displayName') or model_key(chosen)}\n"
                       f"Мощность: {effort or 'не поддерживается'}")
        except Exception as exc:
            send_plain(chat_id, f"Не удалось получить список моделей: {compact(str(exc), 500)}")
        return True
    if cmd == "effort":
        try:
            models = available_models(runtime)
            chosen = selected_model(runtime, models)
            if not chosen:
                send_plain(chat_id, "Codex не вернул доступных моделей.")
                return True
            if not arg:
                send_rich(chat_id, render_effort_picker(runtime))
                return True
            effort = resolve_effort_choice(chosen, arg)
            if effort is None:
                send_rich(chat_id, f"Мощность «{arg}» недоступна для {model_key(chosen)}.\n\n"
                          f"{render_effort_picker(runtime)}")
                return True
            if chat_state(state_key).get("effort") != effort:
                cancel_pending_batch(runtime)
            update_state(state_key, effort=effort)
            send_plain(chat_id, f"⚡ Мощность {chosen.get('displayName') or model_key(chosen)}: {effort}")
        except Exception as exc:
            send_plain(chat_id, f"Не удалось получить уровни мощности: {compact(str(exc), 500)}")
        return True
    if cmd == "mode":
        aliases = {"read": "read-only", "read-only": "read-only", "write": "workspace-write",
                   "workspace-write": "workspace-write", "full": "danger-full-access",
                   "danger-full-access": "danger-full-access"}
        if arg not in aliases:
            send_plain(chat_id, "Использование: /mode read-only|workspace-write|full")
        else:
            if chat_state(state_key).get("sandbox") != aliases[arg]:
                cancel_pending_batch(runtime)
            update_state(state_key, sandbox=aliases[arg])
            send_plain(chat_id, f"Sandbox: {aliases[arg]}.")
        return True
    if cmd == "workspace":
        path = CODEX_CWD if arg.lower() == "default" else os.path.abspath(os.path.expanduser(arg))
        if not arg:
            send_plain(chat_id, f"Workspace: {chat_state(state_key).get('workspace')}\nИспользование: /workspace <путь>|default")
        elif not os.path.isdir(path):
            send_plain(chat_id, f"Директория не существует: {path}")
        else:
            if chat_state(state_key).get("workspace") != path:
                cancel_pending_batch(runtime)
            update_state(state_key, workspace=path)
            send_plain(chat_id, f"Workspace: {path}")
        return True
    if cmd == "restart":
        if chat_id != OWNER_ID:
            send_plain(chat_id, "Перезапуск доступен только владельцу бота.")
            return True
        cancel_pending_batch(runtime)
        request_restart(chat_id)
        return True
    if cmd == "update":
        if chat_id != OWNER_ID:
            send_plain(chat_id, "Обновление доступно только владельцу бота.")
            return True
        # /update configures the owner's shared local server, never a
        # currently selected delegated tenant.
        runtime = get_tenant(OWNER_ID)
        if runtime.update_flow_stage == "installing":
            send_plain(chat_id, "Установка уже идёт, дождись.")
            return True
        cancel_pending_batch(runtime)
        send_plain(chat_id, "⬇️ Обновляю из git...")
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.sh")
        try:
            result = subprocess.run(
                [script_path], capture_output=True, text=True, timeout=120, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            send_plain(chat_id, f"❌ Обновление не удалось:\n```\n{compact(str(exc), 2000)}\n```")
            return True
        if result.returncode != 0:
            failure = (result.stderr.strip() or result.stdout.strip() or
                       f"update.sh завершился с кодом {result.returncode}")
            send_plain(chat_id, f"❌ Обновление не удалось:\n```\n{failure[-2000:]}\n```")
            return True
        summary = next(
            (line.strip() for line in reversed(result.stdout.splitlines()) if line.strip()),
            "Обновление завершено.",
        )
        configured_url = _configured_local_bot_api_url()
        if not configured_url:
            with process_lock:
                runtime.update_flow_stage = "awaiting_yes_no"
                runtime.update_flow_api_id = None
            send_plain(chat_id, f"✅ {summary}")
            send_plain(
                chat_id,
                "Включить приём файлов до 2 ГБ через локальный Bot API сервер? "
                "Ответь Да или Нет.",
            )
            # The install itself only starts once the owner actually answers
            # "да" -- see handle_update_flow_message. Asking here and then
            # starting anyway regardless of the answer would defeat the
            # entire point of asking.
            return True
        repair_status = repair_local_bot_api_service()
        request_restart(chat_id)
        suffix = f"\n{repair_status}" if repair_status else ""
        if runtime.busy or runtime.pending_batch:
            send_plain(chat_id, f"✅ {summary}{suffix}\n🔁 Перезапуск запланирован после завершения текущего хода.")
        else:
            send_plain(chat_id, f"✅ {summary}{suffix}\n🔁 Перезапуск запланирован между ходами.")
        return True
    if cmd == "stop":
        cancel_pending_batch(runtime)
        running = stop_current_process(runtime)
        if running:
            send_plain(chat_id, "⏹ Останавливаю текущее выполнение Codex.")
        else:
            send_plain(chat_id, "Сейчас нечего останавливать.")
        return True
    if raw_cmd.startswith(("/", ".")):
        send_plain(chat_id, "Неизвестная команда. Открой меню команд Telegram.")
        return True
    return False


def process_message_inputs(runtime, message):
    """Download media and queue its inputs without holding the update poller."""
    chat_id = message.get("chat", {}).get("id")
    try:
        inputs, media_paths, attachment_failures = message_inputs(message)
    except UnsupportedAttachmentError as exc:
        send_plain(chat_id, str(exc))
        return
    except Exception as exc:
        send_plain(chat_id, f"Не смог обработать вложение: {compact(str(exc), 500)}")
        return
    for failure in attachment_failures:
        send_plain(chat_id, str(failure))
    if inputs:
        queue_message(runtime, inputs, media_paths)


# Fields message_inputs() actually downloads. has_media (used for command
# routing) is broader -- video/animation/sticker/video_note never reach
# download_telegram_file, so counting them here would promise a download
# status update that never arrives.
DOWNLOADED_MEDIA_FIELDS = ("photo", "document", "voice", "audio")
DOWNLOAD_STATUS_EDIT_MIN_INTERVAL_S = 1.0


def _has_downloadable_media(message):
    return any(message.get(field) for field in DOWNLOADED_MEDIA_FIELDS)


def _update_download_status(queue_state, runtime, force=False):
    """Best-effort progress line for a local-mode download in progress.

    Not a percentage -- the local Bot API server gives no progress signal
    for an in-flight getFile, only "done" once it has the whole file. Count
    of files finished vs. known-queued is the honest signal we actually have.

    The message this creates is handed to the turn that eventually consumes
    this burst (see flush_pending_batch/run_turn), which edits it in place
    into the normal Thinking card instead of leaving two separate messages.
    """
    chat_id = runtime.chat_id
    total = queue_state["total"]
    done = queue_state["done"]
    text = f"📥 Загружаю вложение ({done}/{total})…" if total > 1 else "📥 Загружаю вложение…"
    now = time.monotonic()
    msg_id = queue_state["status_msg_id"]
    if msg_id is None:
        result = tg_call("sendMessage", {"chat_id": chat_id, "text": text})
        if result.get("ok"):
            msg_id = (result.get("result") or {}).get("message_id")
            queue_state["status_msg_id"] = msg_id
            queue_state["status_last_edit_at"] = now
            if msg_id is not None:
                with process_lock:
                    runtime.pending_progress_msg_id = msg_id
        return
    if not force and now - queue_state.get("status_last_edit_at", 0.0) < DOWNLOAD_STATUS_EDIT_MIN_INTERVAL_S:
        return
    tg_call("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": text})
    queue_state["status_last_edit_at"] = now


def _process_local_message_queue(chat_id):
    """Process one chat's local-Bot-API updates in their arrival order."""
    while True:
        with process_lock:
            queue_state = local_message_queues.get(chat_id)
            if queue_state is None or not queue_state["messages"]:
                local_message_queues.pop(chat_id, None)
                return
            runtime, message = queue_state["messages"].popleft()
        has_media = _has_downloadable_media(message)
        if has_media:
            try:
                _update_download_status(queue_state, runtime)
            except Exception as exc:
                log(f"chat={chat_id} download status update failed: {exc}")
        try:
            process_message_inputs(runtime, message)
        except Exception as exc:
            # process_message_inputs handles normal attachment errors itself;
            # this last guard ensures an unexpected failure cannot strand a
            # chat's later updates behind this worker.
            log(f"chat={chat_id} local message worker failed: {exc}")
        if has_media:
            with process_lock:
                queue_state["done"] += 1
                just_completed = queue_state["done"] >= queue_state["total"]
            try:
                # force=True when this looks like the last outstanding file:
                # a fast download can finish inside the throttle window of
                # its own "starting" update, which would otherwise leave the
                # bubble stuck on a stale count with nothing left to fire a
                # later edit. If another file arrives after all, its own
                # "starting" update naturally supersedes this one.
                _update_download_status(queue_state, runtime, force=just_completed)
            except Exception as exc:
                log(f"chat={chat_id} download status update failed: {exc}")


def queue_local_message(runtime, message):
    """Append a local-mode update and ensure its per-chat FIFO worker runs."""
    chat_id = runtime.chat_id
    start_worker = False
    has_media = _has_downloadable_media(message)
    with process_lock:
        queue_state = local_message_queues.get(chat_id)
        if queue_state is None:
            queue_state = {
                "messages": deque(), "running": False,
                "status_msg_id": None, "status_last_edit_at": 0.0,
                "total": 0, "done": 0,
            }
            local_message_queues[chat_id] = queue_state
        queue_state["messages"].append((runtime, message))
        if has_media:
            queue_state["total"] += 1
        already_showing = queue_state["status_msg_id"] is not None
        if not queue_state["running"]:
            queue_state["running"] = True
            start_worker = True
    if has_media and already_showing:
        # A download is already visibly in progress for this chat; refresh
        # its count now instead of waiting for the current file to finish,
        # so the bubble doesn't sit on a stale total while more files queue
        # up behind it. force=True: a new file arriving is discrete, useful
        # information, not a repetitive tick the throttle is meant to guard
        # against.
        try:
            _update_download_status(queue_state, runtime, force=True)
        except Exception as exc:
            log(f"chat={chat_id} download status update failed: {exc}")
    if start_worker:
        threading.Thread(
            target=_process_local_message_queue, args=(chat_id,), daemon=True,
        ).start()


def handle_message(message):
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    if str(user_id) not in load_whitelist():
        if chat_id:
            send_plain(chat_id, "⛔ Доступ к Codex-боту не разрешён. Попроси владельца добавить твой Telegram ID в whitelist.txt.")
        return
    owner_runtime = get_tenant(chat_id)
    raw_text = (
        message.get("text")
        or message.get("caption")
        or rich_message_to_markdown(message.get("rich_message"))
        or ""
    )
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    forwarded = is_forwarded_message(message)
    rich_message = bool(message.get("rich_message"))
    attachment_note = message_attachment_note(message)
    if not text and not attachment_note and not forwarded and not rich_message:
        return
    with process_lock:
        draining = restart_draining
    if draining:
        send_plain(chat_id, "🔄 Уже начинаю перезапуск; сообщение пока не принято.")
        return
    # A reply target, rather than message ordering, authorizes persona edits.
    # Handle it before update/command/Codex routing so the content is never a
    # prompt or an update-flow credential.
    if handle_persona_reply(message):
        return
    # This is deliberately before command/Codex routing: API credentials and
    # setup replies must never become a Codex turn or persistent chat state.
    if handle_update_flow_message(chat_id, text, owner_runtime):
        return
    # A forwarded message is data, not a command.  Otherwise forwarding a
    # message beginning with /new or /stop would execute that command instead
    # of sending the forwarded content to Codex.
    has_media = any(message.get(field) for field in (
        "photo", "document", "animation", "video", "video_note", "voice", "audio", "sticker",
    ))
    if text.startswith(("/", ".")) and not forwarded and not rich_message and not has_media:
        command_name = text.split(None, 1)[0].split("@", 1)[0].lstrip("/.").lower()
        command_runtime = active_delegate_tenant(chat_id) or owner_runtime
        handle_command(chat_id, text, runtime=command_runtime)
        return
    runtime = active_delegate_tenant(chat_id) or owner_runtime
    account_status = chat_state(runtime.state_key).get("account_status")
    if (chat_id != OWNER_ID and account_status != "ready"
            and (account_status == "awaiting_display_name" or not account_is_ready(runtime))):
        if account_status == "awaiting_login":
            send_plain(chat_id, "Сначала заверши вход в Codex по ранее выданной ссылке.")
        elif account_status == "awaiting_display_name":
            tenant_dir = tenant_codex_home(chat_id)
            if tenant_dir is not None:
                agents_path = tenant_dir / "AGENTS.md"
                try:
                    personality = agents_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    pass
                else:
                    agents_path.write_text(personality.replace("<user>", text), encoding="utf-8")
            update_state(runtime.state_key, account_status="ready")
            send_plain(chat_id, "✅ Запомнил, как к тебе обращаться.")
        else:
            threading.Thread(target=start_account_login, args=(runtime,), daemon=True).start()
        return
    if LOCAL_BOT_API:
        # A later text or another attachment must not overtake a getFile that
        # is still downloading for this chat. Other chats have other workers.
        queue_local_message(runtime, message)
        return
    process_message_inputs(runtime, message)


def register_commands():
    payload = {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}
    tg_call("setMyCommands", payload)
    tg_call("setMyCommands", {**payload, "scope": {"type": "all_private_chats"}})


def main():
    offset = None
    register_commands()
    try:
        _ensure_tenant_mcp_config(tenant_codex_home(OWNER_ID), OWNER_ID)
    except Exception as exc:
        log(f"owner could not seed tenant MCP servers: {exc}")
    with state_lock:
        runtime_state = state_db.get("runtime", {})
        completed_restart_chat_id = runtime_state.get("restart_completed_chat_id")
        completed_restart_message_id = runtime_state.get("restart_message_id")
    if completed_restart_chat_id:
        update_runtime_state(restart_completed_chat_id=None, restart_message_id=None)
        if completed_restart_message_id:
            result = edit_plain(
                completed_restart_chat_id, completed_restart_message_id,
                "✅ Перезагрузка окончена, бот готов к работе.",
            )
            if not result.get("ok"):
                send_plain(completed_restart_chat_id, "✅ Перезагрузка окончена, бот готов к работе.")
        else:
            send_plain(completed_restart_chat_id, "✅ Перезагрузка окончена, бот готов к работе.")
    try:
        owner_runtime = get_tenant(OWNER_ID)
        get_app_server(owner_runtime).start_if_needed()
        refresh_rate_limits(owner_runtime)
        log("Persistent Codex app-server is ready")
    except Exception as exc:
        log(f"Codex app-server warm start failed; will retry on first message: {exc}")
    threading.Thread(target=restart_watcher, daemon=True).start()
    threading.Thread(target=external_request_watcher, daemon=True).start()
    threading.Thread(target=cross_delegate_watcher, daemon=True).start()
    threading.Thread(target=file_send_queue_watcher, daemon=True).start()
    threading.Thread(target=pending_delivery_watcher, daemon=True).start()
    retry_pending_deliveries()
    log(f"Codex Telegram bot started; owner={OWNER_ID}, cwd={CODEX_CWD}")
    while True:
        params = {"timeout": 30, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = tg_call("getUpdates", params, timeout=40)
        if not result.get("ok"):
            time.sleep(1)
            continue
        for update in result.get("result", []):
            try:
                offset = max(offset or 0, update["update_id"] + 1)
                message = update.get("message")
                if isinstance(message, dict):
                    handle_message(message)
            except Exception as exc:
                log(f"Unexpected update handler error: {exc}")
                chat_id = (update.get("message") or {}).get("chat", {}).get("id")
                if chat_id == OWNER_ID:
                    send_plain(chat_id, "⚠️ Ошибка моста. Подробности записаны в лог.")


if __name__ == "__main__":
    main()
