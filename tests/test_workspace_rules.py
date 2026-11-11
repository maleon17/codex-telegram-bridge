"""Working-folder guidance reaches every non-owner persona exactly once."""
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class WorkspaceRulesTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "123456:test", "OWNER_ID": "1",
            "CODEX_BOT_STATE_FILE": str(self.root / "state.json"),
            "CODEX_BOT_ACCOUNTS_DIR": str(self.root / "accounts"),
            "CODEX_HOME": str(self.root / "owner-codex-home"),
            "TELEGRAM_API_URL": "",
        }):
            spec = importlib.util.spec_from_file_location("workspace_rules_test_bot", ROOT / "bot.py")
            self.bot = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.bot)
        self._mcp = patch.object(self.bot, "_ensure_tenant_mcp_config")
        self._mcp.start()

    def tearDown(self):
        self._mcp.stop()
        self._directory.cleanup()

    def _template(self, language):
        name = "personality.example.md" if language == "ru" else f"personality.example.{language}.md"
        return (ROOT / name).read_text(encoding="utf-8")

    def _old_tenant(self, chat_id, persona, default_marker):
        tenant = self.root / "accounts" / str(chat_id)
        tenant.mkdir(parents=True, mode=0o700)
        (tenant / "AGENTS.md").write_text(persona, encoding="utf-8")
        if default_marker:
            (tenant / ".persona_default_sha256").write_text(_sha(persona) + "\n", encoding="ascii")
        return tenant

    def test_every_stock_persona_carries_the_guidance(self):
        for language, rules in self.bot.WORKSPACE_RULES.items():
            self.assertEqual(self._template(language).count(rules), 1, language)

    def test_new_tenant_gets_it_from_the_stock_persona(self):
        tenant = self.bot.tenant_codex_home(5151)
        persona = (tenant / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(persona.count("## Рабочая папка"), 1)
        self.assertTrue((tenant / ".workspace_rules_seeded").exists())
        self.assertTrue(self.bot._persona_is_default(5151))

    def test_existing_default_persona_is_extended_once_and_stays_default(self):
        rules = self.bot.WORKSPACE_RULES["en"]
        old_default = self._template("en").replace("\n" + rules + "\n", "")
        self.assertNotIn("## Working folder", old_default)
        tenant = self._old_tenant(6161, old_default, default_marker=True)
        self.bot.tenant_codex_home(6161)
        persona = (tenant / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(persona.endswith(rules + "\n"))
        self.assertEqual(persona.count("## Working folder"), 1)
        self.assertTrue(self.bot._persona_is_default(6161))
        self.bot.tenant_codex_home(6161)
        self.assertEqual((tenant / "AGENTS.md").read_text(encoding="utf-8"), persona)

    def test_customised_persona_is_extended_but_removal_is_respected(self):
        tenant = self._old_tenant(7171, "My own rules.\n", default_marker=False)
        self.bot.tenant_codex_home(7171)
        persona = (tenant / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(persona.startswith("My own rules.\n"))
        self.assertIn(self.bot.WORKSPACE_RULES["ru"], persona)
        self.assertFalse(self.bot._persona_is_default(7171))
        (tenant / "AGENTS.md").write_text("My own rules.\n", encoding="utf-8")
        self.bot.tenant_codex_home(7171)
        self.assertNotIn("## Рабочая папка", (tenant / "AGENTS.md").read_text(encoding="utf-8"))

    def test_owner_persona_is_never_touched(self):
        owner_home = self.root / "owner-codex-home"
        owner_home.mkdir()
        (owner_home / "AGENTS.md").write_text("OWNER PERSONA\n", encoding="utf-8")
        (owner_home / "auth.json").write_text("{}", encoding="utf-8")
        tenant = self.bot.tenant_codex_home(1)
        persona = (tenant / "AGENTS.md").read_text(encoding="utf-8")
        for rules in self.bot.WORKSPACE_RULES.values():
            self.assertNotIn(rules.splitlines()[0], persona)


if __name__ == "__main__":
    unittest.main()
