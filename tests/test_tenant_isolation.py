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
            "BRIDGE_TENANT_CONTROL_DIR": str(self.control),
            # Hermetic: the default MCP directory exists on hosts that installed the isolation module.
            "BRIDGE_ISOLATED_MCP_DIR": str(self.root / "no-mcp"), "TELEGRAM_API_URL": "",
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
        self.assertIn(isolation.with_isolation_note(persona.read_text()).strip(),
                      params["developerInstructions"])
        self.assertEqual(params["developerInstructions"].count(self.bot.SESSION_TITLE_INSTRUCTIONS), 1)
        self.assertNotIn(isolation.ISOLATION_NOTE, persona.read_text())
        delegate = self.bot.get_delegate_tenant(2)
        argv = self.bot.get_app_server(delegate).launch_factory(command)
        self.assertEqual(argv[5:8], ["main", "--product", "delegate"])
        self.assertTrue((self.control / "2" / "delegate" / "persona.md").exists())
        self.assertTrue((self.exchange / "persona.delegate.md").exists())
        self.assertTrue((self.exchange / "mcp.delegate.json").exists())
        self.assertEqual(self.bot.tenant_codex_home(2, delegate.state_key), self.home / ".codex-delegate")
        delegate_params = self.bot._thread_params(delegate)
        self.assertIn(isolation.with_isolation_note(
            (self.control / "2" / "delegate" / "persona.md").read_text()).strip(),
            delegate_params["developerInstructions"])
        self.assertEqual(delegate_params["cwd"], str(self.home / "workspace"))
        class DelegateClient:
            def request(self, method, params=None, timeout=None):
                self_method.append(method)
                return {"data": [{"id": "delegate-thread"}]} if method == "thread/list" else {"account": {"type": "chatgpt"}}
        self_method = []
        with patch.object(self.bot, "get_app_server", return_value=DelegateClient()):
            self.assertEqual(self.bot.session_files(delegate)[0]["id"], "delegate-thread")
            self.assertIn("chatgpt", self.bot.account_status_report(delegate))
        self.assertEqual(self_method, ["thread/list", "account/read"])

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

    def test_expected_mcp_rejects_missing_extra_and_modified_entries(self):
        self._on()
        mcp_dir = self.root / "mcp"
        mcp_dir.mkdir()
        (mcp_dir / "codex_send_telegram_file_mcp.py").write_text("# fixture\n")
        with patch.dict(os.environ, {"BRIDGE_ISOLATED_MCP_DIR": str(mcp_dir)}):
            server = isolation.isolated_mcp_servers(2)["send-telegram-file"]
            runtime = self.bot.get_tenant(2)
            client = self.bot.get_app_server(runtime)
            self.assertIn("TENANT_EXCHANGE_DIR", " ".join(client.config_overrides))
            self.assertIn("CHAT_ID", " ".join(client.config_overrides))
            entry = {"name": "send-telegram-file", "enabled": True,
                     "transport": {"type": "stdio", **server, "env_vars": [], "cwd": None}}
            def mock_list(entries, inherited=None):
                def run(argv, **kwargs):
                    raw = not any("mcp_servers.send-telegram-file.command" in str(part) for part in argv)
                    return SimpleNamespace(returncode=0,
                                           stdout=json.dumps((inherited or []) if raw else entries), stderr="")
                return run
            with patch.object(isolation.subprocess, "run", side_effect=mock_list([entry])):
                client.launch_factory(["codex", "app-server"])
                client.startup_check(lambda method, params: {"data": [{"name": "send-telegram-file"}]})
                for data in ([], [{"name": "evil"}],
                             [{"name": "send-telegram-file"}, {"name": "evil"}]):
                    with self.assertRaises(isolation.TenantUnavailable):
                        client.startup_check(lambda method, params, data=data: {"data": data})
            for changed in ([], [dict(entry, name="evil")],
                            [entry, dict(entry, name="evil")],
                            [dict(entry, transport={**entry["transport"], "command": "/bin/false"})],
                            [dict(entry, transport={**entry["transport"], "args": ["/tmp/evil"]})]):
                with patch.object(isolation.subprocess, "run", side_effect=mock_list(changed)):
                    with self.assertRaises(isolation.TenantUnavailable):
                        client.launch_factory(["codex", "app-server"])
                    with self.assertRaises(isolation.TenantUnavailable):
                        client.startup_check(lambda method, params: {"data": [{"name": "send-telegram-file"}]})
            with patch.object(isolation.subprocess, "run", side_effect=mock_list(
                    [entry], [dict(entry, transport={**entry["transport"], "command": "/bin/false"})])):
                with self.assertRaises(isolation.TenantUnavailable):
                    client.launch_factory(["codex", "app-server"])
            persona = self.bot._persona_path(2)
            self.assertEqual(persona.read_text().count(self.bot.FILE_SEND_AGENTS_MARKER), 1)
            self.assertIn(str(self.exchange / "outbox"), persona.read_text())
            self.assertEqual(set(json.loads((self.control / "2" / "mcp.json").read_text())["mcpServers"]),
                             {"send-telegram-file"})
        self.bot._persona_path(2)
        self.assertNotIn(self.bot.FILE_SEND_AGENTS_MARKER, persona.read_text())

    def test_isolated_incoming_download_uses_inbox_and_keeps_file(self):
        self._on()
        (self.exchange / "inbox").mkdir()
        source = self.root / "incoming"
        source.write_bytes(b"hello")
        with patch.object(self.bot, "LOCAL_BOT_API", True), \
                patch.object(self.bot, "tg_call", return_value={"ok": True, "result": {"file_path": str(source)}}):
            path = self.bot.download_telegram_file("file", "../unsafe\x00.txt", chat_id=2)
        self.assertEqual(Path(path).parent, self.exchange / "inbox")
        self.assertEqual(Path(path).read_bytes(), b"hello")
        self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o644)
        self.bot.cleanup_media_paths([path], 2)
        self.assertTrue(Path(path).exists())
        self.assertFalse((self.root / "accounts" / "2" / "uploads").exists())

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

    def test_non_session_commands_remain_closed(self):
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
            for cmd in ("account", "status", "usage", "model", "effort", "resume", "compact"):
                self.assertTrue(self.bot.handle_command(2, "/" + cmd))
        self.assertEqual(calls, [])
        self.assertEqual(len(sent), 7)


if __name__ == "__main__":
    unittest.main()
