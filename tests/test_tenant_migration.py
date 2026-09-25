import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tenant_migration import apply_pending_tenant_migration


class TenantMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.accounts = self.root / "accounts"
        self.accounts.mkdir()
        self.workspaces = self.root / "workspaces"
        self.workspaces.mkdir()
        self.old_home = self.accounts / "200"
        self.new_home = self.accounts / "300"
        self.old_home.mkdir()
        self.old_workspace = self.workspaces / "200"
        self.new_workspace = self.workspaces / "300"
        self.old_workspace.mkdir()
        (self.old_workspace / "note.txt").write_text("workspace data", encoding="utf-8")
        (self.old_home / "auth.json").write_text("credentials", encoding="utf-8")
        (self.old_home / "config.toml").write_text(
            f'[projects."{self.old_workspace}"]\n', encoding="utf-8",
        )
        rollout = self.old_home / "sessions" / "rollout.jsonl"
        rollout.parent.mkdir()
        rollout.write_text("\n".join((
            json.dumps({"type": "session_meta", "payload": {
                "cwd": str(self.old_workspace),
                "thread_settings": {"cwd": str(self.old_workspace)},
                "workspace_roots": [str(self.old_workspace)],
                "state": {"environments": {"environments": {
                    "local": {"cwd": str(self.old_workspace)},
                }}},
            }}),
            json.dumps({"type": "response_item", "payload": {
                "text": f"Historical reference to {self.old_workspace}",
            }}),
        )) + "\n", encoding="utf-8")
        with sqlite3.connect(self.old_home / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?)",
                       ("session-1", str(rollout), str(self.old_workspace)))
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"version": 2, "chats": {
            "200": {"thread_id": "session-1", "workspace": str(self.old_workspace),
                    "pending_delivery": {"chat_id": 200, "text": "waiting"}},
        }, "runtime": {"restart_completed_chat_id": 200}}), encoding="utf-8")
        self.whitelist = self.root / "whitelist.txt"
        self.whitelist.write_text("1\n200\n", encoding="utf-8")
        (self.root / "last_turn_200.json").write_text('{"text": "hello"}', encoding="utf-8")
        self.request = self.root / ".tenant-id-migration.json"
        self.request.write_text('{"old_id": 200, "new_id": 300}', encoding="utf-8")

    def migrate(self):
        return apply_pending_tenant_migration(
            self.state, self.whitelist, self.accounts, 1, self.workspaces,
        )

    def test_moves_account_state_workspace_and_session_index_with_backup(self):
        self.assertTrue(self.migrate())
        self.assertFalse(self.old_home.exists())
        self.assertFalse(self.old_workspace.exists())
        self.assertFalse(self.request.exists())
        self.assertEqual((self.new_home / "auth.json").read_text(), "credentials")
        self.assertIn(str(self.new_workspace), (self.new_home / "config.toml").read_text())
        self.assertEqual((self.new_workspace / "note.txt").read_text(), "workspace data")
        with (self.new_home / "sessions" / "rollout.jsonl").open(encoding="utf-8") as handle:
            session = [json.loads(line) for line in handle]
        metadata = session[0]["payload"]
        self.assertEqual(metadata["cwd"], str(self.new_workspace))
        self.assertEqual(metadata["thread_settings"]["cwd"], str(self.new_workspace))
        self.assertEqual(metadata["workspace_roots"], [str(self.new_workspace)])
        self.assertEqual(metadata["state"]["environments"]["environments"]["local"]["cwd"],
                         str(self.new_workspace))
        self.assertIn(str(self.old_workspace), session[1]["payload"]["text"])
        with sqlite3.connect(self.new_home / "state_5.sqlite") as db:
            path, cwd = db.execute("SELECT rollout_path, cwd FROM threads").fetchone()
        self.assertEqual(path, str(self.new_home / "sessions" / "rollout.jsonl"))
        self.assertEqual(cwd, str(self.new_workspace))
        migrated_state = json.loads(self.state.read_text())
        chats = migrated_state["chats"]
        self.assertNotIn("200", chats)
        self.assertEqual(chats["300"]["workspace"], str(self.new_workspace))
        self.assertEqual(chats["300"]["pending_delivery"]["chat_id"], 300)
        self.assertEqual(migrated_state["runtime"]["restart_completed_chat_id"], 300)
        self.assertEqual(self.whitelist.read_text(), "1\n300\n")
        self.assertFalse((self.root / "last_turn_200.json").exists())
        self.assertTrue((self.root / "last_turn_300.json").exists())
        backup = self.root / ".tenant-id-migration-backups" / "200-to-300"
        self.assertTrue((backup / "account" / "auth.json").exists())
        self.assertTrue((backup / "state.json").exists())
        self.assertTrue((backup / "whitelist.txt").exists())
        self.assertTrue((backup / "workspace" / "note.txt").exists())

        # A process death just before removing the request can safely retry.
        self.request.write_text('{"old_id": 200, "new_id": 300}', encoding="utf-8")
        self.assertTrue(self.migrate())
        self.assertFalse(self.request.exists())
        self.assertFalse(self.migrate())

    def test_refuses_to_merge_an_existing_target(self):
        self.new_home.mkdir()
        with self.assertRaisesRegex(RuntimeError, "Both tenant account directories"):
            self.migrate()
        self.assertTrue(self.old_home.exists())
        self.assertTrue(self.request.exists())
        self.assertFalse((self.root / ".tenant-id-migration-backups").exists())


if __name__ == "__main__":
    unittest.main()
