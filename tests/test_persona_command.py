"""Regression tests for the owner home migration and /persona command."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory()
sys.path.insert(0, str(ROOT))
_ENV = {
    "HOME": TEMP.name,
    "TELEGRAM_BOT_TOKEN": "123456:test",
    "OWNER_ID": "1000000001",
    "CODEX_HOME": str(Path(TEMP.name) / ".codex"),
    "CODEX_BOT_STATE_FILE": str(Path(TEMP.name) / "state.json"),
    "CODEX_BOT_ACCOUNTS_DIR": str(Path(TEMP.name) / "accounts"),
}
_OLD_ENV = {name: os.environ.get(name) for name in _ENV}
os.environ.update(_ENV)
spec = importlib.util.spec_from_file_location("persona_codex_bot", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
for _name, _value in _OLD_ENV.items():
    if _value is None:
        os.environ.pop(_name, None)
    else:
        os.environ[_name] = _value


class PersonaCommandTests(unittest.TestCase):
    def setUp(self):
        bot.tenants.clear()
        bot.persona_message_ids.clear() if hasattr(bot, "persona_message_ids") else None
        self.home = Path(TEMP.name)
        self.source_home = self.home / ".codex"
        self.source_home.mkdir(exist_ok=True)
        (self.source_home / "AGENTS.md").write_text("OWNER AGENT MARKER\n", encoding="utf-8")
        (self.source_home / "config.toml").write_text(
            "[mcp_servers.owner_marker]\ncommand = \"marker\"\n", encoding="utf-8"
        )
        (self.source_home / "auth.json").write_text("auth", encoding="utf-8")
        (self.source_home / "sessions" / "2026" / "09" / "25").mkdir(parents=True)
        (self.source_home / "sessions" / "2026" / "09" / "25" / "legacy.jsonl").write_text(
            "legacy rollout\n", encoding="utf-8"
        )

    def _seed_mcp(self, tenant_dir, chat_id):
        config = tenant_dir / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\n[mcp_servers.delegate-to-claude]\ncommand = \"delegate\"\n"
            + "\n[mcp_servers.send-telegram-file]\ncommand = \"send\"\n",
            encoding="utf-8",
        )

    def test_owner_migration_and_persona_replies(self):
        self.assertTrue(hasattr(bot, "handle_persona_reply"), "/persona reply handler is missing")
        with patch.dict(os.environ, {"HOME": TEMP.name, "CODEX_HOME": str(self.source_home)}, clear=False), \
                patch.object(bot, "_ensure_tenant_mcp_config", side_effect=self._seed_mcp):
            owner_dir = bot.tenant_codex_home(bot.OWNER_ID)
            self.assertIsNotNone(owner_dir)
            persona = owner_dir / "AGENTS.md"
            self.assertIn("OWNER AGENT MARKER", persona.read_text(encoding="utf-8"))
            config = (owner_dir / "config.toml").read_text(encoding="utf-8")
            self.assertIn("owner_marker", config)
            self.assertIn("delegate-to-claude", config)
            self.assertIn("send-telegram-file", config)
            self.assertTrue((owner_dir / "auth.json").is_symlink())
            migrated = owner_dir / "sessions" / "2026" / "09" / "25" / "legacy.jsonl"
            self.assertEqual(migrated.read_text(encoding="utf-8"), "legacy rollout\n")
            (self.source_home / "sessions" / "2026" / "09" / "25" / "later.jsonl").write_text(
                "must not overwrite the tenant copy\n", encoding="utf-8"
            )
            bot.tenant_codex_home(bot.OWNER_ID)
            self.assertFalse(
                (owner_dir / "sessions" / "2026" / "09" / "25" / "later.jsonl").exists()
            )
            self.assertEqual((owner_dir / "auth.json").resolve(), self.source_home / "auth.json")

        sent, documents = [], []
        with patch.object(bot, "send_plain", side_effect=lambda chat, text: sent.append((chat, text)) or {
            "ok": True, "result": {"message_id": 10 + len(sent)}
        }), patch.object(bot, "send_document", side_effect=lambda chat, path, caption="": documents.append(
            (chat, Path(path).read_text(encoding="utf-8"), caption)
        ) or {"ok": True, "result": {"message_id": 42}}):
            persona.write_text("small current persona", encoding="utf-8")
            self.assertTrue(bot.handle_command(bot.OWNER_ID, "/persona"))
            self.assertTrue(sent[-1][1].startswith("small current persona"))
            self.assertTrue(bot.handle_persona_reply({
                "chat": {"id": bot.OWNER_ID}, "text": "replacement text",
                "reply_to_message": {"message_id": 11},
            }))
            self.assertEqual(persona.read_text(encoding="utf-8"), "replacement text")

            persona.write_text("x" * (bot.MAX_MESSAGE_LEN + 1), encoding="utf-8")
            self.assertTrue(bot.handle_command(bot.OWNER_ID, "/persona"))
            self.assertTrue(documents[-1][1].startswith("x" * (bot.MAX_MESSAGE_LEN + 1)))
            self.assertEqual(documents[-1][2], "Текущая персона")

            uploaded = self.home / "uploaded.md"
            uploaded.write_text("file replacement", encoding="utf-8")
            with patch.object(bot, "download_telegram_file", return_value=str(uploaded)):
                self.assertTrue(bot.handle_persona_reply({
                    "chat": {"id": bot.OWNER_ID},
                    "document": {"file_id": "file", "file_name": "persona.md"},
                    "reply_to_message": {"message_id": 42},
                }))
            self.assertEqual(persona.read_text(encoding="utf-8"), "file replacement")

            before = persona.read_text(encoding="utf-8")
            self.assertFalse(bot.handle_persona_reply({
                "chat": {"id": bot.OWNER_ID}, "text": "ordinary reply",
                "reply_to_message": {"message_id": 999},
            }))
            self.assertEqual(persona.read_text(encoding="utf-8"), before)
            self.assertTrue(bot.handle_persona_reply({
                "chat": {"id": bot.OWNER_ID}, "text": " ",
                "reply_to_message": {"message_id": 42},
            }))
            self.assertEqual(persona.read_text(encoding="utf-8"), before)
            self.assertIn("пуст", sent[-1][1].lower())
            uploaded.write_text("", encoding="utf-8")
            with patch.object(bot, "download_telegram_file", return_value=str(uploaded)):
                self.assertTrue(bot.handle_persona_reply({
                    "chat": {"id": bot.OWNER_ID},
                    "document": {"file_id": "empty", "file_name": "persona.md"},
                    "reply_to_message": {"message_id": 42},
                }))
            self.assertEqual(persona.read_text(encoding="utf-8"), before)
            self.assertIn("пуст", sent[-1][1].lower())

            self.assertTrue(bot.handle_command(bot.OWNER_ID, "/persona reset"))
            self.assertEqual(
                persona.read_text(encoding="utf-8"),
                (ROOT / "personality.example.md").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
