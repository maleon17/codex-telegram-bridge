"""Optional Unix tenant boundary for the Codex bridge."""

import json
import hashlib
import os
import stat
import subprocess
import tempfile
import threading
from pathlib import Path


BRIDGE_ISOLATION = os.environ.get("BRIDGE_ISOLATION", "off")
ISOLATED_TENANTS = frozenset(x.strip() for x in os.environ.get("ISOLATED_TENANTS", "").split(",") if x.strip())
BRIDGE_TENANT_PROVISION = os.environ.get("BRIDGE_TENANT_PROVISION", "/usr/local/sbin/tenant-provision")
BRIDGE_TENANT_RUN = os.environ.get("BRIDGE_TENANT_RUN", "/usr/local/sbin/tenant-run")
_cache = {}
_lock = threading.Lock()


class TenantUnavailable(RuntimeError):
    """Provisioning or the isolated runner refused this tenant."""


def tenant_identity(chat_id):
    value = str(chat_id)
    return (value.removeprefix("delegate:"), "delegate" if value.startswith("delegate:") else "main")


def tenant_isolated(chat_id):
    tenant_id, _ = tenant_identity(chat_id)
    return BRIDGE_ISOLATION == "on" and tenant_id in ISOLATED_TENANTS


def ensure_tenant(chat_id, kind=None):
    tenant_id, inferred_kind = tenant_identity(chat_id)
    kind = kind or inferred_kind
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


def tenant_run_argv(chat_id, kind, cwd="workspace", env=None, command="codex", args=()):
    tenant_id, _ = tenant_identity(chat_id)
    info = ensure_tenant(tenant_id, kind)
    relative = workspace_relative(cwd, info["home"])
    argv = ["sudo", "-n", BRIDGE_TENANT_RUN, tenant_id, "--kind", kind,
            "--product", "bridge", "--cwd", relative]
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
    root = Path(os.environ.get("BRIDGE_TENANT_CONTROL_DIR") or Path(__file__).with_name("tenant_control"))
    base = root / tenant_id
    directory = base / "delegate" if kind == "delegate" else base
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (root, base, directory):
        path.chmod(0o700)
    return directory


def atomic_control_write(path, contents, mode):
    """Restore a file without following tenant-owned links or reading unbounded data."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_size == len(contents) and handle.read(len(contents) + 1) == contents:
                if stat.S_IMODE(info.st_mode) != mode:
                    os.fchmod(handle.fileno(), mode)
                return
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=".bridge-control-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def isolated_mcp_servers(chat_id):
    """Single bridge-controlled list; C1b/C2 will add entries here."""
    return {}


def isolated_mcp_overrides(chat_id):
    if isolated_mcp_servers(chat_id):
        raise TenantUnavailable("isolated MCP overrides are not configured")
    return ("mcp_servers={}",)


def ensure_tenant_control(chat_id, template):
    directory = tenant_control_dir(chat_id)
    persona = directory / "persona.md"
    if not persona.exists():
        atomic_control_write(persona, template.encode("utf-8"), 0o600)
        atomic_control_write(directory / ".persona_default_sha256", hashlib.sha256(template.encode()).hexdigest().encode() + b"\n", 0o600)
    mcp = json.dumps({"mcpServers": isolated_mcp_servers(chat_id)}, ensure_ascii=False, indent=2).encode() + b"\n"
    atomic_control_write(directory / "mcp.json", mcp, 0o600)
    return directory


def stage_tenant_control(chat_id, template):
    directory = ensure_tenant_control(chat_id, template)
    info = ensure_tenant(chat_id)
    exchange = Path(info["exchange"])
    for name in ("persona.md", "mcp.json"):
        atomic_control_write(exchange / name, (directory / name).read_bytes(), 0o644)
    return info


def assert_empty_mcp(chat_id, kind, cwd="workspace"):
    """Codex merges -c tables; fail closed if the tenant added an MCP server."""
    argv = tenant_run_argv(chat_id, kind, cwd=cwd, command="codex", args=("mcp", "list", "--json", "-c", "mcp_servers={}"))
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=35,
                                cwd="/", env={"PATH": "/usr/bin:/bin"})
        if result.returncode:
            raise ValueError((result.stderr or result.stdout).strip() or f"exit {result.returncode}")
        servers = json.loads(result.stdout)
        if not isinstance(servers, list) or servers:
            raise ValueError("tenant MCP config is not empty")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise TenantUnavailable(f"tenant MCP preflight: {exc}") from exc
