import importlib.util
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_bot(api_url=None, **environment):
    """Load a fresh bot module so its environment-derived config is real."""
    with tempfile.TemporaryDirectory() as directory:
        values = {
            "TELEGRAM_BOT_TOKEN": "123456:test",
            "OWNER_ID": "1",
            "CODEX_BOT_STATE_FILE": str(Path(directory) / "state.json"),
            **environment,
        }
        if api_url is not None:
            values["TELEGRAM_API_URL"] = api_url
        elif "TELEGRAM_API_URL" not in values:
            values["TELEGRAM_API_URL"] = ""
        with patch.dict(os.environ, values, clear=False):
            name = f"local_bot_api_{uuid.uuid4().hex}"
            spec = importlib.util.spec_from_file_location(name, ROOT / "bot.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    return module


class ApiUrlTests(unittest.TestCase):
    def test_api_url_normalization_distinguishes_cloud_and_local(self):
        bot = load_bot()
        self.assertEqual(
            bot.telegram_api_config(""), ("https://api.telegram.org", False),
        )
        self.assertEqual(
            bot.telegram_api_config("https://api.telegram.org/"),
            ("https://api.telegram.org", False),
        )
        self.assertEqual(
            bot.telegram_api_config("http://127.0.0.1:8081/"),
            ("http://127.0.0.1:8081", True),
        )


class IncomingFileTests(unittest.TestCase):
    def test_cloud_oversize_is_rejected_before_get_file_or_network(self):
        bot = load_bot()
        with patch.object(bot, "tg_call", side_effect=AssertionError("getFile must not run")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network must not run")):
            with self.assertRaises(bot.AttachmentDownloadError) as caught:
                bot.download_telegram_file("large", file_size=21 * 1024 * 1024)
        self.assertEqual(caught.exception.reason, "too_big_for_cloud")
        self.assertIn("локальный Bot API", str(caught.exception))

    def test_local_absolute_path_is_moved_without_http(self):
        bot = load_bot("http://127.0.0.1:8081/", TELEGRAM_GET_FILE_TIMEOUT_S="777")
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "telegram-file.txt"
            original.write_text("payload", encoding="utf-8")
            with patch.object(bot, "tg_call", return_value={
                "ok": True, "result": {"file_path": str(original)},
            }) as get_file, patch("urllib.request.urlopen", side_effect=AssertionError("HTTP must not run")):
                downloaded = Path(bot.download_telegram_file("file", "note.txt"))
            self.assertFalse(original.exists())
            self.assertEqual(downloaded.read_text(encoding="utf-8"), "payload")
            self.assertEqual(bot.GET_FILE_TIMEOUT_S, 777)
            self.assertEqual(get_file.call_args.kwargs["timeout"], 777)
            downloaded.unlink()

    def test_local_missing_absolute_path_has_structured_reason(self):
        bot = load_bot("http://127.0.0.1:8081/")
        missing = Path(tempfile.gettempdir()) / f"missing-{uuid.uuid4().hex}"
        with patch.object(bot, "tg_call", return_value={
            "ok": True, "result": {"file_path": str(missing)},
        }):
            with self.assertRaises(bot.AttachmentDownloadError) as caught:
                bot.download_telegram_file("gone")
        self.assertEqual(caught.exception.reason, "local_file_missing")

    def test_local_inaccessible_path_has_structured_reason(self):
        bot = load_bot("http://127.0.0.1:8081/")
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "blocked"
            blocked.mkdir()
            source = blocked / "file"
            source.write_text("private", encoding="utf-8")
            blocked.chmod(0)
            try:
                with patch.object(bot, "tg_call", return_value={
                    "ok": True, "result": {"file_path": str(source)},
                }):
                    with self.assertRaises(bot.AttachmentDownloadError) as caught:
                        bot.download_telegram_file("private")
            finally:
                blocked.chmod(0o700)
        self.assertEqual(caught.exception.reason, "local_file_access")

    def test_attachment_failure_is_reported_to_the_agent_and_person(self):
        bot = load_bot()
        queued, replies = [], []
        message = {
            "chat": {"id": 1}, "from": {"id": 1}, "text": "Проверь файл",
            "document": {
                "file_id": "large", "file_name": "archive.zip",
                "mime_type": "application/pdf", "file_size": 21 * 1024 * 1024,
            },
        }
        with patch.object(bot, "queue_message", side_effect=lambda *args: queued.append(args)), \
                patch.object(bot, "send_plain", side_effect=lambda _chat, text: replies.append(text)):
            bot.handle_message(message)
        self.assertEqual(len(queued), 1)
        prompt = "\n".join(item["text"] for item in queued[0][1] if item["type"] == "text")
        self.assertIn("[Файл не скачан:", prompt)
        self.assertIn("Не ищи его на диске.", prompt)
        self.assertTrue(any("локальный Bot API" in reply for reply in replies))


class LocalSendTests(unittest.TestCase):
    def test_local_send_uses_file_uri_before_multipart(self):
        bot = load_bot("http://127.0.0.1:8081/")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "report.txt"
            source.write_text("report", encoding="utf-8")
            with patch.object(bot, "tg_call", return_value={"ok": True}) as send, \
                    patch("urllib.request.urlopen", side_effect=AssertionError("multipart must not run")):
                result = bot.send_document(1, source, "готово")
        self.assertTrue(result["ok"])
        self.assertEqual(send.call_args.args[0], "sendDocument")
        self.assertEqual(send.call_args.args[1]["document"], source.resolve().as_uri())

    def test_local_send_transport_failure_is_not_retried(self):
        bot = load_bot("http://127.0.0.1:8081/", TELEGRAM_SEND_TIMEOUT_S="777")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "report.txt"
            source.write_text("report", encoding="utf-8")
            with patch.object(bot, "tg_call", return_value={
                "ok": False, "error": "timed out",
            }) as send, patch("urllib.request.urlopen") as upload:
                result = bot.send_document(1, source, "готово")
        self.assertFalse(result["ok"])
        self.assertIn("не был отправлен повторно", result["description"])
        self.assertEqual(send.call_args.kwargs["timeout"], 777)
        upload.assert_not_called()

    def test_local_send_server_rejection_falls_back_with_send_timeout(self):
        bot = load_bot("http://127.0.0.1:8081/", TELEGRAM_SEND_TIMEOUT_S="777")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "report.txt"
            source.write_text("report", encoding="utf-8")
            with patch.object(bot, "tg_call", return_value={
                "ok": False, "error_code": 400, "description": "Bad Request",
            }) as send, patch("urllib.request.urlopen", return_value=response) as upload:
                result = bot.send_document(1, source, "готово")
        self.assertTrue(result["ok"])
        self.assertEqual(send.call_args.kwargs["timeout"], 777)
        self.assertEqual(upload.call_args.kwargs["timeout"], 777)


class LocalMessageOrderingTests(unittest.TestCase):
    def test_media_download_failure_keeps_later_updates_in_fifo_order(self):
        bot = load_bot("http://127.0.0.1:8081/")
        download_started = threading.Event()
        release_download = threading.Event()
        queued_all = threading.Event()
        queued = []

        def fake_message_inputs(message):
            if message["text"] == "first":
                download_started.set()
                self.assertTrue(release_download.wait(2))
                raise RuntimeError("download failed")
            return ([{"type": "text", "text": message["text"]}], [], [])

        def record_queue(_runtime, inputs, _paths):
            queued.append(inputs[0]["text"])
            if len(queued) == 2:
                queued_all.set()

        def message(text, media=False):
            result = {"chat": {"id": 1}, "from": {"id": 1}, "text": text}
            if media:
                result["document"] = {"file_id": text, "file_name": f"{text}.txt"}
            return result

        with patch.object(bot, "message_inputs", side_effect=fake_message_inputs), \
                patch.object(bot, "queue_message", side_effect=record_queue), \
                patch.object(bot, "send_plain"):
            bot.handle_message(message("first", media=True))
            self.assertTrue(download_started.wait(1))
            bot.handle_message(message("second", media=True))
            bot.handle_message(message("third"))
            self.assertEqual(queued, [])
            release_download.set()
            self.assertTrue(queued_all.wait(2))
        self.assertEqual(queued, ["second", "third"])


class DownloadStatusTests(unittest.TestCase):
    """A slow local-mode download must not leave the chat silent -- see
    the standing bug report: no feedback at all while a large file's
    getFile blocks, easily minutes for a big upload."""

    def test_status_message_is_visible_while_download_is_still_running(self):
        bot = load_bot("http://127.0.0.1:8081/")
        download_started = threading.Event()
        release_download = threading.Event()
        calls = []

        def fake_tg_call(method, params=None, **_kwargs):
            calls.append((method, dict(params or {})))
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 42}}
            return {"ok": True}

        def fake_message_inputs(message):
            download_started.set()
            self.assertTrue(release_download.wait(2))
            return ([{"type": "text", "text": message["text"]}], [], [])

        def message(text):
            return {"chat": {"id": 1}, "from": {"id": 1}, "text": text,
                    "document": {"file_id": text, "file_name": f"{text}.bin"}}

        with patch.object(bot, "tg_call", side_effect=fake_tg_call), \
                patch.object(bot, "message_inputs", side_effect=fake_message_inputs), \
                patch.object(bot, "queue_message"), patch.object(bot, "send_plain"):
            bot.handle_message(message("big"))
            self.assertTrue(download_started.wait(1))
            # The status message must already be on the wire WHILE the
            # download is still blocked -- not only after it finishes.
            sent = [c for c in calls if c[0] == "sendMessage"]
            self.assertEqual(len(sent), 1)
            self.assertIn("Загружаю", sent[0][1]["text"])
            release_download.set()
            for _ in range(200):
                if bot.local_message_queues == {}:
                    break
                threading.Event().wait(0.01)
        edits = [c for c in calls if c[0] == "editMessageText"]
        self.assertTrue(edits, "the status bubble must be updated once the download finishes")
        self.assertEqual(edits[-1][1]["message_id"], 42)

    def test_status_message_counts_files_queued_during_a_slow_download(self):
        bot = load_bot("http://127.0.0.1:8081/")
        first_started = threading.Event()
        second_queued = threading.Event()
        release_first = threading.Event()
        calls = []

        def fake_tg_call(method, params=None, **_kwargs):
            calls.append((method, dict(params or {})))
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 7}}
            return {"ok": True}

        def fake_message_inputs(message):
            if message["text"] == "first":
                first_started.set()
                self.assertTrue(second_queued.wait(2))
                self.assertTrue(release_first.wait(2))
            return ([{"type": "text", "text": message["text"]}], [], [])

        def message(text):
            return {"chat": {"id": 1}, "from": {"id": 1}, "text": text,
                    "document": {"file_id": text, "file_name": f"{text}.bin"}}

        with patch.object(bot, "tg_call", side_effect=fake_tg_call), \
                patch.object(bot, "message_inputs", side_effect=fake_message_inputs), \
                patch.object(bot, "queue_message"), patch.object(bot, "send_plain"):
            bot.handle_message(message("first"))
            self.assertTrue(first_started.wait(1))
            sent = [c for c in calls if c[0] == "sendMessage"]
            self.assertEqual(len(sent), 1)
            self.assertNotIn("/", sent[0][1]["text"])  # only one file known yet
            bot.handle_message(message("second"))
            second_queued.set()
            # A second file queuing up mid-download must refresh the count
            # immediately, not wait for "first" to finish.
            edits = [c for c in calls if c[0] == "editMessageText"]
            self.assertTrue(edits)
            self.assertIn("(0/2)", edits[-1][1]["text"])
            release_first.set()
            for _ in range(200):
                if bot.local_message_queues == {}:
                    break
                threading.Event().wait(0.01)
        edits = [c[1]["text"] for c in calls if c[0] == "editMessageText"]
        self.assertIn("(2/2)", edits[-1])


class DebounceVsSlowDownloadTests(unittest.TestCase):
    """The debounce timer tracks wall-clock quiet time, not "is everything
    in this FIFO actually done downloading" -- a fast sibling message must
    not let it fire (and permanently lose a still-downloading attachment)
    while local mode's FIFO still has unfinished work for the same chat."""

    def test_flush_waits_for_a_slower_sibling_download_before_starting_a_turn(self):
        bot = load_bot("http://127.0.0.1:8081/")
        doc_started = threading.Event()
        release_doc = threading.Event()
        timers = []

        class FakeTimer:
            def __init__(self, interval, function):
                self.function = function
                self.cancelled = False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        launched = []

        def fake_message_inputs(message):
            if message["text"] == "doc":
                doc_started.set()
                self.assertTrue(release_doc.wait(2))
            return ([{"type": "text", "text": message["text"]}], [], [])

        def message(text, media=False):
            result = {"chat": {"id": 1}, "from": {"id": 1}, "text": text}
            if media:
                result["document"] = {"file_id": text, "file_name": f"{text}.bin"}
            return result

        with patch.object(bot.threading, "Timer", FakeTimer), \
                patch.object(bot, "run_turn", lambda *args: launched.append(args)), \
                patch.object(bot, "message_inputs", side_effect=fake_message_inputs), \
                patch.object(bot, "send_plain"), patch.object(bot, "tg_call", return_value={"ok": True}):
            bot.handle_message(message("text"))
            bot.handle_message(message("doc", media=True))
            self.assertTrue(doc_started.wait(1))
            # "text" already finished and scheduled the debounce timer;
            # "doc" is still blocked downloading. Firing that timer now must
            # NOT start a turn missing "doc" -- it must defer instead.
            self.assertEqual(len(timers), 1)
            timers[0].function()
            self.assertEqual(launched, [])
            self.assertGreaterEqual(len(timers), 2)
            self.assertFalse(timers[-1].cancelled)

            release_doc.set()
            runtime = bot.get_tenant(1)
            for _ in range(200):
                if 1 not in bot.local_message_queues:
                    break
                threading.Event().wait(0.01)
            self.assertNotIn(1, bot.local_message_queues)
            # The retry timer scheduled after the deferred flush fires now,
            # with both messages actually landed.
            timers[-1].function()

        self.assertEqual(len(launched), 1)
        self.assertEqual(
            launched[0][1],
            [
                {"type": "text", "text": "text"},
                {"type": "text", "text": "\n\n---\n\n"},
                {"type": "text", "text": "doc"},
            ],
        )


class GetFileErrorTests(unittest.TestCase):
    def test_cloud_get_file_too_big_description_has_cloud_guidance(self):
        bot = load_bot()
        error = bot._get_file_error({
            "ok": False, "description": "Bad Request: file is too big",
        })
        self.assertEqual(error.reason, "too_big_for_cloud")
        self.assertIn("20 МБ", str(error))
        self.assertIn("локальный Bot API", str(error))
