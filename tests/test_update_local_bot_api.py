import importlib.util
import io
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_bot(**environment):
    directory = tempfile.TemporaryDirectory()
    values = {
        "TELEGRAM_BOT_TOKEN": "123456:test",
        "OWNER_ID": "1",
        "CODEX_BOT_STATE_FILE": str(Path(directory.name) / "state.json"),
        **environment,
    }
    with patch.dict(os.environ, values, clear=False):
        name = f"update_local_bot_api_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "bot.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, directory


class FinishedPopen:
    def __init__(self, output):
        self.stdout = io.StringIO(output)

    def wait(self):
        return 0


class UpdateLocalBotApiTests(unittest.TestCase):
    def setUp(self):
        self.bot, self.directory = load_bot()
        self.addCleanup(self.directory.cleanup)
        self.env_file = Path(self.directory.name) / ".env"
        self.env_file.write_text("TELEGRAM_BOT_TOKEN=123456:test\n", encoding="utf-8")
        self.bot.BOT_ENV_FILE = self.env_file
        self.bot.TELEGRAM_BOT_API_ENV_FILE = Path(self.directory.name) / "no-credentials-yet"
        self.messages = []

    def message(self, text):
        return {"chat": {"id": 1}, "from": {"id": 1}, "text": text}

    @staticmethod
    def update_result():
        return SimpleNamespace(returncode=0, stdout="Already up to date.\n", stderr="")

    @staticmethod
    def success_result():
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def send_plain(self, _chat_id, text):
        self.messages.append(text)

    def test_full_dialogue_switches_and_requests_safe_restart(self):
        popen = FinishedPopen(
            "STAGE:build\nSTAGE:done\n"
            "LOCAL_BOT_API_URL=http://127.0.0.1:8081\n"
        )

        def run(command, **_kwargs):
            if Path(command[0]).name == "update.sh":
                return self.update_result()
            if command[:2] == ["systemctl", "list-unit-files"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertEqual(Path(command[0]).name, "switch-to-local-bot-api.sh")
            self.assertEqual(command[1], "http://127.0.0.1:8081")
            return self.success_result()

        with patch.object(self.bot, "send_plain", side_effect=self.send_plain), \
                patch.object(self.bot, "tg_call", return_value={
                    "ok": True, "result": {"message_id": 77},
                }), \
                patch.object(self.bot, "_local_bot_api_dependencies_available", return_value=True), \
                patch.object(self.bot.subprocess, "run", side_effect=run) as run_mock, \
                patch.object(self.bot.subprocess, "Popen", return_value=popen), \
                patch.object(self.bot, "request_restart") as restart_mock:
            self.bot.handle_message(self.message("/update"))
            self.bot.handle_message(self.message("Да"))
            self.bot.handle_message(self.message("12345"))
            self.bot.handle_message(self.message("secret-hash"))
            for _ in range(100):
                if restart_mock.called:
                    break
                time.sleep(0.01)

        self.assertTrue(restart_mock.called)
        self.assertIn("TELEGRAM_API_URL=http://127.0.0.1:8081\n", self.env_file.read_text())
        self.assertIsNone(self.bot.get_tenant(1).update_flow_api_id)
        self.assertEqual(Path(run_mock.call_args_list[-1].args[0][0]).name, "switch-to-local-bot-api.sh")

    def test_no_clears_flow_and_still_restarts_to_apply_the_pulled_code(self):
        """Declining the local-server offer must not swallow the ordinary
        /update restart -- update.sh already pulled new code by this point,
        and today's /update always restarts to apply it."""
        with patch.object(self.bot, "send_plain", side_effect=self.send_plain), \
                patch.object(self.bot.subprocess, "run", return_value=self.update_result()), \
                patch.object(self.bot.subprocess, "Popen") as popen, \
                patch.object(self.bot, "request_restart") as restart_mock:
            self.bot.handle_message(self.message("/update"))
            self.bot.handle_message(self.message("Нет"))

        self.assertIsNone(self.bot.get_tenant(1).update_flow_stage)
        popen.assert_not_called()
        restart_mock.assert_called_once()

    def test_invalid_api_id_repeats_question_and_keeps_stage(self):
        with patch.object(self.bot, "send_plain", side_effect=self.send_plain), \
                patch.object(self.bot.subprocess, "run", return_value=self.update_result()):
            self.bot.handle_message(self.message("/update"))
            self.bot.handle_message(self.message("ага"))
            self.bot.handle_message(self.message("not-a-number"))

        self.assertEqual(self.bot.get_tenant(1).update_flow_stage, "awaiting_api_id")
        self.assertEqual(self.messages[-1], "Пришли api_id")

    def test_repeated_update_while_existing_credentials_install_is_running(self):
        credentials = Path(self.directory.name) / "telegram-api-env"
        credentials.write_text("TELEGRAM_API_ID=123\n", encoding="utf-8")
        self.bot.TELEGRAM_BOT_API_ENV_FILE = credentials
        entered = threading.Event()
        release = threading.Event()

        class BlockingPopen:
            def __init__(self, *_args, **_kwargs):
                pass

            @property
            def stdout(self):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("installer worker was not released")
                return io.StringIO("LOCAL_BOT_API_URL=http://127.0.0.1:8081\n")

            def wait(self):
                return 0

        with patch.object(self.bot, "send_plain", side_effect=self.send_plain), \
                patch.object(self.bot, "tg_call", return_value={"ok": True, "result": {"message_id": 1}}), \
                patch.object(self.bot, "_local_bot_api_dependencies_available", return_value=True), \
                patch.object(self.bot.subprocess, "run", side_effect=lambda command, **_: self.update_result()
                             if Path(command[0]).name == "update.sh" else self.success_result()), \
                patch.object(self.bot.subprocess, "Popen", side_effect=BlockingPopen) as popen, \
                patch.object(self.bot, "request_restart") as restart_mock:
            self.bot.handle_message(self.message("/update"))
            # Existing credentials skip straight past id/hash, but the
            # install itself must still wait for an actual "да" -- see the
            # regression this guards (install used to start unconditionally
            # as soon as credentials existed, before the owner answered
            # anything at all).
            self.bot.handle_message(self.message("Да"))
            self.assertTrue(entered.wait(1))
            self.bot.handle_message(self.message("/update"))
            release.set()
            for _ in range(100):
                if restart_mock.called:
                    break
                time.sleep(0.01)

        self.assertEqual(popen.call_count, 1)
        self.assertIn("Установка уже идёт, дождись.", self.messages)

    def test_existing_credentials_skip_id_and_hash_questions(self):
        credentials = Path(self.directory.name) / "telegram-api-env"
        credentials.write_text("TELEGRAM_API_ID=123\n", encoding="utf-8")
        self.bot.TELEGRAM_BOT_API_ENV_FILE = credentials
        popen = FinishedPopen("LOCAL_BOT_API_URL=http://127.0.0.1:8081\n")

        with patch.object(self.bot, "send_plain", side_effect=self.send_plain), \
                patch.object(self.bot, "tg_call", return_value={"ok": True, "result": {"message_id": 3}}), \
                patch.object(self.bot, "_local_bot_api_dependencies_available", return_value=True), \
                patch.object(self.bot.subprocess, "run", side_effect=lambda command, **_: self.update_result()
                             if Path(command[0]).name == "update.sh" else self.success_result()), \
                patch.object(self.bot.subprocess, "Popen", return_value=popen) as popen_mock, \
                patch.object(self.bot, "request_restart") as restart_mock:
            self.bot.handle_message(self.message("/update"))
            # The install itself only ever starts once the owner actually
            # confirms -- existing credentials only ever skip the id/hash
            # sub-questions, never the yes/no gate itself.
            popen_mock.assert_not_called()
            self.bot.handle_message(self.message("Да"))
            for _ in range(100):
                if restart_mock.called:
                    break
                time.sleep(0.01)

        self.assertTrue(restart_mock.called)
        self.assertEqual(popen_mock.call_count, 1)
        self.assertNotIn("Пришли api_id", self.messages)
        self.assertNotIn("Пришли api_hash", self.messages)

    def test_configured_inactive_existing_unit_is_restarted_without_installer(self):
        self.env_file.write_text("TELEGRAM_API_URL=http://127.0.0.1:8081\n", encoding="utf-8")
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            name = command[0]
            if Path(name).name == "update.sh":
                return self.update_result()
            if command[:3] == ["systemctl", "is-active", "--quiet"]:
                return SimpleNamespace(returncode=1 if len([c for c in calls if c[:3] == command[:3]]) == 1 else 0,
                                       stdout="", stderr="")
            if command[:2] == ["systemctl", "list-unit-files"]:
                return SimpleNamespace(returncode=0, stdout="telegram-bot-api.service enabled\n", stderr="")
            if command[:3] == ["sudo", "-n", "systemctl"]:
                return self.success_result()
            raise AssertionError(command)

        with patch.object(self.bot, "send_plain", side_effect=self.send_plain), \
                patch.object(self.bot.subprocess, "run", side_effect=run), \
                patch.object(self.bot, "_local_bot_api_port_ready", return_value=True), \
                patch.object(self.bot.time, "sleep"), \
                patch.object(self.bot.subprocess, "Popen") as popen, \
                patch.object(self.bot, "request_restart") as restart_mock:
            self.bot.handle_message(self.message("/update"))

        self.assertTrue(any(command[:3] == ["sudo", "-n", "systemctl"] for command in calls))
        popen.assert_not_called()
        restart_mock.assert_called_once_with(1)

    def test_configured_missing_unit_reports_manual_install(self):
        self.env_file.write_text("TELEGRAM_API_URL=http://127.0.0.1:8081\n", encoding="utf-8")

        def run(command, **_kwargs):
            if Path(command[0]).name == "update.sh":
                return self.update_result()
            if command[:3] == ["systemctl", "is-active", "--quiet"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if command[:2] == ["systemctl", "list-unit-files"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(command)

        with patch.object(self.bot, "send_plain", side_effect=self.send_plain), \
                patch.object(self.bot.subprocess, "run", side_effect=run), \
                patch.object(self.bot.subprocess, "Popen") as popen:
            self.bot.handle_message(self.message("/update"))

        popen.assert_not_called()
        self.assertTrue(any("прогони scripts/install-local-bot-api.sh" in text for text in self.messages))

    def test_failed_install_still_restarts_to_apply_the_pulled_code(self):
        """.env is untouched on a failed switch (still cloud mode, as
        before) -- but update.sh already pulled new code, same as any other
        outcome of this dialogue, so that update must still be applied."""
        popen = FinishedPopen("LOCAL_BOT_API_URL=http://127.0.0.1:8081\n")

        def run(command, **_kwargs):
            if Path(command[0]).name == "update.sh":
                return self.update_result()
            if command[:2] == ["systemctl", "list-unit-files"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertEqual(Path(command[0]).name, "switch-to-local-bot-api.sh")
            return SimpleNamespace(returncode=1, stdout="", stderr="logOut rejected")

        with patch.object(self.bot, "send_plain", side_effect=self.send_plain), \
                patch.object(self.bot, "tg_call", return_value={
                    "ok": True, "result": {"message_id": 9},
                }), \
                patch.object(self.bot, "_local_bot_api_dependencies_available", return_value=True), \
                patch.object(self.bot.subprocess, "run", side_effect=run), \
                patch.object(self.bot.subprocess, "Popen", return_value=popen), \
                patch.object(self.bot, "request_restart") as restart_mock:
            self.bot.handle_message(self.message("/update"))
            self.bot.handle_message(self.message("Да"))
            self.bot.handle_message(self.message("12345"))
            self.bot.handle_message(self.message("secret-hash"))
            for _ in range(100):
                if restart_mock.called:
                    break
                time.sleep(0.01)

        self.assertTrue(restart_mock.called)
        self.assertNotIn("TELEGRAM_API_URL", self.env_file.read_text())
        self.assertIsNone(self.bot.get_tenant(1).update_flow_stage)

    def test_sudo_restart_permission_error_is_human_readable(self):
        self.env_file.write_text("TELEGRAM_API_URL=http://127.0.0.1:8081\n", encoding="utf-8")

        def run(command, **_kwargs):
            if Path(command[0]).name == "update.sh":
                return self.update_result()
            if command[:3] == ["systemctl", "is-active", "--quiet"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if command[:2] == ["systemctl", "list-unit-files"]:
                return SimpleNamespace(returncode=0, stdout="telegram-bot-api.service enabled\n", stderr="")
            if command[:3] == ["sudo", "-n", "systemctl"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="sudo: a password is required")
            raise AssertionError(command)

        with patch.object(self.bot, "send_plain", side_effect=self.send_plain), \
                patch.object(self.bot.subprocess, "run", side_effect=run):
            self.bot.handle_message(self.message("/update"))

        self.assertTrue(any("sudoers-грант" in text for text in self.messages))
