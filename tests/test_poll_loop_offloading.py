import importlib.util
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class StopPolling(Exception):
    pass


class PollLoopOffloadingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "123456:test", "OWNER_ID": "1",
            "CODEX_BOT_STATE_FILE": str(root / "state.json"),
            "CODEX_BOT_ACCOUNTS_DIR": str(root / "accounts"),
            "TELEGRAM_API_URL": "",
        }):
            spec = importlib.util.spec_from_file_location("poll_offload_test_bot", ROOT / "bot.py")
            self.bot = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.bot)
        self.assertFalse(self.bot.LOCAL_BOT_API)
        self.bot.update_state(2, account_status="ready")

    def run_batch(self, first_message, work_done):
        bot = self.bot
        calls = []
        sent = []
        status_seen = threading.Event()
        language_finished = threading.Event()
        started = [None]

        def tg_call(method, params=None, timeout=None):
            if method == "getUpdates":
                if not calls:
                    calls.append(method)
                    started[0] = time.monotonic()
                    return {"ok": True, "result": [
                        {"update_id": 1, "message": first_message},
                        {"update_id": 2, "message": {
                            "chat": {"id": 2}, "from": {"id": 2}, "text": "/status",
                        }},
                    ]}
                raise StopPolling()
            return {"ok": True, "result": {"message_id": 100}}

        def send_plain(chat_id, text):
            sent.append((time.monotonic(), chat_id, text))
            if chat_id == 2:
                status_seen.set()
            return {"ok": True, "result": {"message_id": 100}}

        def edit_plain(chat_id, _message_id, _text):
            if chat_id == 1:
                language_finished.set()
            return {"ok": True}

        def run_main():
            try:
                bot.main()
            except StopPolling:
                pass

        with patch.object(bot, "tg_call", side_effect=tg_call), \
                patch.object(bot, "send_plain", side_effect=send_plain), \
                patch.object(bot, "edit_plain", side_effect=edit_plain), \
                patch.object(bot, "register_commands"), \
                patch.object(bot, "_ensure_tenant_mcp_config"), \
                patch.object(bot, "get_app_server", side_effect=RuntimeError("skip warm start")), \
                patch.object(bot, "retry_pending_deliveries"), \
                patch.object(bot, "restart_watcher"), \
                patch.object(bot, "external_request_watcher"), \
                patch.object(bot, "cross_delegate_watcher"), \
                patch.object(bot, "file_send_queue_watcher"), \
                patch.object(bot, "pending_delivery_watcher"), \
                patch.object(bot, "load_whitelist", return_value={"1", "2"}), \
                patch.object(bot, "selected_model", return_value="model"):
            thread = threading.Thread(target=run_main, daemon=True)
            thread.start()
            self.assertTrue(status_seen.wait(1.5), "tenant B waited for tenant A")
            elapsed = next(ts for ts, chat_id, _ in sent if chat_id == 2) - started[0]
            self.assertLess(elapsed, 1.5)
            self.assertFalse(work_done.is_set(), "tenant A finished before the status check")
            self.assertTrue(work_done.wait(4))
            if first_message.get("text", "").startswith("/language"):
                self.assertTrue(language_finished.wait(2))
            else:
                deadline = time.monotonic() + 2
                while bot.local_message_queues and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(bot.local_message_queues)
            thread.join(1)

    def test_voice_message_does_not_block_other_tenants_on_cloud_api(self):
        bot = self.bot
        done = threading.Event()

        def transcribe(_path):
            time.sleep(2)
            return "transcript"

        voice = {"chat": {"id": 1}, "from": {"id": 1},
                 "voice": {"file_id": "voice-1", "duration": 3}}
        with patch.dict(sys.modules, {"faster_whisper": types.ModuleType("faster_whisper")}), \
                patch.object(bot, "download_telegram_file", return_value="/tmp/voice.ogg"), \
                patch.object(bot, "transcribe_voice", side_effect=transcribe), \
                patch.object(bot, "queue_message", side_effect=lambda *_args: done.set()):
            self.run_batch(voice, done)

    def test_language_switch_does_not_block_other_tenants(self):
        bot = self.bot
        done = threading.Event()
        with patch.object(bot, "_ensure_tenant_mcp_config"):
            persona = bot.tenant_codex_home(1) / "AGENTS.md"
        bot._write_persona(persona, "# Custom persona\nAlways answer in Russian.\n")
        self.assertFalse(bot._persona_is_default(1))

        def translate(command, **_kwargs):
            time.sleep(2)
            Path(command[command.index("--output-last-message") + 1]).write_text(
                persona.read_text(encoding="utf-8").replace("Russian", "English"),
                encoding="utf-8",
            )
            done.set()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        language = {"chat": {"id": 1}, "from": {"id": 1}, "text": "/language en"}
        with patch.object(bot.subprocess, "run", side_effect=translate):
            self.run_batch(language, done)
        self.assertIn("English", persona.read_text(encoding="utf-8"))
