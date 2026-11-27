"""Isolated delegation and shared-queue boundaries, using scratch directories."""

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import delegate_to_claude_exchange_mcp as mcp
import delegate_to_claude_mcp as legacy
import isolation
import tenant_delegate_exchange as broker
import tenant_exchange as files


class TenantDelegateExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exchange = self.root / ".exchange"
        for part in ("queue", "result", "outbox"):
            (self.exchange / part).mkdir(parents=True)
        self.control = self.root / "control"
        self.mcp_dir = self.root / "mcp"
        self.mcp_dir.mkdir()
        self.env = patch.dict(os.environ, {
            "HOME": str(self.root), "OWNER_ID": "1",
            "BRIDGE_TENANT_CONTROL_DIR": str(self.control),
            "BRIDGE_ISOLATED_MCP_DIR": str(self.mcp_dir),
            "TENANT_EXCHANGE_DIR": str(self.exchange),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.old_mode = isolation.BRIDGE_ISOLATION, isolation.ISOLATED_TENANTS
        isolation.BRIDGE_ISOLATION, isolation.ISOLATED_TENANTS = "on", frozenset({"1", "2"})
        self.addCleanup(self.restore_mode)
        isolation._cache.clear()

    def restore_mode(self):
        isolation.BRIDGE_ISOLATION, isolation.ISOLATED_TENANTS = self.old_mode
        isolation._cache.clear()

    def put(self, body, name=None):
        name = name or f"delegate-{uuid.uuid4()}.json"
        path = self.exchange / "queue" / name
        path.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
        return name, path

    def result(self, name):
        return json.loads((self.exchange / "result" / name).read_text())

    def wait_result(self, name):
        for _ in range(200):
            if (self.exchange / "result" / name).exists():
                return self.result(name)
            time.sleep(0.01)
        self.fail("delegation result did not arrive")

    def test_mapping_and_mcp_allowlist(self):
        self.assertEqual(isolation.tenant_target("2"), ("main", "bridge"))
        self.assertEqual(isolation.tenant_target("delegate:2"), ("main", "delegate"))
        self.assertEqual(isolation.tenant_target("delegate:1"), ("delegate", "bridge"))
        info = {"home": str(self.root), "exchange": str(self.exchange)}
        (self.mcp_dir / "codex_send_telegram_file_mcp.py").write_text("# fixture")
        (self.mcp_dir / "delegate_to_claude_exchange_mcp.py").write_text("# fixture")
        with patch.object(isolation, "ensure_tenant", return_value=info), \
                patch("delegate_to_claude_mcp.claude_bridge_dir", return_value=self.root):
            servers = isolation.isolated_mcp_servers("delegate:2")
            self.assertEqual(set(servers), {"send-telegram-file", "delegate-to-claude"})
            argv = isolation.tenant_run_argv("delegate:2", "main")
            self.assertEqual(argv[4:8], ["--kind", "main", "--product", "delegate"])
            owner_argv = isolation.tenant_run_argv("delegate:1", "delegate")
            self.assertEqual(owner_argv[4:8], ["--kind", "delegate", "--product", "bridge"])
            self.assertIn("mcp_servers.delegate-to-claude.tool_timeout_sec=1900",
                          isolation.isolated_mcp_overrides("delegate:2"))
            # codex_apps (built-in ChatGPT connectors server) loads with any ChatGPT login and is not allowlisted
            self.assertIn("features.apps=false", isolation.isolated_mcp_overrides("delegate:2"))
            isolation.stage_tenant_control("2", "main persona")
            isolation.stage_tenant_control("delegate:2", "delegate persona")
            self.assertEqual((self.exchange / "persona.md").read_text(), "main persona")
            self.assertEqual((self.exchange / "persona.delegate.md").read_text(), "delegate persona")
            self.assertTrue((self.exchange / "mcp.delegate.json").exists())
            entry = lambda name: {"name": name, "enabled": True,
                                  "transport": {"type": "stdio", **{k: servers[name][k] for k in ("command", "args", "env")},
                                                "env_vars": [], "cwd": None},
                                  "tool_timeout_sec": servers[name].get("tool_timeout_sec")}
            expected = [entry(name) for name in servers]
            def run_with(entries, inherited=()):
                def run(argv, **kwargs):
                    raw = not any(".command=" in arg for arg in argv)
                    return SimpleNamespace(returncode=0, stdout=json.dumps(list(inherited if raw else entries)), stderr="")
                return run
            with patch.object(isolation.subprocess, "run", side_effect=run_with(expected)):
                isolation.assert_empty_mcp("delegate:2", "main")
            bad_sets = ([], expected + [dict(expected[0], name="evil")],
                        [dict(expected[0], transport={**expected[0]["transport"], "args": ["/tmp/evil"]}), expected[1]],
                        [expected[0], dict(expected[1], tool_timeout_sec=1)])
            for bad in bad_sets:
                with patch.object(isolation.subprocess, "run", side_effect=run_with(bad)):
                    with self.assertRaises(isolation.TenantUnavailable):
                        isolation.assert_empty_mcp("delegate:2", "main")
            with patch.object(isolation.subprocess, "run", side_effect=run_with(expected, expected[:1])):
                with self.assertRaises(isolation.TenantUnavailable):
                    isolation.assert_empty_mcp("delegate:2", "main")
            with patch("delegate_to_claude_mcp.claude_bridge_dir", return_value=None):
                self.assertEqual(set(isolation.isolated_mcp_servers("2")), {"send-telegram-file"})

    def test_broker_forwards_directory_identity_and_rejects_attacks(self):
        called = []
        with patch.object(broker, "_delegate_request", side_effect=lambda text, chat_id: (called.append((text, chat_id)) or True, "answer")):
            name, path = self.put({"text": "task"})
            broker.process_tenant_delegate_queue("2", str(self.exchange))
            self.assertEqual(self.wait_result(name), {"done": True, "ok": True, "text": "answer"})
            self.assertEqual(called, [("task", 2)])
            self.assertFalse(path.exists())
            rejected = (b"{broken", b"x" * 65537, b'{"text":"a","text":"b"}',
                        {"text": ""}, {"text": "x" * 32001},
                        {"text": "ok", "chat_id": 1}, {"text": "ok", "target": "1"})
            for body in rejected:
                name, path = self.put(body)
                broker.process_tenant_delegate_queue("2", str(self.exchange))
                self.assertFalse(self.result(name)["ok"])
                self.assertFalse(path.exists())
            name, _ = self.put({"text": "ok"}, "delegate-bad.json")
            broker.process_tenant_delegate_queue("2", str(self.exchange))
            self.assertFalse(self.result(name)["ok"])
            outside = self.root / "outside"
            outside.write_text("secret")
            for create in (lambda p: p.symlink_to(outside), lambda p: os.mkfifo(p)):
                name = f"delegate-{uuid.uuid4()}.json"
                create(self.exchange / "queue" / name)
                broker.process_tenant_delegate_queue("2", str(self.exchange))
                self.assertFalse(self.result(name)["ok"])
            self.assertEqual(outside.read_text(), "secret")
            self.assertEqual(called, [("task", 2)])
            foreign, path = self.put({"text": "ok"}, f"{uuid.uuid4()}.json")
            broker.process_tenant_delegate_queue("2", str(self.exchange))
            self.assertTrue(path.exists())
            self.assertFalse((self.exchange / "result" / foreign).exists())

    def test_mcp_reaches_exchange_below_a_search_only_directory(self):
        """The tenants root is mode 711 on a real host: the tenant may traverse it but not read it."""
        base = self.root / "tenants"
        exchange = base / "123456" / ".exchange"
        for part in ("queue", "result"):
            (exchange / part).mkdir(parents=True)
        os.chmod(base, 0o111)
        try:
            with patch.dict(os.environ, {"TENANT_EXCHANGE_DIR": str(exchange)}):
                request_id = str(uuid.uuid4())
                mcp._write_request(str(exchange), request_id, "task")
                self.assertTrue((exchange / "queue" / f"delegate-{request_id}.json").exists())
        finally:
            os.chmod(base, 0o755)

    def test_busy_and_unavailable_result(self):
        gate = threading.Event()
        def slow(text, chat_id):
            gate.wait(3)
            return True, "done"
        with patch.object(broker, "_delegate_request", side_effect=slow):
            first, _ = self.put({"text": "first"})
            broker.process_tenant_delegate_queue("2", str(self.exchange))
            second, _ = self.put({"text": "second"})
            broker.process_tenant_delegate_queue("2", str(self.exchange))
            self.assertIn("занят", self.result(second)["text"])
            gate.set()
            self.assertTrue(self.wait_result(first)["ok"])
        result_dir = self.exchange / "result"
        result_dir.rename(self.exchange / "saved-result")
        result_dir.symlink_to(self.root, target_is_directory=True)
        name, path = self.put({"text": "pending"})
        with patch.object(broker, "_delegate_request", side_effect=AssertionError("forwarded")):
            broker.process_tenant_delegate_queue("2", str(self.exchange))
        self.assertTrue(path.exists())

    def test_existing_peer_queue_receives_directory_chat_id(self):
        peer = self.root / "peer"
        peer.mkdir()
        name, _ = self.put({"text": "peer task"})
        def forward(text, chat_id):
            return legacy._delegate_request(text, wait_seconds=0, chat_id=chat_id)
        with patch.object(legacy, "claude_bridge_dir", return_value=peer), \
                patch.object(broker, "_delegate_request", side_effect=forward):
            broker.process_tenant_delegate_queue("2", str(self.exchange))
            requests = []
            for _ in range(200):
                requests = list((peer / "cross_delegate_queue").glob("*.json"))
                if requests:
                    break
                time.sleep(0.01)
            self.assertEqual(len(requests), 1)
            self.assertEqual(json.loads(requests[0].read_text()), {"chat_id": 2, "text": "peer task"})
            (peer / "cross_delegate_result" / requests[0].name).write_text(
                json.dumps({"done": True, "ok": True, "text": "accepted"}))
            self.assertTrue(self.wait_result(name)["ok"])

    def test_mcp_request_and_safe_result(self):
        request_id = str(uuid.uuid4())
        mcp._write_request(str(self.exchange), request_id, "hello")
        name = f"delegate-{request_id}.json"
        self.assertEqual(json.loads((self.exchange / "queue" / name).read_text()), {"text": "hello"})
        self.assertEqual((self.exchange / "queue" / name).stat().st_mode & 0o777, 0o660)
        (self.exchange / "result" / name).write_text(json.dumps({"done": True, "ok": True, "text": "ok"}))
        self.assertEqual(mcp._read_result(str(self.exchange), name)["text"], "ok")
        self.assertFalse((self.exchange / "result" / name).exists())
        (self.exchange / "queue" / name).unlink()
        self.assertEqual(mcp._handle_message({"id": 1, "method": "tools/list"})["result"]["tools"][0]["name"],
                         "delegate_to_claude")
        def answer():
            for _ in range(200):
                requests = list((self.exchange / "queue").glob("delegate-*.json"))
                if requests:
                    (self.exchange / "result" / requests[0].name).write_text(
                        json.dumps({"done": True, "ok": True, "text": "from Claude"}))
                    return
                time.sleep(0.01)
        with patch.object(mcp, "POLL_INTERVAL_S", 0.01):
            thread = threading.Thread(target=answer)
            thread.start()
            self.assertEqual(mcp._delegate_request("task", 1), (True, "from Claude"))
            thread.join()
        for request in (self.exchange / "queue").glob("delegate-*.json"):
            request.unlink()
        self.assertFalse(mcp._delegate_request("")[0])
        self.assertFalse(mcp._delegate_request("x" * 32001)[0])
        outside = self.root / "outside"
        outside.write_text("secret")
        (self.exchange / "result" / name).symlink_to(outside)
        with self.assertRaises(OSError):
            mcp._read_result(str(self.exchange), name)
        self.assertEqual(outside.read_text(), "secret")

    def test_watchers_do_not_eat_each_other(self):
        attachment = self.exchange / "outbox" / "f.txt"
        attachment.write_text("hello")
        file_name = f"{uuid.uuid4()}.json"
        delegate_name = f"delegate-{uuid.uuid4()}.json"
        self.put({"path": str(attachment)}, file_name)
        self.put({"text": "task"}, delegate_name)
        sent = []
        with patch.object(broker, "_delegate_request", return_value=(True, "answered")):
            broker.process_tenant_delegate_queue("2", str(self.exchange))
            self.assertTrue((self.exchange / "queue" / file_name).exists())
            self.assertEqual(self.wait_result(delegate_name)["text"], "answered")
            second_delegate, _ = self.put({"text": "second"})
            files.process_tenant_queue(2, str(self.exchange),
                                       lambda chat, path, caption: (sent.append(Path(path).read_text()), {"ok": True})[1], 100)
            self.assertTrue((self.exchange / "queue" / second_delegate).exists())
            for _ in range(100):
                with broker._lock:
                    active = any(key[0] == "2" for key in broker._active)
                if not active:
                    break
                time.sleep(0.01)
            broker.process_tenant_delegate_queue("2", str(self.exchange))
            self.assertEqual(self.wait_result(second_delegate)["text"], "answered")
        self.assertEqual(sent, ["hello"])
        self.assertTrue(self.result(file_name)["ok"])


if __name__ == "__main__":
    unittest.main()
