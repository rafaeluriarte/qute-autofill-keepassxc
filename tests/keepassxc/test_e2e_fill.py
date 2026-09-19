#!/usr/bin/env python3
"""End-to-end test: a real, isolated qutebrowser drives the real userscript
against a mock KeePassXC and a local HTTP fixture server.

Isolation:
  - qutebrowser runs with `-B <tempdir>` (a throwaway basedir: its own
    config/data/cache/runtime dirs - QUTE_CONFIG_DIR and QUTE_DATA_DIR for
    every userscript it spawns point inside this tempdir automatically).
  - `QT_QPA_PLATFORM=offscreen` - no real/virtual display is touched; the
    DOM/JS behaviour under test doesn't need pixels.
  - The userscript is told to use our mock KeePassXC's Unix-socket path via
    --socket, and --insecure (a plaintext association key inside the temp
    basedir) so no GPG key or real KeePassXC is involved.
  - `rofi` is faked via a PATH override (same fixture scripts test_save_flow
    uses), so no real X/rofi is needed either.
  - Fixture pages are served over 127.0.0.1 only.

qutebrowser is driven purely via its own `:command` startup arguments
(`:open`, `:later N <cmd>`, `:spawn --userscript ...`, `:jseval`, `:quit`):
a non-quiet `:jseval` result is shown via qutebrowser's message.info(),
which also lands as an "INFO: <value>" line in qutebrowser's own stdout -
that's how this test reads filled-field values back out, since userscripts
themselves have no return channel from the page.
"""
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
_repo_scripts = TESTS_DIR.parent.parent / "userscripts"
SCRIPT_DIR = _repo_scripts if _repo_scripts.is_dir() else TESTS_DIR.parent
SCRIPT_PATH = SCRIPT_DIR / "keepassxc-newpass"
FIXTURES = TESTS_DIR / "fixtures"

sys.path.insert(0, str(TESTS_DIR))
from mock_keepassxc_server import MockKeepassXCServer  # noqa: E402

REAL_SOCKET = "/run/user/{}/org.keepassxc.KeePassXC.BrowserServer".format(os.getuid())

QUTEBROWSER_BIN = shutil.which("qutebrowser")


def _extract_info_lines(log_text):
    return [m.group(1) for m in re.finditer(r"INFO:\s(.*)", log_text)]


@unittest.skipUnless(QUTEBROWSER_BIN, "qutebrowser not installed")
class E2EFillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="keepassxc-newpass-e2e-")

        # local fixture HTTP server, 127.0.0.1 only - stateless, safe to share
        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(FIXTURES), **kw
        )
        cls.http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.http_port = cls.http_server.server_port
        cls.http_thread = threading.Thread(target=cls.http_server.serve_forever, daemon=True)
        cls.http_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http_server.shutdown()
        cls.http_thread.join(timeout=2)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        # Mock KeePassXC servers are per-TEST (not per-class): every fixture
        # page in this class is served from the same 127.0.0.1:<port> host,
        # and the mock's get-logins now matches the way real KeePassXC does
        # (host-based - see mock_keepassxc_server.py's module docstring),
        # which is path-insensitive. A class-scoped server would let one
        # test's saved/seeded entries be "found" as duplicates by another
        # test's dedup check purely because they share a host, even though
        # their fixture pages differ - a fresh server per test keeps each
        # test's entries isolated regardless of matching precision.
        unique = uuid.uuid4().hex[:8]
        self.mock_socket = os.path.join(self.tmpdir, f"mock-keepassxc-{unique}.sock")
        self.assertNotEqual(self.mock_socket, REAL_SOCKET)
        self.mock_server = MockKeepassXCServer(self.mock_socket)
        self.mock_server.start()
        self.addCleanup(self.mock_server.stop)

        self.mock_socket_deny = os.path.join(self.tmpdir, f"mock-keepassxc-deny-{unique}.sock")
        self.mock_server_deny = MockKeepassXCServer(self.mock_socket_deny, deny_set_login="denied")
        self.mock_server_deny.start()
        self.addCleanup(self.mock_server_deny.stop)

    def _basedir_with_fake_profile(self, name):
        basedir = os.path.join(self.tmpdir, name)
        os.makedirs(os.path.join(basedir, "config", "autofill"), exist_ok=True)
        shutil.copy(FIXTURES / "fake_profiles.toml", os.path.join(basedir, "config", "autofill", "profiles.toml"))
        return basedir

    def _run_qutebrowser(self, basedir, page, socket_path, extra_jseval, length=12, timeout=30):
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["PATH"] = str(FIXTURES / "fake_rofi_echo_filter") + os.pathsep + env.get("PATH", "")
        # find_matching_entries() consults kpxc_lookup's alias store; keep
        # it inside this test's own basedir so the real
        # ~/.local/share/qutebrowser/keepassxc-login/aliases.json is never
        # read (qutebrowser's own -B basedir only wires up QUTE_DATA_DIR/
        # QUTE_CONFIG_DIR automatically, not this).
        env["QUTE_KEEPASSXC_ALIASES_DIR"] = os.path.join(basedir, "keepassxc-login")
        url = f"http://127.0.0.1:{self.http_port}/{page}"
        cmd = [
            QUTEBROWSER_BIN, "-B", basedir,
            f":open {url}",
            ":later 1500 spawn --userscript {} --insecure --socket {} --length {}".format(
                SCRIPT_PATH, socket_path, length
            ),
            f":later 4000 jseval --world jseval {extra_jseval}",
            ":later 4600 quit",
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return proc

    def test_signup_form_gets_filled_with_the_saved_password(self):
        basedir = self._basedir_with_fake_profile("signup")
        before = len(self.mock_server.saved_logins)
        js = (
            "JSON.stringify([document.querySelector('input[name=password]').value,"
            "document.querySelector('input[name=confirm]').value,"
            "document.querySelector('input[name=email]').value])"
        )
        proc = self._run_qutebrowser(basedir, "signup.html", self.mock_socket, js, length=12)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        saved = self.mock_server.saved_logins[before:]
        self.assertEqual(len(saved), 1, proc.stdout + proc.stderr)
        saved_password = saved[0]["password"]
        self.assertEqual(len(saved_password), 12)
        self.assertEqual(saved[0]["login"], "testuser")

        info_lines = _extract_info_lines(proc.stdout + proc.stderr)
        self.assertTrue(any("saved new password and filled" in line for line in info_lines), proc.stdout)
        field_values = json.loads(info_lines[-1])
        password_field, confirm_field, email_field = field_values
        self.assertEqual(password_field, saved_password)
        self.assertEqual(confirm_field, saved_password)
        self.assertEqual(email_field, "testuser")

    def test_change_password_form_leaves_current_password_untouched_and_never_submits(self):
        basedir = self._basedir_with_fake_profile("changepw")
        before = len(self.mock_server.saved_logins)
        js = (
            "JSON.stringify([document.getElementById('current-pw').value,"
            "document.getElementById('new-pw').value,"
            "document.getElementById('confirm-pw').value,"
            "document.location.href])"
        )
        proc = self._run_qutebrowser(basedir, "changepw.html", self.mock_socket, js, length=14)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        saved = self.mock_server.saved_logins[before:]
        self.assertEqual(len(saved), 1)
        saved_password = saved[0]["password"]

        info_lines = _extract_info_lines(proc.stdout + proc.stderr)
        current_pw, new_pw, confirm_pw, href = json.loads(info_lines[-1])
        self.assertEqual(current_pw, "should-not-be-touched")
        self.assertEqual(new_pw, saved_password)
        self.assertEqual(confirm_pw, saved_password)
        # no submission happened: the URL has no query string appended
        self.assertNotIn("?", href)
        self.assertTrue(href.endswith("changepw.html"))

    def test_denied_save_leaves_the_form_empty(self):
        basedir = self._basedir_with_fake_profile("denied")
        before_mock = len(self.mock_server.saved_logins)
        before_deny = len(self.mock_server_deny.saved_logins)
        js = (
            "JSON.stringify([document.querySelector('input[name=password]').value,"
            "document.querySelector('input[name=confirm]').value])"
        )
        proc = self._run_qutebrowser(basedir, "signup.html", self.mock_socket_deny, js, length=12)
        # the userscript itself exits non-zero on a denied save; qutebrowser
        # logs that as an ERROR but :quit via `:later` still runs, so the
        # overall qutebrowser process still exits 0.
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertEqual(len(self.mock_server_deny.saved_logins), before_deny)  # nothing saved
        self.assertEqual(len(self.mock_server.saved_logins), before_mock)  # and the *other* mock untouched

        info_lines = _extract_info_lines(proc.stdout + proc.stderr)
        password_field, confirm_field = json.loads(info_lines[-1])
        self.assertEqual(password_field, "")
        self.assertEqual(confirm_field, "")
        self.assertIn("save failed", proc.stdout + proc.stderr)
        self.assertNotIn("and filled", proc.stdout)

    def test_change_password_form_updates_the_existing_entry_not_a_duplicate(self):
        basedir = self._basedir_with_fake_profile("changepw-update")
        # each test gets its own fresh mock server (see setUp), so this
        # doesn't need a distinct query string to stay isolated any more -
        # kept anyway as a realistic page URL.
        page = "changepw.html?dedup-test=1"
        url = f"http://127.0.0.1:{self.http_port}/{page}"
        entry_uuid = "e2e-seed-1"
        self.mock_server.entries[entry_uuid] = {
            "uuid": entry_uuid, "login": "testuser", "password": "old-password",
            "url": url, "name": url,
        }
        before = len(self.mock_server.saved_logins)
        js = "document.getElementById('new-pw').value"
        proc = self._run_qutebrowser(basedir, page, self.mock_socket, js, length=14)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        saved = self.mock_server.saved_logins[before:]
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["is_update"])
        self.assertEqual(saved[0]["entry_uuid"], entry_uuid)
        # no duplicate created: still exactly the one (updated) entry -
        # its stored url is now the ORIGIN (keepassxc-newpass saves
        # url=origin, submitUrl=page URL - see its module docstring's
        # "Avoiding duplicate entries" section), not the full page URL.
        self.assertEqual(len(self.mock_server.entries), 1)
        self.assertEqual(self.mock_server.entries[entry_uuid]["url"], f"http://127.0.0.1:{self.http_port}/")
        self.assertEqual(saved[0]["submitUrl"], url)
        self.assertNotEqual(self.mock_server.entries[entry_uuid]["password"], "old-password")

        info_lines = _extract_info_lines(proc.stdout + proc.stderr)
        self.assertTrue(any("updated password and filled" in line for line in info_lines), proc.stdout)


if __name__ == "__main__":
    unittest.main()
