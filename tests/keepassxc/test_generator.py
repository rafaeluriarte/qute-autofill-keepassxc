#!/usr/bin/env python3
"""Unit tests for keepassxc-newpass's password generator.

Pure unittest, no KeePassXC, no qutebrowser, no network. Does run the real
`keepassxc-cli generate` (it needs no database / master password and
touches no KeePassXC state), plus exercises the Python `secrets` fallback
path directly and via a forced "keepassxc-cli unavailable" scenario.

Run with:  python3 -m unittest discover -s . -p 'test_*.py' -v
"""
import importlib.machinery
import importlib.util
import re
import shutil
import string
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
_repo_scripts = TESTS_DIR.parent.parent / "userscripts"
SCRIPT_DIR = _repo_scripts if _repo_scripts.is_dir() else TESTS_DIR.parent
SCRIPT_PATH = SCRIPT_DIR / "keepassxc-newpass"


def _load_module():
    loader = importlib.machinery.SourceFileLoader("keepassxc_newpass", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


kpn = _load_module()

KEEPASSXC_CLI_AVAILABLE = shutil.which("keepassxc-cli") is not None


class ClampLengthTests(unittest.TestCase):
    def test_no_constraints(self):
        self.assertEqual(kpn.clamp_length(14, None, None), 14)

    def test_maxlength_below_requested(self):
        self.assertEqual(kpn.clamp_length(24, None, 16), 16)

    def test_minlength_above_requested(self):
        self.assertEqual(kpn.clamp_length(8, 12, None), 12)

    def test_maxlength_wins_over_conflicting_minlength(self):
        # minlength (20) > maxlength (16): field is contradictory, maxlength
        # is the hard field-capacity limit so it must win.
        self.assertEqual(kpn.clamp_length(14, 20, 16), 16)

    def test_never_below_one(self):
        self.assertEqual(kpn.clamp_length(14, None, 0), 14)  # maxlength=0 ignored as "no limit"


class PasswordRulesParsingTests(unittest.TestCase):
    def test_empty(self):
        r = kpn.parse_password_rules(None)
        self.assertIsNone(r["minlength"])
        self.assertIsNone(r["maxlength"])
        self.assertIsNone(r["required"])

    def test_full_directive(self):
        r = kpn.parse_password_rules("minlength: 8; maxlength: 20; required: upper, lower, digit, special;")
        self.assertEqual(r["minlength"], 8)
        self.assertEqual(r["maxlength"], 20)
        self.assertEqual(r["required"], {"upper", "lower", "digit", "special"})

    def test_partial_required_classes(self):
        r = kpn.parse_password_rules("required: lower, digit;")
        self.assertEqual(r["required"], {"lower", "digit"})

    def test_unknown_directives_ignored(self):
        r = kpn.parse_password_rules("max-consecutive: 3; allowed: ascii-printable; minlength: 10;")
        self.assertEqual(r["minlength"], 10)
        self.assertIsNone(r["required"])


class ClassFallbackSequenceTests(unittest.TestCase):
    def test_drops_special_then_digit_then_upper(self):
        seq = kpn._class_fallback_sequence({"lower", "upper", "digit", "special"})
        self.assertEqual(seq[0], frozenset({"lower", "upper", "digit", "special"}))
        self.assertEqual(seq[-1], frozenset({"lower"}))
        # each subsequent set is a strict subset of the previous one
        for a, b in zip(seq, seq[1:]):
            self.assertTrue(b < a)

    def test_never_empty_even_for_single_class(self):
        seq = kpn._class_fallback_sequence({"digit"})
        self.assertEqual(seq, [frozenset({"digit"})])


class BuildKeepassxcCliArgsTests(unittest.TestCase):
    """The exact constraint -> argv mapping, without running the binary."""

    def test_all_four_classes(self):
        args = kpn.build_keepassxc_cli_args(14, {"lower", "upper", "digit", "special"}, exclude_similar=True)
        self.assertIn("-l", args)
        self.assertIn("-U", args)
        self.assertIn("-n", args)
        self.assertIn("-s", args)
        self.assertIn("--every-group", args)
        self.assertIn("--exclude-similar", args)
        self.assertIn("-L", args)
        self.assertEqual(args[args.index("-L") + 1], "14")
        # symbols are restricted to the conservative whitelist via -x
        self.assertIn("-x", args)
        excluded = args[args.index("-x") + 1]
        self.assertTrue(set(excluded).isdisjoint(set(kpn.CONSERVATIVE_SYMBOLS)))
        self.assertTrue(set(excluded).issubset(set(string.punctuation)))

    def test_single_class_has_no_every_group(self):
        args = kpn.build_keepassxc_cli_args(10, {"lower"}, exclude_similar=False)
        self.assertIn("-l", args)
        self.assertNotIn("-U", args)
        self.assertNotIn("-n", args)
        self.assertNotIn("-s", args)
        self.assertNotIn("--every-group", args)
        self.assertNotIn("--exclude-similar", args)
        self.assertNotIn("-x", args)  # no special class -> nothing to exclude by default

    def test_alnum_classes_no_symbol_exclusion(self):
        args = kpn.build_keepassxc_cli_args(10, {"lower", "upper", "digit"})
        self.assertNotIn("-s", args)
        self.assertNotIn("-x", args)

    def test_length_is_first_class_argument_position_stable(self):
        args = kpn.build_keepassxc_cli_args(9, {"digit"})
        self.assertEqual(args[:4], ["generate", "-q", "-L", "9"])


class RunKeepassxcCliGenerateTests(unittest.TestCase):
    """Runs the *real* keepassxc-cli binary (touches no database)."""

    @unittest.skipUnless(KEEPASSXC_CLI_AVAILABLE, "keepassxc-cli not installed")
    def test_generates_requested_length(self):
        argv = kpn.build_keepassxc_cli_args(16, {"lower", "upper", "digit"})
        pw = kpn.run_keepassxc_cli_generate(argv)
        self.assertIsNotNone(pw)
        self.assertEqual(len(pw), 16)
        self.assertTrue(set(pw).issubset(set(string.ascii_letters + string.digits)))

    @unittest.skipUnless(KEEPASSXC_CLI_AVAILABLE, "keepassxc-cli not installed")
    def test_too_short_for_four_classes_returns_none_not_raises(self):
        # Empirically verified floor for our own (conservative-symbols,
        # exclude-similar) argv: keepassxc-cli refuses length=6 with all
        # four classes ("Invalid password generator after applying all
        # options") but accepts length=7. This must come back as None, not
        # an exception - generate_password()'s class-dropping fallback
        # relies on that. (The floor position is sensitive to exactly which
        # characters are excluded via -x; with the *unrestricted* -s pool
        # and no -x at all the same four-class combination instead needs
        # length>=9 - both are keepassxc-cli implementation details we only
        # rely on indirectly, via None-on-failure.)
        argv_too_short = kpn.build_keepassxc_cli_args(6, {"lower", "upper", "digit", "special"})
        self.assertIsNone(kpn.run_keepassxc_cli_generate(argv_too_short))
        argv_ok = kpn.build_keepassxc_cli_args(7, {"lower", "upper", "digit", "special"})
        self.assertIsNotNone(kpn.run_keepassxc_cli_generate(argv_ok))

    def test_missing_binary_returns_none(self):
        argv = ["generate", "-q", "-L", "10", "-l"]
        pw = kpn.run_keepassxc_cli_generate(argv, cli_path="/no/such/keepassxc-cli-binary")
        self.assertIsNone(pw)


class GenerateWithPythonSecretsTests(unittest.TestCase):
    def test_length_honoured(self):
        pw = kpn.generate_with_python_secrets(20, {"lower", "upper", "digit", "special"})
        self.assertEqual(len(pw), 20)

    def test_all_requested_classes_present(self):
        pw = kpn.generate_with_python_secrets(24, {"lower", "upper", "digit", "special"})
        self.assertTrue(any(c in string.ascii_lowercase for c in pw))
        self.assertTrue(any(c in string.ascii_uppercase for c in pw))
        self.assertTrue(any(c in string.digits for c in pw))
        self.assertTrue(any(c in kpn.CONSERVATIVE_SYMBOLS for c in pw))

    def test_charset_restricted_to_requested_classes(self):
        pw = kpn.generate_with_python_secrets(30, {"lower", "digit"})
        allowed = set(string.ascii_lowercase) | set(string.digits)
        self.assertTrue(set(pw).issubset(allowed))

    def test_exclude_similar_removes_ambiguous_chars(self):
        pw = kpn.generate_with_python_secrets(200, {"lower", "upper", "digit"}, exclude_similar=True)
        self.assertTrue(set(pw).isdisjoint(kpn.AMBIGUOUS_CHARS))

    def test_short_length_still_returns_that_length(self):
        # length shorter than the number of required classes: still returns
        # `length` characters (truncates the "one of each" guarantee).
        pw = kpn.generate_with_python_secrets(2, {"lower", "upper", "digit", "special"})
        self.assertEqual(len(pw), 2)


class GeneratePasswordIntegrationTests(unittest.TestCase):
    """The orchestrator: constraints -> generated password, end to end."""

    def test_default_length(self):
        pw, meta = kpn.generate_password()
        self.assertEqual(len(pw), kpn.DEFAULT_LENGTH)
        self.assertEqual(meta["length"], kpn.DEFAULT_LENGTH)

    def test_maxlength_is_honoured(self):
        pw, meta = kpn.generate_password(requested_length=24, maxlength=16)
        self.assertEqual(len(pw), 16)

    def test_minlength_is_honoured(self):
        pw, meta = kpn.generate_password(requested_length=8, minlength=20)
        self.assertEqual(len(pw), 20)

    def test_prefers_keepassxc_cli_when_available(self):
        if not KEEPASSXC_CLI_AVAILABLE:
            self.skipTest("keepassxc-cli not installed")
        pw, meta = kpn.generate_password(requested_length=14)
        self.assertEqual(meta["source"], "keepassxc-cli")
        self.assertEqual(len(pw), 14)

    def test_falls_back_to_python_secrets_when_cli_missing(self):
        pw, meta = kpn.generate_password(requested_length=14, cli_path="/no/such/keepassxc-cli-binary")
        self.assertTrue(meta["source"].startswith("python-secrets"))
        self.assertIn("keepassxc-cli unavailable", meta["source"])
        self.assertEqual(len(pw), 14)

    def test_alnum_only_pattern_drops_symbols(self):
        pattern = r"[A-Za-z0-9]{8}"
        pw, meta = kpn.generate_password(requested_length=8, minlength=8, maxlength=8, pattern=pattern)
        self.assertEqual(len(pw), 8)
        self.assertRegex(pw, r"\A[A-Za-z0-9]{8}\Z")
        self.assertTrue(re.fullmatch(pattern, pw))

    def test_pattern_matching_with_full_classes(self):
        # A pattern that requires at least one of each of the four classes.
        pattern = r"(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*\-_=+.,?~]).{12,20}"
        pw, meta = kpn.generate_password(requested_length=16, pattern=pattern)
        self.assertTrue(re.fullmatch(pattern, pw), f"{pw!r} does not match {pattern!r}")

    def test_passwordrules_required_classes_are_honoured(self):
        pw, meta = kpn.generate_password(
            requested_length=16, passwordrules="minlength: 12; maxlength: 18; required: lower, digit;"
        )
        self.assertTrue(12 <= len(pw) <= 18)
        self.assertTrue(any(c in string.ascii_lowercase for c in pw))
        self.assertTrue(any(c in string.digits for c in pw))

    def test_impossible_pattern_falls_back_without_raising(self):
        # A pattern nothing bounded-random generation will ever satisfy;
        # must fall back to the alnum last resort rather than raise/hang.
        pw, meta = kpn.generate_password(requested_length=10, pattern=r"THIS_EXACT_STRING_ONLY")
        self.assertEqual(len(pw), 10)
        self.assertEqual(meta["source"], "python-secrets-fallback-alnum")
        self.assertIsNotNone(meta.get("warning"))

    def test_generation_never_prints_the_password(self):
        # generate_password/run_keepassxc_cli_generate must not print/log
        # anything - capture stdout/stderr and assert they're silent.
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            pw, meta = kpn.generate_password(requested_length=14)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn(pw, out.getvalue())


class ParsePasswordFieldsTests(unittest.TestCase):
    def test_signup_form_new_password_and_confirm(self):
        html = """
        <html><body><form>
        <input type="email" name="email" autocomplete="email">
        <input type="password" name="password" autocomplete="new-password" minlength="10" maxlength="16">
        <input type="password" name="confirm" autocomplete="new-password" minlength="10" maxlength="16">
        </form></body></html>
        """
        c = kpn.parse_password_fields(html)
        self.assertTrue(c["has_new_password_field"])
        self.assertFalse(c["has_current_password_field"])
        self.assertTrue(c["has_confirm_pair"])
        self.assertEqual(c["minlength"], 10)
        self.assertEqual(c["maxlength"], 16)

    def test_change_password_form_skips_current_password(self):
        html = """
        <input type="password" name="current" autocomplete="current-password">
        <input type="password" name="new" autocomplete="new-password" minlength="12">
        <input type="password" name="confirm" autocomplete="new-password" minlength="12">
        """
        c = kpn.parse_password_fields(html)
        self.assertTrue(c["has_current_password_field"])
        self.assertTrue(c["has_new_password_field"])
        self.assertEqual(c["password_field_count"], 3)

    def test_pattern_and_passwordrules_extracted(self):
        html = """
        <input type="password" name="p" autocomplete="new-password"
               pattern="[A-Za-z0-9]{8,16}" passwordrules="minlength: 8; required: lower, digit;">
        """
        c = kpn.parse_password_fields(html)
        self.assertEqual(c["pattern"], "[A-Za-z0-9]{8,16}")
        self.assertEqual(c["passwordrules"], "minlength: 8; required: lower, digit;")

    def test_no_password_field(self):
        c = kpn.parse_password_fields("<input type='text' name='q'>")
        self.assertFalse(c["has_new_password_field"])

    def test_empty_html_does_not_raise(self):
        c = kpn.parse_password_fields("")
        self.assertFalse(c["has_new_password_field"])


if __name__ == "__main__":
    unittest.main()
