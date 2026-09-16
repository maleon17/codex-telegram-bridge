import io
import json
import queue
import threading
import unittest
from unittest.mock import patch

from app_server import AppServerClient, AppServerError


class _Stream:
    def __init__(self):
        self.items = queue.Queue()
    def __iter__(self):
        while True:
            item = self.items.get()
            if item is None:
                return
            yield item


class _Input:
    def __init__(self, output):
        self.output = output
    def write(self, payload):
        message = json.loads(payload)
        if message.get("method") == "initialize":
            self.output.items.put(json.dumps({"id": message["id"], "result": {}}) + "\n")
    def flush(self):
        pass


class _Process:
    next_pid = 10
    def __init__(self):
        self.pid = _Process.next_pid
        _Process.next_pid += 1
        self.stdout = _Stream()
        self.stderr = _Stream()
        self.stdin = _Input(self.stdout)
        self._code = None
    def poll(self):
        return self._code
    def terminate(self):
        self._code = -15
    def wait(self, timeout=None):
        return self._code


class GenerationTests(unittest.TestCase):
    def test_eof_from_old_process_cannot_fail_new_request(self):
        created = []
        with patch("app_server.subprocess.Popen", side_effect=lambda *args, **kwargs: created.append(_Process()) or created[-1]):
            client = AppServerClient(lambda *args: None, lambda *args: None, request_timeout=2)
            client.start()
            old = created[0]
            client.close(timeout=0.01)
            client.start()
            new = created[1]
            outcome = {}
            worker = threading.Thread(target=lambda: outcome.setdefault("value", client.request("new/request")))
            worker.start()
            old.stdout.items.put(None)
            new.stdout.items.put('{"id":3,"result":{"fresh":true}}\n')
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome.get("value"), {"fresh": True})


class _SilentInput(_Input):
    def write(self, payload):
        pass


class _ClosingProcess(_Process):
    def __init__(self, respond=True):
        super().__init__()
        if not respond:
            self.stdin = _SilentInput(self.stdout)
    def terminate(self):
        self._code = -15
        self.stdout.items.put(None)
        self.stderr.items.put(None)
    kill = terminate


class InitFailureTests(unittest.TestCase):
    def test_failed_initialize_is_not_reused_by_next_request(self):
        created = []
        def spawn(*args, **kwargs):
            created.append(_ClosingProcess(respond=len(created) > 0))
            return created[-1]
        with patch("app_server.subprocess.Popen", side_effect=spawn):
            client = AppServerClient(lambda *args: None, lambda *args: None, request_timeout=0.2)
            with self.assertRaises(AppServerError):
                client.start()
            self.assertEqual(created[0].poll(), -15)
            client.start()
        self.assertEqual(len(created), 2)
        self.assertIsNotNone(client.process)
        client.close(timeout=0.1)


if __name__ == "__main__":
    unittest.main()
