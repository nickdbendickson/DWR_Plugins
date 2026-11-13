"""reference_sources.json stays loadable, and the documents that describe it stay true.

Why this exists: INSTALL.md said "58 more sources" in two groups for two days after the
catalog had grown to 91 in four, and Vor's own domain context named publications the
DWR copy of the catalog did not list. A count in prose goes stale the moment someone adds
an entry, so the prose is checked against the file.
"""
import json
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
VOR = os.path.dirname(HERE)
KINDS = {"pdf", "html", "index", "manual"}
CHECKS = {"tested", "listed", "cited", "pattern", ""}


def slurp(name):
    with open(os.path.join(VOR, name), encoding="utf-8") as f:
        return f.read()


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = json.loads(slurp("reference_sources.json"))
        cls.groups = cls.cat["groups"]
        cls.entries = [(g, e) for g, es in cls.groups.items() for e in es]

    def test_ids_are_unique_across_groups(self):
        seen = {}
        for g, e in self.entries:
            self.assertNotIn(e["id"], seen, "%s is in both %s and %s" % (e["id"], seen.get(e["id"]), g))
            seen[e["id"]] = g

    def test_ids_are_safe_file_names(self):
        # the id becomes the file name on disk: <group>/<id>.pdf
        for _, e in self.entries:
            self.assertRegex(e["id"], r"^[a-z0-9][a-z0-9-]*$")

    def test_every_entry_has_the_fields_the_fetcher_reads(self):
        for g, e in self.entries:
            for k in ("id", "kind", "title", "org", "url"):
                self.assertIn(k, e, "%s/%s has no %r" % (g, e.get("id"), k))
            self.assertIn(e["kind"], KINDS, e["id"])
            self.assertIn(e.get("check", ""), CHECKS, e["id"])     # older manual entries carry none

    def test_a_download_entry_has_an_https_url(self):
        for _, e in self.entries:
            if e["kind"] == "manual" or e.get("skip"):
                continue
            for u in [e["url"]] + list(e.get("alt") or []):
                self.assertTrue(u.startswith("https://"), "%s: %r" % (e["id"], u))
                self.assertNotIn(" ", u, e["id"])

    def test_a_get_by_hand_entry_says_how(self):
        for _, e in self.entries:
            if e["kind"] == "manual":
                self.assertGreater(len(e.get("note", "")), 20, "%s: a manual entry needs a note" % e["id"])

    def test_an_index_entry_has_a_pattern_that_compiles(self):
        for _, e in self.entries:
            if e["kind"] == "index":
                re.compile(e["match"])

    def test_about_names_every_group(self):
        for g in self.groups:
            self.assertIn("Group '%s'" % g, self.cat["about"])

    def test_install_md_lists_every_group_with_its_count(self):
        text = slurp("INSTALL.md")
        for g, es in self.groups.items():
            self.assertIn("- **%s** (%d):" % (g, len(es)), text,
                          "INSTALL.md must list group %r with %d sources" % (g, len(es)))
        self.assertIn("lists %d more sources" % len(self.entries), text)

    def test_the_fetcher_usage_names_every_group(self):
        head = slurp("fetch_asdso.py").split("\nimport ", 1)[0]
        for g in self.groups:
            self.assertRegex(head, r"\b%s\b" % g)


if __name__ == "__main__":
    unittest.main()
