"""Cross-delegation to the Claude bridge only exists when that bridge does."""
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

import delegate_to_claude_mcp as delegate  # noqa: E402


def _make_peer(parent, name):
    peer = Path(parent) / name
    peer.mkdir()
    for marker in delegate.PEER_MARKER_FILES:
        (peer / marker).touch()
    return peer


class PeerDiscoveryTests(unittest.TestCase):
    def test_absent_peer_is_reported_and_creates_nothing(self):
        before = set(os.listdir(ROOT.parent))
        with patch.object(delegate, "PEER_DIR_NAMES", ()), \
                patch.dict(os.environ, {"CHAT_ID": "4242"}):
            os.environ.pop("CLAUDE_TELEGRAM_BRIDGE_DIR", None)
            self.assertIsNone(delegate.claude_bridge_dir())
            ok, text = delegate._delegate_request("do something")
        self.assertFalse(ok)
        self.assertIn("не установлен", text)
        self.assertEqual(set(os.listdir(ROOT.parent)), before)

    def test_stray_directory_is_not_a_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CLAUDE_TELEGRAM_BRIDGE_DIR": directory}), \
                    patch.object(delegate, "PEER_DIR_NAMES", ()):
                self.assertIsNone(delegate.claude_bridge_dir())

    def test_both_clone_names_are_recognised(self):
        self.assertEqual(
            delegate.PEER_DIR_NAMES, ("claude-telegram-bridge", ".claude-telegram-bridge"),
        )

    def test_present_peer_round_trip_and_truthful_timeouts(self):
        with tempfile.TemporaryDirectory() as directory:
            peer = _make_peer(directory, "claude-telegram-bridge")
            queue, results = peer / "cross_delegate_queue", peer / "cross_delegate_result"

            def play_peer(confirm):
                deadline = time.time() + 5
                while time.time() < deadline:
                    names = sorted(queue.glob("*.json")) if queue.is_dir() else []
                    if names:
                        request = json.loads(names[0].read_text(encoding="utf-8"))
                        names[0].unlink()
                        if confirm:
                            self.assertEqual(request, {"chat_id": 4242, "text": "real task"})
                            (results / names[0].name).write_text(
                                json.dumps({"done": True, "ok": True, "text": "accepted"}),
                                encoding="utf-8",
                            )
                        return
                    time.sleep(0.05)

            env = {"CLAUDE_TELEGRAM_BRIDGE_DIR": str(peer), "CHAT_ID": "4242"}
            with patch.dict(os.environ, env):
                self.assertEqual(delegate.claude_bridge_dir(), peer)
                worker = threading.Thread(target=play_peer, args=(True,))
                worker.start()
                self.assertEqual(delegate._delegate_request("real task"), (True, "accepted"))
                worker.join()

                with patch.object(delegate, "POLL_TIMEOUT_S", 0.6):
                    ok, text = delegate._delegate_request("nobody home")
                    self.assertFalse(ok)
                    self.assertIn("НЕ начата", text)
                    self.assertEqual(list(queue.glob("*.json")), [])

                    worker = threading.Thread(target=play_peer, args=(False,))
                    worker.start()
                    ok, text = delegate._delegate_request("claimed")
                    worker.join()
                    self.assertFalse(ok)
                    self.assertIn("забрал запрос", text)
                    self.assertNotIn("НЕ начата", text)


class TenantSeedingTests(unittest.TestCase):
    def _load_bot(self, root):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "123456:test", "OWNER_ID": "1",
            "CODEX_BOT_STATE_FILE": str(root / "state.json"),
            "CODEX_BOT_ACCOUNTS_DIR": str(root / "accounts"),
            "TELEGRAM_API_URL": "",
        }):
            spec = importlib.util.spec_from_file_location("cross_delegate_test_bot", ROOT / "bot.py")
            bot = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bot)
        return bot

    def test_delegate_server_follows_the_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot = self._load_bot(root)
            tenant = root / "tenant"
            tenant.mkdir()
            seeded = []
            with patch.object(bot, "_ensure_tenant_mcp_server",
                              side_effect=lambda _dir, _chat, name, _file: seeded.append(name)), \
                    patch.object(bot.subprocess, "run") as run:
                with patch.object(bot, "claude_bridge_dir", return_value=None):
                    bot._ensure_tenant_mcp_config(tenant, 5)
                self.assertEqual(seeded, ["send-telegram-file"])
                run.assert_not_called()  # nothing of ours to withdraw

                # A server this bot seeded earlier is withdrawn once the peer is gone...
                script = str((ROOT / "delegate_to_claude_mcp.py").resolve())
                (tenant / "config.toml").write_text(
                    f'[mcp_servers.delegate-to-claude]\ncommand = "{sys.executable}"\nargs = ["{script}"]\n',
                    encoding="utf-8",
                )
                with patch.object(bot, "claude_bridge_dir", return_value=None):
                    bot._ensure_tenant_mcp_config(tenant, 5)
                self.assertEqual(run.call_args.args[0], ["codex", "mcp", "remove", "delegate-to-claude"])

                # ...but one the tenant repointed is left alone.
                run.reset_mock()
                (tenant / "config.toml").write_text(
                    '[mcp_servers.delegate-to-claude]\ncommand = "custom"\nargs = []\n',
                    encoding="utf-8",
                )
                with patch.object(bot, "claude_bridge_dir", return_value=None):
                    bot._ensure_tenant_mcp_config(tenant, 5)
                run.assert_not_called()

                seeded.clear()
                with patch.object(bot, "claude_bridge_dir", return_value=root):
                    bot._ensure_tenant_mcp_config(tenant, 5)
                self.assertEqual(seeded, ["delegate-to-claude", "send-telegram-file"])


if __name__ == "__main__":
    unittest.main()
