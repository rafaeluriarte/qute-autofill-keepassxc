#!/usr/bin/env python3
"""E2E: isolated qutebrowser captures a typed login and saves via mock KPXC."""

import http.server
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent.parent / "userscripts"
SCRIPT_PATH = SCRIPT_DIR / "keepassxc-save"
FIXTURES = TESTS_DIR / "fixtures"

sys.path.insert(0, str(TESTS_DIR))
from mock_keepassxc_server import MockKeepassXCServer  # noqa: E402

QUTEBROWSER = shutil.which("qutebrowser")
XVFB_RUN = shutil.which("xvfb-run")
REAL_SOCKET = "/run/user/{}/org.keepassxc.KeePassXC.BrowserServer".format(os.getuid())


@unittest.skipUnless(QUTEBROWSER and XVFB_RUN, "qutebrowser/xvfb-run not installed")
class E2ESaveExistingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="keepassxc-save-existing-e2e-")
        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(FIXTURES), **kw
        )
        cls.http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.http_port = cls.http_server.server_port
        cls.http_thread = threading.Thread(target=cls.http_server.serve_forever, daemon=True)
        cls.http_thread.start()

        cert_path = os.path.join(cls.tmpdir, "localhost-cert.pem")
        key_path = os.path.join(cls.tmpdir, "localhost-key.pem")
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", key_path, "-out", cert_path, "-days", "1", "-subj", "/CN=127.0.0.1",
            ],
            check=True, capture_output=True, text=True,
        )
        cls.https_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(certfile=cert_path, keyfile=key_path)
        cls.https_server.socket = tls.wrap_socket(cls.https_server.socket, server_side=True)
        cls.https_port = cls.https_server.server_port
        cls.https_thread = threading.Thread(target=cls.https_server.serve_forever, daemon=True)
        cls.https_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http_server.shutdown()
        cls.http_thread.join(timeout=2)
        cls.https_server.shutdown()
        cls.https_thread.join(timeout=2)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _run_capture(self, scheme, port, name):
        basedir = os.path.join(self.tmpdir, name)
        os.makedirs(basedir)
        socket_path = os.path.join(self.tmpdir, name + ".sock")
        self.assertNotEqual(socket_path, REAL_SOCKET)
        server = MockKeepassXCServer(socket_path)
        server.start()
        self.addCleanup(server.stop)

        env = dict(os.environ)
        env["PATH"] = str(FIXTURES / "fake_rofi_echo_filter") + os.pathsep + env.get("PATH", "")
        env["QUTE_KEEPASSXC_ALIASES_DIR"] = os.path.join(basedir, "aliases")
        url = f"{scheme}://127.0.0.1:{port}/login.html"
        # Dummy test values are entered before invoking the userscript. The
        # userscript itself then reads them via --world jseval and sends only
        # its secret-free capture program through QUTE_FIFO.
        enter_values = (
            "document.querySelector('input[type=email]').value='captured-user';"
            "document.querySelector('input[type=password]').value='captured-test-value'"
        )
        cmd = [
            XVFB_RUN, "-a", QUTEBROWSER, "-B", basedir,
            *([":set content.tls.certificate_errors load-insecurely"] if scheme == "https" else []),
            f":open {url}",
            f":later 700 jseval --quiet --world jseval {enter_values}",
            ":later 1300 spawn --userscript {} --insecure --socket {}".format(
                SCRIPT_PATH, socket_path,
            ),
            ":later 5200 quit",
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(server.saved_logins), 1, proc.stdout + proc.stderr)
        saved = server.saved_logins[0]
        self.assertEqual(saved["login"], "captured-user")
        self.assertEqual(saved["password"], "captured-test-value")
        self.assertEqual(saved["url"], f"{scheme}://127.0.0.1:{port}/")
        self.assertEqual(saved["submitUrl"], url)
        # The userscript's own output/log messages never reveal it.
        self.assertNotIn("captured-test-value", proc.stdout + proc.stderr)

    def test_typed_fields_are_captured_in_isolated_world_and_saved(self):
        self._run_capture("http", self.http_port, "http-basedir")

    def test_https_page_can_reach_the_one_shot_loopback_capture(self):
        """Protect against mixed-content/PNA changes breaking real sites."""
        self._run_capture("https", self.https_port, "https-basedir")


if __name__ == "__main__":
    unittest.main()
