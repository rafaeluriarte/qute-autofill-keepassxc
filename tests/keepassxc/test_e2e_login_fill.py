#!/usr/bin/env python3
"""End-to-end test: a real, isolated qutebrowser drives the real
keepassxc-login userscript against a mock KeePassXC and a local HTTP
fixture server - same isolation approach as test_e2e_fill.py (its own
docstring explains the -B basedir / QT_QPA_PLATFORM=offscreen / fake rofi
/ 127.0.0.1-only approach in full; not repeated here).

Exercises the one thing a purely in-process test (test_keepassxc_login_flow.py)
can't: that the isolated-world jseval fill actually lands in the real page's
DOM, for both a login form (reached via the domain-widening cascade's
www-variant, not the exact page URL) and TOTP.
"""
import http.server
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
SCRIPT_PATH = SCRIPT_DIR / "keepassxc-login"
FIXTURES = TESTS_DIR / "fixtures"

sys.path.insert(0, str(TESTS_DIR))
from mock_keepassxc_server import MockKeepassXCServer  # noqa: E402

REAL_SOCKET = "/run/user/{}/org.keepassxc.KeePassXC.BrowserServer".format(os.getuid())

QUTEBROWSER_BIN = shutil.which("qutebrowser")


def _extract_info_lines(log_text):
    return [m.group(1) for m in re.finditer(r"INFO:\s(.*)", log_text)]


@unittest.skipUnless(QUTEBROWSER_BIN, "qutebrowser not installed")
class E2ELoginFillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="keepassxc-login-e2e-")
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
        unique = uuid.uuid4().hex[:8]
        self.mock_socket = os.path.join(self.tmpdir, f"mock-keepassxc-{unique}.sock")
        self.assertNotEqual(self.mock_socket, REAL_SOCKET)
        self.mock_server = MockKeepassXCServer(self.mock_socket)
        self.mock_server.start()
        self.addCleanup(self.mock_server.stop)

    def _basedir(self, name):
        basedir = os.path.join(self.tmpdir, name)
        os.makedirs(basedir, exist_ok=True)
        return basedir

    def _run_qutebrowser(self, basedir, page, extra_userscript_args, later_commands, timeout=30):
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["PATH"] = str(FIXTURES / "fake_rofi_pick_index_0") + os.pathsep + env.get("PATH", "")
        env["QUTE_KEEPASSXC_ALIASES_DIR"] = os.path.join(basedir, "keepassxc-login")
        url = f"http://127.0.0.1:{self.http_port}/{page}"
        args = " ".join(extra_userscript_args)
        cmd = [
            QUTEBROWSER_BIN, "-B", basedir,
            f":open {url}",
            *later_commands,
            ":later 1500 spawn --userscript {} --insecure --socket {} {}".format(
                SCRIPT_PATH, self.mock_socket, args
            ).rstrip(),
        ]
        cmd.append(":later 4600 quit")
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return proc

    def test_login_form_filled_via_the_cascades_origin_step(self):
        # The domain-widening cascade's query-construction/dedup/ordering
        # logic (bare vs www, scheme flip, parent-domain descent) is
        # exhaustively covered without a browser at all by
        # test_kpxc_lookup.py and test_keepassxc_login_flow.py's
        # DomainWideningCascadeTests (127.0.0.1 has no meaningful "www."
        # or parent-domain form to drive that same scenario through a real
        # browser anyway). What only a *real* qutebrowser can prove is
        # that the isolated-world jseval fill actually lands in the DOM -
        # so this seeds the entry at the page's origin (cascade step c,
        # not step b: the page below has a path, the entry doesn't) and
        # checks the real, rendered form afterwards.
        entry_uuid = "e2e-login-1"
        basedir = self._basedir("login-origin")
        page_host = f"127.0.0.1:{self.http_port}"
        self.mock_server.entries[entry_uuid] = {
            "uuid": entry_uuid, "login": "alice", "password": "hunter2-e2e",
            "url": f"http://{page_host}/", "name": "e2e entry",
        }
        js = (
            "JSON.stringify([document.querySelector('input[type=email]').value,"
            "document.querySelector('input[type=password]').value])"
        )
        proc = self._run_qutebrowser(
            basedir, "login.html", [],
            [f":later 4000 jseval --world jseval {js}"],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info_lines = _extract_info_lines(proc.stdout + proc.stderr)
        self.assertTrue(info_lines, proc.stdout + proc.stderr)
        email_field, password_field = json.loads(info_lines[-1])
        self.assertEqual(email_field, "alice")
        self.assertEqual(password_field, "hunter2-e2e")

    def test_totp_fills_the_focused_input(self):
        entry_uuid = "e2e-totp-1"
        basedir = self._basedir("totp")
        page_host = f"127.0.0.1:{self.http_port}"
        self.mock_server.entries[entry_uuid] = {
            "uuid": entry_uuid, "login": "alice", "password": "hunter2-e2e",
            "url": f"http://{page_host}/", "name": "e2e entry",
        }
        self.mock_server.totp_by_uuid[entry_uuid] = "654321"
        proc = self._run_qutebrowser(
            basedir, "totp.html", ["--totp"],
            [
                ':later 800 jseval --quiet document.getElementById("totp-input").focus()',
                ':later 4000 jseval --world jseval document.getElementById("totp-input").value',
            ],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        info_lines = _extract_info_lines(proc.stdout + proc.stderr)
        self.assertTrue(info_lines, proc.stdout + proc.stderr)
        self.assertEqual(info_lines[-1], "654321")


if __name__ == "__main__":
    unittest.main()
