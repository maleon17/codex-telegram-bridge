"""Isolated exchange boundaries, using only temporary tenant directories."""

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import select
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tenant_exchange as exchange_io
import codex_send_telegram_file_mcp as mcp


class TenantExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exchange = self.root / ".exchange"
        for part in ("inbox", "outbox", "queue", "result"):
            (self.exchange / part).mkdir(parents=True, exist_ok=True)
        self.good = self.exchange / "outbox" / "report.txt"
        self.good.write_text("safe content")
        self.calls = []

    def send(self, chat_id, path, caption):
        self.calls.append((chat_id, Path(path).read_text(), caption, Path(path).name, path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        return {"ok": True}

    def request(self, body, name=None, limit=32):
        name = name or f"{uuid.uuid4()}.json"
        request = self.exchange / "queue" / name
        request.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
        exchange_io.process_tenant_queue(2, str(self.exchange), self.send, limit)
        self.assertFalse(request.exists())
        return json.loads((self.exchange / "result" / name).read_text())

    def test_normal_send_keeps_name_and_ignores_chat_id(self):
        result = self.request({"path": str(self.good), "caption": "hello", "chat_id": 999})
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[0][:4], (2, "safe content", "hello", "report.txt"))
        self.assertFalse(Path(self.calls[0][4]).exists())

    def test_outbox_and_request_attacks(self):
        outside = self.root / "outside.txt"
        outside.write_text("secret")
        (self.exchange / "outbox" / "link.txt").symlink_to(outside)
        (self.exchange / "outbox" / "linkdir").symlink_to(self.root, target_is_directory=True)
        os.mkfifo(self.exchange / "outbox" / "pipe")
        (self.exchange / "outbox" / "large").write_bytes(b"x" * 33)
        denied = [
            {"path": str(self.exchange / "outbox" / "link.txt")},
            {"path": str(self.exchange / "outbox" / "linkdir" / "outside.txt")},
            {"path": str(self.exchange / "outbox" / ".." / "inbox" / "x")},
            {"path": str(outside)}, {"path": "relative.txt"},
            {"path": str(self.exchange / "outbox" / "pipe")},
            {"path": str(self.exchange / "outbox" / "large")},
            {"path": str(self.good), "extra": 1},
            {"path": str(self.good), "caption": "x" * 1025},
        ]
        for body in denied:
            self.assertFalse(self.request(body)["ok"], body)
        for body in (b"{broken", b"x" * (exchange_io.REQUEST_MAX_BYTES + 1),
                     ('{"path":' + json.dumps(str(self.good)) + ',"path":"other"}').encode()):
            self.assertFalse(self.request(body)["ok"])
        self.assertFalse(self.request({"path": str(self.good)}, "bad.json")["ok"])
        self.assertEqual(self.calls, [])
        for create in (lambda p: os.mkfifo(p), lambda p: p.symlink_to(outside)):
            name = f"{uuid.uuid4()}.json"
            create(self.exchange / "queue" / name)
            exchange_io.process_tenant_queue(2, str(self.exchange), self.send, 32)
            self.assertFalse(json.loads((self.exchange / "result" / name).read_text())["ok"])
        self.assertEqual(outside.read_text(), "secret")

    def test_unavailable_result_does_not_send_or_delete(self):
        result = self.exchange / "result"
        result.rename(self.exchange / "old-result")
        result.symlink_to(self.root, target_is_directory=True)
        name = f"{uuid.uuid4()}.json"
        request = self.exchange / "queue" / name
        request.write_text(json.dumps({"path": str(self.good)}))
        exchange_io.process_tenant_queue(2, str(self.exchange), self.send, 32)
        self.assertTrue(request.exists())
        self.assertEqual(self.calls, [])

    def test_queue_pass_is_bounded_and_exchange_path_cannot_be_symlink(self):
        for _ in range(5):
            (self.exchange / "queue" / f"{uuid.uuid4()}.json").write_text(
                json.dumps({"path": str(self.good)}))
        exchange_io.process_tenant_queue(2, str(self.exchange), self.send, 32)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(len(list((self.exchange / "queue").glob("*.json"))), 1)
        linked = self.root / "linked-exchange"
        linked.symlink_to(self.exchange, target_is_directory=True)
        with self.assertRaises(OSError):
            exchange_io.process_tenant_queue(2, str(linked), self.send, 32)

    def test_inbox_name_collision_retention_and_size(self):
        with self.assertRaises(exchange_io.ExchangeRefusal):
            exchange_io.receive_inbox_file(str(self.exchange), "large", io.BytesIO(b"x" * 11), 10)
        self.assertEqual(list((self.exchange / "inbox").iterdir()), [])
        first = Path(exchange_io.receive_inbox_file(str(self.exchange), "../bad\x00name", io.BytesIO(b"one"), 100))
        self.assertEqual(first.parent, self.exchange / "inbox")
        self.assertNotIn("..", first.name)
        self.assertNotIn("\x00", first.name)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o644)
        old = self.exchange / "inbox" / "old"
        old.write_bytes(b"old")
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        extra = self.exchange / "inbox" / "extra"
        extra.write_bytes(b"12345678")
        os.utime(extra, (time.time() - 100,) * 2)
        with patch.object(exchange_io, "INBOX_MAX_BYTES", 7):
            second = Path(exchange_io.receive_inbox_file(str(self.exchange), "file", io.BytesIO(b"new"), 100))
        self.assertTrue(second.exists())
        self.assertFalse(old.exists())
        self.assertFalse(extra.exists())
        with patch.object(exchange_io.uuid, "uuid4", return_value=type("Fixed", (), {"hex": "0" * 32})()), \
                patch.object(exchange_io.time, "time", return_value=1234567890):
            fixed = Path(exchange_io.receive_inbox_file(str(self.exchange), "x", io.BytesIO(b"first"), 100))
            with self.assertRaises(FileExistsError):
                exchange_io.receive_inbox_file(str(self.exchange), "x", io.BytesIO(b"second"), 100)
            self.assertEqual(fixed.read_bytes(), b"first")

    def test_mcp_isolated_queue_and_legacy_queue(self):
        with patch.dict(os.environ, {"TENANT_EXCHANGE_DIR": str(self.exchange), "CHAT_ID": "2"}):
            request_id = str(uuid.uuid4())
            mcp._write_request(request_id, 2, str(self.good), "")
            body = json.loads((self.exchange / "queue" / f"{request_id}.json").read_text())
            self.assertEqual(body, {"path": str(self.good), "caption": ""})
        with patch.dict(os.environ, {"TENANT_EXCHANGE_DIR": ""}), \
                patch.object(mcp, "QUEUE_DIR", self.root / "legacy-queue"), \
                patch.object(mcp, "RESULT_DIR", self.root / "legacy-result"):
            request_id = str(uuid.uuid4())
            mcp._write_request(request_id, 2, str(self.good), "")
            self.assertEqual(json.loads((mcp.QUEUE_DIR / f"{request_id}.json").read_text())["chat_id"], 2)

    def test_real_mcp_process_exchanges_with_bridge(self):
        env = dict(os.environ, CHAT_ID="2", TENANT_EXCHANGE_DIR=str(self.exchange))
        process = subprocess.Popen([sys.executable, str(ROOT / "codex_send_telegram_file_mcp.py")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env)
        try:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": "send_telegram_file",
                                                       "arguments": {"path": str(self.good)}}}) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 5
            while not list((self.exchange / "queue").glob("*.json")) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(list((self.exchange / "queue").glob("*.json")))
            exchange_io.process_tenant_queue(2, str(self.exchange), self.send, 32)
            ready, _, _ = select.select([process.stdout], [], [], 5)
            self.assertTrue(ready)
            response = json.loads(process.stdout.readline())
            self.assertFalse(response["result"]["isError"])
            self.assertEqual(self.calls[0][0], 2)
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
