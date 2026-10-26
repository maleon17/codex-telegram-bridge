import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TenantFileModesTests(unittest.TestCase):
    def test_last_turn_and_tenant_home_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {
                "TELEGRAM_BOT_TOKEN": "123456:test", "OWNER_ID": "1",
                "CODEX_BOT_STATE_FILE": str(root / "state.json"),
                "CODEX_BOT_ACCOUNTS_DIR": str(root / "accounts"),
                "TELEGRAM_API_URL": "",
            }):
                spec = importlib.util.spec_from_file_location("file_modes_test_bot", ROOT / "bot.py")
                bot = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(bot)
            old_umask = os.umask(0o022)
            try:
                bot.write_last_turn(4242, "secret")
                signal = root / "last_turn_4242.json"
                self.assertEqual(stat.S_IMODE(signal.stat().st_mode) & 0o044, 0)
                with patch.object(bot, "_ensure_tenant_mcp_config"):
                    bot.tenant_file_outbox(5151)
                    bot.tenant_codex_home(5151)
                tenant = root / "accounts" / "5151"
                self.assertEqual(stat.S_IMODE(tenant.stat().st_mode) & 0o077, 0)
            finally:
                os.umask(old_umask)
