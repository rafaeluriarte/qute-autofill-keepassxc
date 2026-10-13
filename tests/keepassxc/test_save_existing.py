#!/usr/bin/env python3
"""Tests for saving credentials already typed in a page login form."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent.parent / "userscripts"
SCRIPT_PATH = SCRIPT_DIR / "keepassxc-save"
FIXTURES = TESTS_DIR / "fixtures"

import sys
sys.path.insert(0, str(TESTS_DIR))
from mock_keepassxc_server import MockKeepassXCServer  # noqa: E402

REAL_SOCKET = "/run/user/{}/org.keepassxc.KeePassXC.BrowserServer".format(os.getuid())


def _load_module():
    loader = importlib.machinery.SourceFileLoader("keepassxc_save_existing", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


kps = _load_module()


class SaveExistingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="keepassxc-save-existing-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.socket_path = os.path.join(self.tmpdir, "mock.sock")
        self.assertNotEqual(self.socket_path, REAL_SOCKET)
        self.server = MockKeepassXCServer(self.socket_path)
        self.server.start()
        self.addCleanup(self.server.stop)

        self.fifo_path = os.path.join(self.tmpdir, "fifo")
        Path(self.fifo_path).touch()
        self.data_dir = os.path.join(self.tmpdir, "data")
        self.aliases_dir = os.path.join(self.tmpdir, "aliases")
        os.makedirs(self.data_dir)
        self.answers_path = os.path.join(self.tmpdir, "answers")
        Path(self.answers_path).touch()
        self._env_patch = None
        self._use_rofi("fake_rofi_echo_filter")

    def _use_rofi(self, fixture_name):
        if self._env_patch:
            self._env_patch.stop()
        self._env_patch = mock.patch.dict(os.environ, {
            "QUTE_FIFO": self.fifo_path,
            "QUTE_DATA_DIR": self.data_dir,
            "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
            "FAKE_ROFI_ANSWERS_FILE": self.answers_path,
            "PATH": str(FIXTURES / fixture_name) + os.pathsep + os.environ.get("PATH", ""),
        })
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _queue_answers(self, *answers):
        Path(self.answers_path).write_text("\x00".join(answers))

    def _seed(self, login, url="https://example.test/"):
        uuid = "existing-1"
        self.server.entries[uuid] = {
            "uuid": uuid,
            "login": login,
            "password": "fixture-old-value",
            "url": url,
            "name": "Existing account",
        }
        return uuid

    def _run(self, username="typed-user", password="typed-secret"):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            kps, "capture_credentials", return_value=({"username": username, "password": password}, None)
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = kps.main([
                "https://example.test/login", "--socket", self.socket_path, "--insecure",
            ])
        return rc, out.getvalue(), err.getvalue()

    def _fifo(self):
        return Path(self.fifo_path).read_text()


class SaveExistingFlowTests(SaveExistingTestCase):
    def test_new_site_entry_uses_origin_and_exact_submit_url(self):
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.saved_logins), 1)
        saved = self.server.saved_logins[0]
        self.assertEqual(saved["url"], "https://example.test/")
        self.assertEqual(saved["submitUrl"], "https://example.test/login")
        self.assertEqual(saved["login"], "typed-user")
        self.assertEqual(saved["password"], "typed-secret")
        self.assertFalse(saved["is_update"])

    def test_existing_site_and_same_username_updates_in_place(self):
        uuid = self._seed("typed-user")
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.entries), 1)
        self.assertEqual(self.server.saved_logins[-1]["entry_uuid"], uuid)
        self.assertTrue(self.server.saved_logins[-1]["is_update"])

    def test_different_username_requires_a_second_confirmation(self):
        self._seed("other-user")
        # echo-filter picks Save=Yes, then the differing-login prompt's
        # default No. Nothing may be overwritten.
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.server.saved_logins, [])
        self.assertIn("cancelled", self._fifo())

    def test_different_username_can_be_confirmed_and_updated(self):
        uuid = self._seed("other-user")
        self._use_rofi("fake_rofi_scripted")
        self._queue_answers("Yes", "Yes")
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.server.saved_logins[-1]["entry_uuid"], uuid)
        self.assertTrue(self.server.saved_logins[-1]["is_update"])

    def test_save_confirmation_no_is_a_clean_noop(self):
        self._use_rofi("fake_rofi_scripted")
        self._queue_answers("No")
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.server.saved_logins, [])

    def test_password_is_never_printed_or_put_in_fifo(self):
        rc, out, err = self._run(password="never-expose-this-value")
        self.assertEqual(rc, 0, err)
        combined = out + err + self._fifo()
        self.assertNotIn("never-expose-this-value", combined)

    def test_denied_save_reports_error_without_exposing_password(self):
        self.server.deny_set_login = "denied"
        rc, out, err = self._run(password="denied-test-value")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.server.saved_logins, [])
        combined = out + err + self._fifo()
        self.assertIn("save failed", combined)
        self.assertNotIn("denied-test-value", combined)


class CaptureJavascriptTests(unittest.TestCase):
    def test_capture_command_is_secret_free_and_targets_isolated_world(self):
        js = kps.build_capture_js("http://127.0.0.1:12345/unguessable-token")
        self.assertIn("fetch", js)
        self.assertIn("passwordField.value", js)
        self.assertNotIn("--world main", js)

    def test_origin_preserves_explicit_port(self):
        self.assertEqual(kps._origin_of("https://example.test:8443/login"),
                         "https://example.test:8443/")

    def test_confirmation_wording_and_yes_default(self):
        completed = subprocess.CompletedProcess(["rofi"], 0, stdout="Yes\n", stderr="")
        with mock.patch.object(kps.subprocess, "run", return_value=completed) as run:
            self.assertTrue(kps.prompt_yes_no("Save password for example.test?"))
        self.assertEqual(run.call_args.args[0][-1], "Save password for example.test?")
        self.assertEqual(run.call_args.kwargs["input"].splitlines(), ["Yes", "No"])


if __name__ == "__main__":
    unittest.main()
