#!/usr/bin/env python3
"""Tests for keepassxc-login's keepassxc-cli database-file fallback
(cli_fallback() and friends) - the last resort tried when the browser-
protocol cascade AND the interactive "search which site?" prompt both
find nothing, most commonly because an entry has no URL at all.

Fully offline, real keepassxc-cli, throwaway databases created with
`keepassxc-cli db-create` (password on stdin) under a temp dir - never
the real socket (never even connected to in this fallback: it's a pure
subprocess-to-keepassxc-cli path) and never ~/personal/** (every test
asserts its database paths are under the temp dir, and QUTE_KEEPASSXC_HOME/
QUTE_KEEPASSXC_CLI_CONFIG always point into it too, so an accidental
discovery scan or config read/write can't reach the real ones).

Most tests use `--cli` to reach the fallback directly, independent of
the browser-protocol cascade (already covered in isolation by
test_keepassxc_login_flow.py) - one test in ReachedNaturallyTests proves
it's also reached the ordinary way, after the cascade and the interactive
search both fail.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
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
KEEPASSXC_CLI = shutil.which("keepassxc-cli")


def _load_module():
    loader = importlib.machinery.SourceFileLoader("keepassxc_login_cli_fallback", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


kpl = _load_module()

MASTER_PASSWORD = "throwaway-master-pw"


def _create_db(path, entries=()):
    """Create a throwaway .kdbx at `path` (MASTER_PASSWORD) with `entries`,
    each {"title", "username", "password", "url"?}. Real keepassxc-cli,
    real subprocess calls, all under the caller's own temp dir.
    """
    proc = subprocess.run(
        [KEEPASSXC_CLI, "db-create", "-p", "-q", str(path)],
        input=f"{MASTER_PASSWORD}\n{MASTER_PASSWORD}\n", capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    for e in entries:
        argv = [KEEPASSXC_CLI, "add", "-p", "-u", e["username"], "-q"]
        if e.get("url"):
            argv += ["--url", e["url"]]
        argv += [str(path), e["title"]]
        proc = subprocess.run(
            argv, input=f"{MASTER_PASSWORD}\n{e['password']}\n", capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr


def _show(db, entry_path, attrs):
    """Read `attrs` straight from the database with a fresh, independent
    keepassxc-cli call - used to verify what the userscript actually did
    (e.g. an --add-url write) without trusting the userscript's own
    reporting of it.
    """
    args = [KEEPASSXC_CLI, "show", "-s"]
    for a in attrs:
        args += ["-a", a]
    args += [str(db), entry_path]
    proc = subprocess.run(args, input=f"{MASTER_PASSWORD}\n", capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


@unittest.skipUnless(KEEPASSXC_CLI, "keepassxc-cli not installed")
class CliFallbackTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="keepassxc-cli-fallback-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.fifo_path = os.path.join(self.tmpdir, "fifo")
        Path(self.fifo_path).touch()
        self.data_dir = os.path.join(self.tmpdir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.aliases_dir = os.path.join(self.tmpdir, "keepassxc-login-aliases")
        self.cli_config_path = os.path.join(self.tmpdir, "keepassxc-login.toml")
        self.fake_home = os.path.join(self.tmpdir, "fake-home")
        os.makedirs(self.fake_home, exist_ok=True)

        self.db_a = os.path.join(self.tmpdir, "a.kdbx")
        self.assertTrue(self.db_a.startswith(self.tmpdir))
        self.assertFalse(self.db_a.startswith(os.path.expanduser("~/personal")))

        self.answers_path = os.path.join(self.tmpdir, "rofi_answers.txt")
        Path(self.answers_path).touch()

        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "QUTE_DATA_DIR": self.data_dir,
                "QUTE_FIFO": self.fifo_path,
                "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
                "QUTE_KEEPASSXC_CLI_CONFIG": self.cli_config_path,
                "QUTE_KEEPASSXC_HOME": self.fake_home,
                "FAKE_ROFI_ANSWERS_FILE": self.answers_path,
                "PATH": str(FIXTURES / "fake_rofi_scripted") + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _queue_answers(self, *answers):
        """Program the fake rofi's answer queue, in order - one string per
        prompt it'll be asked; "__CANCEL__" simulates Escape.
        """
        Path(self.answers_path).write_text("\x00".join(answers))

    def _run(self, url, extra_argv=("--cli",)):
        out, err = io.StringIO(), io.StringIO()
        argv = [url, "--cli-path", KEEPASSXC_CLI] + list(extra_argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = kpl.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _fifo_contents(self):
        return Path(self.fifo_path).read_text()


class SingleDatabaseNoUrlEntryTests(CliFallbackTestCase):
    """The core scenario that prompted this feature: an entry with NO URL
    at all, found by title/username search, filled correctly.
    """

    def setUp(self):
        super().setUp()
        _create_db(self.db_a, entries=[
            {"title": "Ticketshop.example account", "username": "alice", "password": "hunter2", "url": None},
            {"title": "Unrelated entry", "username": "bob", "password": "other-pw", "url": None},
        ])
        cfg = kl.load_cli_config()
        kl.add_cli_database(cfg, self.db_a)
        kl.save_cli_config(cfg)

    def test_single_configured_database_is_used_silently(self):
        # only 2 prompts needed: master password, then the combined
        # search/pick (round 1's automatic search "ticketshop.example" - the
        # registrable domain - matches by title even with no URL at all)
        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account")
        rc, out, err = self._run("https://ticketshop.example/some/event")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn('"alice"', fifo)
        self.assertIn('"hunter2"', fifo)
        # no database-picker prompt was needed - the queue only had 2
        # answers and both were consumed correctly (no "queue exhausted")
        self.assertNotIn("queue exhausted", err)

    def test_fill_uses_isolated_world_native_setter(self):
        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn("jseval --quiet --world jseval", fifo)
        self.assertIn("getOwnPropertyDescriptor", fifo)  # native setter, same as the protocol path

    def test_password_never_in_argv(self):
        # structural guarantee, independent of any particular flow: patch
        # subprocess.run and confirm the password string is never one of
        # its positional argv elements, only ever the `input=` kwarg.
        calls = []
        real_run = subprocess.run

        def spy(args, *a, **kw):
            calls.append((list(args), kw.get("input")))
            return real_run(args, *a, **kw)

        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account")
        with mock.patch("subprocess.run", spy):
            rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        cli_calls = [(argv, stdin) for argv, stdin in calls if argv and argv[0] == KEEPASSXC_CLI]
        self.assertTrue(cli_calls)
        for argv, stdin in cli_calls:
            self.assertNotIn(MASTER_PASSWORD, argv)
            joined = " ".join(argv)
            self.assertNotIn(MASTER_PASSWORD, joined)

    def test_password_never_in_a_message(self):
        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertNotIn(MASTER_PASSWORD, fifo)
        self.assertNotIn(MASTER_PASSWORD, out)
        self.assertNotIn(MASTER_PASSWORD, err)

    def test_password_never_in_config_file(self):
        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account", "No")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        self.assertTrue(MASTER_PASSWORD)  # sanity: our own constant isn't accidentally empty
        config_text = Path(self.cli_config_path).read_text()
        self.assertNotIn(MASTER_PASSWORD, config_text)

    def test_wrong_master_password(self):
        self._queue_answers("totally-wrong-password", "/Ticketshop.example account")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertNotEqual(rc, 0)
        self.assertIn("wrong master password", self._fifo_contents())
        self.assertNotIn("jseval", self._fifo_contents())

    def test_cancel_at_master_password_prompt(self):
        self._queue_answers("__CANCEL__")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        self.assertIn("cancelled", self._fifo_contents())
        self.assertNotIn("jseval", self._fifo_contents())

    def test_empty_master_password_is_a_clean_abort(self):
        self._queue_answers("")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        self.assertIn("cancelled", self._fifo_contents())

    def test_cancel_at_search_pick_prompt(self):
        self._queue_answers(MASTER_PASSWORD, "__CANCEL__")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        self.assertIn("cancelled", self._fifo_contents())
        self.assertNotIn("jseval", self._fifo_contents())

    def test_editing_the_search_term_then_picking_a_result(self):
        # first round's default term "ticketshop.example" would already match,
        # but the user types something else instead ("unrelated") -
        # proving the combined prompt really re-searches on a typed term.
        self._queue_answers(MASTER_PASSWORD, "unrelated", "/Unrelated entry")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn('"bob"', fifo)
        self.assertIn('"other-pw"', fifo)

    def test_term_with_no_results_then_a_working_term(self):
        # a host whose own default term ("something-else.test") matches
        # neither seeded entry, so round 1 is guaranteed empty too -
        # every round up to the last needs an explicit typed term.
        self._queue_answers(
            MASTER_PASSWORD, "zzz-nothing-matches-zzz", "ticketshop.example", "/Ticketshop.example account",
        )
        rc, out, err = self._run("https://something-else.test/")
        self.assertEqual(rc, 0, err)
        self.assertIn('"alice"', self._fifo_contents())

    def test_entry_without_username_or_password_is_a_clean_error(self):
        # keepassxc-cli accepts an empty password at entry-creation time
        # (verified empirically); such a real entry must produce a clear
        # error, not a fill of empty strings.
        proc = subprocess.run(
            [KEEPASSXC_CLI, "add", "-p", "-u", "", "-q", self.db_a, "Blank Entry"],
            input=f"{MASTER_PASSWORD}\n\n", capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # default term "ticketshop.example" doesn't match this entry; type "Blank" instead
        self._queue_answers(MASTER_PASSWORD, "Blank", "/Blank Entry")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertNotEqual(rc, 0)
        self.assertIn("no username/password to fill", self._fifo_contents())
        self.assertNotIn("jseval", self._fifo_contents())

    def test_bounded_retries_then_gives_up(self):
        terms = ["nope1", "nope2", "nope3", "nope4", "nope5", "nope6"]
        self._queue_answers(MASTER_PASSWORD, *terms)
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertNotEqual(rc, 0)
        self.assertIn("no entry chosen after several searches", self._fifo_contents())


class MultipleDatabasesTests(CliFallbackTestCase):
    def setUp(self):
        super().setUp()
        self.db_b = os.path.join(self.tmpdir, "b.kdbx")
        # titles contain the registrable domain of the test host below
        # ("example.test") so the round-1 automatic search already
        # matches - the point of these tests is the database picker /
        # default_database, not the search-term editing (covered
        # elsewhere), so keep the search side of things trivial.
        _create_db(self.db_a, entries=[
            {"title": "example.test in A", "username": "a-user", "password": "a-pw", "url": None},
        ])
        _create_db(self.db_b, entries=[
            {"title": "example.test in B", "username": "b-user", "password": "b-pw", "url": None},
        ])
        cfg = kl.load_cli_config()
        kl.add_cli_database(cfg, self.db_a)
        kl.add_cli_database(cfg, self.db_b)
        kl.save_cli_config(cfg)

    def test_picker_shown_when_more_than_one_database(self):
        # database picker (-format i): index 1 = db_b; then master
        # password; then search/pick within db_b.
        self._queue_answers("1", MASTER_PASSWORD, "/example.test in B")
        rc, out, err = self._run("https://example.test/")
        self.assertEqual(rc, 0, err)
        self.assertIn('"b-user"', self._fifo_contents())
        self.assertIn('"b-pw"', self._fifo_contents())

    def test_database_picker_cancel(self):
        self._queue_answers("__CANCEL__")
        rc, out, err = self._run("https://example.test/")
        self.assertEqual(rc, 0, err)
        self.assertIn("cancelled", self._fifo_contents())

    def test_default_database_skips_the_picker(self):
        cfg = kl.load_cli_config()
        cfg["default_database"] = self.db_b
        kl.save_cli_config(cfg)
        self._queue_answers(MASTER_PASSWORD, "/example.test in B")  # no db-picker answer needed
        rc, out, err = self._run("https://example.test/")
        self.assertEqual(rc, 0, err)
        self.assertIn('"b-user"', self._fifo_contents())


class DiscoveryTests(CliFallbackTestCase):
    def test_single_discovered_database_used_and_persisted_silently(self):
        candidate = os.path.join(self.fake_home, "personal", "Keepass", "Passwords.kdbx")
        os.makedirs(os.path.dirname(candidate), exist_ok=True)
        _create_db(candidate, entries=[
            {"title": "example.test Found Entry", "username": "u1", "password": "p1", "url": None},
        ])

        self.assertEqual(kl.get_cli_databases(kl.load_cli_config()), [])
        self._queue_answers(MASTER_PASSWORD, "/example.test Found Entry")
        rc, out, err = self._run("https://example.test/")
        self.assertEqual(rc, 0, err)
        self.assertIn('"u1"', self._fifo_contents())
        self.assertEqual(kl.get_cli_databases(kl.load_cli_config()), [candidate])

    def test_multiple_discovered_databases_shows_picker(self):
        cand1 = os.path.join(self.fake_home, "personal", "Keepass", "Passwords.kdbx")
        cand2 = os.path.join(self.fake_home, "personal", "keepass", "Passwords.kdbx")  # case-different dir
        os.makedirs(os.path.dirname(cand1), exist_ok=True)
        os.makedirs(os.path.dirname(cand2), exist_ok=True)
        _create_db(cand1, entries=[
            {"title": "example.test in cand1", "username": "u1", "password": "p1", "url": None},
        ])
        _create_db(cand2, entries=[
            {"title": "example.test in cand2", "username": "u2", "password": "p2", "url": None},
        ])
        discovered = kl.discover_candidate_databases()
        self.assertEqual(sorted(discovered), sorted([cand1, cand2]))

        idx = discovered.index(cand2)
        self._queue_answers(str(idx), MASTER_PASSWORD, "/example.test in cand2")
        rc, out, err = self._run("https://example.test/")
        self.assertEqual(rc, 0, err)
        self.assertIn('"u2"', self._fifo_contents())
        self.assertEqual(kl.get_cli_databases(kl.load_cli_config()), [cand2])

    def test_scan_skips_dot_cache_and_trash(self):
        skipped_cache = os.path.join(self.fake_home, ".cache", "hidden.kdbx")
        skipped_trash = os.path.join(self.fake_home, ".local", "share", "Trash", "hidden.kdbx")
        os.makedirs(os.path.dirname(skipped_cache), exist_ok=True)
        os.makedirs(os.path.dirname(skipped_trash), exist_ok=True)
        Path(skipped_cache).write_bytes(b"not a real kdbx, presence is all that matters")
        Path(skipped_trash).write_bytes(b"not a real kdbx, presence is all that matters")
        self.assertEqual(kl.discover_candidate_databases(), [])

    def test_no_databases_anywhere_is_a_clean_error(self):
        rc, out, err = self._run("https://example.test/")
        self.assertNotEqual(rc, 0)
        self.assertIn("no KeePassXC database found", self._fifo_contents())

    def test_never_scans_outside_fake_home(self):
        # sanity check on the test fixture itself, not production code:
        # confirms QUTE_KEEPASSXC_HOME really did redirect discovery.
        real_home = os.path.expanduser("~")
        discovered = kl.discover_candidate_databases()
        for path in discovered:
            self.assertTrue(path.startswith(self.fake_home))
            self.assertFalse(path.startswith(os.path.join(real_home, "personal")))


class SelfHealingUrlTests(CliFallbackTestCase):
    def setUp(self):
        super().setUp()
        _create_db(self.db_a, entries=[
            {"title": "Ticketshop.example account", "username": "alice", "password": "hunter2", "url": None},
        ])
        cfg = kl.load_cli_config()
        kl.add_cli_database(cfg, self.db_a)
        kl.save_cli_config(cfg)

    def test_declining_the_url_add_leaves_the_entry_unchanged(self):
        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account", "No")
        rc, out, err = self._run("https://ticketshop.example/tickets")
        self.assertEqual(rc, 0, err)
        url_after = _show(self.db_a, "/Ticketshop.example account", ["URL"])[0]
        self.assertEqual(url_after, "")
        # remembered instead, since the URL wasn't added
        self.assertEqual(
            kl.get_remembered_cli(kl.load_cli_config(), "ticketshop.example"),
            {"database": self.db_a, "entry": "/Ticketshop.example account"},
        )

    def test_accepting_the_url_add_writes_the_origin_verified_independently(self):
        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account", "Yes")
        rc, out, err = self._run("https://ticketshop.example/tickets?ref=abc")
        self.assertEqual(rc, 0, err)
        fifo = self._fifo_contents()
        self.assertIn("added https://ticketshop.example/ as this entry's URL", fifo)
        # the reload-warning wording itself is asserted in
        # test_url_add_prompt_wording_mentions_the_reload_warning below
        # verified with a FRESH, independent keepassxc-cli call - never
        # trusting the userscript's own claim of success
        url_after = _show(self.db_a, "/Ticketshop.example account", ["URL"])[0]
        self.assertEqual(url_after, "https://ticketshop.example/")
        # nothing else about the entry changed
        login_after, pw_after = _show(self.db_a, "/Ticketshop.example account", ["UserName", "Password"])
        self.assertEqual(login_after, "alice")
        self.assertEqual(pw_after, "hunter2")
        # not remembered: the fast protocol path will match from now on
        self.assertIsNone(kl.get_remembered_cli(kl.load_cli_config(), "ticketshop.example"))

    def test_url_add_prompt_wording_mentions_the_reload_warning(self):
        # capture the rofi -p prompt text itself rather than trusting a
        # canned "Yes"/"No" answer to prove the prompt happened
        prompts_file = Path(self.tmpdir) / "prompts.txt"
        recorder_dir = Path(self.tmpdir) / "fake_rofi_prompt_recorder"
        recorder_dir.mkdir()
        (recorder_dir / "rofi").write_text(
            "#!/usr/bin/env python3\n"
            "import sys, os, pathlib\n"
            f"prompts_file = pathlib.Path({str(prompts_file)!r})\n"
            f"answers_file = os.environ['FAKE_ROFI_ANSWERS_FILE']\n"
            "argv = sys.argv[1:]\n"
            "p = argv[argv.index('-p') + 1] if '-p' in argv else ''\n"
            "with prompts_file.open('a') as f:\n"
            "    f.write(p + chr(10))\n"
            "queue = open(answers_file).read().split(chr(0))\n"
            "answer, rest = (queue[0], queue[1:]) if queue and queue[0] else ('', [])\n"
            "open(answers_file, 'w').write(chr(0).join(rest))\n"
            "if answer == '__CANCEL__':\n"
            "    sys.exit(1)\n"
            "sys.stdout.write(answer)\n"
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
                "FAKE_ROFI_ANSWERS_FILE": self.answers_path,
                "PATH": str(recorder_dir) + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()
        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account", "No")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        prompts = prompts_file.read_text()
        self.assertIn("reload", prompts.lower())
        self.assertIn("ticketshop.example", prompts)


class RememberedMappingTests(CliFallbackTestCase):
    def setUp(self):
        super().setUp()
        _create_db(self.db_a, entries=[
            {"title": "Ticketshop.example account", "username": "alice", "password": "hunter2", "url": None},
        ])
        cfg = kl.load_cli_config()
        kl.add_cli_database(cfg, self.db_a)
        kl.set_remembered_cli(cfg, "ticketshop.example", self.db_a, "/Ticketshop.example account")
        kl.save_cli_config(cfg)

    def test_remembered_mapping_skips_database_and_search_prompts(self):
        # only ONE answer queued: the master password. If the database
        # picker or the search/pick prompt were (wrongly) shown too, the
        # queue would run out and fake_rofi_scripted would exit 1.
        self._queue_answers(MASTER_PASSWORD)
        rc, out, err = self._run("https://ticketshop.example/anything")
        self.assertEqual(rc, 0, err)
        self.assertIn('"hunter2"', self._fifo_contents())

    def test_remembered_mapping_still_asks_for_the_master_password(self):
        self._queue_answers("__CANCEL__")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        self.assertIn("cancelled", self._fifo_contents())
        self.assertNotIn("jseval", self._fifo_contents())

    def test_forget_clears_the_remembered_mapping(self):
        rc, out, err = self._run("https://ticketshop.example/", extra_argv=["--forget"])
        self.assertEqual(rc, 0, err)
        self.assertIsNone(kl.get_remembered_cli(kl.load_cli_config(), "ticketshop.example"))
        # now the full flow (database already configured, so no picker;
        # master password; search/pick) is needed again
        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account")
        rc2, out2, err2 = self._run("https://ticketshop.example/")
        self.assertEqual(rc2, 0, err2)
        self.assertIn('"hunter2"', self._fifo_contents())

    def test_adding_the_url_from_a_remembered_mapping_drops_the_mapping(self):
        self._queue_answers(MASTER_PASSWORD, "Yes")
        rc, out, err = self._run("https://ticketshop.example/")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(kl.get_remembered_cli(kl.load_cli_config(), "ticketshop.example"))
        url_after = _show(self.db_a, "/Ticketshop.example account", ["URL"])[0]
        self.assertEqual(url_after, "https://ticketshop.example/")


class TotpTests(CliFallbackTestCase):
    def setUp(self):
        super().setUp()
        _create_db(self.db_a, entries=[
            {"title": "Ticketshop.example account", "username": "alice", "password": "hunter2", "url": None},
        ])
        cfg = kl.load_cli_config()
        kl.add_cli_database(cfg, self.db_a)
        kl.save_cli_config(cfg)

    def test_no_totp_configured_is_a_clean_error(self):
        # real keepassxc-cli, real "no TOTP set up" response - this
        # KeePassXC build's CLI has no flag to seed a TOTP secret via
        # `add`/`edit`, so the success path is verified separately
        # (test_totp_success_path_argv_and_fill below) with a stubbed
        # keepassxc-cli instead.
        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account")
        rc, out, err = self._run("https://ticketshop.example/", extra_argv=["--cli", "--totp"])
        self.assertNotEqual(rc, 0)
        self.assertIn("no TOTP key found", self._fifo_contents())

    def test_totp_success_path_argv_and_fill(self):
        # stubs keepassxc-cli's `show -t` response only; search/show for
        # username+password still go through the real binary via the
        # earlier tests. Confirms: the right argv shape (-t, db, entry)
        # and that a successful TOTP value reaches the fill JS.
        real_run = kpl.run_cli

        def fake_run_cli(cli_path, args_tail, password, timeout=25):
            if args_tail[:2] == ["show", "-t"]:
                self.assertEqual(args_tail, ["show", "-t", self.db_a, "/Ticketshop.example account"])
                self.assertEqual(password, MASTER_PASSWORD)
                return kpl.CliResult(True, ["123456"], "ok")
            return real_run(cli_path, args_tail, password, timeout=timeout)

        self._queue_answers(MASTER_PASSWORD, "/Ticketshop.example account")
        with mock.patch.object(kpl, "run_cli", fake_run_cli):
            rc, out, err = self._run("https://ticketshop.example/", extra_argv=["--cli", "--totp"])
        self.assertEqual(rc, 0, err)
        self.assertIn('"123456"', self._fifo_contents())


class ReachedNaturallyTests(unittest.TestCase):
    """Proves cli_fallback() is reached the ordinary way too - after the
    browser-protocol cascade AND the interactive site-search both fail -
    not just via --cli. Needs the mock protocol server too (unlike every
    other class in this file, which uses --cli and never touches it).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="keepassxc-cli-fallback-natural-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.socket_path = os.path.join(self.tmpdir, "mock-keepassxc.sock")
        self.assertNotEqual(self.socket_path, REAL_SOCKET)
        self.server = MockKeepassXCServer(self.socket_path)
        self.server.start()
        self.addCleanup(self.server.stop)

        self.data_dir = os.path.join(self.tmpdir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.fifo_path = os.path.join(self.tmpdir, "fifo")
        Path(self.fifo_path).touch()
        self.aliases_dir = os.path.join(self.tmpdir, "keepassxc-login-aliases")
        self.cli_config_path = os.path.join(self.tmpdir, "keepassxc-login.toml")
        self.fake_home = os.path.join(self.tmpdir, "fake-home")
        os.makedirs(self.fake_home, exist_ok=True)
        self.answers_path = os.path.join(self.tmpdir, "rofi_answers.txt")
        Path(self.answers_path).touch()

        self.db_a = os.path.join(self.tmpdir, "a.kdbx")
        _create_db(self.db_a, entries=[
            {"title": "Ticketshop.example account", "username": "alice", "password": "hunter2", "url": None},
        ])
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "QUTE_DATA_DIR": self.data_dir,
                "QUTE_FIFO": self.fifo_path,
                "QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir,
                "QUTE_KEEPASSXC_CLI_CONFIG": self.cli_config_path,
                "QUTE_KEEPASSXC_HOME": self.fake_home,
                "FAKE_ROFI_ANSWERS_FILE": self.answers_path,
                "PATH": str(FIXTURES / "fake_rofi_scripted") + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        cfg = kl.load_cli_config()
        kl.add_cli_database(cfg, self.db_a)
        kl.save_cli_config(cfg)

    @unittest.skipUnless(KEEPASSXC_CLI, "keepassxc-cli not installed")
    def test_reached_after_cascade_and_search_prompt_both_fail(self):
        # the mock protocol server has ZERO entries - cascade fails; the
        # interactive search prompt (no -filter/-format/-password in its
        # rofi call) consumes the first queued answer as the typed term,
        # which also matches nothing over the protocol - only then does
        # the CLI fallback run, finding the (protocol-invisible, no-URL)
        # entry by title.
        Path(self.answers_path).write_text(
            "\x00".join(["irrelevant-term", MASTER_PASSWORD, "/Ticketshop.example account"])
        )
        argv = [
            "https://ticketshop.example/tickets", "--socket", self.socket_path, "--insecure",
            "--cli-path", KEEPASSXC_CLI,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = kpl.main(argv)
        self.assertEqual(rc, 0, err.getvalue())
        fifo = Path(self.fifo_path).read_text()
        self.assertIn('"alice"', fifo)
        self.assertIn('"hunter2"', fifo)
        self.assertEqual(self.server.saved_logins, [])  # protocol path never wrote anything


if __name__ == "__main__":
    unittest.main()
