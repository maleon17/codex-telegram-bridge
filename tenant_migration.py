"""Apply a requested Telegram tenant ID migration before bot state is loaded.

The live bot keeps state in memory, so an external rewrite while it is running
would be overwritten. This module runs during the next deferred restart, after
the old process and its app-server children have stopped.
"""

import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile


def _exists(path):
    return os.path.lexists(path)


def _atomic_text(path, text):
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _replace_whitelist_id(contents, old, new):
    old_pattern = re.compile(rf"(?<!\d){re.escape(old)}(?!\d)")
    new_pattern = re.compile(rf"(?<!\d){re.escape(new)}(?!\d)")
    old_count = len(old_pattern.findall(contents))
    new_count = len(new_pattern.findall(contents))
    if old_count == 1 and new_count == 0:
        return old_pattern.sub(new, contents)
    if old_count == 0 and new_count == 1:
        return contents  # A prior attempt already updated the allowlist.
    raise ValueError("migration requires exactly one old or new whitelist entry")


def _backup_once(backup, old_home, old_workspace, state_file, whitelist_file, old_last_turn, old, new):
    if backup.is_dir():
        manifest = backup / "manifest.json"
        if not manifest.is_file():
            raise RuntimeError(f"Incomplete migration backup: {backup}")
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        if saved != {"old_id": old, "new_id": new}:
            raise RuntimeError(f"Unexpected migration backup: {backup}")
        return
    if not old_home.is_dir():
        raise RuntimeError("Source account is missing before migration backup")
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{old}-to-{new}.", dir=backup.parent))
    shutil.copytree(old_home, staging / "account", symlinks=True)
    if old_workspace.is_dir():
        shutil.copytree(old_workspace, staging / "workspace", symlinks=True)
    shutil.copy2(state_file, staging / "state.json")
    shutil.copy2(whitelist_file, staging / "whitelist.txt")
    if old_last_turn.is_file():
        shutil.copy2(old_last_turn, staging / old_last_turn.name)
    (staging / "manifest.json").write_text(
        json.dumps({"old_id": old, "new_id": new}) + "\n", encoding="utf-8",
    )
    staging.rename(backup)


def _update_thread_paths(database, old_home, new_home, old_workspace, new_workspace):
    if not database.is_file():
        return
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE threads SET rollout_path = replace(rollout_path, ?, ?) "
            "WHERE instr(rollout_path, ?) > 0",
            (str(old_home), str(new_home), str(old_home)),
        )
        connection.execute(
            "UPDATE threads SET cwd = ? WHERE cwd = ?",
            (str(new_workspace), str(old_workspace)),
        )
        remaining = connection.execute(
            "SELECT count(*) FROM threads WHERE instr(rollout_path, ?) > 0 OR cwd = ?",
            (str(old_home), str(old_workspace)),
        ).fetchone()[0]
        if remaining:
            raise RuntimeError("Old tenant paths remain in the session index")


def _update_session_paths(home, old_workspace, new_workspace):
    """Change session metadata paths without altering historical message text."""
    sessions = home / "sessions"
    if not sessions.is_dir():
        return
    old, new = str(old_workspace), str(new_workspace)
    for rollout in sessions.rglob("*.jsonl"):
        original = rollout.read_text(encoding="utf-8")
        updated = []
        changed = False
        for line in original.splitlines(keepends=True):
            if not line.strip():
                updated.append(line)
                continue
            record = json.loads(line)
            payload = record.get("payload")
            edited = False
            if isinstance(payload, dict):
                if payload.get("cwd") == old:
                    payload["cwd"] = new
                    edited = True
                settings = payload.get("thread_settings")
                if isinstance(settings, dict) and settings.get("cwd") == old:
                    settings["cwd"] = new
                    edited = True
                roots = payload.get("workspace_roots")
                if isinstance(roots, list) and old in roots:
                    payload["workspace_roots"] = [new if root == old else root for root in roots]
                    edited = True
                state = payload.get("state")
                if isinstance(state, dict):
                    environments = state.get("environments")
                    if isinstance(environments, dict):
                        environments = environments.get("environments")
                        if isinstance(environments, dict):
                            local = environments.get("local")
                            if isinstance(local, dict) and local.get("cwd") == old:
                                local["cwd"] = new
                                edited = True
            updated.append(
                json.dumps(record, ensure_ascii=False) + ("\n" if line.endswith("\n") else "")
                if edited else line
            )
            changed |= edited
        if changed:
            original_stat = rollout.stat()
            _atomic_text(rollout, "".join(updated))
            os.utime(rollout, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))


def apply_pending_tenant_migration(
    state_file, whitelist_file, accounts_dir, owner_id,
    workspace_root="/var/tmp/codex-telegram-bot-workspaces",
):
    """Migrate one account from an exact, local request file; return if absent."""
    state_file = Path(state_file).resolve()
    whitelist_file = Path(whitelist_file).resolve()
    accounts_dir = Path(accounts_dir).resolve()
    request_file = state_file.with_name(".tenant-id-migration.json")
    if not request_file.is_file():
        return False
    request = json.loads(request_file.read_text(encoding="utf-8"))
    old, new = str(request.get("old_id", "")), str(request.get("new_id", ""))
    if (not old.isdecimal() or not new.isdecimal() or old == new
            or old == str(owner_id) or new == str(owner_id)):
        raise ValueError("Invalid tenant migration IDs")

    old_home, new_home = accounts_dir / old, accounts_dir / new
    workspace_root = Path(workspace_root).resolve()
    old_workspace, new_workspace = workspace_root / old, workspace_root / new
    old_last_turn = state_file.with_name(f"last_turn_{old}.json")
    new_last_turn = state_file.with_name(f"last_turn_{new}.json")
    backup = state_file.parent / ".tenant-id-migration-backups" / f"{old}-to-{new}"

    if _exists(old_home) and _exists(new_home):
        raise RuntimeError("Both tenant account directories exist; refusing to merge")
    if _exists(old_workspace) and _exists(new_workspace):
        raise RuntimeError("Both tenant workspaces exist; refusing to merge")
    if _exists(old_last_turn) and _exists(new_last_turn):
        raise RuntimeError("Both last-turn files exist; refusing to overwrite")
    if not state_file.is_file() or not whitelist_file.is_file():
        raise RuntimeError("Tenant state or whitelist file is missing")

    state = json.loads(state_file.read_text(encoding="utf-8"))
    chats = state.get("chats")
    if not isinstance(chats, dict) or (old in chats) == (new in chats):
        raise RuntimeError("Expected exactly one source or destination tenant in state")
    whitelist = whitelist_file.read_text(encoding="utf-8")
    updated_whitelist = _replace_whitelist_id(whitelist, old, new)
    _backup_once(backup, old_home, old_workspace, state_file, whitelist_file, old_last_turn, old, new)

    if _exists(old_home):
        old_home.rename(new_home)
    if _exists(old_workspace):
        old_workspace.rename(new_workspace)

    config = new_home / "config.toml"
    if config.is_file():
        before = config.read_text(encoding="utf-8")
        after = before.replace(old, new)
        if after != before:
            _atomic_text(config, after)
    _update_session_paths(new_home, old_workspace, new_workspace)
    _update_thread_paths(new_home / "state_5.sqlite", old_home, new_home, old_workspace, new_workspace)

    state = json.loads(state_file.read_text(encoding="utf-8"))
    chats = state["chats"]
    if old in chats:
        entry = chats.pop(old)
        if entry.get("workspace") == str(old_workspace):
            entry["workspace"] = str(new_workspace)
        pending_delivery = entry.get("pending_delivery")
        if isinstance(pending_delivery, dict) and pending_delivery.get("chat_id") == int(old):
            pending_delivery["chat_id"] = int(new)
        chats[new] = entry
        runtime = state.get("runtime")
        if isinstance(runtime, dict) and runtime.get("restart_completed_chat_id") == int(old):
            runtime["restart_completed_chat_id"] = int(new)
        _atomic_text(state_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    if updated_whitelist != whitelist:
        _atomic_text(whitelist_file, updated_whitelist)
    if _exists(old_last_turn):
        old_last_turn.rename(new_last_turn)
    request_file.unlink()
    return True
