#!/usr/bin/python3
"""Stdio MCP delegation through the isolated tenant's exchange directory."""

import json
import os
import stat
import sys
import time
import uuid


POLL_TIMEOUT_S = 20
POLL_INTERVAL_S = 0.2
DEFAULT_WAIT_S = 600
MAX_WAIT_S = 1800
RESULT_MAX_BYTES = 64 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Intermediate directories (for example a tenants root that is search-only) may not be readable: walk with O_PATH.
WALK_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
SERVER_INFO = {"name": "delegate-to-claude", "version": "1.0.0"}
TOOL = {
    "name": "delegate_to_claude",
    "description": (
        "Передать задачу Claude-тенанту текущего Telegram-пользователя и "
        "получить его итоговый ответ. Telegram chat_id берётся только из "
        "окружения этого MCP-процесса и не может быть указан аргументом. Тул "
        "ждёт завершения задачи (по умолчанию до 10 минут) и возвращает ответ; "
        "тот же ответ приходит пользователю в чат Claude-бота. Если задача "
        "не успела завершиться за время ожидания, тул сообщает об этом — "
        "ответ тогда придёт только в чат."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Полный текст задачи для Claude."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_S,
                             "description": ("Сколько секунд ждать итоговый ответ (по умолчанию 600). "
                                             "0 — не ждать: только убедиться, что задача принята.")},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}


def _open_dir(exchange, name):
    if not os.path.isabs(exchange) or "\x00" in exchange:
        raise ValueError("Недопустимый каталог обмена.")
    root = os.open("/", WALK_FLAGS)
    try:
        for part in exchange.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise ValueError("Недопустимый каталог обмена.")
            next_fd = os.open(part, WALK_FLAGS, dir_fd=root)
            os.close(root)
            root = next_fd
        return os.open(name, WALK_FLAGS, dir_fd=root)
    finally:
        os.close(root)


def _write_request(exchange, request_id, prompt):
    payload = (json.dumps({"text": prompt}, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > 64 * 1024:
        raise ValueError("Запрос больше 64 КБ.")
    directory = _open_dir(exchange, "queue")
    temporary = f".delegate-{uuid.uuid4()}.tmp"
    name = f"delegate-claude-{request_id}.json"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o660, dir_fd=directory)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o660)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _read_result(exchange, name):
    directory = _open_dir(exchange, "result")
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > RESULT_MAX_BYTES:
                raise ValueError("Недопустимый файл ответа.")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(RESULT_MAX_BYTES + 1)
            if len(raw) > RESULT_MAX_BYTES:
                raise ValueError("Ответ слишком большой.")
            result = json.loads(raw.decode("utf-8"))
            if (not isinstance(result, dict) or set(result) != {"done", "ok", "text"}
                    or result["done"] is not True or not isinstance(result["ok"], bool)
                    or not isinstance(result["text"], str)):
                raise ValueError("Неверный формат ответа.")
            os.unlink(name, dir_fd=directory)
            return result
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _delegate_request(prompt, wait_seconds=DEFAULT_WAIT_S):
    exchange = os.environ.get("TENANT_EXCHANGE_DIR")
    if not exchange:
        return False, "Отклонено локально: TENANT_EXCHANGE_DIR отсутствует."
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        return False, "Отклонено локально: текст задачи должен содержать от 1 до 32000 символов."
    request_id = str(uuid.uuid4())
    name = f"delegate-claude-{request_id}.json"
    try:
        _write_request(exchange, request_id, prompt)
    except (OSError, ValueError) as exc:
        return False, f"Не удалось записать запрос: {exc}"
    deadline = time.monotonic() + POLL_TIMEOUT_S + max(0, min(wait_seconds, MAX_WAIT_S))
    while time.monotonic() < deadline:
        try:
            result = _read_result(exchange, name)
            return result["ok"], result["text"]
        except FileNotFoundError:
            time.sleep(POLL_INTERVAL_S)
        except (OSError, ValueError, UnicodeError) as exc:
            return False, f"Не удалось прочитать ответ Claude: {exc}"
    return False, "Claude-бридж не ответил до истечения времени ожидания."


def _reply(request_id, result=None, error=None):
    response = {"jsonrpc": "2.0", "id": request_id}
    response["error" if error else "result"] = error or result
    return response


def _handle_message(request):
    if not isinstance(request, dict):
        return _reply(None, error={"code": -32600, "message": "Invalid Request"})
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        version = (request.get("params") or {}).get("protocolVersion", "2025-06-18")
        return _reply(request_id, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                   "serverInfo": SERVER_INFO})
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _reply(request_id, {})
    if method == "tools/list":
        return _reply(request_id, {"tools": [TOOL]})
    if method == "tools/call":
        params = request.get("params") or {}
        arguments = params.get("arguments") or {}
        if params.get("name") != TOOL["name"] or not isinstance(arguments, dict) or (
                set(arguments) - {"prompt", "wait_seconds"}) or "prompt" not in arguments:
            return _reply(request_id, error={"code": -32602, "message": "Invalid arguments"})
        wait = arguments.get("wait_seconds", DEFAULT_WAIT_S)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)):
            return _reply(request_id, error={"code": -32602, "message": "wait_seconds must be a number"})
        ok, text = _delegate_request(arguments["prompt"], max(0, min(float(wait), MAX_WAIT_S)))
        return _reply(request_id, {"content": [{"type": "text", "text": text}], "isError": not ok})
    return None if request_id is None else _reply(request_id, error={"code": -32601, "message": "Method not found"})


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = _handle_message(request)
        except (TypeError, ValueError, UnicodeError):
            response = _reply(None, error={"code": -32700, "message": "Parse error"})
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
