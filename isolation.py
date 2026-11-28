"""Optional Unix tenant boundary for the Codex bridge."""

import json
import hashlib
import os
import re
import stat
import subprocess
import threading
import sys
from pathlib import Path
from tenant_exchange import open_absolute_dir


BRIDGE_ISOLATION = os.environ.get("BRIDGE_ISOLATION", "off")
ISOLATED_TENANTS = frozenset(x.strip() for x in os.environ.get("ISOLATED_TENANTS", "").split(",") if x.strip())
BRIDGE_TENANT_PROVISION = os.environ.get("BRIDGE_TENANT_PROVISION", "/usr/local/sbin/tenant-provision")
BRIDGE_TENANT_RUN = os.environ.get("BRIDGE_TENANT_RUN", "/usr/local/sbin/tenant-run")
_cache = {}
_lock = threading.Lock()
_missing_mcp_warned = False


class TenantUnavailable(RuntimeError):
    """Provisioning or the isolated runner refused this tenant."""


def tenant_identity(chat_id):
    value = str(chat_id)
    return (value.removeprefix("delegate:"), "delegate" if value.startswith("delegate:") else "main")


def tenant_target(chat_id):
    """Map a chat slot to the provision kind and runner product."""
    tenant_id, slot = tenant_identity(chat_id)
    if slot == "main":
        return "main", "bridge"
    if tenant_id == os.environ.get("OWNER_ID"):
        return "delegate", "bridge"
    return "main", "delegate"


def tenant_isolated(chat_id):
    tenant_id, _ = tenant_identity(chat_id)
    return BRIDGE_ISOLATION == "on" and tenant_id in ISOLATED_TENANTS


def ensure_tenant(chat_id, kind=None):
    tenant_id, inferred_kind = tenant_identity(chat_id)
    target_kind = tenant_target(chat_id)[0]
    if tenant_isolated(chat_id) and kind is not None and kind != target_kind:
        raise TenantUnavailable("tenant kind differs from target")
    kind = (target_kind if tenant_isolated(chat_id) else inferred_kind) if kind is None else kind
    if not tenant_id.isdecimal() or kind not in ("main", "delegate"):
        raise TenantUnavailable("invalid tenant identity")
    key = (tenant_id, kind)
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        result = subprocess.run(
            ["sudo", "-n", BRIDGE_TENANT_PROVISION, "ensure", tenant_id, "--kind", kind],
            capture_output=True, text=True, timeout=30, cwd="/", env={"PATH": "/usr/bin:/bin"},
        )
        if result.returncode:
            raise ValueError((result.stderr or result.stdout).strip() or f"exit {result.returncode}")
        data = json.loads(result.stdout)
        if (not isinstance(data, dict) or str(data.get("id")) != tenant_id or data.get("kind") != kind
                or not isinstance(data.get("uid"), int)
                or not all(isinstance(data.get(k), str) and os.path.isabs(data[k]) for k in ("home", "exchange"))):
            raise ValueError("invalid tenant-provision response")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise TenantUnavailable(f"tenant {tenant_id} ({kind}): {exc}") from exc
    with _lock:
        _cache[key] = data
    return data


def workspace_relative(value, home):
    """Return a path rooted in workspace, rejecting traversal before normalization."""
    if not isinstance(value, str) or not value or "\x00" in value or ".." in value.split("/"):
        raise ValueError("invalid workspace")
    root = os.path.join(home, "workspace")
    if os.path.isabs(value):
        normalized = os.path.normpath(value)
        if normalized != root and not normalized.startswith(root + os.sep):
            raise ValueError("workspace outside tenant root")
        relative = os.path.relpath(normalized, home)
    else:
        relative = os.path.normpath(value)
        if relative != "workspace" and not relative.startswith("workspace/"):
            relative = os.path.normpath(os.path.join("workspace", relative))
    if relative != "workspace" and not relative.startswith("workspace/"):
        raise ValueError("workspace outside tenant root")
    return relative


def workspace_for_state(value):
    if (isinstance(value, str) and (value == "workspace" or value.startswith("workspace/"))
            and ".." not in value.split("/") and not os.path.isabs(value)):
        return value
    return "workspace"


def tenant_run_argv(chat_id, kind=None, cwd="workspace", env=None, command="codex", args=()):
    tenant_id, _ = tenant_identity(chat_id)
    target_kind, product = tenant_target(chat_id)
    if tenant_isolated(chat_id):
        kind = target_kind
    else:
        kind = tenant_identity(chat_id)[1] if kind is None else kind
        product = "bridge"
    info = ensure_tenant(chat_id, kind)
    relative = workspace_relative(cwd, info["home"])
    argv = ["sudo", "-n", BRIDGE_TENANT_RUN, tenant_id, "--kind", kind,
            "--product", product, "--cwd", relative]
    allowed = {"CHAT_ID", "INSTANCE_ID", "TOPIC_ID", "EXCLUDE_MSG_ID",
               "CLAUDE_TELEGRAM_OUTBOX", "MAX_THINKING_TOKENS"}
    for key, value in (env or {}).items():
        if key not in allowed and not (key.startswith("DISABLE_") and key[8:].replace("_", "").isupper()):
            raise TenantUnavailable(f"tenant-run environment key denied: {key}")
        argv.extend(("--env", f"{key}={value}"))
    return argv + ["--", command, *args]


def tenant_control_dir(chat_id):
    tenant_id, kind = tenant_identity(chat_id)
    if not tenant_id.isdecimal():
        raise ValueError("invalid tenant id")
    root = Path(os.environ.get("BRIDGE_TENANT_CONTROL_DIR") or Path(__file__).with_name("tenant_control")).absolute()
    base = root / tenant_id
    directory = base / "delegate" if kind == "delegate" else base
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (root, base, directory):
        path.chmod(0o700)
    return directory


def atomic_control_write(path, contents, mode):
    """Restore a file without following tenant-owned links or reading unbounded data."""
    path = Path(path)
    directory_fd = open_absolute_dir(str(path.parent))
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if stat.S_ISREG(info.st_mode) and info.st_size == len(contents) and handle.read(len(contents) + 1) == contents:
                    if stat.S_IMODE(info.st_mode) != mode:
                        os.fchmod(handle.fileno(), mode)
                    return
        except OSError:
            pass
        temporary = f".bridge-control-{os.urandom(12).hex()}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode, dir_fd=directory_fd)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory_fd)


def isolated_mcp_servers(chat_id):
    """Single bridge-controlled list of servers for this tenant process."""
    global _missing_mcp_warned
    tenant_id, _ = tenant_identity(chat_id)
    kind, _ = tenant_target(chat_id)
    if kind != "main":
        return {}
    directory = Path(os.environ.get("BRIDGE_ISOLATED_MCP_DIR", "/opt/telegram-ai/mcp"))
    script = directory / "codex_send_telegram_file_mcp.py"
    servers = {}
    exchange = ensure_tenant(chat_id)["exchange"]
    if not script.is_file():
        if not _missing_mcp_warned:
            print(f"Isolated file MCP unavailable: {script}", file=sys.stderr, flush=True)
            _missing_mcp_warned = True
    else:
        servers["send-telegram-file"] = {"command": "/usr/bin/python3", "args": [str(script)],
                                          "env": {"CHAT_ID": tenant_id, "TENANT_EXCHANGE_DIR": exchange}}
    from delegate_to_claude_mcp import claude_bridge_dir
    delegate_script = directory / "delegate_to_claude_exchange_mcp.py"
    if delegate_script.is_file() and claude_bridge_dir() is not None:
        servers["delegate-to-claude"] = {"command": "/usr/bin/python3", "args": [str(delegate_script)],
                                          "env": {"CHAT_ID": tenant_id, "TENANT_EXCHANGE_DIR": exchange},
                                          "tool_timeout_sec": 1900}
    return servers


def isolated_mcp_overrides(chat_id):
    # "apps" is the built-in ChatGPT connectors MCP server (codex_apps): it appears
    # with any ChatGPT login and is not part of the bridge allowlist, so turn it off.
    overrides = ["mcp_servers={}", "features.apps=false"]
    for name, server in isolated_mcp_servers(chat_id).items():
        for key in ("command", "args"):
            overrides.append(f"mcp_servers.{name}.{key}={json.dumps(server[key])}")
        for key, value in server["env"].items():
            overrides.append(f"mcp_servers.{name}.env.{key}={json.dumps(value)}")
        if "tool_timeout_sec" in server:
            overrides.append(f"mcp_servers.{name}.tool_timeout_sec={server['tool_timeout_sec']}")
    return tuple(overrides)


ISOLATION_NOTE = """
## Среда выполнения

Ты работаешь в изолированной среде под отдельным непривилегированным пользователем.
`sudo`, установка пакетов через `apt`/`dpkg` и любое повышение прав здесь недоступны:
это штатное ограничение, а не ошибка. Нужные инструменты ставь в пользовательское
пространство: `pip install --user` или виртуальное окружение (`python3 -m venv`),
`npm` с префиксом в домашней папке, готовые бинарники в `~/.local/bin`. Если без
системного пакета не обойтись, не пытайся обойти ограничение, а скажи пользователю,
что такой пакет должен поставить владелец сервера.
""".strip()


ADMIN_ISOLATION_NOTE = """
## Среда выполнения

Ты работаешь как администратор сервера под отдельным пользователем с `sudo` без пароля.
Серверные операции (перезапуск служб, чтение журналов, установка пакетов) выполняй через
`sudo` осознанно: сначала выясни, что именно изменится, и не делай разрушающих действий
без явной просьбы пользователя. Домашняя папка основного пользователя сервера и файлы
других тенантов закрыты обычными правами: заходи туда через `sudo` и только когда этого
требует задача.
""".strip()


def with_isolation_note(text, role="member"):
    """Persona text as the isolated agent sees it: the stored persona plus the fixed runtime note for its role."""
    note = ADMIN_ISOLATION_NOTE if role == "admin" else ISOLATION_NOTE
    if note in text:
        return text
    return text.rstrip() + "\n\n" + note + "\n"


def ensure_tenant_control(chat_id, template, file_send_section=None):
    directory = tenant_control_dir(chat_id)
    persona = directory / "persona.md"
    if not persona.exists():
        atomic_control_write(persona, template.encode("utf-8"), 0o600)
        atomic_control_write(directory / ".persona_default_sha256", hashlib.sha256(template.encode()).hexdigest().encode() + b"\n", 0o600)
    recorded = directory / ".file_send_section"
    content = persona.read_text(encoding="utf-8")
    old_section = recorded.read_text(encoding="utf-8") if recorded.exists() else None
    if old_section and old_section in content and old_section != file_send_section:
        content = content.replace("\n\n" + old_section, "", 1)
    # The stock persona predates isolation and contains guidance for the
    # legacy outbox; that path is inaccessible to a Unix tenant.
    legacy = re.search(r"(?ms)^## Отправка файлов в Telegram\n.*?(?=^## |^# |\Z)", content)
    if legacy and "CODEX_TELEGRAM_OUTBOX" in legacy.group():
        content = content[:legacy.start()] + content[legacy.end():]
    if file_send_section and "## Отправка файлов в Telegram" not in content:
        content = content.rstrip() + "\n\n" + file_send_section.strip() + "\n"
    if content != persona.read_text(encoding="utf-8"):
        old_digest = hashlib.sha256(persona.read_bytes()).hexdigest()
        marker = directory / ".persona_default_sha256"
        was_default = marker.exists() and marker.read_text(encoding="ascii").strip() == old_digest
        atomic_control_write(persona, content.encode(), 0o600)
        if was_default:
            atomic_control_write(marker, hashlib.sha256(content.encode()).hexdigest().encode() + b"\n", 0o600)
    if file_send_section:
        atomic_control_write(recorded, file_send_section.encode(), 0o600)
    elif recorded.exists():
        recorded.unlink()
    mcp = json.dumps({"mcpServers": isolated_mcp_servers(chat_id)}, ensure_ascii=False, indent=2).encode() + b"\n"
    atomic_control_write(directory / "mcp.json", mcp, 0o600)
    return directory


def stage_tenant_control(chat_id, template, file_send_section=None):
    directory = ensure_tenant_control(chat_id, template, file_send_section)
    info = ensure_tenant(chat_id)
    exchange = Path(info["exchange"])
    suffix = ".delegate" if tenant_identity(chat_id)[1] == "delegate" else ""
    for stem, extension in (("persona", "md"), ("mcp", "json")):
        name = f"{stem}.{extension}"
        data = (directory / name).read_bytes()
        if stem == "persona":
            # The fixed runtime note is added to the staged copy only, never to the stored persona.
            data = with_isolation_note(data.decode("utf-8"), info.get("role", "member")).encode("utf-8")
        atomic_control_write(exchange / f"{stem}{suffix}.{extension}", data, 0o644)
    return info


def assert_empty_mcp(chat_id, kind, cwd="workspace"):
    """Compare effective Codex MCP configuration with the bridge allowlist."""
    state_key = chat_id
    overrides = isolated_mcp_overrides(state_key)
    try:
        def listed(extra):
            args = ["mcp", "list", "--json"]
            for override in extra:
                args.extend(("-c", override))
            argv = tenant_run_argv(state_key, kind, cwd=cwd, command="codex", args=args)
            result = subprocess.run(argv, capture_output=True, text=True, timeout=35,
                                    cwd="/", env={"PATH": "/usr/bin:/bin"})
            if result.returncode:
                raise ValueError((result.stderr or result.stdout).strip() or f"exit {result.returncode}")
            return json.loads(result.stdout)

        # -c merges tables, so a tenant's own entry with the approved name
        # could otherwise be hidden by the bridge's command override.
        inherited = listed(("mcp_servers={}",))
        if not isinstance(inherited, list) or inherited:
            raise ValueError("tenant configured an MCP server")
        servers = listed(overrides)
        expected = isolated_mcp_servers(state_key)
        if not isinstance(servers, list) or len(servers) != len(expected):
            raise ValueError("tenant MCP list differs from bridge allowlist")
        seen = set()
        for entry in servers:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ValueError("invalid tenant MCP entry")
            name = entry["name"]
            if name in seen or name not in expected or entry.get("enabled") is not True:
                raise ValueError("tenant MCP list differs from bridge allowlist")
            seen.add(name)
            transport = entry.get("transport")
            server = expected[name]
            if (not isinstance(transport, dict) or transport.get("type") != "stdio"
                    or transport.get("command") != server["command"]
                    or transport.get("args") != server["args"]
                    or transport.get("env") != server["env"]
                    or transport.get("env_vars") != [] or transport.get("cwd") is not None
                    or entry.get("tool_timeout_sec") != server.get("tool_timeout_sec")):
                raise ValueError("tenant MCP command differs from bridge allowlist")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise TenantUnavailable(f"tenant MCP preflight: {exc}") from exc
