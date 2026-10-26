import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_bot(directory):
    values = {
        "TELEGRAM_BOT_TOKEN": "123456:test", "OWNER_ID": "1",
        "CODEX_BOT_STATE_FILE": str(directory / "state.json"),
        "TELEGRAM_API_URL": "",
    }
    with patch.dict(os.environ, values):
        spec = importlib.util.spec_from_file_location("watcher_test_bot", ROOT / "bot.py")
        bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot)
    return bot


class WatcherSurvivalTests(unittest.TestCase):
    def test_file_send_queue_watcher_survives_unreadable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot = load_bot(root)
            queue_dir, result_dir, outbox = root / "queue", root / "result", root / "outbox"
            queue_dir.mkdir()
            outbox.mkdir()
            blocked = outbox / "blocked.txt"
            blocked.write_text("private", encoding="utf-8")
            blocked.chmod(0)
            good = outbox / "good.txt"
            good.write_text("ok", encoding="utf-8")
            def send_document(_chat_id, source, _caption):
                if source == blocked:
                    raise PermissionError("unreadable outbox file")
                return {"ok": True}

            try:
                with patch.object(bot, "FILE_SEND_QUEUE_DIR", queue_dir), \
                        patch.object(bot, "FILE_SEND_RESULT_DIR", result_dir), \
                        patch.object(bot, "tenant_file_outbox", return_value=outbox), \
                        patch.object(bot, "load_whitelist", return_value={"1"}), \
                        patch.object(bot, "send_document", side_effect=send_document):
                    (queue_dir / "first.json").write_text(json.dumps({
                        "chat_id": 1, "path": str(blocked), "caption": "",
                    }), encoding="utf-8")
                    bot.process_file_send_queue()
                    first = json.loads((result_dir / "first.json").read_text(encoding="utf-8"))
                    self.assertFalse(first["ok"])
                    (queue_dir / "second.json").write_text(json.dumps({
                        "chat_id": 1, "path": str(good), "caption": "",
                    }), encoding="utf-8")
                    bot.process_file_send_queue()
                    second = json.loads((result_dir / "second.json").read_text(encoding="utf-8"))
                    self.assertTrue(second["ok"])
            finally:
                blocked.chmod(0o600)

    def test_external_request_watcher_survives_non_dict_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot = load_bot(root)
            request_path = root / "external.json"
            bad_seen, good_seen = threading.Event(), threading.Event()
            original = bot.start_delegate_turn
            calls = []
            stop = threading.Event()
            original_sleep = time.sleep

            class StopWatcher(Exception):
                pass

            def sleep(seconds):
                if threading.current_thread() is thread:
                    if stop.is_set():
                        raise StopWatcher()
                    original_sleep(0.01)
                else:
                    original_sleep(seconds)

            def run_watcher():
                try:
                    bot.external_request_watcher()
                except StopWatcher:
                    pass

            def start_delegate_turn(*args, **kwargs):
                calls.append(kwargs.get("env"))
                if kwargs.get("env") == "not-a-dict":
                    bad_seen.set()
                    return original(*args, **kwargs)
                good_seen.set()
                return True

            with patch.object(bot, "EXTERNAL_REQUEST_FILE", request_path), \
                    patch.object(bot, "start_delegate_turn", side_effect=start_delegate_turn), \
                    patch.object(bot.time, "sleep", side_effect=sleep):
                thread = threading.Thread(target=run_watcher, daemon=True)
                thread.start()
                request_path.write_text(json.dumps({
                    "chat_id": 1, "text": "bad", "env": "not-a-dict",
                }), encoding="utf-8")
                self.assertTrue(bad_seen.wait(3))
                deadline = time.monotonic() + 3
                while request_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                request_path.write_text(json.dumps({"chat_id": 1, "text": "good"}), encoding="utf-8")
                self.assertTrue(good_seen.wait(3))
                self.assertTrue(thread.is_alive())
                self.assertEqual(calls, ["not-a-dict", None])
                stop.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
