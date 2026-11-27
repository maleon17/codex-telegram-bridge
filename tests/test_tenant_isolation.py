"""Isolated launches and control files use only scratch tenant fixtures."""

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import isolation


class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.exchange = self.root / "exchange"
        self.exchange.mkdir()
        self.home = self.root / "tenant"
        self.home.mkdir()
        self.control = self.root / "control"
        self.env = patch.dict(os.environ, {
            "HOME": str(self.root), "TELEGRAM_BOT_TOKEN": "000000:test", "OWNER_ID": "1",
            "CODEX_BOT_STATE_FILE": str(self.root / "state.json"),
            "CODEX_BOT_ACCOUNTS_DIR": str(self.root / "accounts"),
            "BRIDGE_TENANT_CONTROL_DIR": str(self.control), "TELEGRAM_API_URL": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        spec = importlib.util.spec_from_file_location("isolation_test_bot", ROOT / "bot.py")
        self.bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bot)
        self.old_isolation = isolation.BRIDGE_ISOLATION, isolation.ISOLATED_TENANTS
        isolation._cache.clear()
        self.addCleanup(self._restore_isolation)
        self.calls = []
        def run(argv, **kwargs):
            self.calls.append(argv)
            if "tenant-provision" in " ".join(argv):
                kind = argv[-1]
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "id": "2", "kind": kind, "home": str(self.home),
                    "exchange": str(self.exchange), "uid": 1234,
                }), stderr="")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        self.run_patch = patch.object(isolation.subprocess, "run", side_effect=run)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def _restore_isolation(self):
        isolation.BRIDGE_ISOLATION, isolation.ISOLATED_TENANTS = self.old_isolation
        isolation._cache.clear()

    def _on(self):
        isolation.BRIDGE_ISOLATION = "on"
        isolation.ISOLATED_TENANTS = frozenset({"2"})

    def test_off_uses_legacy_argv(self):
        isolation.BRIDGE_ISOLATION = "off"
        runtime = self.bot.get_tenant(2)
        with patch.object(self.bot, "_ensure_tenant_mcp_config"):
            client = self.bot.get_app_server(runtime)
        self.assertIsNone(client.launch_factory)
        self.assertEqual(client.config_overrides[0], 'mcp_servers.send-telegram-file.env.CHAT_ID="2"')
        self.assertEqual(self.calls, [])

    def test_isolated_app_server_argv_persona_mcp_and_delegate(self):
        self._on()
        runtime = self.bot.get_tenant(2)
        client = self.bot.get_app_server(runtime)
        with self.assertRaises(isolation.TenantUnavailable):
            client.startup_check(lambda method, params: {"data": [{"name": "unapproved"}]})
        client.startup_check(lambda method, params: {"data": [], "nextCursor": None})
        command = ["codex", "app-server", "--listen", "stdio://", "-c", "mcp_servers={}"]
        argv = client.launch_factory(command)
        self.assertEqual(argv, ["sudo", "-n", isolation.BRIDGE_TENANT_RUN, "2", "--kind", "main",
                                "--product", "bridge", "--cwd", "workspace", "--env", "CHAT_ID=2",
                                "--", *command])
        self.assertIn("codex", self.calls[-1])
        persona = self.control / "2" / "persona.md"
        mcp = self.control / "2" / "mcp.json"
        self.assertTrue(persona.exists())
        self.assertEqual(json.loads(mcp.read_text()), {"mcpServers": {}})
        self.assertFalse((self.root / "accounts" / "2").exists())
        self.assertEqual(stat.S_IMODE(persona.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.exchange / "persona.md").stat().st_mode), 0o644)
        params = self.bot._thread_params(runtime)
        self.assertEqual(params["cwd"], str(self.home / "workspace"))
        self.assertEqual(params["developerInstructions"], persona.read_text())
        delegate = self.bot.get_delegate_tenant(2)
        argv = self.bot.get_app_server(delegate).launch_factory(command)
        self.assertEqual(argv[5], "delegate")
        self.assertTrue((self.control / "2" / "delegate" / "persona.md").exists())

    def test_provision_refusal_sends_localized_message_and_stops(self):
        self._on()
        isolation._cache.clear()
        self.run_patch.stop()
        with patch.object(isolation.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="denied")), \
                patch.object(self.bot, "load_whitelist", return_value={"2"}), \
                patch.object(self.bot, "send_plain") as send:
            self.bot.handle_message({"chat": {"id": 2}, "from": {"id": 2}, "text": "hello"})
        self.assertIn("недоступен", send.call_args.args[1])
        self.assertIsNone(self.bot.get_tenant(2).app_server)

    def test_workspace_rejects_escape_and_stores_relative_path(self):
        self._on()
        messages = []
        with patch.object(self.bot, "send_plain", side_effect=lambda chat, text: messages.append(text)):
            for value in ("../escape", "/tmp/outside", "workspace/../outside"):
                self.bot.handle_command(2, "/workspace " + value)
                self.assertEqual(messages[-1], self.bot.t("tenant_workspace_invalid"))
            self.bot.handle_command(2, "/workspace project")
        self.assertEqual(self.bot.chat_state(2)["workspace"], "workspace/project")
        self.assertEqual(self.bot.runtime_workspace(self.bot.get_tenant(2)), str(self.home / "workspace/project"))

    def test_exchange_copy_restored_from_symlink_fifo_and_large_file(self):
        self._on()
        self.bot.get_app_server(self.bot.get_tenant(2)).launch_factory(["codex", "app-server"])
        target = self.exchange / "persona.md"
        original = target.read_bytes()
        target.unlink()
        target.symlink_to(self.root / "missing")
        isolation.stage_tenant_control("2", "ignored")
        self.assertEqual(target.read_bytes(), original)
        target.unlink()
        os.mkfifo(target)
        isolation.stage_tenant_control("2", "ignored")
        self.assertEqual(target.read_bytes(), original)
        with target.open("wb") as handle:
            handle.truncate(32 * 1024 * 1024)
        isolation.stage_tenant_control("2", "ignored")
        self.assertEqual(target.read_bytes(), original)

    def test_tenant_configured_mcp_blocks_start(self):
        self._on()
        isolation.ensure_tenant(2)
        client = self.bot.get_app_server(self.bot.get_tenant(2))
        with patch.object(isolation.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout='[{"name":"unapproved"}]', stderr="",
        )):
            with self.assertRaises(isolation.TenantUnavailable):
                client.launch_factory(["codex", "app-server"])

    def test_runner_refusal_with_empty_stdout_blocks_start(self):
        self._on()
        isolation.ensure_tenant(2)
        client = self.bot.get_app_server(self.bot.get_tenant(2))
        with patch.object(isolation.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="runner denied",
        )):
            with self.assertRaisesRegex(isolation.TenantUnavailable, "runner denied"):
                client.launch_factory(["codex", "app-server"])

    def test_sessions_use_app_server_and_readiness_is_cached(self):
        self._on()
        runtime = self.bot.get_tenant(2)
        calls = []
        class Client:
            def request(self, method, params=None, timeout=None):
                calls.append(method)
                if method == "thread/list":
                    return {"data": [{"id": "thread-one", "preview": "A task"}], "nextCursor": None}
                if method == "account/read":
                    return {"account": {"type": "chatgpt"}}
                return {}
        with patch.object(self.bot, "get_app_server", return_value=Client()):
            self.assertEqual(self.bot.session_info(self.bot.session_files(runtime)[0]), ("thread-one", "A task"))
            self.assertTrue(self.bot.account_is_ready(runtime))
            self.assertTrue(self.bot.account_is_ready(runtime))
        self.assertEqual(calls, ["thread/list", "account/read"])
        self.assertFalse((self.home / ".codex-bridge").exists())

    def test_status_refreshes_account_through_tenant_app_server(self):
        self._on()
        calls, sent = [], []
        class Client:
            def start_if_needed(self):
                calls.append("start")
            def request(self, method, params=None, timeout=None):
                calls.append(method)
                return {"account": {"type": "chatgpt"}}
        with patch.object(self.bot, "get_app_server", return_value=Client()), \
                patch.object(self.bot, "selected_model"), \
                patch.object(self.bot, "send_plain", side_effect=lambda chat, text: sent.append(text)):
            self.bot.handle_command(2, "/status")
        self.assertEqual(calls, ["start", "account/read"])
        self.assertIn("ready", self.bot.chat_state(2)["account_status"])
        self.assertTrue(sent)


if __name__ == "__main__":
    unittest.main()
