"""A late live-progress edit must never overwrite the delivered answer."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class LiveCardRaceTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "123456:test", "OWNER_ID": "1",
            "CODEX_BOT_STATE_FILE": str(root / "state.json"),
            "CODEX_BOT_ACCOUNTS_DIR": str(root / "accounts"),
            "TELEGRAM_API_URL": "",
        }):
            spec = importlib.util.spec_from_file_location("live_race_test_bot", ROOT / "bot.py")
            self.bot = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.bot)

    def tearDown(self):
        self._directory.cleanup()

    def test_flush_after_delivery_leaves_the_final_answer_alone(self):
        calls = []

        def tg_call(method, params):
            calls.append((method, dict(params)))
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 77}}
            return {"ok": True, "result": {}}

        with patch.object(self.bot, "tg_call", side_effect=tg_call), \
                patch.object(self.bot, "write_last_turn"):
            view = self.bot.TurnView(1)
            view.current_thought = "4"
            view.flush(force=True)                      # the live "🤔 4" card
            self.assertEqual(view.progress_msg_id, 77)
            view.items.append({"type": "agent_message", "text": "4"})
            view.usage = {"input_tokens": 10, "output_tokens": 1}
            view.deliver()                              # final answer replaces the card
            delivered = len(calls)
            view.flush(force=True)                      # a late event's forced flush
            view.flush()

        self.assertEqual(len(calls), delivered, "a live edit ran after delivery")
        final_method, final_params = calls[-1]
        self.assertEqual(final_method, "editMessageText")
        self.assertEqual(final_params["message_id"], 77)
        self.assertIn("rich_message", final_params)
        self.assertNotIn("🤔", final_params["rich_message"]["markdown"])


if __name__ == "__main__":
    unittest.main()
