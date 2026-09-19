#!/usr/bin/env python3
"""Unit tests for the Python side of the qutebrowser `autofill` userscript:
per-site settings resolution (requirement #4) and the misses-report grouping
(requirement #5). The main script has no .py extension (it's a userscript,
invoked directly by qutebrowser), so it's imported here via importlib rather
than a normal `import`.

Run with:  python3 test_site_settings.py
(or: python3 -m unittest test_site_settings -v)
"""
import importlib.machinery
import importlib.util
import pathlib
import sys
import unittest

_here = pathlib.Path(__file__).resolve().parent
_repo = _here.parent.parent / "userscripts" / "autofill"
SCRIPT_PATH = _repo if _repo.exists() else pathlib.Path.home() / ".local" / "share" / "qutebrowser" / "userscripts" / "autofill"

# The script has no .py suffix (it's a userscript qutebrowser execs
# directly), so the suffix-based loader lookup in spec_from_file_location()
# can't find a matching loader on its own -- an explicit SourceFileLoader is
# needed, otherwise spec_from_file_location() silently returns None.
_loader = importlib.machinery.SourceFileLoader("autofill_userscript", str(SCRIPT_PATH))
spec = importlib.util.spec_from_file_location("autofill_userscript", SCRIPT_PATH, loader=_loader)
autofill = importlib.util.module_from_spec(spec)
sys.modules["autofill_userscript"] = autofill
spec.loader.exec_module(autofill)


class HostCandidatesTest(unittest.TestCase):
    def test_simple_host(self):
        self.assertEqual(autofill.host_candidates("amazon.it"), ["amazon.it", "it"])

    def test_subdomain_chain(self):
        self.assertEqual(
            autofill.host_candidates("checkout.amazon.it"),
            ["checkout.amazon.it", "amazon.it", "it"],
        )

    def test_empty(self):
        self.assertEqual(autofill.host_candidates(""), [])
        self.assertEqual(autofill.host_candidates(None), [])

    def test_trailing_dot_and_case(self):
        self.assertEqual(
            autofill.host_candidates("Checkout.Amazon.IT."),
            ["checkout.amazon.it", "amazon.it", "it"],
        )


class ResolveSiteSettingsTest(unittest.TestCase):
    def setUp(self):
        self.sites = {
            "amazon.it": {"profile": "italia", "skip": ["birth_date"], "overwrite": False},
            "www.amazon.it": {"profile": "lavoro"},
            "shop.example.com": {"skip": ["phone"]},
        }

    def test_exact_match_wins_over_parent(self):
        got = autofill.resolve_site_settings("www.amazon.it", self.sites)
        self.assertEqual(got.get("profile"), "lavoro")

    def test_falls_back_to_parent_domain(self):
        # "checkout.amazon.it" has no entry of its own -> falls back to "amazon.it"
        got = autofill.resolve_site_settings("checkout.amazon.it", self.sites)
        self.assertEqual(got.get("profile"), "italia")
        self.assertEqual(got.get("skip"), ["birth_date"])

    def test_exact_top_level_match(self):
        got = autofill.resolve_site_settings("amazon.it", self.sites)
        self.assertEqual(got.get("profile"), "italia")

    def test_no_match_returns_empty_dict(self):
        got = autofill.resolve_site_settings("totally-unrelated.example.org", self.sites)
        self.assertEqual(got, {})
        # Safe to .get() on the result without checking for None first.
        self.assertIsNone(got.get("profile"))
        self.assertEqual(got.get("skip", []), [])

    def test_empty_host_or_sites(self):
        self.assertEqual(autofill.resolve_site_settings("", self.sites), {})
        self.assertEqual(autofill.resolve_site_settings("amazon.it", {}), {})

    def test_site_with_only_skip_no_profile(self):
        got = autofill.resolve_site_settings("shop.example.com", self.sites)
        self.assertIsNone(got.get("profile"))
        self.assertEqual(got.get("skip"), ["phone"])

    def test_case_insensitive_host_match(self):
        got = autofill.resolve_site_settings("Amazon.IT", self.sites)
        self.assertEqual(got.get("profile"), "italia")


class MissesReportTest(unittest.TestCase):
    def test_groups_by_normalized_label_with_counts_and_hosts(self):
        records = [
            {"label_text": "Fav colour", "host": "a.example"},
            {"label_text": "fav  colour", "host": "b.example"},  # same after normalization
            {"label_text": "T-shirt size", "host": "a.example"},
            {"label_text": "", "host": "c.example"},  # no label
        ]
        report = autofill.build_misses_report(records)
        self.assertIn("total records: 4, unique labels: 2", report)
        self.assertIn('2x  "Fav colour"', report)
        self.assertIn("a.example, b.example", report)
        self.assertIn('1x  "T-shirt size"', report)
        self.assertIn("(no label text)", report)

    def test_empty_records(self):
        report = autofill.build_misses_report([])
        self.assertIn("total records: 0, unique labels: 0", report)
        self.assertIn("nothing to report", report)

    def test_never_includes_a_value_field(self):
        # Sanity: the report is built purely from label_text/host, so even if
        # a caller accidentally passed a "value" key through, it must never
        # be rendered into the report text.
        records = [{"label_text": "X", "host": "h", "value": "TOP-SECRET-VALUE"}]
        report = autofill.build_misses_report(records)
        self.assertNotIn("TOP-SECRET-VALUE", report)


if __name__ == "__main__":
    unittest.main()
