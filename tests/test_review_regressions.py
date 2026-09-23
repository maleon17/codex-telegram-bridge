import importlib.util
import io
import os
import queue
import sys
import tempfile
import threading
import unittest
import builtins
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("CODEX_BOT_STATE_FILE", str(Path(tempfile.gettempdir()) / "review-state.json"))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("review_bot", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class CommandCancellationTests(unittest.TestCase):
    def test_read_only_and_invalid_commands_do_not_cancel_pending_batch(self):
        runtime = bot.TenantRuntime(1)
        runtime.pending_batch = [([{"type": "text", "text": "keep"}], [])]
        catalog = [{"id": "m", "model": "m", "isDefault": True,
                    "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]
        cancelled = []
        with patch.object(bot, "available_models", return_value=catalog), \
                patch.object(bot, "send_plain"), patch.object(bot, "send_rich"), \
                patch.object(bot, "cancel_pending_batch", side_effect=lambda value: cancelled.append(value)):
            for command in ("/status", "/model", "/model nope", "/effort", "/effort nope",
                            "/mode", "/mode nope", "/workspace", "/workspace /missing"):
                self.assertTrue(bot.handle_command(1, command, runtime=runtime))
        self.assertEqual(cancelled, [])
        self.assertTrue(runtime.pending_batch)


class StartupStopTests(unittest.TestCase):
    def test_stop_before_turn_id_prevents_thread_start(self):
        runtime = bot.TenantRuntime(1)
        runtime.busy = True
        runtime.app_server = object()
        bot.update_state(1, model="m", effort="low")
        requests = []
        class Client:
            process = None
            def start_if_needed(self):
                pass
            def request(self, method, params, timeout=None):
                requests.append(method)
                return {}
        self.assertTrue(bot.stop_current_process(runtime))
        client = Client()
        runtime.app_server = client
        with patch.object(bot, "get_app_server", return_value=client), \
                patch.object(bot.TurnView, "deliver"):
            bot.run_turn(runtime, [{"type": "text", "text": "x"}], None)
        self.assertEqual(requests, [])

    def test_stop_immediately_after_turn_id_interrupts(self):
        runtime = bot.TenantRuntime(1)
        runtime.busy = True
        bot.update_state(1, model="m", effort="low")
        calls = []
        class Client:
            process = None
            def start_if_needed(self):
                pass
            def request(self, method, params, timeout=None):
                calls.append(method)
                if method == "thread/start":
                    return {"thread": {"id": "thread"}}
                if method == "turn/start":
                    runtime.cancel_requested = True
                    return {"turn": {"id": "turn"}}
                if method == "turn/interrupt":
                    return {}
                return {}
        client = Client()
        runtime.app_server = client
        with patch.object(bot, "get_app_server", return_value=client), \
                patch.object(bot.TurnView, "flush"), patch.object(bot.TurnView, "deliver"):
            bot.run_turn(runtime, [{"type": "text", "text": "x"}], None)
        self.assertIn("turn/interrupt", calls)


class MediaCommandTests(unittest.TestCase):
    def test_captioned_document_is_prompt_not_command(self):
        queued = []
        message = {"chat": {"id": 1}, "from": {"id": 1}, "caption": "/stop",
                   "document": {"file_id": "d", "file_name": "note.txt", "mime_type": "text/plain"}}
        with patch.object(bot, "load_whitelist", return_value={"1"}), \
                patch.object(bot, "message_inputs", return_value=([{"type": "text", "text": "/stop"}], [], [])), \
                patch.object(bot, "queue_message", side_effect=lambda *args: queued.append(args)), \
                patch.object(bot, "handle_command") as command:
            bot.handle_message(message)
        command.assert_not_called()
        self.assertEqual(len(queued), 1)

    def test_captioned_document_workspace_command_is_prompt_not_command(self):
        queued = []
        message = {"chat": {"id": 1}, "from": {"id": 1}, "caption": "/workspace /tmp",
                   "document": {"file_id": "d", "file_name": "note.txt", "mime_type": "text/plain"}}
        with patch.object(bot, "load_whitelist", return_value={"1"}), \
                patch.object(bot, "message_inputs", return_value=([{"type": "text", "text": "/workspace /tmp"}], [], [])), \
                patch.object(bot, "queue_message", side_effect=lambda *args: queued.append(args)), \
                patch.object(bot, "handle_command") as command:
            bot.handle_message(message)
        command.assert_not_called()
        self.assertEqual(len(queued), 1)


class IncomingFileTests(unittest.TestCase):
    def test_small_text_document_is_included_as_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_text("evidence", encoding="utf-8")
            with patch.object(bot, "download_telegram_file", return_value=str(path)):
                inputs, paths, failures = bot.message_inputs({"chat": {"id": 1}, "caption": "analyse",
                    "document": {"file_id": "d", "file_name": "note.txt", "mime_type": "text/plain"}})
        self.assertEqual(paths, [str(path)])
        self.assertEqual(failures, [])
        self.assertTrue(any("evidence" in value.get("text", "") for value in inputs))

    def test_pdf_is_explicitly_passed_by_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brief.pdf"
            path.write_bytes(b"%PDF")
            with patch.object(bot, "download_telegram_file", return_value=str(path)):
                inputs, paths, failures = bot.message_inputs({"chat": {"id": 1}, "document": {
                    "file_id": "d", "file_name": "brief.pdf", "mime_type": "application/pdf"}})
        self.assertEqual(paths, [str(path)])
        self.assertEqual(failures, [])
        self.assertTrue(any(str(path) in value.get("text", "") for value in inputs))

    def test_zip_is_explicitly_passed_by_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.zip"
            path.write_bytes(b"PK\x03\x04")
            with patch.object(bot, "download_telegram_file", return_value=str(path)):
                inputs, paths, failures = bot.message_inputs({"chat": {"id": 1}, "document": {
                    "file_id": "d", "file_name": "archive.zip", "mime_type": "application/zip"}})
        self.assertEqual(paths, [str(path)])
        self.assertEqual(failures, [])
        self.assertTrue(any(str(path) in value.get("text", "") for value in inputs))

    def test_octet_stream_zip_is_accepted_by_suffix(self):
        """Telegram clients commonly mislabel archives as application/octet-stream;
        the file name suffix must still let a real zip through."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.zip"
            path.write_bytes(b"PK\x03\x04")
            with patch.object(bot, "download_telegram_file", return_value=str(path)):
                inputs, paths, failures = bot.message_inputs({"chat": {"id": 1}, "document": {
                    "file_id": "d", "file_name": "archive.zip", "mime_type": "application/octet-stream"}})
        self.assertEqual(paths, [str(path)])
        self.assertEqual(failures, [])

    def test_unrecognized_format_is_passed_by_local_path_not_rejected(self):
        """No attachment format is pre-rejected -- an unknown one still
        reaches Codex as a local path, same as pdf/zip."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world.sav"
            path.write_bytes(b"\x00\x01save-data")
            with patch.object(bot, "download_telegram_file", return_value=str(path)):
                inputs, paths, failures = bot.message_inputs({"chat": {"id": 1}, "document": {
                    "file_id": "d", "file_name": "world.sav", "mime_type": "application/octet-stream"}})
        self.assertEqual(paths, [str(path)])
        self.assertEqual(failures, [])
        self.assertTrue(any(str(path) in value.get("text", "") for value in inputs))

    def test_oversized_text_document_falls_back_to_local_path(self):
        """A text file too big to inline is still usable, not rejected --
        mirrors the oversized-text branch existing before this fix, which
        used to raise UnsupportedAttachmentError instead."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "huge.txt"
            path.write_text("x" * (bot.TEXT_DOCUMENT_MAX_BYTES + 1), encoding="utf-8")
            with patch.object(bot, "download_telegram_file", return_value=str(path)):
                inputs, paths, failures = bot.message_inputs({"chat": {"id": 1}, "document": {
                    "file_id": "d", "file_name": "huge.txt", "mime_type": "text/plain"}})
        self.assertEqual(paths, [str(path)])
        self.assertEqual(failures, [])
        self.assertTrue(any(str(path) in value.get("text", "") for value in inputs))
        self.assertFalse(any("xxxx" in value.get("text", "") for value in inputs))

    def test_undecodable_text_suffix_falls_back_to_local_path(self):
        """A .txt-named file that isn't valid UTF-8 is still usable, not
        rejected -- mirrors the bad-encoding branch existing before this
        fix, which used to raise UnsupportedAttachmentError instead."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_bytes(b"\xff\xfe\x00binary-not-utf8")
            with patch.object(bot, "download_telegram_file", return_value=str(path)):
                inputs, paths, failures = bot.message_inputs({"chat": {"id": 1}, "document": {
                    "file_id": "d", "file_name": "note.txt", "mime_type": "text/plain"}})
        self.assertEqual(paths, [str(path)])
        self.assertEqual(failures, [])
        self.assertTrue(any(str(path) in value.get("text", "") for value in inputs))

    def test_voice_without_faster_whisper_is_explicitly_rejected(self):
        original_import = builtins.__import__

        def no_whisper(name, *args, **kwargs):
            if name == "faster_whisper":
                raise ImportError("not installed")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=no_whisper):
            with self.assertRaisesRegex(bot.UnsupportedAttachmentError, "не поддерживаются"):
                bot.message_inputs({"chat": {"id": 1}, "voice": {"file_id": "v"}})


class RichDeliveryTests(unittest.TestCase):
    def test_final_rich_text_is_chunked_without_open_fences(self):
        calls = []
        text = "intro\n\n```python\n" + ("x" * (bot.RICH_MAX_CHARS + 20)) + "\n```\n\noutro"
        with patch.object(bot, "tg_call", side_effect=lambda method, params: calls.append((method, params)) or {"ok": True}):
            result = bot.send_rich(1, text)
        self.assertTrue(result["ok"])
        parts = [params["rich_message"]["markdown"] for method, params in calls if method == "sendRichMessage"]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= bot.RICH_MAX_CHARS for part in parts))
        self.assertTrue(all(part.count("```") % 2 == 0 for part in parts))
        self.assertIn("outro", parts[-1])

    def test_undelivered_final_is_retained_then_retried_once(self):
        view = bot.TurnView(1)
        view.items = [{"type": "agent_message", "text": "answer"}]
        with patch.object(bot, "send_rich", side_effect=[{"ok": False}, {"ok": True}]) as send:
            view.deliver()
            pending = bot.chat_state(1).get("pending_delivery")
            self.assertTrue(pending)
            bot.retry_pending_deliveries()  # not due yet: backoff must hold
            self.assertEqual(send.call_count, 1)
            bot.update_state(1, pending_delivery=dict(pending, next_retry_at=0))
            bot.retry_pending_deliveries()
        self.assertEqual(send.call_count, 2)
        self.assertIsNone(bot.chat_state(1).get("pending_delivery"))

    def test_fresh_delivery_is_not_picked_up_by_retry_loop(self):
        bot._set_pending_delivery(2, 2, "answer", False)
        with patch.object(bot, "send_rich", return_value={"ok": True}) as send:
            bot.retry_pending_deliveries()
        send.assert_not_called()
        bot.update_state(2, pending_delivery=None)

    def test_concurrent_delivery_of_same_key_does_not_duplicate(self):
        bot._set_pending_delivery(3, 3, "answer", False)
        delivery = dict(bot.chat_state(3)["pending_delivery"], next_retry_at=0)
        bot.update_state(3, pending_delivery=delivery)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow_send(chat_id, text):
            calls.append(text)
            entered.set()
            release.wait(2)
            return {"ok": True}
        with patch.object(bot, "send_rich", side_effect=slow_send):
            first = threading.Thread(target=bot._deliver_pending, args=(3, delivery))
            first.start()
            self.assertTrue(entered.wait(2))
            second = bot._deliver_pending(3, delivery)
            release.set()
            first.join(2)
        self.assertTrue(second.get("in_flight"))
        self.assertEqual(calls, ["answer"])
        self.assertIsNone(bot.chat_state(3).get("pending_delivery"))

    def test_permanent_failure_gives_up_after_max_attempts(self):
        bot._set_pending_delivery(4, 4, "answer", False)
        with patch.object(bot, "send_rich", return_value={"ok": False, "description": "blocked"}) as send, \
                patch.object(bot, "write_last_turn") as last_turn:
            for _ in range(bot.DELIVERY_MAX_ATTEMPTS + 3):
                pending = bot.chat_state(4).get("pending_delivery")
                if not pending:
                    break
                bot.update_state(4, pending_delivery=dict(pending, next_retry_at=0))
                bot.retry_pending_deliveries()
        self.assertEqual(send.call_count, bot.DELIVERY_MAX_ATTEMPTS)
        self.assertIsNone(bot.chat_state(4).get("pending_delivery"))
        last_turn.assert_called_with(4, "answer", delegated=False, ok=False)

    def test_long_final_replaces_progress_card_then_sends_rest(self):
        text = "\n".join(["line " + "y" * 200] * (bot.RICH_MAX_CHARS // 100))
        bot._set_pending_delivery(5, 5, text, False)
        delivery = bot.chat_state(5)["pending_delivery"]
        self.assertGreater(len(delivery["parts"]), 1)
        with patch.object(bot, "edit_rich", return_value={"ok": True}) as edit, \
                patch.object(bot, "send_rich", return_value={"ok": True}) as send:
            self.assertTrue(bot._deliver_pending(5, delivery, progress_message_id=77)["ok"])
        edit.assert_called_once_with(5, 77, delivery["parts"][0])
        self.assertEqual(send.call_count, len(delivery["parts"]) - 1)


class FileQueueWatcherTests(unittest.TestCase):
    def test_file_queue_has_its_own_periodic_watcher(self):
        calls = []
        class Stop(Exception):
            pass
        with patch.object(bot, "process_file_send_queue", side_effect=lambda: calls.append(True)), \
                patch.object(bot.time, "sleep", side_effect=Stop):
            with self.assertRaises(Stop):
                bot.file_send_queue_watcher()
        self.assertEqual(calls, [True])


class ResumeErrorTests(unittest.TestCase):
    def test_transient_resume_error_preserves_thread(self):
        runtime = bot.TenantRuntime(1)
        bot.update_state(1, thread_id="old-thread")
        class Client:
            process = None
            def request(self, method, params, timeout=None):
                raise bot.AppServerError("app-server request timed out: thread/resume")
        with self.assertRaises(bot.AppServerError):
            bot.ensure_thread(runtime, Client(), "old-thread")
        self.assertEqual(bot.chat_state(1)["thread_id"], "old-thread")

    def test_missing_resume_starts_new_thread_and_reports_it(self):
        runtime = bot.TenantRuntime(1)
        bot.update_state(1, thread_id="old-thread")
        calls = []
        class Client:
            process = None
            def request(self, method, params, timeout=None):
                calls.append(method)
                if method == "thread/resume":
                    raise bot.AppServerError("thread not found")
                return {"thread": {"id": "new-thread"}}
        with patch.object(bot, "send_plain") as sent:
            self.assertEqual(bot.ensure_thread(runtime, Client(), "old-thread"), "new-thread")
        self.assertEqual(calls, ["thread/resume", "thread/start"])
        sent.assert_called()


if __name__ == "__main__":
    unittest.main()


class RealMissingThreadWordingTests(unittest.TestCase):
    def test_codex_no_rollout_wording_starts_new_thread(self):
        for wording in ("no rollout found for thread id abc", "invalid thread id: abc"):
            runtime = bot.TenantRuntime(1)
            class Client:
                process = None
                def request(self, method, params, timeout=None):
                    if method == "thread/resume":
                        raise bot.AppServerError(wording)
                    return {"thread": {"id": "new-thread"}}
            with patch.object(bot, "send_plain"):
                self.assertEqual(bot.ensure_thread(runtime, Client(), "old"), "new-thread")
