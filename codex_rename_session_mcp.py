#!/usr/bin/env python3
"""MCP rename request over the existing tenant file exchange."""

import json
import os
import stat
import sys
import time
import unicodedata
import uuid
from pathlib import Path

from codex_send_telegram_file_mcp import _directories, _exchange_subdir


SERVER_INFO = {"name": "rename-session", "version": "1.0.0"}
TOOL = {
    "name": "rename_session",
    "description": "Задать название текущей сессии этого Telegram-чата.",
    "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}},
                    "required": ["title"], "additionalProperties": False},
}


def validate_title(value):
    if not isinstance(value, str) or any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError("Название должно быть строкой без управляющих символов.")
    title = " ".join(value.split())
    if not 1 <= len(title) <= 80:
        raise ValueError("Название должно содержать от 1 до 80 символов.")
    return title


def _write_request(name, title, chat_id):
    exchange = bool(os.environ.get("TENANT_EXCHANGE_DIR"))
    if exchange:
        queue_fd = _exchange_subdir("queue")
        temporary = f".{name}.{os.getpid()}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o660, dir_fd=queue_fd)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"title": title}, handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=queue_fd, dst_dir_fd=queue_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=queue_fd)
            except FileNotFoundError:
                pass
            os.close(queue_fd)
    else:
        queue_dir, result_dir = _directories()
        queue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        result_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = queue_dir / f".{name}.{os.getpid()}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump({"title": title, "chat_id": chat_id}, handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, queue_dir / name)
        finally:
            temporary.unlink(missing_ok=True)


def _read_result(name):
    if os.environ.get("TENANT_EXCHANGE_DIR"):
        result_fd = _exchange_subdir("result")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=result_fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("Некорректный результат переименования.")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    raw = handle.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    raise ValueError("Слишком большой результат переименования.")
            finally:
                os.close(fd)
            os.unlink(name, dir_fd=result_fd)
        finally:
            os.close(result_fd)
    else:
        path = _directories()[1] / name
        raw = path.read_bytes()
        path.unlink()
    return json.loads(raw)


def rename_session(title):
    try:
        title = validate_title(title)
        chat_id = int(os.environ.get("CHAT_ID", ""))
    except (ValueError, TypeError) as exc:
        return False, str(exc)
    name = f"rename-{uuid.uuid4()}.json"
    try:
        _write_request(name, title, chat_id)
    except (OSError, ValueError) as exc:
        return False, f"Не удалось записать запрос переименования: {exc}"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            result = _read_result(name)
        except FileNotFoundError:
            time.sleep(0.2)
            continue
        except (OSError, ValueError, UnicodeError) as exc:
            return False, f"Не удалось прочитать результат переименования: {exc}"
        if not isinstance(result, dict) or result.get("done") is not True:
            return False, "Некорректный результат переименования."
        return bool(result.get("ok")), str(result.get("text") or "Мост не указал результат.")
    return False, "Таймаут переименования сессии."


def _reply(request):
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    request_id = request.get("id")
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        value = {"protocolVersion": (request.get("params") or {}).get("protocolVersion", "2025-06-18"),
                 "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO}
    elif method == "ping":
        value = {}
    elif method == "tools/list":
        value = {"tools": [TOOL]}
    elif method == "tools/call":
        params = request.get("params") or {}
        args = params.get("arguments") or {}
        if params.get("name") != TOOL["name"] or not isinstance(args, dict) or set(args) != {"title"}:
            ok, message = False, "Нужен только аргумент title."
        else:
            ok, message = rename_session(args["title"])
        value = {"content": [{"type": "text", "text": message}], "isError": not ok}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def main():
    while True:
        line = sys.stdin.buffer.readline(64 * 1024 + 1)
        if not line:
            break
        if len(line) > 64 * 1024:
            while line and not line.endswith(b"\n"):
                line = sys.stdin.buffer.readline(64 * 1024 + 1)
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "Request too large"}}), flush=True)
            continue
        try:
            reply = _reply(json.loads(line))
        except (ValueError, UnicodeError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
