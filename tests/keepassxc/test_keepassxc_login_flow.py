#!/usr/bin/env python3
"""Flow tests for keepassxc-login, against the mock KeePassXC server (never
the real one - every test asserts its socket path is not the real one).

Covers: the domain-widening cascade actually finding entries that a plain
get-logins(page URL) would miss (subdomain-broader-entry via KeePassXC's
own matching, and the www-variant/parent-domain case that only this
cascade's step (e) can find), the "no entry -> search -> remember" flow
and its alias reuse on a later visit, --forget, --totp, the rofi account
picker (single vs multiple matches, default and non-default pick,
cancel), and that a password is never printed/logged/shown in a rofi
line.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
_repo_scripts = TESTS_DIR.parent.parent / "userscripts"
SCRIPT_DIR = _repo_scripts if _repo_scripts.is_dir() else TESTS_DIR.parent
SCRIPT_PATH = SCRIPT_DIR / "keepassxc-login"
FIXTURES = TESTS_DIR / "fixtures"

sys.path.insert(0, str(TESTS_DIR))
from mock_keepassxc_server import MockKeepassXCServer  # noqa: E402

sys.path.insert(0, str(SCRIPT_DIR))
import kpxc_lookup as kl  # noqa: E402

REAL_SOCKET = "/run/user/{}/org.keepassxc.KeePassXC.BrowserServer".format(os.getuid())


def _load_module():
    loader = importlib.machinery.SourceFileLoader("keepassxc_login_flow", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


kpl = _load_module()


class LoginFlowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="keepassxc-login-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.socket_path = os.path.join(self.tmpdir, "mock-keepassxc.sock")
        self.assertNotEqual(self.socket_path, REAL_SOCKET)

        self.data_dir = os.path.join(self.tmpdir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.aliases_dir = os.path.join(self.tmpdir, "keepassxc-login")

        self.fifo_path = os.path.join(self.tmpdir, "fifo")
        Path(self.fifo_path).touch()

        # cli_fallback() (reached whenever nothing at all is found) reads
        # its own config and, absent one, scans "$HOME" for *.kdbx files -
        # both MUST be redirected away from the real ones for every test
        # in this class, not just ones that exercise the CLI fallback on
        # purpose (see kpxc_lookup.cli_config_path()/discover_candidate_
        # databases()). fake_home is an empty dir: an accidental scan
        # finds nothing rather than erroring or, worse, finding something.
        self.cli_config_path = os.path.join(self.tmpdir, "keepassxc-login.toml")
        self.fake_home = os.path.join(self.tmpdir, "fake-home")
        os.makedirs(self.fake_home, exist_ok=True)

        self.server = MockKeepassXCServer(self.socket_path)
        self.server.start()
        self.addCleanup(self.server.stop)

        self._env_patch = None
        self._use_rofi("fake_rofi_pick_index_0")

    def _use_rofi(self, fixture_dir_name):
        if self._env_patch is not None:
            self._env_patch.stop()
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "QUTE_DATA_DIR": self.data_dir,
                "QUTE_FIFO": self.fifo_path,
                "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
                "QUTE_KEEPASSXC_CLI_CONFIG": self.cli_config_path,
                "QUTE_KEEPASSXC_HOME": self.fake_home,
                "PATH": str(FIXTURES / fixture_dir_name) + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _run(self, url, extra_argv=()):
        out, err = io.StringIO(), io.StringIO()
        argv = [url, "--socket", self.socket_path, "--insecure"] + list(extra_argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = kpl.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _fifo_contents(self):
        return Path(self.fifo_path).read_text()

    def _seed_entry(self, url, login, password="s3cr3t-do-not-print"):
        entry_uuid = "seed-{}".format(len(self.server.entries) + 1)
        self.server.entries[entry_uuid] = {
            "uuid": entry_uuid, "login": login, "password": password, "url": url, "name": url,
        }
        return entry_uuid

    def _use_filter_recorder_rofi(self):
        """Swap in a fake `rofi` that, for any -filter prompt (i.e.
        prompt_search_term's "search which site?" box), appends the
        -filter value it was given to a record file and then answers with
        it as usual (so the run continues normally) - lets a test observe
        exactly what default term a prompt was prefilled with, independent
        of whatever happens afterwards in the run. For any other prompt
        (no -filter, e.g. cli_fallback's -format i pickers) it falls back
        to echoing the first stdin line, like fake_rofi_pick_index_0.
        Returns the record file's Path; each -filter answer is one line.
        """
        record_file = Path(self.tmpdir) / "filter_recorder.txt"
        recorder_dir = Path(self.tmpdir) / "fake_rofi_filter_recorder"
        recorder_dir.mkdir(exist_ok=True)
        (recorder_dir / "rofi").write_text(
            "#!/usr/bin/env python3\n"
            "import sys, pathlib\n"
            f"record_file = pathlib.Path({str(record_file)!r})\n"
            "argv = sys.argv[1:]\n"
            "if '-filter' in argv:\n"
            "    value = argv[argv.index('-filter') + 1]\n"
            "    with record_file.open('a') as f:\n"
            "        f.write(value + chr(10))\n"
            "    print(value)\n"
            "else:\n"
            "    lines = [line.rstrip(chr(10)) for line in sys.stdin]\n"
            "    print(lines[0] if lines else '')\n"
            "sys.exit(0)\n"
        )
        os.chmod(recorder_dir / "rofi", 0o755)
        self._env_patch.stop()
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "QUTE_DATA_DIR": self.data_dir,
                "QUTE_FIFO": self.fifo_path,
                "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
                "QUTE_KEEPASSXC_CLI_CONFIG": self.cli_config_path,
                "QUTE_KEEPASSXC_HOME": self.fake_home,
                "PATH": str(recorder_dir) + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()
        return record_file


class ExactAndSubdomainMatchTests(LoginFlowTestCase):
    """These don't need the cascade's own domain-widening at all - they
    confirm the plumbing (association, fill) works, and that KeePassXC's
    own subdomain matching (an entry at the origin matches any subdomain
    of it) is exercised correctly through the mock.
    """

    def test_exact_origin_match_fills_login_and_password(self):
        self._seed_entry("https://example.test/", "alice", password="hunter2")
        rc, out, err = self._run("https://example.test/login")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn("jseval --quiet --world jseval", fifo)
        self.assertIn('"alice"', fifo)
        self.assertIn('"hunter2"', fifo)
        # login prompt / search prompt never triggered
        self.assertNotIn("Search which site", fifo)

    def test_entry_saved_at_parent_domain_matches_a_subdomain_page(self):
        # entry lives at the bare/origin domain; page is on a subdomain -
        # this is KeePassXC's own subdomain matching, reached via cascade
        # step (c) (the origin), no www/parent-domain guessing needed.
        self._seed_entry("https://example.test/", "alice", password="hunter2")
        rc, out, err = self._run("https://app.example.test/dashboard")
        self.assertEqual(rc, 0, err)
        self.assertIn('"hunter2"', self._fifo_contents())

    def test_password_never_printed_to_stdout_or_stderr(self):
        self._seed_entry("https://example.test/", "alice", password="hunter2")
        rc, out, err = self._run("https://example.test/login")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("hunter2", err)


class DomainWideningCascadeTests(LoginFlowTestCase):
    """The case a plain get-logins(page URL) structurally cannot solve:
    the entry lives at a *narrower*/sibling host (www.example.test) than
    the page currently browsed (bare example.test) - only cascade step
    (e)'s www-variant of the registrable domain finds it.
    """

    def test_www_variant_found_when_browsing_the_bare_domain(self):
        self._seed_entry("https://www.example.test/", "alice", password="hunter2")
        rc, out, err = self._run("https://example.test/account")
        self.assertEqual(rc, 0, err)
        self.assertIn('"hunter2"', self._fifo_contents())

    def test_scheme_mismatch_still_found(self):
        self._seed_entry("http://example.test/", "alice", password="hunter2")
        rc, out, err = self._run("https://example.test/account")
        self.assertEqual(rc, 0, err)
        self.assertIn('"hunter2"', self._fifo_contents())


class LoginSubdomainGuessTests(LoginFlowTestCase):
    """Coordinator-requested addition: the entry lives on a *sibling*
    login subdomain the site itself picked (accounts.example.test), while
    the user is on www.example.test - not a case steps (a)-(e) can solve
    (no parent/www relationship between the two hosts in either
    direction), only step (f)'s guessed-prefix probe of the registrable
    domain. Must be found with NO interactive prompt, filled, and
    remembered as the alias automatically.
    """

    def test_sibling_login_subdomain_found_automatically_filled_and_remembered(self):
        self._seed_entry("https://accounts.example.test/", "alice", password="hunter2")
        self.assertIsNone(kl.get_alias("www.example.test"))

        rc, out, err = self._run("https://www.example.test/dashboard")

        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        # filled, with no detour through the "search which site?" prompt
        # (fake_rofi_pick_index_0, the default fixture, would have echoed
        # the WRONG term "example.test" back if that prompt had run -
        # which has no seeded entry of its own, so the run would have
        # failed instead of succeeding)
        self.assertIn('"hunter2"', fifo)
        self.assertNotIn("Search which site", fifo)
        # says which guessed host it used
        self.assertIn("keepassxc: matched accounts.example.test", fifo)
        # remembered for next time, with no further guessing needed
        self.assertEqual(kl.get_alias("www.example.test"), "accounts.example.test")

    def test_second_visit_uses_the_remembered_alias_directly(self):
        entry_uuid = self._seed_entry("https://accounts.example.test/", "alice", password="hunter2")
        self._run("https://www.example.test/dashboard")  # remembers the alias
        Path(self.fifo_path).write_text("")  # fifo is append-only; reset before the 2nd run

        # a second, distinct page on the same host - now resolved via the
        # alias alone, no guessing needed
        rc, out, err = self._run("https://www.example.test/settings")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn('"hunter2"', fifo)
        # no repeat "matched"/"remembered" chatter on a plain alias hit
        self.assertNotIn("matched accounts.example.test", fifo)
        self.assertEqual(self.server.entries[entry_uuid]["login"], "alice")

    def test_password_never_shown_for_a_guessed_match(self):
        self._seed_entry("https://accounts.example.test/", "alice", password="hunter2")
        rc, out, err = self._run("https://www.example.test/dashboard")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("hunter2", err)

    def test_multiple_entries_on_the_guessed_subdomain_still_use_the_picker(self):
        self._seed_entry("https://accounts.example.test/", "alice", password="pw-1")
        self._seed_entry("https://accounts.example.test/", "bob", password="pw-2")
        self._use_rofi("fake_rofi_pick_index_1")
        rc, out, err = self._run("https://www.example.test/dashboard")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn('"pw-2"', fifo)
        self.assertIn("matched accounts.example.test", fifo)


class SearchAndRememberTests(LoginFlowTestCase):
    """Nothing in the cascade matches (different registrable domain
    entirely, like an SSO/accounts.* setup) - falls back to the rofi text
    prompt, and on success remembers the alias for next time.
    """

    def test_no_match_prompts_search_and_remembers_on_success(self):
        self._seed_entry("https://other-corp.example/", "alice", password="hunter2")
        self._use_rofi("fake_rofi_type_other_corp")
        rc, out, err = self._run("https://portal.example.test/")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn('"hunter2"', fifo)
        self.assertIn("using entries of other-corp.example for portal.example.test (remembered)", fifo)
        self.assertEqual(kl.get_alias("portal.example.test"), "other-corp.example")

    def test_alias_is_reused_on_a_later_visit_without_prompting_again(self):
        self._seed_entry("https://other-corp.example/", "alice", password="hunter2")
        kl.save_alias("portal.example.test", "other-corp.example")
        # fake_rofi_pick_index_0 (the default in setUp) would fail the
        # search prompt's expectations if hit (it echoes whatever -filter
        # value it's given, which would be "portal.example.test" - the
        # wrong term) - using it here (unchanged from setUp) proves the
        # search prompt path is never reached.
        rc, out, err = self._run("https://portal.example.test/")
        self.assertEqual(rc, 0, err)
        self.assertIn('"hunter2"', self._fifo_contents())
        self.assertNotIn("remembered", self._fifo_contents())

    def test_no_match_anywhere_is_a_message_error(self):
        self._use_rofi("fake_rofi_type_other_corp")  # types a term with no entry either
        rc, out, err = self._run("https://portal.example.test/")
        self.assertNotEqual(rc, 0)
        self.assertIn("message-error", self._fifo_contents())
        # cascade + typed search both fail -> falls through to the
        # keepassxc-cli database fallback (see LastResortCliFallbackTests),
        # which here also finds nothing (no database configured, and
        # fake_home - see setUp - is an empty directory)
        self.assertIn("no KeePassXC database found to search", self._fifo_contents())

    def test_escape_on_search_prompt_cancels(self):
        self._use_rofi("fake_rofi_cancel")
        rc, out, err = self._run("https://portal.example.test/")
        self.assertEqual(rc, 0)
        self.assertIn("cancelled", self._fifo_contents())
        self.assertNotIn("jseval", self._fifo_contents())

    def test_search_prompt_prefilled_with_registrable_domain(self):
        # can't observe rofi's actual UI, but a recording fake rofi can
        # capture exactly what -filter value prompt_search_term used.
        record_file = self._use_filter_recorder_rofi()
        rc, out, err = self._run("https://checkout.shop.example.test/cart")
        self.assertEqual(record_file.read_text().splitlines(), ["example.test"])


class ForgetTests(LoginFlowTestCase):
    def test_forget_removes_a_remembered_alias(self):
        kl.save_alias("portal.example.test", "other-corp.example")
        rc, out, err = self._run("https://portal.example.test/", extra_argv=["--forget"])
        self.assertEqual(rc, 0, err)
        self.assertIsNone(kl.get_alias("portal.example.test"))
        self.assertIn("forgot", self._fifo_contents())

    def test_forget_with_no_alias_set_is_a_harmless_no_op(self):
        rc, out, err = self._run("https://portal.example.test/", extra_argv=["--forget"])
        self.assertEqual(rc, 0, err)
        self.assertIn("no remembered site", self._fifo_contents())

    def test_forget_never_touches_keepassxc(self):
        # --forget must return before any association/connection attempt -
        # the mock server records no activity of any kind.
        rc, out, err = self._run("https://portal.example.test/", extra_argv=["--forget"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.server.saved_logins, [])

    def test_after_forget_the_search_prompt_is_needed_again(self):
        self._seed_entry("https://other-corp.example/", "alice", password="hunter2")
        kl.save_alias("portal.example.test", "other-corp.example")
        self._run("https://portal.example.test/", extra_argv=["--forget"])
        # a recording fake rofi proves the search prompt genuinely ran
        # again (not skipped via the now-forgotten alias) - it's asked for
        # the registrable-domain default "example.test", which doesn't
        # match the seeded "other-corp.example" entry.
        record_file = self._use_filter_recorder_rofi()
        rc, out, err = self._run("https://portal.example.test/")
        self.assertEqual(record_file.read_text().splitlines(), ["example.test"])
        self.assertIsNone(kl.get_alias("portal.example.test"))


class MultipleAccountsPickerTests(LoginFlowTestCase):
    def test_single_match_needs_no_picker(self):
        self._seed_entry("https://example.test/", "alice", password="hunter2")
        rc, out, err = self._run("https://example.test/login")
        self.assertEqual(rc, 0, err)
        self.assertIn('"hunter2"', self._fifo_contents())

    def test_multiple_matches_default_picks_the_first(self):
        self._seed_entry("https://example.test/", "alice", password="pw-1")
        self._seed_entry("https://example.test/", "bob", password="pw-2")
        rc, out, err = self._run("https://example.test/login")  # fake_rofi_pick_index_0
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn('"pw-1"', fifo)
        self.assertNotIn('"pw-2"', fifo)

    def test_multiple_matches_can_pick_a_non_default_entry(self):
        self._seed_entry("https://example.test/", "alice", password="pw-1")
        self._seed_entry("https://example.test/", "bob", password="pw-2")
        self._use_rofi("fake_rofi_pick_index_1")
        rc, out, err = self._run("https://example.test/login")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn('"pw-2"', fifo)
        self.assertNotIn('"pw-1"', fifo)

    def test_picker_never_shows_a_password(self):
        self._seed_entry("https://example.test/", "alice", password="pw-1")
        self._seed_entry("https://example.test/", "bob", password="pw-2")
        # fake_rofi_pick_index_0/1 don't record their stdin, so assert via
        # a recording fake rofi instead.
        recorder_dir = Path(self.tmpdir) / "fake_rofi_recorder"
        recorder_dir.mkdir()
        record_file = Path(self.tmpdir) / "rofi_stdin.txt"
        (recorder_dir / "rofi").write_text(
            "#!/usr/bin/env python3\n"
            "import sys, pathlib\n"
            f"pathlib.Path({str(record_file)!r}).write_text(sys.stdin.read())\n"
            "print(0)\n"
            "sys.exit(0)\n"
        )
        os.chmod(recorder_dir / "rofi", 0o755)
        # not FIXTURES-relative like _use_rofi()'s other fixtures - built
        # fresh under this test's own tmpdir above - so patch PATH directly.
        self._env_patch.stop()
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "QUTE_DATA_DIR": self.data_dir,
                "QUTE_FIFO": self.fifo_path,
                "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
                "QUTE_KEEPASSXC_CLI_CONFIG": self.cli_config_path,
                "QUTE_KEEPASSXC_HOME": self.fake_home,
                "PATH": str(recorder_dir) + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()
        rc, out, err = self._run("https://example.test/login")
        self.assertEqual(rc, 0, err)
        shown = record_file.read_text()
        self.assertNotIn("pw-1", shown)
        self.assertNotIn("pw-2", shown)
        self.assertIn("alice", shown)
        self.assertIn("bob", shown)

    def test_cancel_on_picker_fills_nothing(self):
        self._seed_entry("https://example.test/", "alice", password="pw-1")
        self._seed_entry("https://example.test/", "bob", password="pw-2")
        self._use_rofi("fake_rofi_cancel")
        rc, out, err = self._run("https://example.test/login")
        self.assertEqual(rc, 0)
        self.assertNotIn("jseval", self._fifo_contents())
        self.assertIn("cancelled", self._fifo_contents())


class TotpTests(LoginFlowTestCase):
    def test_totp_fills_via_get_totp_for_the_selected_entry(self):
        entry_uuid = self._seed_entry("https://example.test/", "alice", password="hunter2")
        self.server.totp_by_uuid = {entry_uuid: "123456"}
        rc, out, err = self._run("https://example.test/login", extra_argv=["--totp"])
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn('"123456"', fifo)
        self.assertNotIn("hunter2", fifo)

    def test_totp_with_no_totp_key_is_a_message_error(self):
        self._seed_entry("https://example.test/", "alice", password="hunter2")
        rc, out, err = self._run("https://example.test/login", extra_argv=["--totp"])
        self.assertNotEqual(rc, 0)
        self.assertIn("no TOTP key found", self._fifo_contents())


class ErrorHandlingTests(LoginFlowTestCase):
    def test_missing_url_is_a_message_error(self):
        rc, out, err = self._run("")
        self.assertNotEqual(rc, 0)
        self.assertIn("missing URL", self._fifo_contents())


if __name__ == "__main__":
    unittest.main()
