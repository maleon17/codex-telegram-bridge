"""Bounded, symlink-safe bridge access to a tenant-owned exchange directory."""

import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid


REQUEST_MAX_BYTES = 64 * 1024
REQUESTS_PER_TENANT_PASS = 4
UUID_JSON = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.json$")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
WALK_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
INBOX_MAX_AGE_S = 7 * 24 * 60 * 60
INBOX_MAX_BYTES = 500 * 1024 * 1024


class ExchangeRefusal(ValueError):
    pass


def open_exchange_dir(exchange, name):
    root_fd = open_absolute_dir(exchange)
    try:
        return os.open(name, DIR_FLAGS, dir_fd=root_fd)
    finally:
        os.close(root_fd)


def open_absolute_dir(path):
    """Walk every component without following tenant-controlled links."""
    if not os.path.isabs(path) or "\x00" in path:
        raise ExchangeRefusal("Invalid exchange directory")
    fd = os.open("/", WALK_FLAGS)
    try:
        for part in path.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise ExchangeRefusal("Invalid exchange directory")
            next_fd = os.open(part, WALK_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def open_absolute_file(path):
    parent, name = os.path.split(path)
    directory_fd = open_absolute_dir(parent)
    try:
        return os.open(name, FILE_FLAGS, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ExchangeRefusal("Повторяющееся поле запроса.")
        value[key] = item
    return value


def _read_request(queue_fd, name):
    if not UUID_JSON.fullmatch(name):
        raise ExchangeRefusal("Имя запроса должно быть UUID.json.")
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
            request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ExchangeRefusal("Некорректный JSON запроса.") from exc
        if (not isinstance(request, dict) or "path" not in request
                or set(request) - {"path", "caption", "chat_id"}):
            raise ExchangeRefusal("Допустимы только path и caption.")
        path, caption = request["path"], request.get("caption", "")
        if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
            raise ExchangeRefusal("Нужен абсолютный путь к файлу.")
        if not isinstance(caption, str) or len(caption) > 1024:
            raise ExchangeRefusal("Подпись должна быть строкой до 1024 символов.")
        return path, caption
    finally:
        os.close(fd)


def _open_outbox_file(exchange, path, max_bytes):
    outbox = os.path.join(exchange, "outbox")
    # Reject traversal before normalization, including components which normalize away.
    if ".." in path.split(os.sep) or os.path.normpath(path) == outbox:
        raise ExchangeRefusal("Путь должен быть внутри outbox без '..'.")
    if not os.path.normpath(path).startswith(os.path.normpath(outbox) + os.sep):
        raise ExchangeRefusal("Путь должен быть внутри outbox.")
    parts = os.path.relpath(path, outbox).split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        raise ExchangeRefusal("Недопустимый путь к файлу.")
    directory_fd = open_exchange_dir(exchange, "outbox")
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, DIR_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(parts[-1], FILE_FLAGS, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        os.close(fd)
        raise ExchangeRefusal("Нужен обычный файл в пределах лимита отправки.")
    return fd


def _safe_send_name(path):
    """Telegram shows the file name taken from the path: keep the tenant's own name, made harmless."""
    name = re.sub(r"[^\w.\- ]", "_", os.path.basename(path)).strip(" .")[:100]
    return name or "file"


def _copy_for_send(exchange, path, max_bytes):
    """Copy the outbox file into a private bridge directory, keeping its (sanitized) name."""
    source_fd = _open_outbox_file(exchange, path, max_bytes)
    try:
        directory = tempfile.mkdtemp(prefix="bridge-file-send-")
    except Exception:
        os.close(source_fd)
        raise
    temporary = os.path.join(directory, _safe_send_name(path))
    try:
        target_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(source_fd, "rb") as source:
            source_fd = -1
            with os.fdopen(target_fd, "wb") as target:
                total = 0
                while True:
                    chunk = source.read(min(1024 * 1024, max_bytes + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ExchangeRefusal("Файл превышает лимит отправки.")
                    target.write(chunk)
        return temporary
    except Exception:
        if source_fd >= 0:
            os.close(source_fd)
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _write_result(result_fd, name, ok, message):
    temporary = f".bridge-{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o644, dir_fd=result_fd)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"done": True, "ok": ok, "text": message}, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=result_fd, dst_dir_fd=result_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=result_fd)
        except FileNotFoundError:
            pass


def process_tenant_queue(chat_id, exchange, send_document, max_bytes):
    """Process a bounded number of requests; never trust a request's chat_id."""
    queue_fd = open_exchange_dir(exchange, "queue")
    try:
        # Do not send if the reply channel is already unavailable: a retry
        # would otherwise risk a duplicate Telegram document.
        try:
            result_fd = open_exchange_dir(exchange, "result")
        except OSError as exc:
            print(f"Could not open isolated file results for tenant {chat_id}: {type(exc).__name__}", flush=True)
            return
        try:
            _process_tenant_queue_open(chat_id, exchange, send_document, max_bytes, queue_fd, result_fd)
        finally:
            os.close(result_fd)
    finally:
        os.close(queue_fd)


def _process_tenant_queue_open(chat_id, exchange, send_document, max_bytes, queue_fd, result_fd):
    """Process one bounded pass with both queue and result directories open."""
    with os.scandir(queue_fd) as entries:
        names = []
        for entry in entries:
            if entry.name.endswith(".json"):
                names.append(entry.name)
                if len(names) >= REQUESTS_PER_TENANT_PASS:
                    break
    for name in names:
        ok, message = False, "Запрос отклонён."
        try:
            path, caption = _read_request(queue_fd, name)
            temporary = _copy_for_send(exchange, path, max_bytes)
            try:
                result = send_document(chat_id, temporary, caption)
            finally:
                shutil.rmtree(os.path.dirname(temporary), ignore_errors=True)
            ok = bool(result.get("ok"))
            message = "Файл отправлен." if ok else "Не удалось отправить файл в Telegram."
        except ExchangeRefusal as exc:
            message = str(exc)
        except Exception:
            message = "Не удалось обработать запрос на отправку файла."
        try:
            _write_result(result_fd, name, ok, message)
        except OSError as exc:
            print(f"Could not write isolated file result for tenant {chat_id}: {type(exc).__name__}", flush=True)
        finally:
            try:
                os.unlink(name, dir_fd=queue_fd)
            except OSError:
                pass


def safe_inbox_name(hint):
    name = re.sub(r"[^\w.\-]", "_", os.path.basename(str(hint).replace("\x00", "_")))
    name = name.replace("..", "_").strip(".")[:80]
    return name or "file.bin"


def prune_inbox(inbox_fd, now=None):
    now = time.time() if now is None else now
    files = []
    with os.scandir(inbox_fd) as entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_mtime < now - INBOX_MAX_AGE_S:
                os.unlink(entry.name, dir_fd=inbox_fd)
            else:
                files.append((info.st_mtime, entry.name, info.st_size))
    total = sum(size for _, _, size in files)
    for _, name, size in sorted(files):
        if total <= INBOX_MAX_BYTES:
            break
        try:
            os.unlink(name, dir_fd=inbox_fd)
            total -= size
        except FileNotFoundError:
            pass


def receive_inbox_file(exchange, suggested_name, source, max_bytes):
    """Copy a bounded stream into a unique tenant inbox entry and prune it."""
    inbox_fd = open_exchange_dir(exchange, "inbox")
    name = f"{int(time.time() * 1000)}_{uuid.uuid4().hex}_{safe_inbox_name(suggested_name)}"
    fd = None
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644, dir_fd=inbox_fd)
        os.fchmod(fd, 0o644)
        total = 0
        with os.fdopen(fd, "wb", closefd=False) as target:
            while True:
                chunk = source.read(min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes or total > INBOX_MAX_BYTES:
                    raise ExchangeRefusal("Incoming file exceeds limit")
                target.write(chunk)
        prune_inbox(inbox_fd)
        return os.path.join(exchange, "inbox", name)
    except Exception:
        if fd is not None:
            os.unlink(name, dir_fd=inbox_fd)
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(inbox_fd)
