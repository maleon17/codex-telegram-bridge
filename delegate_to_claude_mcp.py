#!/usr/bin/env python3
"""Tenant-only stdio MCP server for delegating one task to Claude."""

import json
import os
import sys
import time
import uuid
from pathlib import Path


# Directory names the peer bridge is cloned under: the GitHub repository name
# first, then the historical local name.
PEER_DIR_NAMES = ('claude-telegram-bridge', '.claude-telegram-bridge')
PEER_MARKER_FILES = ('bridge.py', 'delegate_to_codex_mcp.py')


def claude_bridge_dir():
    """Locate the peer bridge checkout, or None when it is not installed.

    $CLAUDE_TELEGRAM_BRIDGE_DIR names it explicitly; otherwise a sibling
    directory of this checkout is used. A candidate counts only if it really is that bridge, so
    a stray directory with the right name is never mistaken for it.
    """
    override = os.environ.get("CLAUDE_TELEGRAM_BRIDGE_DIR")
    parent = Path(__file__).resolve().parent.parent
    candidates = [Path(override).expanduser()] if override else []
    candidates += [parent / name for name in PEER_DIR_NAMES]
    for candidate in candidates:
        if all((candidate / marker).is_file() for marker in PEER_MARKER_FILES):
            return candidate
    return None


POLL_TIMEOUT_S = 20
POLL_INTERVAL_S = 0.2

SERVER_INFO = {"name": "delegate-to-claude", "version": "1.0.0"}
TOOL = {
    "name": "delegate_to_claude",
    "description": (
        "Передать задачу Claude-тенанту текущего Telegram-пользователя. "
        "Telegram chat_id берётся только из окружения этого MCP-процесса и "
        "не может быть указан аргументом. Тул сразу сообщает, запущена ли "
        "задача; итоговый ответ позже придёт в тот же чат от Claude bridge."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Полный текст задачи для Claude.",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}


def _write_request(peer_dir, request_id, chat_id, prompt):
    queue_dir = peer_dir / "cross_delegate_queue"
    # Only the two queue leaves are created, inside a checkout already
    # verified to be the peer bridge -- never the checkout itself.
    queue_dir.mkdir(mode=0o700, exist_ok=True)
    (peer_dir / "cross_delegate_result").mkdir(mode=0o700, exist_ok=True)
    request_path = queue_dir / f"{request_id}.json"
    temporary = queue_dir / f".{request_id}.{os.getpid()}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(
                {"chat_id": chat_id, "text": prompt},
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
    return request_path


def _delegate_request(prompt):
    raw_chat_id = os.environ.get("CHAT_ID", "")
    try:
        chat_id = int(raw_chat_id)
    except (TypeError, ValueError):
        return False, "Отклонено локально: MCP-процесс не получил корректный CHAT_ID."
    if not isinstance(prompt, str) or not prompt.strip():
        return False, "Отклонено локально: текст задачи пуст."

    peer_dir = claude_bridge_dir()
    if peer_dir is None:
        return False, (
            "Claude-бридж на этом сервере не установлен, делегировать некому. "
            "Сообщи об этом пользователю и не повторяй попытку."
        )

    request_id = str(uuid.uuid4())
    result_path = peer_dir / "cross_delegate_result" / f"{request_id}.json"
    try:
        request_path = _write_request(peer_dir, request_id, chat_id, prompt)
    except OSError as exc:
        return False, f"Не удалось записать запрос в очередь Claude bridge: {exc}"

    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            raw_result = result_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            time.sleep(POLL_INTERVAL_S)
            continue
        except OSError as exc:
            return False, f"Не удалось прочитать ответ Claude bridge: {exc}"
        try:
            result = json.loads(raw_result)
        except (TypeError, ValueError) as exc:
            return False, f"Claude bridge вернул повреждённый ответ: {exc}"
        finally:
            try:
                result_path.unlink()
            except FileNotFoundError:
                pass
        if not isinstance(result, dict) or result.get("done") is not True:
            return False, "Claude bridge вернул ответ неизвестного формата."
        text = result.get("text")
        if not isinstance(text, str) or not text:
            text = "Claude bridge не указал причину ответа."
        return bool(result.get("ok")), text

    try:
        request_path.unlink()
    except FileNotFoundError:
        # The peer claimed the request but its confirmation did not arrive.
        return False, (
            "Claude-бридж забрал запрос, но не подтвердил его за "
            f"{POLL_TIMEOUT_S} с. Задача могла начаться; не отправляй её повторно."
        )
    return False, (
        f"Claude-бридж не ответил за {POLL_TIMEOUT_S} с: похоже, его сервис не "
        "запущен. Запрос отозван, задача НЕ начата."
    )


def delegate_to_claude(prompt: str) -> str:
    """Delegate to the Claude tenant bound to this process's CHAT_ID."""
    return _delegate_request(prompt)[1]


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
        if not isinstance(arguments, dict) or set(arguments) != {"prompt"}:
            return _jsonrpc_error(
                request_id,
                -32602,
                "delegate_to_claude accepts exactly one argument: prompt",
            )
        ok, text = _delegate_request(arguments["prompt"])
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
