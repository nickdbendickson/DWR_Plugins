"""vor --doc / --per-doc: retrieval scoped to one publication.

Why this exists: asking "What depth defines the inundation boundary in FEMA P-946?"
returned nothing although FEMA P-946 was indexed with 444 passages. Two causes, both
covered here -- naming the document in the question only adds search words that match
every FEMA document, and ask/research capped every document at 3 passages with no way
to raise it.

No Ollama and no model: embed() is replaced with a tiny keyword vector so that ranking
is deterministic. Both the numpy path and the pure-python fallback are exercised.
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "vor.py")
_TMP = tempfile.mkdtemp(prefix="vor-tests-")
os.environ["VOR_HOME"] = _TMP
os.environ["VOR_DB"] = os.path.join(_TMP, "vor.db")

_spec = importlib.util.spec_from_file_location("vor_under_test", SRC)
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)

DIMS = ["depth", "boundary", "breach", "culvert"]


def vec(text):
    t = text.lower()
    v = [1.0 if d in t else 0.0 for d in DIMS]
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


dr.embed = lambda texts, model=None: [vec(t) for t in texts]
dr.query_text = lambda model, q: q

P946, FEMA541, CULVERTS = 1, 2, 3
DOCS = [
    ("Inundation Mapping of Flood Risks (FEMA P-946)",
     "/lib/FEMA Guidelines InundatnMap P946.pdf", "FEMA P-946",
     ["the inundation boundary is mapped at the depth where flow becomes negligible",
      "depth grids are produced for each scenario",
      "boundary smoothing is applied after mapping",
      "tiered approaches to inundation mapping",
      "depth and boundary together define the mapped extent"]),
    # the chatty neighbour: many passages that score on the same words
    ("Embankment Dam Failure Analysis (FEMA 541)",
     "/lib/EmbankmentDamFailureAnalysis.pdf", "FEMA 541",
     ["breach width relates to reservoir storage and depth"] * 8),
    ("Culvert Hydraulics", "/lib/culverts.pdf", "",
     ["culvert inlet control depth"] * 4),
]
QUESTION = "what depth defines the inundation boundary"


class Args(object):
    def __init__(self, doc=None, per_doc=None):
        self.doc = doc
        self.per_doc = per_doc


class DocScopeTest(unittest.TestCase):

    def setUp(self):
        if os.path.exists(dr.DB_PATH):
            os.remove(dr.DB_PATH)
        self.db = dr.connect()
        for i, (title, path, number, chunks) in enumerate(DOCS, 1):
            self.db.execute(
                "INSERT INTO docs(id, sha1, path, title, number, pages) VALUES(?,?,?,?,?,?)",
                (i, "sha%d" % i, path, title, number, 10))
            for j, c in enumerate(chunks):
                cur = self.db.execute(
                    "INSERT INTO chunks(doc_id, page, label, idx, txt, vec) VALUES(?,?,?,?,?,?)",
                    (i, j + 1, str(j + 1), j, c, array("f", vec(c)).tobytes()))
                if self.db.fts:
                    self.db.execute("INSERT INTO chunks_fts(rowid, txt, title) VALUES(?,?,?)",
                                    (cur.lastrowid, c, title))
        self.db.commit()
        self._np = dr.np

    def tearDown(self):
        dr.np = self._np
        self.db.close()

    def chunk_ids(self, doc_id):
        return [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]

    # ---------------------------------------------------------------- retrieval

    def test_retrieval_both_backends(self):
        for label, use_numpy in (("numpy", True), ("pure python", False)):
            with self.subTest(backend=label):
                dr.np = self._np if use_numpy else None
                idx = dr.Index(self.db)

                hits = idx.search(QUESTION, k=10, per_doc=3)
                per = {}
                for h in hits:
                    per[h.doc_id] = per.get(h.doc_id, 0) + 1
                self.assertTrue(all(v <= 3 for v in per.values()),
                                "unscoped search must keep the per-document cap: %r" % per)

                hits = idx.search(QUESTION, k=10, per_doc=None, keep=[P946])
                self.assertTrue(hits, "a scoped search must still return passages")
                self.assertEqual({h.doc_id for h in hits}, {P946})
                self.assertGreater(len(hits), 3, "per_doc=None must lift the 3-passage cap")
                self.assertTrue(any("negligible" in h.txt for h in hits),
                                "the answering passage must be reachable once scoped")

                self.assertEqual(len(idx.search(QUESTION, k=10, per_doc=3, keep=[P946])), 3,
                                 "an explicit --per-doc must still be honoured")

                other = idx.search("culvert inlet control depth", k=10, per_doc=None, keep=[CULVERTS])
                self.assertEqual({h.doc_id for h in other}, {CULVERTS})

                self.assertEqual(idx.search(QUESTION, k=10, keep=[99]), [],
                                 "scoping to a document with no chunks must return nothing, "
                                 "not fall back to the whole library")

    def test_filter_is_applied_before_ranking(self):
        # The point of keep=: a pool drawn from the whole library can hold no chunk of the
        # document that was asked for, so filtering afterwards would return nothing.
        idx = dr.Index(self.db)
        want = set(self.chunk_ids(P946))
        self.assertTrue(set(cid for cid, _ in idx.vector(QUESTION, 50, keep=[P946])) <= want)
        self.assertTrue(set(cid for cid, _ in idx.lexical("depth boundary", 50, keep=[P946])) <= want)

    # ---------------------------------------------------------------- --doc parsing

    def test_find_docs_matches_id_path_title_and_number(self):
        self.assertEqual(dr.find_docs(self.db, "1")[0], [P946])
        self.assertEqual(dr.find_docs(self.db, "Inundation Mapping")[0], [P946])
        self.assertEqual(dr.find_docs(self.db, "culverts.pdf")[0], [CULVERTS])
        self.assertEqual(dr.find_docs(self.db, "FEMA 541")[0], [FEMA541])

    def test_find_docs_ignores_punctuation(self):
        # the file is named "... P946.pdf" but everyone writes P-946
        ids, labels = dr.find_docs(self.db, "P-946")
        self.assertEqual(ids, [P946])
        self.assertIn("P-946", labels[0])

    def test_find_docs_unions_comma_separated_keys(self):
        self.assertEqual(dr.find_docs(self.db, "P-946,culverts")[0], [P946, CULVERTS])

    def test_find_docs_refuses_a_miss_and_a_too_short_key(self):
        self.assertRaises(SystemExit, dr.find_docs, self.db, "no-such-document")
        self.assertRaises(SystemExit, dr.find_docs, self.db, "x")

    # ---------------------------------------------------------------- defaults

    def test_doc_scope_defaults(self):
        self.assertEqual(dr.doc_scope(self.db, Args()), (None, 3, ""),
                         "without --doc the per-document cap stays at 3")
        keep, per_doc, note = dr.doc_scope(self.db, Args(doc="P-946"))
        self.assertEqual((keep, per_doc), ([P946], None),
                         "--doc scopes the search and lifts the cap")
        self.assertIn("P-946", note, "the run must say what it scoped to")
        self.assertEqual(dr.doc_scope(self.db, Args(doc="P-946", per_doc=2))[1], 2,
                         "an explicit --per-doc wins over the lifted cap")


if __name__ == "__main__":
    unittest.main()
