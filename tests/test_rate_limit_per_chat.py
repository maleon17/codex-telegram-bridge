import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class TelegramEmu:
    def __init__(self):
        self.last = {}
        self.http_calls = []

    def __call__(self, request, timeout=None):
        method = request.full_url.rsplit("/", 1)[-1]
        params = json.loads(request.data)
        chat_id = params.get("chat_id")
        self.http_calls.append((method, chat_id))
        now = time.monotonic()
        if method == "sendMessage" and now - self.last.get(chat_id, 0) < 1:
            payload = {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(json.dumps(payload).encode()))
        self.last[chat_id] = now
        return Response({"ok": True, "result": {"message_id": len(self.http_calls)}})


class RateLimitPerChatTests(unittest.TestCase):
    def test_stranger_rate_limit_does_not_suppress_whitelisted_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                "TELEGRAM_BOT_TOKEN": "123456:test", "OWNER_ID": "1",
                "CODEX_BOT_STATE_FILE": str(Path(directory) / "state.json"),
                "TELEGRAM_API_URL": "",
            }):
                spec = importlib.util.spec_from_file_location("rate_limit_test_bot", ROOT / "bot.py")
                bot = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(bot)
            api = TelegramEmu()
            with patch.object(bot.urllib.request, "urlopen", side_effect=api), \
                    patch.object(bot, "load_whitelist", return_value={"1", "2"}):
                for _ in range(3):
                    bot.handle_message({"chat": {"id": 999}, "from": {"id": 999}, "text": "hi"})
                result = bot.send_plain(2, "hello")
            self.assertTrue(result.get("ok"))
            self.assertEqual(api.http_calls.count(("sendMessage", 999)), 2)
            self.assertIn(("sendMessage", 2), api.http_calls)
