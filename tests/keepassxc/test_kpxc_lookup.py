#!/usr/bin/env python3
"""Unit tests for kpxc_lookup.py: registrable-domain math, the cascade's
query order/dedup/bounding, and alias storage (including --forget).

Pure-Python, no sockets, no rofi, no real KeePassXC - QUTE_KEEPASSXC_ALIASES_DIR
is set to a fresh temp dir for every test so ~/.local/share/qutebrowser/
keepassxc-login/aliases.json is never read or written.
"""
import importlib
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
sys.path.insert(0, str(SCRIPT_DIR))

import kpxc_lookup as kl  # noqa: E402


class RegistrableDomainTests(unittest.TestCase):
    def test_simple_two_label_domain(self):
        self.assertEqual(kl.registrable_domain("example.com"), "example.com")

    def test_subdomain_strips_to_registrable(self):
        self.assertEqual(kl.registrable_domain("shop.example.com"), "example.com")
        self.assertEqual(kl.registrable_domain("a.b.c.example.com"), "example.com")

    def test_builtin_multi_part_suffix_co_uk(self):
        self.assertEqual(kl.registrable_domain("shop.mysite.co.uk"), "mysite.co.uk")
        self.assertEqual(kl.registrable_domain("mysite.co.uk"), "mysite.co.uk")

    def test_builtin_multi_part_suffix_com_br(self):
        self.assertEqual(kl.registrable_domain("loja.exemplo.com.br"), "exemplo.com.br")

    def test_all_task_required_suffixes(self):
        cases = {
            "a.co.uk": "a.co.uk",
            "a.com.br": "a.com.br",
            "a.com.pt": "a.com.pt",
            "a.org.br": "a.org.br",
            "a.net.br": "a.net.br",
            "a.gov.br": "a.gov.br",
            "a.com.au": "a.com.au",
            "a.co.jp": "a.co.jp",
            "a.gov.it": "a.gov.it",
            "a.edu.it": "a.edu.it",
        }
        for host, expected in cases.items():
            with self.subTest(host=host):
                self.assertEqual(kl.registrable_domain("sub." + host), expected)

    def test_ip_address_returned_unchanged(self):
        self.assertEqual(kl.registrable_domain("192.168.1.1"), "192.168.1.1")

    def test_localhost_returned_unchanged(self):
        self.assertEqual(kl.registrable_domain("localhost"), "localhost")

    def test_empty_host(self):
        self.assertEqual(kl.registrable_domain(""), "")


class DomainLadderTests(unittest.TestCase):
    def test_bare_registrable_domain_still_yields_itself(self):
        # so the www-variant of the bare domain still gets a chance (the
        # only case where cascade step (e) contributes something for a
        # host that's already at the registrable domain).
        self.assertEqual(kl.domain_ladder("example.com"), ["example.com"])

    def test_one_level_subdomain(self):
        self.assertEqual(kl.domain_ladder("shop.example.com"), ["example.com"])

    def test_multi_level_subdomain_descends_one_label_at_a_time(self):
        self.assertEqual(
            kl.domain_ladder("checkout.shop.example.com"),
            ["shop.example.com", "example.com"],
        )

    def test_stops_at_registrable_domain_for_multi_part_suffix(self):
        self.assertEqual(
            kl.domain_ladder("a.b.mysite.co.uk"),
            ["b.mysite.co.uk", "mysite.co.uk"],
        )
        # never descends into "co.uk" alone
        self.assertNotIn("co.uk", kl.domain_ladder("a.b.mysite.co.uk"))

    def test_ip_address_has_no_ladder(self):
        self.assertEqual(kl.domain_ladder("192.168.1.1"), [])

    def test_single_label_host_has_no_ladder(self):
        self.assertEqual(kl.domain_ladder("localhost"), [])


class UrlCascadeCandidatesTests(unittest.TestCase):
    def test_full_url_with_path_yields_b_then_c_then_d(self):
        cands = kl.url_cascade_candidates("https://shop.example.com/cart?x=1")
        self.assertEqual(cands[0], "https://shop.example.com/cart?x=1")  # b
        self.assertEqual(cands[1], "https://shop.example.com/")  # c: origin
        self.assertEqual(cands[2], "http://shop.example.com/")  # d: other scheme
        # e: parent domain ladder, bare + www, https only
        self.assertIn("https://example.com/", cands[3:])
        self.assertIn("https://www.example.com/", cands[3:])

    def test_bare_origin_url_does_not_duplicate_b_and_c(self):
        cands = kl.url_cascade_candidates("https://example.com/")
        # no separate "b" entry distinct from the origin when there's
        # nothing beyond the origin in the URL
        self.assertEqual(cands[0], "https://example.com/")
        self.assertEqual(cands.count("https://example.com/"), 1)

    def test_scheme_flip_preserves_port(self):
        cands = kl.url_cascade_candidates("https://example.com:8443/login")
        self.assertIn("https://example.com:8443/", cands)
        self.assertIn("http://example.com:8443/", cands)

    def test_ladder_entries_always_trailing_slash_https_only(self):
        cands = kl.url_cascade_candidates("https://checkout.shop.example.com/pay")
        ladder_part = cands[3:]
        for c in ladder_part:
            self.assertTrue(c.startswith("https://"))
            self.assertTrue(c.endswith("/"))

    def test_no_host_returns_empty(self):
        self.assertEqual(kl.url_cascade_candidates("not a url"), [])


class LoginSubdomainGuessTests(unittest.TestCase):
    """Step (f): the registrable domain with each of
    LOGIN_SUBDOMAIN_PREFIXES, tried last, after steps (b)-(e)."""

    def test_guess_candidates_use_the_registrable_domain_https_only(self):
        cands = kl.login_subdomain_guess_candidates("checkout.shop.example.com")
        self.assertEqual(len(cands), len(kl.LOGIN_SUBDOMAIN_PREFIXES))
        for prefix, cand in zip(kl.LOGIN_SUBDOMAIN_PREFIXES, cands):
            self.assertEqual(cand, f"https://{prefix}.example.com/")

    def test_prefix_list_is_editable_and_reflected_immediately(self):
        original = kl.LOGIN_SUBDOMAIN_PREFIXES
        try:
            kl.LOGIN_SUBDOMAIN_PREFIXES = ["custom-prefix"]
            self.assertEqual(
                kl.login_subdomain_guess_candidates("example.com"),
                ["https://custom-prefix.example.com/"],
            )
        finally:
            kl.LOGIN_SUBDOMAIN_PREFIXES = original

    def test_no_guesses_for_ip_or_single_label_host(self):
        self.assertEqual(kl.login_subdomain_guess_candidates("192.168.1.1"), [])
        self.assertEqual(kl.login_subdomain_guess_candidates("localhost"), [])

    def test_url_cascade_candidates_appends_guesses_after_steps_b_to_e(self):
        cands = kl.url_cascade_candidates("https://www.example.com/account")
        base, _host = kl._base_cascade_candidates("https://www.example.com/account")
        self.assertEqual(cands[: len(base)], base)
        guesses_seen = cands[len(base):]
        # every guess candidate appears, in LOGIN_SUBDOMAIN_PREFIXES order,
        # minus whatever the base steps already produced (e.g. "www")
        expected_guesses = [
            c for c in kl.login_subdomain_guess_candidates("www.example.com") if c not in base
        ]
        self.assertEqual(guesses_seen, expected_guesses)
        self.assertIn("https://accounts.example.com/", cands)
        self.assertIn("https://login.example.com/", cands)

    def test_deduplicated_against_base_steps(self):
        # "www" is in LOGIN_SUBDOMAIN_PREFIXES too, and domain_ladder()'s
        # own www-variant (step e) already produces the same URL for a
        # bare registrable-domain host - must not appear twice.
        cands = kl.url_cascade_candidates("https://example.com/")
        self.assertEqual(cands.count("https://www.example.com/"), 1)

    def test_bounded_even_with_a_deep_host(self):
        deep_host = ".".join(f"level{i}" for i in range(15)) + ".example.com"
        cands = kl.url_cascade_candidates(f"https://{deep_host}/path")
        self.assertLessEqual(len(cands), kl.MAX_CANDIDATES)
        # the guesses must still all make it in within that bound
        for prefix in kl.LOGIN_SUBDOMAIN_PREFIXES:
            self.assertIn(f"https://{prefix}.example.com/", cands)


class IsLoginSubdomainGuessTests(unittest.TestCase):
    def test_true_when_only_reachable_via_the_guess(self):
        matched = "https://accounts.example.com/"
        self.assertTrue(kl.is_login_subdomain_guess(matched, "https://www.example.com/account"))

    def test_false_when_matched_url_is_the_page_itself(self):
        url = "https://www.example.com/account"
        self.assertFalse(kl.is_login_subdomain_guess("https://www.example.com/", url))

    def test_false_when_matched_url_came_from_the_parent_domain_ladder(self):
        url = "https://app.example.com/"
        self.assertFalse(kl.is_login_subdomain_guess("https://example.com/", url))

    def test_false_for_coincidental_string_overlap_with_a_base_step(self):
        # "www" is both a domain_ladder() www-variant AND a
        # LOGIN_SUBDOMAIN_PREFIXES entry for a bare registrable-domain
        # host - it's reachable via step (e) already, so it's not a
        # "guess-only" match even though it also appears in the guess list.
        url = "https://example.com/"
        self.assertFalse(kl.is_login_subdomain_guess("https://www.example.com/", url))

    def test_false_for_empty_matched_url(self):
        self.assertFalse(kl.is_login_subdomain_guess("", "https://example.com/"))
        self.assertFalse(kl.is_login_subdomain_guess(None, "https://example.com/"))


class TermCascadeCandidatesTests(unittest.TestCase):
    def test_bare_domain_term_expands_like_a_url(self):
        cands = kl.term_cascade_candidates("example.com")
        self.assertIn("https://example.com/", cands)
        self.assertIn("http://example.com/", cands)
        self.assertIn("https://www.example.com/", cands)

    def test_term_with_scheme_is_treated_as_a_url(self):
        cands = kl.term_cascade_candidates("https://accounts.example.com/login")
        self.assertEqual(cands[0], "https://accounts.example.com/login")

    def test_term_with_path_no_scheme(self):
        cands = kl.term_cascade_candidates("example.com/account")
        self.assertIn("https://example.com/account", cands)
        self.assertIn("https://example.com/", cands)

    def test_empty_term(self):
        self.assertEqual(kl.term_cascade_candidates(""), [])
        self.assertEqual(kl.term_cascade_candidates(None), [])

    def test_login_subdomain_prefixes_applied_to_a_typed_bare_domain(self):
        # coordinator requirement: typing "example.com" in the "search
        # which site?" fallback also probes the guessed login subdomains.
        cands = kl.term_cascade_candidates("example.com")
        for prefix in kl.LOGIN_SUBDOMAIN_PREFIXES:
            self.assertIn(f"https://{prefix}.example.com/", cands)

    def test_login_subdomain_prefixes_applied_to_a_typed_url(self):
        cands = kl.term_cascade_candidates("https://example.com/")
        self.assertIn("https://accounts.example.com/", cands)


class BuildCascadeTests(unittest.TestCase):
    def test_alias_candidates_come_before_page_url_candidates(self):
        cascade = kl.build_cascade("https://app.example.com/dashboard", alias_term="other-site.example")
        alias_first = kl.term_cascade_candidates("other-site.example")[0]
        self.assertEqual(cascade[0], alias_first)
        # the real page URL's own exact form still appears somewhere after
        self.assertIn("https://app.example.com/dashboard", cascade)

    def test_no_alias_uses_only_page_url_cascade(self):
        cascade = kl.build_cascade("https://example.com/", alias_term=None)
        self.assertEqual(cascade, kl.url_cascade_candidates("https://example.com/"))

    def test_deduplicated(self):
        cascade = kl.build_cascade("https://example.com/", alias_term="example.com")
        self.assertEqual(len(cascade), len(set(cascade)))

    def test_bounded(self):
        # a deep subdomain chain would otherwise generate a lot of ladder
        # entries; the total must stay bounded regardless.
        deep_host = ".".join(["level%d" % i for i in range(20)]) + ".example.com"
        cascade = kl.build_cascade(f"https://{deep_host}/", alias_term="another.example.org")
        self.assertLessEqual(len(cascade), kl.MAX_CANDIDATES)


class AliasStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kpxc-lookup-alias-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.aliases_dir = os.path.join(self.tmpdir, "keepassxc-login")
        self._patch = mock.patch.dict(os.environ, {"QUTE_KEEPASSXC_ALIASES_DIR": self.aliases_dir})
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_get_alias_missing_file_returns_none(self):
        self.assertIsNone(kl.get_alias("example.com"))

    def test_save_then_get_alias(self):
        kl.save_alias("app.example.com", "example.com")
        self.assertEqual(kl.get_alias("app.example.com"), "example.com")

    def test_aliases_file_and_dir_permissions(self):
        kl.save_alias("app.example.com", "example.com")
        path = kl.aliases_path()
        self.assertTrue(path.exists())
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
        self.assertEqual(oct(path.parent.stat().st_mode)[-3:], "700")

    def test_aliases_file_contains_no_secrets_just_host_to_term(self):
        kl.save_alias("app.example.com", "example.com")
        raw = kl.aliases_path().read_text()
        self.assertNotIn("password", raw.lower())
        data = kl.load_aliases()
        self.assertEqual(data, {"app.example.com": "example.com"})

    def test_overwriting_an_alias_replaces_it(self):
        kl.save_alias("app.example.com", "old-alias.example")
        kl.save_alias("app.example.com", "new-alias.example")
        self.assertEqual(kl.get_alias("app.example.com"), "new-alias.example")

    def test_multiple_hosts_independent(self):
        kl.save_alias("a.example.com", "site-a.example")
        kl.save_alias("b.example.com", "site-b.example")
        self.assertEqual(kl.get_alias("a.example.com"), "site-a.example")
        self.assertEqual(kl.get_alias("b.example.com"), "site-b.example")

    def test_forget_removes_alias_and_returns_true(self):
        kl.save_alias("app.example.com", "example.com")
        self.assertTrue(kl.forget_alias("app.example.com"))
        self.assertIsNone(kl.get_alias("app.example.com"))

    def test_forget_nonexistent_alias_returns_false_and_is_a_noop(self):
        self.assertFalse(kl.forget_alias("never-set.example.com"))

    def test_forget_leaves_other_hosts_untouched(self):
        kl.save_alias("a.example.com", "site-a.example")
        kl.save_alias("b.example.com", "site-b.example")
        kl.forget_alias("a.example.com")
        self.assertIsNone(kl.get_alias("a.example.com"))
        self.assertEqual(kl.get_alias("b.example.com"), "site-b.example")

    def test_load_aliases_survives_corrupt_file(self):
        d = kl.aliases_dir()
        d.mkdir(parents=True, exist_ok=True)
        kl.aliases_path().write_text("not json{{{")
        self.assertEqual(kl.load_aliases(), {})
        self.assertIsNone(kl.get_alias("app.example.com"))


class LookupCascadeAgainstFakeKpTests(unittest.TestCase):
    """lookup() itself, against a tiny fake `kp` (no socket at all) so the
    cascade-stops-at-first-non-empty-result contract is unit-testable
    without a mock server.
    """

    class _FakeKp:
        def __init__(self, entries_by_url):
            self.entries_by_url = entries_by_url
            self.queried = []

        def get_logins(self, url):
            self.queried.append(url)
            return self.entries_by_url.get(url, [])

    def test_stops_at_first_non_empty_result(self):
        kp = self._FakeKp({"https://example.com/": [{"login": "u", "password": "p", "uuid": "1"}]})
        matched_url, entries = kl.lookup(kp, "https://shop.example.com/cart")
        self.assertEqual(matched_url, "https://example.com/")
        self.assertEqual(len(entries), 1)
        # didn't need to try the www-variant after finding a match
        self.assertNotIn("https://www.example.com/", kp.queried)

    def test_alias_tried_before_page_url(self):
        kp = self._FakeKp({"https://other.example/": [{"login": "u", "password": "p", "uuid": "1"}]})
        matched_url, entries = kl.lookup(kp, "https://app.example.com/", alias_term="other.example")
        self.assertEqual(matched_url, "https://other.example/")
        self.assertTrue(entries)

    def test_nothing_matches_anywhere(self):
        kp = self._FakeKp({})
        matched_url, entries = kl.lookup(kp, "https://example.com/")
        self.assertIsNone(matched_url)
        self.assertEqual(entries, [])

    def test_get_logins_exception_is_treated_as_empty_and_cascade_continues(self):
        class _FlakyKp:
            def __init__(self):
                self.calls = 0

            def get_logins(self, url):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("socket hiccup")
                return [{"login": "u", "password": "p", "uuid": "1"}] if url == "http://example.com/" else []

        kp = _FlakyKp()
        matched_url, entries = kl.lookup(kp, "https://example.com/")
        self.assertEqual(matched_url, "http://example.com/")
        self.assertTrue(entries)


if __name__ == "__main__":
    unittest.main()
