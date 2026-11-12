"""Optional link to ClaudeAsk / CodexAsk: Telegram actions for a chat's owner.

When telegram-ai-core is installed on this host and the person talking to the
bridge is the registered owner of a userbot instance there, their agent gets
that instance's Telegram-action tools (send a message, read a chat's history,
manage groups and triggers, ...). The actions run in the owner's own Telegram
account, through their userbot.

Who may use which instance is not decided here and not by the model: the
relay's owners map says which instances a Telegram id owns, and the token
minted for the bridge is bound to exactly one of those instances. Someone who
owns no instance gets no tools; nobody gets another person's.

This module only works out the binding; each bridge writes it into its own
agent's MCP configuration. Everything is optional: without telegram-ai-core
`resolve_binding` returns None and the bridge behaves exactly as before.
"""
import json
import os
import subprocess

SERVER_NAME = "telegram-actions"
CACHE_FILE = ".telegram_actions.json"
CLI_TIMEOUT_S = 30


def registry_component(name):
    """One entry of the Telegram AI component registry, or None.

    ~/.config/telegram-ai/registry.json is written by each product's setup.sh
    (format: telegram-ai-core's README). An entry whose checkout is gone is
    treated as absent.
    """
    base = os.environ.get("TELEGRAM_AI_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "telegram-ai",
    )
    try:
        with open(os.path.join(base, "registry.json"), encoding="utf-8") as handle:
            entry = json.load(handle)["components"][name]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        return None
    return entry if os.path.isdir(entry["path"]) else None


def _relay_cli(core, relay, *arguments):
    """Run the relay's own admin CLI against the relay's own configuration."""
    env = dict(os.environ)
    config_dir = relay.get("config_dir")
    if config_dir:
        env["TELEGRAM_AI_CONFIG_DIR"] = config_dir
        env.pop("JARVIS_RELAY_CONFIG_DIR", None)
    result = subprocess.run(
        [core["python"], os.path.join(core["path"], "cmd_queue.py"), *arguments],
        env=env, capture_output=True, text=True, timeout=CLI_TIMEOUT_S, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"relay CLI exited with {result.returncode}")
    return result.stdout.strip()


def _choose_instance(owned, prefer_product):
    """One of the instances this person owns: the one the matching product
    (ClaudeAsk for the Claude bridge, CodexAsk for the Codex bridge) serves if
    that is known, otherwise the first by name."""
    preferred = registry_component(prefer_product) or {}
    served = [item.strip() for item in str(preferred.get("instances") or "").split(",") if item.strip()]
    for instance in owned:
        if instance in served:
            return instance
    return owned[0]


def resolve_binding(chat_id, tenant_dir, prefer_product):
    """How this chat's agent should start the Telegram-action MCP server:
    {"command", "args", "env", "instance"} -- or None when there is nothing
    to bind (no core on this host, or this person owns no instance).

    The minted token is kept in the tenant's own directory and reused, so a
    restart does not pile up tokens; it is re-minted if the person's instance
    changed.
    """
    core = registry_component("core")
    relay = registry_component("relay")
    if not core or not relay or not core.get("python") or not relay.get("url"):
        return None
    server = os.path.join(core["path"], "telegram_actions_mcp.py")
    if not os.path.isfile(server) or not os.path.isfile(core["python"]):
        return None
    try:
        owned = [line for line in _relay_cli(core, relay, "--owned-by", str(int(chat_id))).splitlines() if line]
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        return None
    cache_path = os.path.join(tenant_dir, CACHE_FILE)
    if not owned:
        try:
            os.unlink(cache_path)
        except OSError:
            pass
        return None
    instance = _choose_instance(owned, prefer_product)
    token = None
    try:
        with open(cache_path, encoding="utf-8") as handle:
            cached = json.load(handle)
        if cached.get("instance") == instance and isinstance(cached.get("token"), str):
            token = cached["token"]
    except (OSError, ValueError, AttributeError):
        pass
    if not token:
        try:
            token = _relay_cli(core, relay, "--add-bridge-token", instance)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return None
        if not token:
            return None
        temporary = cache_path + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"instance": instance, "token": token}, handle)
        os.replace(temporary, cache_path)
    return {
        "instance": instance,
        "command": core["python"],
        "args": [server],
        "env": {
            "JARVIS_RELAY_URL": relay["url"],
            "JARVIS_RELAY_TOKEN": token,
            "INSTANCE_ID": instance,
            # No chat request stands behind these calls: see the MCP server.
            "JARVIS_RELAY_DIRECT": "1",
        },
    }
