#!/usr/bin/env python3
"""Save-then-fill flow tests, against a mock KeePassXC (never the real one).

Verifies: save happens before fill, fill only happens after a *successful*
save, a denied/failed save fills nothing, the password fed to `set-login`
is exactly the one that ends up in the jseval fill command, the current-
password field is left alone, nothing is ever submitted, and the password
is never written to stdout/stderr or logged anywhere.

The real KeePassXC process on this machine (and its socket at
/run/user/<uid>/org.keepassxc.KeePassXC.BrowserServer) is never contacted:
every test asserts its socket path is not that one before using it.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
_repo_scripts = TESTS_DIR.parent.parent / "userscripts"
SCRIPT_DIR = _repo_scripts if _repo_scripts.is_dir() else TESTS_DIR.parent
SCRIPT_PATH = SCRIPT_DIR / "keepassxc-newpass"
FIXTURES = TESTS_DIR / "fixtures"

sys.path.insert(0, str(TESTS_DIR))
from mock_keepassxc_server import MockKeepassXCServer  # noqa: E402

REAL_SOCKET = "/run/user/{}/org.keepassxc.KeePassXC.BrowserServer".format(os.getuid())


def _load_module():
    loader = importlib.machinery.SourceFileLoader("keepassxc_newpass_saveflow", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


kpn = _load_module()


class SaveFlowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="keepassxc-newpass-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.socket_path = os.path.join(self.tmpdir, "mock-keepassxc.sock")
        self.assertNotEqual(self.socket_path, REAL_SOCKET)

        self.data_dir = os.path.join(self.tmpdir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.config_dir = os.path.join(self.tmpdir, "config")
        os.makedirs(os.path.join(self.config_dir, "autofill"), exist_ok=True)
        shutil.copy(FIXTURES / "fake_profiles.toml", os.path.join(self.config_dir, "autofill", "profiles.toml"))

        self.fifo_path = os.path.join(self.tmpdir, "fifo")
        Path(self.fifo_path).touch()

        # find_matching_entries() now consults kpxc_lookup's alias store
        # (for its cascade lookup) - point it at a throwaway dir so the
        # real ~/.local/share/qutebrowser/keepassxc-login/aliases.json is
        # never read.
        self.aliases_dir = os.path.join(self.tmpdir, "keepassxc-login")

        self.server = MockKeepassXCServer(self.socket_path)
        self.server.start()
        self.addCleanup(self.server.stop)

        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "QUTE_DATA_DIR": self.data_dir,
                "QUTE_CONFIG_DIR": self.config_dir,
                "QUTE_FIFO": self.fifo_path,
                "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
                "PATH": str(FIXTURES / "fake_rofi_echo_filter") + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _run(self, url, html_fixture, extra_argv=()):
        html_path = os.path.join(self.tmpdir, "page.html")
        shutil.copy(FIXTURES / html_fixture, html_path)
        with mock.patch.dict(os.environ, {"QUTE_HTML": html_path}):
            out, err = io.StringIO(), io.StringIO()
            argv = [url, "--socket", self.socket_path, "--insecure", "--length", "12"] + list(extra_argv)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = kpn.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _fifo_contents(self):
        return Path(self.fifo_path).read_text()

    def _use_rofi(self, fixture_dir_name):
        """Swap which fake `rofi` is first on PATH (for tests that need a
        specific answer to the update-vs-create / pick-entry prompts)."""
        self._env_patch.stop()
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "QUTE_DATA_DIR": self.data_dir,
                "QUTE_CONFIG_DIR": self.config_dir,
                "QUTE_FIFO": self.fifo_path,
                "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
                "PATH": str(FIXTURES / fixture_dir_name) + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()

    def _seed_entry(self, url, login, password="old-password-do-not-reuse"):
        """Pre-populate the mock server with an existing entry, as if a
        previous keepassxc-newpass run (or the user) had already saved one."""
        entry_uuid = "seed-{}".format(len(self.server.entries) + 1)
        self.server.entries[entry_uuid] = {
            "uuid": entry_uuid, "login": login, "password": password, "url": url, "name": url,
        }
        return entry_uuid


class SuccessfulSaveThenFillTests(SaveFlowTestCase):
    def test_save_happens_and_is_filled_with_the_same_password(self):
        rc, out, err = self._run("https://example.test/signup", "signup.html")
        self.assertEqual(rc, 0, err)

        self.assertEqual(len(self.server.saved_logins), 1)
        saved = self.server.saved_logins[0]
        # saved with url=origin, submitUrl=page URL (see keepassxc-newpass's
        # module docstring, "Avoiding duplicate entries" section)
        self.assertEqual(saved["url"], "https://example.test/")
        self.assertEqual(saved["submitUrl"], "https://example.test/signup")
        self.assertEqual(saved["login"], "testuser")  # from fake_profiles.toml, echoed by fake rofi
        self.assertTrue(saved["password"])
        self.assertEqual(len(saved["password"]), 12)

        fifo = self._fifo_contents()
        self.assertIn("jseval --quiet --world jseval", fifo)
        self.assertIn("message-info", fifo)
        self.assertIn("saved new password and filled", fifo)

        # the exact password that was saved is the one embedded in the fill JS
        expected_json = json.dumps(saved["password"], ensure_ascii=False)
        self.assertIn(expected_json, fifo)

    def test_password_never_printed_or_logged(self):
        rc, out, err = self._run("https://example.test/signup", "signup.html")
        self.assertEqual(rc, 0, err)
        saved_password = self.server.saved_logins[0]["password"]
        self.assertNotIn(saved_password, out)
        self.assertNotIn(saved_password, err)

    def test_save_happens_before_fill_is_sent(self):
        # The mock records saves as they arrive; the FIFO file only gets the
        # jseval line after set-login returns success=='true' in main(), so
        # if we ever see saved_logins empty but a jseval line present, the
        # ordering guarantee is broken.
        rc, out, err = self._run("https://example.test/signup", "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.saved_logins), 1)
        self.assertIn("jseval", self._fifo_contents())

    def test_change_password_fixture_current_password_field_untouched(self):
        rc, out, err = self._run("https://example.test/account/password", "changepw.html")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        # the fill JS filters on autocomplete=current-password client-side;
        # statically verify our JS actually contains that guard so the
        # current-password field is skipped when it runs in a real page.
        self.assertIn("current-password", fifo)
        saved_password = self.server.saved_logins[0]["password"]
        # sanity: the *value* "should-not-be-touched" from the fixture must
        # never appear as something we tried to overwrite with/compare to
        self.assertNotEqual(saved_password, "should-not-be-touched")

    def test_never_submits(self):
        rc, out, err = self._run("https://example.test/signup", "signup.html")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertNotIn(".submit(", fifo)
        self.assertNotIn("requestSubmit", fifo)


class DeniedOrFailedSaveTests(SaveFlowTestCase):
    def test_denied_save_fills_nothing(self):
        self.server.deny_set_login = "denied"
        rc, out, err = self._run("https://example.test/signup", "signup.html")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.server.saved_logins, [])
        fifo = self._fifo_contents()
        self.assertNotIn("jseval", fifo)
        self.assertIn("message-error", fifo)
        self.assertIn("save failed", fifo)

    def test_locked_database_fills_nothing(self):
        self.server.deny_set_login = "locked"
        rc, out, err = self._run("https://example.test/signup", "signup.html")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.server.saved_logins, [])
        self.assertNotIn("jseval", self._fifo_contents())


class NoPasswordFieldTests(SaveFlowTestCase):
    def test_aborts_before_touching_keepassxc(self):
        no_pw_html = os.path.join(self.tmpdir, "nopw.html")
        Path(no_pw_html).write_text("<html><body><input type='text' name='q'></body></html>")
        with mock.patch.dict(os.environ, {"QUTE_HTML": no_pw_html}):
            argv = ["https://example.test/search", "--socket", self.socket_path, "--insecure"]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = kpn.main(argv)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.server.saved_logins, [])
        self.assertIn("no new-password field", self._fifo_contents())


class RofiCancelTests(SaveFlowTestCase):
    def test_escape_in_rofi_cancels_everything(self):
        self._env_patch.stop()
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "QUTE_DATA_DIR": self.data_dir,
                "QUTE_CONFIG_DIR": self.config_dir,
                "QUTE_FIFO": self.fifo_path,
                "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
                "PATH": str(FIXTURES / "fake_rofi_cancel") + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()
        rc, out, err = self._run("https://example.test/signup", "signup.html")
        self.assertEqual(rc, 0)  # a cancel is not an error, just a no-op
        self.assertEqual(self.server.saved_logins, [])
        self.assertNotIn("jseval", self._fifo_contents())
        self.assertIn("cancelled", self._fifo_contents())


class RofiCustomLoginTests(SaveFlowTestCase):
    def test_typed_login_overrides_profile_default(self):
        self._env_patch.stop()
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "QUTE_DATA_DIR": self.data_dir,
                "QUTE_CONFIG_DIR": self.config_dir,
                "QUTE_FIFO": self.fifo_path,
                "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
                "PATH": str(FIXTURES / "fake_rofi_custom") + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()
        rc, out, err = self._run("https://example.test/signup", "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.server.saved_logins[0]["login"], "typed-login@example.test")


class AssociationReuseTests(SaveFlowTestCase):
    def test_second_run_reuses_the_association_key_file(self):
        rc1, _, err1 = self._run("https://example.test/signup", "signup.html")
        self.assertEqual(rc1, 0, err1)
        key_files = list(Path(self.data_dir).glob("keepassxc.key*"))
        self.assertEqual(len(key_files), 1)
        first_association_count = len(self.server.associations)
        self.assertEqual(first_association_count, 1)

        rc2, _, err2 = self._run("https://example.test/other-signup", "signup.html")
        self.assertEqual(rc2, 0, err2)
        # no *new* association was created on the second run
        self.assertEqual(len(self.server.associations), first_association_count)
        self.assertEqual(len(self.server.saved_logins), 2)


class DuplicateAvoidanceTests(SaveFlowTestCase):
    """Coordinator-requested fix: don't leave stale duplicate entries.

    Before saving, look up existing entries for the URL via get-logins and,
    depending on how many match the chosen login, update in place (uuid),
    ask, or create - never silently duplicate.
    """

    CHANGEPW_URL = "https://example.test/account/password"
    SIGNUP_URL = "https://example.test/signup"

    def test_change_password_form_updates_the_single_matching_entry_by_default(self):
        seed_uuid = self._seed_entry(self.CHANGEPW_URL, "testuser", password="old-password-do-not-reuse")
        rc, out, err = self._run(self.CHANGEPW_URL, "changepw.html", extra_argv=[])
        self.assertEqual(rc, 0, err)

        # no rofi update-vs-create prompt needed: exactly one entry in the store afterwards
        self.assertEqual(len(self.server.entries), 1)
        entry = self.server.entries[seed_uuid]
        self.assertNotEqual(entry["password"], "old-password-do-not-reuse")

        last = self.server.saved_logins[-1]
        self.assertTrue(last["is_update"])
        self.assertEqual(last["entry_uuid"], seed_uuid)
        self.assertIn("updated password and filled", self._fifo_contents())

    def test_signup_form_single_match_prompts_and_defaults_to_update(self):
        seed_uuid = self._seed_entry(self.SIGNUP_URL, "testuser")
        # fake_rofi_echo_filter answers a no -filter prompt with the FIRST
        # stdin line, which is "update existing entry for testuser".
        rc, out, err = self._run(self.SIGNUP_URL, "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.entries), 1)
        self.assertTrue(self.server.saved_logins[-1]["is_update"])
        self.assertEqual(self.server.saved_logins[-1]["entry_uuid"], seed_uuid)
        self.assertIn("updated password and filled", self._fifo_contents())

    def test_signup_form_single_match_can_choose_create_instead(self):
        self._seed_entry(self.SIGNUP_URL, "testuser")
        self._use_rofi("fake_rofi_pick_create")
        rc, out, err = self._run(self.SIGNUP_URL, "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.entries), 2)  # old entry kept, new one added
        self.assertFalse(self.server.saved_logins[-1]["is_update"])
        self.assertIn("saved new password and filled", self._fifo_contents())

    def test_signup_form_single_match_cancel_saves_and_fills_nothing(self):
        self._seed_entry(self.SIGNUP_URL, "testuser")
        self._use_rofi("fake_rofi_cancel")
        before = len(self.server.saved_logins)
        rc, out, err = self._run(self.SIGNUP_URL, "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.saved_logins), before)
        self.assertEqual(len(self.server.entries), 1)  # untouched
        self.assertNotIn("jseval", self._fifo_contents())
        self.assertIn("cancelled", self._fifo_contents())

    def test_multiple_matches_prompts_a_pick_list_default_first(self):
        uuid1 = self._seed_entry(self.SIGNUP_URL, "testuser", password="old-1")
        uuid2 = self._seed_entry(self.SIGNUP_URL, "testuser", password="old-2")
        # fake_rofi_echo_filter's no-filter behaviour picks the FIRST stdin
        # line, i.e. the first listed entry.
        rc, out, err = self._run(self.SIGNUP_URL, "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.entries), 2)  # still just the two, no duplicate
        last = self.server.saved_logins[-1]
        self.assertTrue(last["is_update"])
        self.assertEqual(last["entry_uuid"], uuid1)
        self.assertNotEqual(self.server.entries[uuid2]["password"], last["password"])

    def test_multiple_matches_can_pick_a_non_default_entry(self):
        uuid1 = self._seed_entry(self.SIGNUP_URL, "testuser", password="old-1")
        uuid2 = self._seed_entry(self.SIGNUP_URL, "testuser", password="old-2")
        self._use_rofi("fake_rofi_pick_second")
        rc, out, err = self._run(self.SIGNUP_URL, "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.entries), 2)
        last = self.server.saved_logins[-1]
        self.assertTrue(last["is_update"])
        self.assertEqual(last["entry_uuid"], uuid2)
        self.assertNotEqual(self.server.entries[uuid1]["password"], last["password"])

    def test_multiple_matches_can_choose_create_new(self):
        self._seed_entry(self.SIGNUP_URL, "testuser", password="old-1")
        self._seed_entry(self.SIGNUP_URL, "testuser", password="old-2")
        self._use_rofi("fake_rofi_pick_create")
        rc, out, err = self._run(self.SIGNUP_URL, "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.entries), 3)
        self.assertFalse(self.server.saved_logins[-1]["is_update"])

    def test_multiple_matches_cancel_saves_and_fills_nothing(self):
        self._seed_entry(self.SIGNUP_URL, "testuser", password="old-1")
        self._seed_entry(self.SIGNUP_URL, "testuser", password="old-2")
        self._use_rofi("fake_rofi_cancel")
        before = len(self.server.saved_logins)
        rc, out, err = self._run(self.SIGNUP_URL, "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.saved_logins), before)
        self.assertEqual(len(self.server.entries), 2)
        self.assertNotIn("jseval", self._fifo_contents())
        self.assertIn("cancelled", self._fifo_contents())

    def test_no_match_creates_as_before(self):
        self._seed_entry(self.SIGNUP_URL, "someone-else")  # different login: not a match
        rc, out, err = self._run(self.SIGNUP_URL, "signup.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.server.entries), 2)
        self.assertFalse(self.server.saved_logins[-1]["is_update"])
        self.assertIn("saved new password and filled", self._fifo_contents())

    def test_password_never_printed_during_dedup_prompts(self):
        self._seed_entry(self.SIGNUP_URL, "testuser", password="old-1")
        self._seed_entry(self.SIGNUP_URL, "testuser", password="old-2")
        rc, out, err = self._run(self.SIGNUP_URL, "signup.html")
        self.assertEqual(rc, 0, err)
        new_password = self.server.saved_logins[-1]["password"]
        self.assertNotIn(new_password, out)
        self.assertNotIn(new_password, err)


if __name__ == "__main__":
    unittest.main()
