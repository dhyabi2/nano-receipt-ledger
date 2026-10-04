"""The survey table is derived, offline-checkable, and cannot flatter us.

Four properties, each with its own test, because each is a way this table could
quietly become untrue:

1.  The README is REBUILT from `rows.json`, never appended to or hand-edited.
2.  No row is marked confirmed on its author's word, and a row whose author has
    not consented is not published at all.
3.  `rederive.py` makes no request in these tests - every outcome here comes
    from an injected opener, so the suite is the same offline and on a runner.
4.  Our own row is graded on the same two columns as everyone else's.
"""
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SURVEY = os.path.join(ROOT, "survey")
sys.path.insert(0, SURVEY)

import build_readme  # noqa: E402
import rederive  # noqa: E402


def load(name):
    with open(os.path.join(SURVEY, name)) as fh:
        return json.load(fh)


def readme():
    with open(os.path.join(SURVEY, "README.md")) as fh:
        return fh.read()


class Reply:
    """A urlopen context manager, so no socket is ever opened in these tests."""

    def __init__(self, status=200, body=b"x" * 10):
        self.status, self._body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _n=None):
        return self._body


class TheTableIsDerived(unittest.TestCase):
    def test_the_readme_is_exactly_what_a_rebuild_produces(self):
        """Hand-editing the Markdown is how a published row comes to disagree
        with the data it was made from - the failure this survey is about."""
        rendered = build_readme.render(load("rows.json"), load("checked.json"))
        with open(os.path.join(SURVEY, "README.md")) as fh:
            self.assertEqual(fh.read(), rendered,
                             "survey/README.md is stale: run python3 survey/build_readme.py")

    def test_the_check_flag_agrees(self):
        self.assertEqual(build_readme.main(["--check"]), 0)

    def test_a_pipe_in_a_quote_cannot_break_the_table(self):
        self.assertEqual(build_readme.cell("a | b"), "a \\| b")
        self.assertEqual(build_readme.cell("two\nlines"), "two lines")

    def test_every_published_row_appears_in_the_table_and_the_quotes(self):
        survey, text = load("rows.json"), readme()
        published = [r for r in survey["rows"] if r.get("published")]
        self.assertGreaterEqual(len(published), 3)
        for row in published:
            self.assertIn(row["author"], text)
            self.assertIn(row["said"][:60], text)

    def test_a_withheld_row_is_not_in_the_table_body(self):
        survey = load("rows.json")
        text = readme()
        withheld = [r for r in survey["rows"] if not r.get("published")]
        self.assertTrue(withheld, "the fixture must keep one withheld row for this test to mean anything")
        table = text.split("### In each author's own words")[0]
        for row in withheld:
            self.assertNotIn(row["author"], table,
                             "a row whose consent is open must not be in the table")
            self.assertIn(row["author"], text.split("### Withheld")[1])


class NothingIsTakenOnTrust(unittest.TestCase):
    def test_a_row_naming_no_endpoint_is_not_checkable(self):
        row = {"id": "x", "published": True, "record_lives": "a receipt I keep on my own box"}
        result = rederive.check_row(row, opener=lambda *a, **k: self.fail("a request was made"))
        self.assertEqual(result["outcome"], "not_checkable")
        self.assertFalse(result["transfers_confirmed_against_chain"])

    def test_a_withheld_rows_evidence_is_not_fetched_either(self):
        row = {"id": "x", "published": False, "withheld_reason": "consent is open",
               "record_lives": "https://example.invalid/gist"}
        result = rederive.check_row(row, opener=lambda *a, **k: self.fail("a request was made"))
        self.assertEqual(result["outcome"], "not_checkable")
        self.assertIn("consent is open", result["detail"])

    def test_a_fetched_document_is_confirmed_and_says_what_that_means(self):
        row = {"id": "x", "published": True, "record_lives": "https://example.test/rows.json"}
        result = rederive.check_row(row, opener=lambda *a, **k: Reply(200, b"abc"))
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(result["evidence"][0]["bytes"], 3)
        self.assertEqual(len(result["evidence"][0]["sha256_prefix_32"]), 32)
        self.assertFalse(result["transfers_confirmed_against_chain"],
                         "fetching a document is not reading a chain")
        self.assertIn("no chain was read", result["detail"])

    def test_a_404_is_unreachable_and_says_the_publisher_did_not_answer(self):
        def not_found(*a, **k):
            raise urllib.error.HTTPError("https://example.test/x", 404, "Not Found", {}, io.BytesIO())
        result = rederive.check_row(
            {"id": "x", "published": True, "record_lives": "https://example.test/x"}, opener=not_found)
        self.assertEqual(result["outcome"], "unreachable")
        self.assertIn("404", result["detail"])

    def test_a_refusal_by_our_own_network_is_not_evidence_against_the_row(self):
        """Conflating "this machine was not allowed to look" with "the document
        is not there" publishes a false negative about somebody else's ledger."""
        def blocked(*a, **k):
            raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
        result = rederive.check_row(
            {"id": "x", "published": True, "record_lives": "https://example.test/x"}, opener=blocked)
        self.assertEqual(result["outcome"], "unreachable")
        self.assertIn("not evidence against the row", result["detail"])

    def test_offline_makes_no_request_at_all(self):
        report = rederive.run(offline=True)
        self.assertEqual(report["counts"]["confirmed"], 0)
        for result in report["results"]:
            self.assertIn(result["outcome"], ("unreachable", "not_checkable"))

    def test_no_row_in_the_committed_file_claims_a_chain_check(self):
        for result in load("checked.json")["results"]:
            self.assertFalse(result["transfers_confirmed_against_chain"], result["id"])

    def test_the_checker_imports_no_signer_and_cannot_move_money(self):
        with open(os.path.join(SURVEY, "rederive.py")) as fh:
            source = fh.read()
        for forbidden in ("import socket", "nanoaddr", "money", "ledger.node", "POST", "data="):
            self.assertNotIn(forbidden, source, forbidden)


class WeAreNotTheControlGroup(unittest.TestCase):
    def test_our_own_row_is_present_and_graded(self):
        rows = {r["id"]: r for r in load("rows.json")["rows"]}
        ours = rows["nanoswarm-nano-receipt-ledger"]
        self.assertTrue(ours["published"])
        for column in ("transfers_rederivable", "meaning_rederivable", "divergence_note"):
            self.assertNotIn(ours[column], ("", None, "unassessed"), column)

    def test_our_own_meaning_column_is_a_no(self):
        """The spec's instruction, pinned: transfers yes, meaning no until
        nano-invoice's receipt v2 is in use. A later edit that quietly upgrades
        our own row without the work having landed turns this red."""
        ours = {r["id"]: r for r in load("rows.json")["rows"]}["nanoswarm-nano-receipt-ledger"]
        self.assertTrue(ours["transfers_rederivable"].startswith("yes"))
        self.assertTrue(ours["meaning_rederivable"].startswith("no"))
        self.assertIn("ours to keep honest", ours["meaning_rederivable"])

    def test_the_table_carries_no_advocacy(self):
        """It is a survey. A row that sells Nano is not one."""
        text = readme().lower()
        for pitch in ("feeless", "faster than", "you should use", "superior",
                      "unlike usdc", "best rail"):
            self.assertNotIn(pitch, text, pitch)


class TheScriptsRun(unittest.TestCase):
    def test_rederive_offline_runs_as_a_process(self):
        out = subprocess.run([sys.executable, os.path.join(SURVEY, "rederive.py"),
                              "--offline", "--print"],
                             capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["counts"]["confirmed"], 0)

    def test_build_readme_check_runs_as_a_process(self):
        out = subprocess.run([sys.executable, os.path.join(SURVEY, "build_readme.py"), "--check"],
                             capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)


if __name__ == "__main__":
    unittest.main()
