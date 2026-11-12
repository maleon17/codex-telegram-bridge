"""Telegram actions are offered only to the owner of a userbot instance."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import telegram_actions_link as link  # noqa: E402

FAKE_CLI = '''
import json, sys
owners = json.load(open({owners!r}))
if sys.argv[1] == "--owned-by":
    print("\\n".join(sorted(i for i, o in owners.items() if str(o) == sys.argv[2])))
elif sys.argv[1] == "--add-bridge-token":
    assert sys.argv[2] in owners
    print("bridge-token-for-" + sys.argv[2])
else:
    sys.exit(2)
'''


@unittest.skipIf(tomllib is None, "needs tomllib to verify the written config")
class TelegramActionsLinkTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.core = self.root / "telegram-ai-core"
        self.core.mkdir()
        (self.core / "telegram_actions_mcp.py").touch()
        self.owners = self.root / "owners.json"
        self._set_owners({"alice-bot": 111, "bob-bot": 222})
        (self.core / "cmd_queue.py").write_text(FAKE_CLI.format(owners=str(self.owners)))
        config = self.root / "config"
        config.mkdir()
        (config / "registry.json").write_text(json.dumps({"version": 1, "components": {
            "core": {"path": str(self.core), "python": sys.executable},
            "relay": {"path": str(self.core), "url": "http://127.0.0.1:9092", "config_dir": str(config)},
        }}))
        self._env = patch.dict(os.environ, {
            "TELEGRAM_AI_CONFIG_DIR": str(config),
            "TELEGRAM_BOT_TOKEN": "123456:test", "OWNER_ID": "1",
            "CODEX_BOT_STATE_FILE": str(self.root / "state.json"),
            "CODEX_BOT_ACCOUNTS_DIR": str(self.root / "accounts"),
            "TELEGRAM_API_URL": "",
        })
        self._env.start()
        spec = importlib.util.spec_from_file_location("tg_actions_test_bot", ROOT / "bot.py")
        self.bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bot)

    def tearDown(self):
        self._env.stop()
        self._directory.cleanup()

    def _set_owners(self, owners):
        self.owners.write_text(json.dumps(owners))

    def _tenant(self, name, config=""):
        tenant = self.root / name
        tenant.mkdir()
        if config:
            (tenant / "config.toml").write_text(config)
        return tenant

    def _servers(self, tenant):
        with (tenant / "config.toml").open("rb") as handle:
            return tomllib.load(handle).get("mcp_servers", {})

    def test_owner_gets_their_own_instance_and_nobody_elses(self):
        alice = self._tenant("alice", '[mcp_servers.other]\ncommand = "keep"\n')
        self.bot._sync_telegram_actions_server(alice, 111)
        servers = self._servers(alice)
        server = servers[link.SERVER_NAME]
        self.assertEqual(server["command"], sys.executable)
        self.assertEqual(server["args"], [str(self.core / "telegram_actions_mcp.py")])
        self.assertEqual(server["env"], {
            "JARVIS_RELAY_URL": "http://127.0.0.1:9092",
            "JARVIS_RELAY_TOKEN": "bridge-token-for-alice-bot",
            "INSTANCE_ID": "alice-bot", "JARVIS_RELAY_DIRECT": "1",
        })
        self.assertEqual(servers["other"], {"command": "keep"})
        self.assertEqual(oct((alice / "config.toml").stat().st_mode & 0o777), "0o600")

        carol = self._tenant("carol")
        self.bot._sync_telegram_actions_server(carol, 333)
        self.assertFalse((carol / "config.toml").exists())

    def test_idempotent_and_withdrawn_when_ownership_ends(self):
        alice = self._tenant("alice")
        self.bot._sync_telegram_actions_server(alice, 111)
        first = (alice / "config.toml").read_text()
        self.bot._telegram_actions_checked.clear()
        self.bot._sync_telegram_actions_server(alice, 111)
        self.assertEqual((alice / "config.toml").read_text(), first)

        self._set_owners({"alice-bot": 999})
        self.bot._telegram_actions_checked.clear()
        self.bot._sync_telegram_actions_server(alice, 111)
        self.assertNotIn(link.SERVER_NAME, self._servers(alice))

    def test_a_server_the_tenant_configured_is_left_alone(self):
        mine = f'[mcp_servers.{link.SERVER_NAME}]\ncommand = "mine"\n'
        alice = self._tenant("alice", mine)
        self.bot._sync_telegram_actions_server(alice, 111)
        self.assertEqual((alice / "config.toml").read_text(), mine)

    def test_no_core_on_the_host_changes_nothing(self):
        with patch.dict(os.environ, {"TELEGRAM_AI_CONFIG_DIR": str(self.root / "nowhere")}):
            alice = self._tenant("alice", '[mcp_servers.other]\ncommand = "keep"\n')
            self.bot._sync_telegram_actions_server(alice, 111)
            self.assertEqual(list(self._servers(alice)), ["other"])


if __name__ == "__main__":
    unittest.main()
