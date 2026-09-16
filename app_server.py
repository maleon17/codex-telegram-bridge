"""Minimal persistent JSONL client for ``codex app-server``."""

import json
import queue
import subprocess
import threading


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    """Generation-safe JSONL client for a replaceable app-server process."""

    def __init__(self, notification_handler, log, request_timeout=30, env=None):
        self.notification_handler, self.log = notification_handler, log
        self.request_timeout, self.env, self.process = request_timeout, env, None
        self._write_lock, self._state_lock = threading.Lock(), threading.Lock()
        self._pending, self._next_id, self._closed_error = {}, 1, None
        self._generation, self._ready, self._init_error, self._readers = 0, None, None, {}

    def start(self):
        with self._state_lock:
            process, ready = self.process, self._ready
            if process is not None and process.poll() is None:
                initializer = False
            else:
                self._closed_error = self._init_error = None
                self._generation += 1
                generation = self._generation
                process = subprocess.Popen(
                    ["codex", "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=self.env,
                )
                self.process, ready, self._ready = process, threading.Event(), None
                self._ready = ready
                readers = (
                    threading.Thread(target=self._read_stdout, args=(process, generation), daemon=True),
                    threading.Thread(target=self._read_stderr, args=(process, generation), daemon=True),
                )
                self._readers[generation] = readers
                for reader in readers:
                    reader.start()
                initializer = True
        if not initializer:
            ready.wait(self.request_timeout)
            if not ready.is_set():
                raise AppServerError("app-server initialization timed out")
            with self._state_lock:
                if self._init_error:
                    raise self._init_error
            return
        try:
            self._request("initialize", {
                "clientInfo": {"name": "codex-telegram-bot", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True},
            }, generation=generation)
            self._notify(generation, "initialized")
        except Exception as exc:
            error = exc if isinstance(exc, AppServerError) else AppServerError(str(exc))
            with self._state_lock:
                current = generation == self._generation and self.process is process
                if current:
                    self._init_error = error
            if current:
                # An uninitialized process must not be reused: the next
                # request starts a fresh generation instead of failing forever.
                self.close()
            raise error
        finally:
            ready.set()

    def close(self, timeout=5):
        with self._state_lock:
            process, generation = self.process, self._generation
            readers = self._readers.get(generation, ())
            self.process = None
            self._fail_pending_locked(generation, AppServerError("app-server was closed"))
            if self._ready is not None:
                self._ready.set()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        for reader in readers:
            if reader is not threading.current_thread():
                reader.join(timeout=timeout)

    def notify(self, method, params=None):
        self.start_if_needed()
        with self._state_lock:
            generation = self._generation
        self._notify(generation, method, params)

    def _notify(self, generation, method, params=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message, generation)

    def request(self, method, params=None, timeout=None):
        self.start_if_needed()
        with self._state_lock:
            generation, ready = self._generation, self._ready
        ready.wait(timeout or self.request_timeout)
        if not ready.is_set():
            raise AppServerError("app-server initialization timed out")
        with self._state_lock:
            if self._init_error:
                raise self._init_error
        return self._request(method, params, timeout, generation=generation)

    def _request(self, method, params=None, timeout=None, *, generation):
        response_queue = queue.Queue(maxsize=1)
        with self._state_lock:
            if generation != self._generation or self.process is None:
                raise AppServerError(self._closed_error or "app-server is not running")
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = (generation, response_queue)
        try:
            self._write({"id": request_id, "method": method, "params": params or {}}, generation)
            response = response_queue.get(timeout=timeout or self.request_timeout)
        except queue.Empty as exc:
            raise AppServerError(f"app-server request timed out: {method}") from exc
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        if isinstance(response, Exception):
            raise response
        if "error" in response:
            error = response["error"]
            message = error.get("message", error) if isinstance(error, dict) else error
            raise AppServerError(f"{method}: {message}")
        return response.get("result")

    def start_if_needed(self):
        self.start()  # start() is also the ready barrier for concurrent callers.

    def _write(self, message, generation):
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            with self._state_lock:
                process = self.process
                if (generation != self._generation or process is None or process.poll() is not None
                        or process.stdin is None):
                    raise AppServerError(self._closed_error or "app-server is not running")
            process.stdin.write(payload + "\n")
            process.stdin.flush()

    def _read_stdout(self, process, generation):
        try:
            for raw_line in process.stdout:
                try:
                    message = json.loads(raw_line)
                except Exception as exc:
                    self.log(f"app-server JSON parse error: {exc}")
                    continue
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    with self._state_lock:
                        target = self._pending.get(request_id)
                    if target is not None and target[0] == generation:
                        try:
                            target[1].put_nowait(message)
                        except queue.Full:
                            pass
                elif request_id is not None and message.get("method"):
                    self._reply_to_server_request(message, generation)
                elif message.get("method"):
                    with self._state_lock:
                        current = generation == self._generation and process is self.process
                    if current:
                        try:
                            self.notification_handler(message["method"], message.get("params") or {})
                        except Exception as exc:
                            self.log(f"app-server notification handler failed: {exc}")
        finally:
            self._fail_pending(generation, AppServerError(
                f"app-server exited with status {process.poll()}"))

    def _read_stderr(self, process, generation):
        for line in process.stderr:
            if line.rstrip():
                self.log(f"app-server: {line.rstrip()}")

    def _reply_to_server_request(self, message, generation):
        self.log(f"Declining unexpected app-server request: {message.get('method', '')}")
        self._write({"id": message["id"], "result": {"decision": "decline"}}, generation)

    def _fail_pending(self, generation, error):
        with self._state_lock:
            self._fail_pending_locked(generation, error)
            if generation == self._generation:
                self._closed_error = str(error)

    def _fail_pending_locked(self, generation, error):
        for request_generation, target in list(self._pending.values()):
            if request_generation == generation:
                try:
                    target.put_nowait(error)
                except queue.Full:
                    pass
