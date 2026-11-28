"""Hermetic session listing, title exchange, and turn instruction checks."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import isolation
import tenant_exchange
import codex_rename_session_mcp as mcp


class SessionTitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"HOME": str(self.root), "TMPDIR": str(self.root),
                                  "TELEGRAM_BOT_TOKEN": "000000:test", "OWNER_ID": "1",
                                  "CODEX_BOT_STATE_FILE": str(self.root / "state.json"),
                                  "CODEX_BOT_ACCOUNTS_DIR": str(self.root / "accounts"),
                                  "BRIDGE_ISOLATED_MCP_DIR": str(self.root / "no-mcp"),
                                  "TELEGRAM_API_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("session_title_bot", ROOT / "bot.py")
        self.bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bot)

    def test_name_beats_preview_and_file_fallback_skips_greeting(self):
        self.assertEqual(self.bot.session_info({"id": "abcdef123", "name": "Useful name", "preview": "привет"}),
                         ("abcdef123", "Useful name"))
        file = self.root / "rollout.jsonl"
        file.write_text("\n".join(json.dumps({"type": "response_item", "payload": {
            "role": "user", "content": [{"type": "input_text", "text": text}]}}) for text in
            ("привет", "<environment_context>metadata", "Как починить сборку?", "Какие тесты?")),
            encoding="utf-8")
        self.assertEqual(self.bot.session_info(file)[1], "Как починить сборку?")
        sid, display = self.bot.session_display(self.bot.get_tenant(1), file)
        self.assertEqual(sid, "rollout")
        self.assertEqual(display, "Как починить сборку? ↳ Какие тесты?")

    def test_isolated_sessions_are_scoped_to_their_app_server(self):
        calls, sent = [], []
        class Client:
            def __init__(self, tenant):
                self.tenant = tenant
            def start_if_needed(self):
                calls.append((self.tenant, "start"))
            def request(self, method, params=None, timeout=None):
                calls.append((self.tenant, method))
                if method == "thread/list":
                    return {"data": [{"id": f"{self.tenant}-thread", "name": f"Title {self.tenant}",
                                      "preview": "привет"}]}
                if method == "thread/read":
                    return {"thread": {"turns": [{"items": [{"type": "userMessage",
                        "content": [{"type": "text", "text": f"Question {self.tenant}"}]}]}]}}
                raise AssertionError(method)
        with patch.object(self.bot, "tenant_isolated", return_value=True), \
                patch.object(self.bot, "get_app_server", side_effect=lambda r: Client(r.chat_id)), \
                patch.object(self.bot, "send_plain", side_effect=lambda _id, text: sent.append(text)), \
                patch.object(self.bot, "tenant_codex_home", side_effect=AssertionError("filesystem access")):
            self.bot.handle_command(2, "/sessions")
            self.bot.handle_command(3, "/sessions")
        self.assertIn("2-thread", sent[0])
        self.assertNotIn("3-thread", sent[0])
        self.assertIn("3-thread", sent[1])
        self.assertNotIn("2-thread", sent[1])
        self.assertIn("Title 2 ↳ Question 2", sent[0])
        self.assertEqual([x for x in calls if x[1] == "thread/list"], [(2, "thread/list"), (3, "thread/list")])

    def test_rename_is_bound_to_current_thread_and_cache_updates(self):
        runtime = self.bot.get_tenant(2)
        self.bot.save_thread_id(runtime.state_key, "actual-thread-id")
        calls = []
        class Client:
            def request(self, method, params=None, timeout=None):
                calls.append((method, params))
                return {}
        with patch.object(self.bot, "get_app_server", return_value=Client()):
            ok, text = self.bot.rename_current_session(runtime, "  Fix   build  ")
        self.assertTrue(ok)
        self.assertIn("Fix build", text)
        self.assertEqual(calls, [("thread/name/set", {"threadId": "actual-thread-id", "name": "Fix build"})])
        self.assertEqual(self.bot.chat_state(runtime.state_key)["thread_title"], "Fix build")
        for invalid in ("", "a" * 81, "bad\x00title", "bad\nname"):
            with self.assertRaises(ValueError):
                mcp.validate_title(invalid)
        self.assertEqual(mcp.validate_title("  One   two  "), "One two")
        self.assertEqual(len(calls), 1)

    def test_mcp_request_has_no_thread_id_and_returns_bridge_result(self):
        queue = self.root / "queue"
        result = self.root / "result"
        queue.mkdir()
        result.mkdir()
        with patch.object(mcp, "_directories", return_value=(queue, result)), \
                patch.dict(os.environ, {"CHAT_ID": "2"}, clear=False), \
                patch.object(mcp, "_read_result", return_value={"done": True, "ok": True, "text": "renamed"}):
            ok, text = mcp.rename_session(" New   title ")
        self.assertEqual((ok, text), (True, "renamed"))
        request = json.loads(next(queue.glob("rename-*.json")).read_text())
        self.assertEqual(request, {"title": "New title", "chat_id": 2})
        reply = mcp._reply({"id": 1, "method": "tools/list"})
        self.assertEqual(reply["result"]["tools"][0]["inputSchema"]["required"], ["title"])
        self.assertTrue(reply["result"]["tools"][0]["inputSchema"]["additionalProperties"] is False)
        with patch.object(mcp, "rename_session", return_value=(True, "renamed")):
            response = mcp._reply({"id": 2, "method": "tools/call", "params": {
                "name": "rename_session", "arguments": {"title": "Title"}}})
        self.assertEqual(response["result"], {"content": [{"type": "text", "text": "renamed"}],
                                               "isError": False})
        rejected = mcp._reply({"id": 3, "method": "tools/call", "params": {
            "name": "rename_session", "arguments": {"title": "Title", "thread_id": "other"}}})
        self.assertTrue(rejected["result"]["isError"])

    def test_isolated_mcp_is_offered_only_with_existing_exchange(self):
        directory = self.root / "mcp"
        directory.mkdir()
        for name in ("codex_send_telegram_file_mcp.py", "codex_rename_session_mcp.py"):
            (directory / name).write_text("")
        with patch.dict(os.environ, {"BRIDGE_ISOLATED_MCP_DIR": str(directory)}), \
                patch.object(isolation, "ensure_tenant", return_value={"exchange": str(self.root / "exchange")}), \
                patch.object(isolation, "tenant_target", return_value=("main", "bridge")), \
                patch.object(isolation, "tenant_identity", return_value=("2", "main")), \
                patch("delegate_to_claude_mcp.claude_bridge_dir", return_value=None):
            self.assertEqual(set(isolation.isolated_mcp_servers(2)), {"send-telegram-file", "rename-session"})
            overrides = isolation.isolated_mcp_overrides(2)
            self.assertIn("features.apps=false", overrides)
            self.assertTrue(any("mcp_servers.rename-session.command" in item for item in overrides))
            (directory / "codex_send_telegram_file_mcp.py").unlink()
            self.assertEqual(isolation.isolated_mcp_servers(2), {})

    def test_isolated_exchange_rejects_symlink_and_wrong_owner(self):
        exchange = self.root / "exchange"
        for name in ("queue", "result"):
            (exchange / name).mkdir(parents=True)
        name = f"rename-{uuid.uuid4()}.json"
        target = self.root / "outside.json"
        target.write_text('{"title":"Outside"}')
        (exchange / "queue" / name).symlink_to(target)
        called = []
        tenant_exchange.process_tenant_title_queue(str(exchange), os.getuid(),
                                                   lambda title: called.append(title) or (True, "ok"))
        self.assertEqual(called, [])
        name = f"rename-{uuid.uuid4()}.json"
        (exchange / "queue" / name).write_text('{"title":"Own title"}')
        tenant_exchange.process_tenant_title_queue(str(exchange), os.getuid() + 1,
                                                   lambda title: called.append(title) or (True, "ok"))
        self.assertEqual(called, [])
        name = f"rename-{uuid.uuid4()}.json"
        path = exchange / "queue" / name
        path.write_text('{"title":"Own title"}')
        path.chmod(0o660)
        tenant_exchange.process_tenant_title_queue(str(exchange), os.getuid(),
                                                   lambda title: called.append(title) or (True, "ok"))
        self.assertEqual(called, ["Own title"])
        malicious = exchange / "queue" / f"rename-{uuid.uuid4()}.json"
        malicious.write_text('{"title":"Attack","threadId":"other"}')
        malicious.chmod(0o660)
        tenant_exchange.process_tenant_title_queue(str(exchange), os.getuid(),
                                                   lambda title: called.append(title) or (True, "ok"))
        self.assertEqual(called, ["Own title"])

    def test_resume_searches_main_and_delegate(self):
        main = self.bot.get_tenant(1)
        delegate = self.bot.get_delegate_tenant(1)
        sent = []
        def files(runtime):
            return ([{"id": "main-thread"}] if runtime is main
                    else [{"id": "delegate-thread"}])
        with patch.object(self.bot, "session_files", side_effect=files), \
                patch.object(self.bot, "stop_and_wait_for_worker", return_value=True), \
                patch.object(self.bot, "cancel_pending_batch"), \
                patch.object(self.bot, "send_plain", side_effect=lambda _, text: sent.append(text)):
            self.bot.handle_command(1, "/resume delegate-th")
        self.assertEqual(self.bot.chat_state(delegate.state_key)["thread_id"], "delegate-thread")
        self.assertTrue(sent)

    def test_turn_reminders_and_footer(self):
        runtime = self.bot.get_tenant(2)
        self.bot.save_thread_id(runtime.state_key, "abcd1234-long")
        for n in range(1, 11):
            self.assertEqual(self.bot.next_title_turn(runtime, "abcd1234-long"), n)
            params = self.bot._thread_params(runtime, "abcd1234-long")
            # developer instructions are static: they never carry the reminder (it would force a resume per turn)
            self.assertEqual(params["developerInstructions"].count(self.bot.SESSION_TITLE_INSTRUCTIONS), 1)
            self.assertNotIn("bridge-note", params["developerInstructions"])
            self.assertNotIn("input", params)
            user = [{"type": "text", "text": "вопрос"}]
            items = self.bot._turn_input(user, n)
            self.assertEqual(items[0], user[0])
            self.assertEqual(len(items) == 2, n in (2, 10))
            if len(items) == 2:
                self.assertEqual(items[1]["text"], self.bot.SESSION_TITLE_NOTE)
            self.assertEqual(user, [{"type": "text", "text": "вопрос"}])  # the caller's list is untouched
            self.bot.update_state(runtime.state_key, title_turn_thread_id="abcd1234-long", title_turn_count=n)
        self.bot.save_thread_id(runtime.state_key, "new-thread")
        self.assertEqual(self.bot.next_title_turn(runtime, "new-thread"), 1)
        self.bot._cache_thread_title(runtime.state_key, "new-thread", "Build repair")
        view = self.bot.TurnView(2, state_key=runtime.state_key)
        view.usage = {"input_tokens": 1}
        with patch.object(self.bot, "_deliver_pending", return_value={"ok": True}):
            view.deliver()
        text = self.bot.chat_state(runtime.state_key)["pending_delivery"]["text"]
        self.assertIn("сессия: new-thre «Build repair»", text)
        self.bot.update_state(runtime.state_key, pending_delegator_session_id="old-thread")
        delegated = self.bot.TurnView(2, state_key=runtime.state_key)
        delegated.delegated = True
        delegated.usage = {"input_tokens": 1}
        with patch.object(self.bot, "_deliver_pending", return_value={"ok": True}):
            delegated.deliver()
        text = self.bot.chat_state(runtime.state_key)["pending_delivery"]["text"]
        self.assertEqual(text.count("new-thre"), 1)
        self.bot.save_thread_id(runtime.state_key, None)
        empty = self.bot.TurnView(2, state_key=runtime.state_key)
        empty.usage = {"input_tokens": 1}
        with patch.object(self.bot, "_deliver_pending", return_value={"ok": True}):
            empty.deliver()
        self.assertNotIn("сессия:", self.bot.chat_state(runtime.state_key)["pending_delivery"]["text"])

    def test_title_notification_updates_only_current_thread(self):
        runtime = self.bot.get_tenant(2)
        self.bot.save_thread_id(runtime.state_key, "current-thread")
        self.bot.handle_app_notification(runtime, "thread/name/updated",
                                         {"threadId": "other-thread", "threadName": "Wrong"})
        self.assertEqual(self.bot.chat_state(runtime.state_key).get("thread_title"), "")
        self.bot.handle_app_notification(runtime, "thread/name/updated",
                                         {"threadId": "current-thread", "threadName": "Right"})
        self.assertEqual(self.bot.chat_state(runtime.state_key)["thread_title"], "Right")


if __name__ == "__main__":
    unittest.main()
