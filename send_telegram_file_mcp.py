#!/usr/bin/env python3
"""Tenant-scoped MCP tool for returning an outbox file to Telegram."""

import json
import os
import sys
import time
import uuid
from pathlib import Path


BRIDGE_DIR = Path(__file__).resolve().parent
QUEUE_DIR = BRIDGE_DIR / "file_send_queue"
RESULT_DIR = BRIDGE_DIR / "file_send_result"
POLL_TIMEOUT_S = 45
POLL_INTERVAL_S = 0.2

SERVER_INFO = {"name": "send-telegram-file", "version": "1.0.0"}
TOOL = {
    "name": "send_telegram_file",
    "description": (
        "Отправить готовый файл в текущий Telegram-чат. Telegram chat_id жёстко "
        "привязан к этому MCP-процессу и не задаётся аргументом. Разрешены только "
        "обычные файлы из каталога CODEX_TELEGRAM_OUTBOX; скопируй или создай файл "
        "там, затем передай абсолютный путь."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Абсолютный путь к файлу внутри CODEX_TELEGRAM_OUTBOX.",
            },
            "caption": {
                "type": "string",
                "description": "Необязательная подпись к документу.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


def _write_request(request_id, chat_id, path, caption):
    QUEUE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    RESULT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    request_path = QUEUE_DIR / f"{request_id}.json"
    temporary = QUEUE_DIR / f".{request_id}.{os.getpid()}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(
                {"chat_id": chat_id, "path": path, "caption": caption},
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, request_path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _send_file(path, caption=""):
    raw_chat_id = os.environ.get("CHAT_ID", "")
    try:
        chat_id = int(raw_chat_id)
    except (TypeError, ValueError):
        return False, "Отклонено локально: MCP-процесс не получил корректный CHAT_ID."
    if not isinstance(path, str) or not path.strip():
        return False, "Отклонено локально: путь к файлу пуст."
    if not Path(path).is_absolute():
        return False, "Отклонено локально: передай абсолютный путь к файлу."
    if not isinstance(caption, str):
        return False, "Отклонено локально: подпись должна быть строкой."

    request_id = str(uuid.uuid4())
    result_path = RESULT_DIR / f"{request_id}.json"
    try:
        _write_request(request_id, chat_id, path, caption)
    except OSError as exc:
        return False, f"Не удалось записать запрос на отправку файла: {exc}"

    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            raw_result = result_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            time.sleep(POLL_INTERVAL_S)
            continue
        except OSError as exc:
            return False, f"Не удалось прочитать результат отправки файла: {exc}"
        try:
            result = json.loads(raw_result)
        except (TypeError, ValueError) as exc:
            return False, f"Получен повреждённый результат отправки файла: {exc}"
        finally:
            try:
                result_path.unlink()
            except FileNotFoundError:
                pass
        if not isinstance(result, dict) or result.get("done") is not True:
            return False, "Мост вернул результат отправки файла неизвестного формата."
        text = result.get("text")
        if not isinstance(text, str) or not text:
            text = "Мост не указал результат отправки файла."
        return bool(result.get("ok")), text

    return False, "Таймаут отправки файла: запрос мог быть обработан позднее."


def _jsonrpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _handle_message(request):
    if not isinstance(request, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        protocol_version = (request.get("params") or {}).get(
            "protocolVersion", "2025-06-18"
        )
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _jsonrpc_result(request_id, {})
    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": [TOOL]})
    if method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") != TOOL["name"]:
            return _jsonrpc_error(request_id, -32602, "Unknown tool")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict) or set(arguments) - {"path", "caption"} or "path" not in arguments:
            return _jsonrpc_error(
                request_id,
                -32602,
                "send_telegram_file accepts path and optional caption",
            )
        ok, text = _send_file(arguments["path"], arguments.get("caption", ""))
        return _jsonrpc_result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": not ok},
        )
    if request_id is None:
        return None
    return _jsonrpc_error(request_id, -32601, "Method not found")


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except (TypeError, ValueError):
            response = _jsonrpc_error(None, -32700, "Parse error")
        else:
            try:
                response = _handle_message(request)
            except Exception as exc:
                response = _jsonrpc_error(request.get("id"), -32603, str(exc))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
