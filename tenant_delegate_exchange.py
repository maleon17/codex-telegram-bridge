"""Bounded bridge-side forwarding of isolated Codex delegation requests."""

import json
import os
import re
import stat
import threading

from delegate_to_claude_mcp import _delegate_request
from tenant_exchange import (ExchangeRefusal, FILE_FLAGS, REQUEST_MAX_BYTES,
                             _unique_object, _write_result, open_exchange_dir)


# Direction is part of the name: this bridge forwards only requests addressed to Claude.
NAME_PREFIX = "delegate-claude-"
NAME_RE = re.compile(r"delegate-claude-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.json\Z")
MAX_WORKERS = 8
_slots = threading.BoundedSemaphore(MAX_WORKERS)
_active = set()
_lock = threading.Lock()


def _read_request(queue_fd, name):
    if not NAME_RE.fullmatch(name):
        raise ExchangeRefusal("Недопустимое имя запроса делегирования.")
    fd = os.open(name, FILE_FLAGS, dir_fd=queue_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ExchangeRefusal("Запрос должен быть обычным файлом.")
        if info.st_size > REQUEST_MAX_BYTES:
            raise ExchangeRefusal("Запрос больше 64 КБ.")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(REQUEST_MAX_BYTES + 1)
        if len(raw) > REQUEST_MAX_BYTES:
            raise ExchangeRefusal("Запрос больше 64 КБ.")
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ExchangeRefusal("Некорректный JSON запроса.") from exc
        if not isinstance(data, dict) or set(data) != {"text"}:
            raise ExchangeRefusal("Допустимо только поле text.")
        text = data["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ExchangeRefusal("Текст задачи должен содержать от 1 до 32000 символов.")
        return text
    finally:
        os.close(fd)


def _finish(tenant_id, exchange, name, text):
    try:
        try:
            ok, message = _delegate_request(text, chat_id=int(tenant_id))
        except Exception:
            ok, message = False, "Не удалось передать задачу Claude-бриджу."
        message = str(message)
        if len(message) > 15000:
            message = message[:15000] + "\n[Ответ сокращён до лимита канала.]"
        try:
            result_fd = open_exchange_dir(exchange, "result")
            try:
                _write_result(result_fd, name, bool(ok), message)
            finally:
                os.close(result_fd)
        except OSError as exc:
            print(f"Could not write isolated delegation result for tenant {tenant_id}: {type(exc).__name__}", flush=True)
        try:
            queue_fd = open_exchange_dir(exchange, "queue")
            try:
                os.unlink(name, dir_fd=queue_fd)
            finally:
                os.close(queue_fd)
        except OSError:
            pass
    finally:
        with _lock:
            _active.discard((tenant_id, name))
        _slots.release()


def process_tenant_delegate_queue(tenant_id, exchange):
    """Process a bounded pass; tenant identity comes only from the caller's directory."""
    queue_fd = open_exchange_dir(exchange, "queue")
    try:
        try:
            result_fd = open_exchange_dir(exchange, "result")
        except OSError:
            print(f"Isolated delegation result directory unavailable for tenant {tenant_id}", flush=True)
            return
        try:
            with os.scandir(queue_fd) as entries:
                names = []
                for entry in entries:
                    # Plain <uuid>.json names belong to the file-send watcher.
                    if entry.name.startswith(NAME_PREFIX) and entry.name.endswith(".json"):
                        names.append(entry.name)
                        if len(names) >= 16:
                            break
            for name in names:
                with _lock:
                    if (tenant_id, name) in _active:
                        continue
                try:
                    text = _read_request(queue_fd, name)
                    with _lock:
                        busy = any(key[0] == tenant_id for key in _active)
                        if not busy and _slots.acquire(blocking=False):
                            _active.add((tenant_id, name))
                            accepted = True
                        else:
                            accepted = False
                    if accepted:
                        try:
                            threading.Thread(target=_finish, args=(tenant_id, exchange, name, text), daemon=True).start()
                        except Exception:
                            with _lock:
                                _active.discard((tenant_id, name))
                            _slots.release()
                            raise
                        continue
                    message = "Делегат занят: дождитесь завершения текущего запроса."
                except (ExchangeRefusal, OSError) as exc:
                    message = str(exc) if isinstance(exc, ExchangeRefusal) else "Не удалось прочитать запрос делегирования."
                except Exception:
                    message = "Не удалось обработать запрос делегирования."
                try:
                    _write_result(result_fd, name, False, message)
                except OSError as exc:
                    print(f"Could not write isolated delegation refusal for tenant {tenant_id}: {type(exc).__name__}", flush=True)
                finally:
                    try:
                        os.unlink(name, dir_fd=queue_fd)
                    except OSError:
                        pass
        finally:
            os.close(result_fd)
    finally:
        os.close(queue_fd)
