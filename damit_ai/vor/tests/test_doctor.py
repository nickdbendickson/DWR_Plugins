"""vor doctor: the states in which Vor runs without an error and answers from nothing.

Why this exists: "empty, and nothing said so" has cost a wrong answer four separate times
(an empty knowledge collection, a renamed collection, a second empty library beside the full
one, a page too big to index). Each is silent by construction: an empty index is a valid
index. `vor doctor` now names them, and exits non-zero on the ones that mean Vor is reading
less than you think. No Ollama: `--library` checks need no model.
"""
import argparse
import importlib.util
import io
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from array import array
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "vor.py")
KEYS = ("HOME", "VOR_HOME", "VOR_DB", "DAMRESEARCH_HOME", "VOR_EMBED")


class Args(object):
    library_only = True


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vor-doctor-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = {k: os.environ.get(k) for k in KEYS}
        for k in KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = self.tmp
        self.addCleanup(self.restore)
        self.n = 0

    def restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def vor(self):
        """vor.py imported now, so it sees the folders as they stand."""
        self.n += 1
        spec = importlib.util.spec_from_file_location("vor_doctor_test_%d" % self.n, SRC)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def library(self, home, files=("a.md",), index=None, dbname="vor.db", notext="", added=None):
        """A library folder with document files, and optionally an index of some of them."""
        lib = os.path.join(self.tmp, home, "library")
        os.makedirs(lib, exist_ok=True)
        paths = []
        for f in files:
            p = os.path.join(lib, f)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("# %s\n\nThe breach width relates to reservoir storage.\n" % f)
            paths.append(p)
        if index is not None:
            os.environ["VOR_HOME"] = os.path.join(self.tmp, home)
            os.environ["VOR_DB"] = os.path.join(self.tmp, home, dbname)
            m = self.vor()
            db = m.connect()
            for i, f in enumerate(index, 1):
                db.execute("INSERT INTO docs(id, sha1, path, title, pages, notext, added) VALUES(?,?,?,?,?,?,?)",
                           (i, "sha%d" % i, os.path.join(lib, f), f, 3, notext,
                            added or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 5))))
                db.execute("INSERT INTO chunks(doc_id, page, label, idx, txt, vec) VALUES(?,1,'1',0,'text',?)",
                           (i, array("f", [1.0, 0.0]).tobytes()))
            m.meta_set(db, "embed_model", m.EMBED_MODEL)
            db.commit()
            db.close()
            os.environ.pop("VOR_HOME")
            os.environ.pop("VOR_DB")
        return paths

    def checks(self):
        m = self.vor()
        return m, m.library_checks()

    def levels(self, checks, level):
        return [t for l, t in checks if l == level]

    # ------------------------------------------------------------------ healthy

    def test_a_healthy_library_has_no_problem_and_no_watch(self):
        self.library("Vor", files=("a.md", "b.md"), index=("a.md", "b.md"))
        m, c = self.checks()
        self.assertEqual(self.levels(c, "PROBLEM"), [])
        self.assertEqual(self.levels(c, "WATCH"), [])
        self.assertIn("index: 2 documents, 2 passages", self.levels(c, "OK"))
        self.assertIn("every file in the library is indexed, and none changed since", self.levels(c, "OK"))
        self.assertTrue(self.levels(c, "OK")[0].endswith("(it holds the index)"))

    # ------------------------------------------------------------------ empty

    def test_no_index_is_a_problem_and_doctor_does_not_create_one(self):
        self.library("Vor", files=("a.md", "b.md"))
        m, c = self.checks()
        p = self.levels(c, "PROBLEM")
        self.assertEqual(len(p), 1)
        self.assertIn("no index at", p[0])
        self.assertIn("2 document file(s) are waiting in the library. Run: vor ingest", p[0])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "Vor", "vor.db")),
                         "a doctor that makes an empty index hides the thing it is looking for")

    def test_an_empty_index_is_a_problem(self):
        self.library("Vor", files=("a.md",), index=())
        m, c = self.checks()
        self.assertTrue(any("the index is empty" in t and "1 file(s) are in the library" in t
                            for t in self.levels(c, "PROBLEM")), c)

    # ------------------------------------------------------------------ two libraries

    def test_the_full_library_under_the_old_name_beside_an_empty_new_one(self):
        """The 2026-11-13 hole: something made ~/Vor, the index stayed in ~/DamResearch.
        The index decides, so Vor reads the old one, and says a second folder exists."""
        self.library("DamResearch", files=("a.md", "b.md"), index=("a.md", "b.md"), dbname="research.db")
        self.library("Vor", files=("new.md",))
        m, c = self.checks()
        self.assertEqual(m.HOME, os.path.join(self.tmp, "DamResearch"))
        self.assertEqual(self.levels(c, "PROBLEM"), [])
        w = [t for t in self.levels(c, "WATCH") if "second library" in t]
        self.assertEqual(len(w), 1)
        self.assertIn(os.path.join(self.tmp, "Vor"), w[0])
        self.assertIn("no index, 1 document file(s)", w[0])

    def test_reading_the_smaller_of_two_indexes_is_a_problem(self):
        self.library("DamResearch", files=("a.md", "b.md", "c.md"), index=("a.md", "b.md", "c.md"),
                     dbname="research.db")
        self.library("Vor", files=("new.md",), index=("new.md",))
        m, c = self.checks()
        self.assertEqual(m.HOME, os.path.join(self.tmp, "Vor"))
        p = self.levels(c, "PROBLEM")
        self.assertEqual(len(p), 1, c)
        self.assertIn("an index with 3 documents", p[0])
        self.assertIn("It holds MORE than the one in use", p[0])

    def test_vor_home_pointing_at_an_empty_folder_while_the_library_is_elsewhere(self):
        """What the Linux installer did: pin VOR_HOME to a new empty ~/Vor."""
        self.library("DamResearch", files=("a.md",), index=("a.md",), dbname="research.db")
        os.makedirs(os.path.join(self.tmp, "Vor"))
        os.environ["VOR_HOME"] = os.path.join(self.tmp, "Vor")
        m, c = self.checks()
        p = self.levels(c, "PROBLEM")
        self.assertTrue(any("no index at" in t for t in p), c)
        self.assertTrue(any("second library" in t and "MORE than the one in use" in t for t in p), c)
        self.assertTrue(self.levels(c, "OK")[0].endswith("(VOR_HOME is set)"))

    def test_a_folder_set_aside_by_setup_is_reported_once(self):
        self.library("Vor", files=("a.md",), index=("a.md",))
        self.library("Vor.before-rename-20261113-1200", files=("x.md",))
        m, c = self.checks()
        w = [t for t in self.levels(c, "WATCH") if "second library" in t]
        self.assertEqual(len(w), 1)
        self.assertIn("Nothing reads it", w[0])

    # ------------------------------------------------------------------ index behind the files

    def test_files_not_indexed_missing_changed_and_scanned(self):
        paths = self.library("Vor", files=("a.md", "b.md", "c.md"), index=("a.md", "b.md", "gone.md"),
                             notext="3,4", added="2026-11-01T10:00:00")
        old = time.mktime((2026, 10, 1, 0, 0, 0, 0, 0, -1))
        os.utime(paths[0], (old, old))            # a.md: older than its index entry -> not "changed"
        m, c = self.checks()
        w = "\n".join(self.levels(c, "WATCH"))
        self.assertIn("1 file(s) in the library are not in the index (first: c.md). Run: vor ingest", w)
        self.assertIn("1 indexed document(s) no longer have a file (first: gone.md)", w)
        self.assertIn("1 file(s) changed after they were indexed (first: b.md)", w)
        self.assertIn("3 document(s) have 6 page(s) with no text layer", w)
        self.assertEqual(self.levels(c, "PROBLEM"), [])
        self.assertNotIn("every file in the library is indexed", "\n".join(self.levels(c, "OK")))

    def test_an_index_built_with_another_embedding_model(self):
        self.library("Vor", files=("a.md",), index=("a.md",))
        os.environ["VOR_EMBED"] = "some-other-embedder"
        m, c = self.checks()
        self.assertTrue(any("VOR_EMBED is some-other-embedder" in t for t in self.levels(c, "PROBLEM")), c)

    # ------------------------------------------------------------------ files ingest never adds

    def test_an_exact_copy_of_an_indexed_file_is_not_sent_to_ingest(self):
        """Ingest goes by content and skips a second copy, so "Run: vor ingest" for it is a
        circle: ingest adds nothing and the doctor says it again."""
        paths = self.library("Vor", files=("a.md", "b.md"), index=("a.md", "b.md"))
        twin = os.path.join(os.path.dirname(paths[0]), "asdso", "a-again.md")
        os.makedirs(os.path.dirname(twin))
        shutil.copy(paths[0], twin)
        m = self.vor()
        db = m.connect()
        db.execute("UPDATE docs SET sha1=? WHERE path=?", (m.sha1_of(paths[0]), paths[0]))
        db.commit()
        db.close()
        c = m.library_checks()
        self.assertEqual(self.levels(c, "WATCH"), [], c)
        ok = "\n".join(self.levels(c, "OK"))
        self.assertIn("1 file(s) are exact copies of an indexed document, so ingest leaves them out "
                      "(first: %s is the same file as a.md)" % os.path.join("asdso", "a-again.md"), ok)
        self.assertIn("every file in the library is indexed, and none changed since", ok)

    def test_a_file_over_the_size_limit_is_not_sent_to_ingest(self):
        self.library("Vor", files=("a.md", "huge.md"), index=("a.md",))
        m = self.vor()
        m.MAX_FILE_BYTES = 10
        os.utime(os.path.join(self.tmp, "Vor", "library", "a.md"), (1, 1))
        c = m.library_checks()
        w = [t for t in self.levels(c, "WATCH") if "not in the index" not in t or "huge" in t]
        self.assertEqual(len(self.levels(c, "WATCH")), 1, c)
        self.assertIn("1 file(s) are over 0 MB, which Vor does not index (first: huge.md)", w[0])
        self.assertNotIn("Run: vor ingest", w[0])

    def test_a_missing_file_outside_the_library_is_shown_with_its_folder(self):
        """`vor ingest --prune` only looks under the library, so it is not the advice for a
        document that was indexed from somewhere else."""
        self.library("Vor", files=("a.md",), index=("a.md",))
        m = self.vor()
        away = os.path.join(self.tmp, "DamResearch", "library", "old.pdf")
        db = m.connect()
        db.execute("INSERT INTO docs(id, sha1, path, title, pages, notext, added) VALUES(9,'x',?,?,3,'',?)",
                   (away, "old", "2026-11-01T10:00:00"))
        db.commit()
        db.close()
        c = m.library_checks()
        (w,) = [t for t in self.levels(c, "WATCH") if "no longer have a file" in t]
        self.assertIn("(first: %s)" % os.path.join("~", "DamResearch", "library", "old.pdf"), w)
        self.assertIn("List them: vor list --missing.", w)
        self.assertIn("1 of them was indexed from outside the library", w)
        self.assertNotIn("To drop them: vor ingest --prune", w)

    def test_list_missing_shows_only_the_documents_without_a_file(self):
        self.library("Vor", files=("a.md",), index=("a.md", "gone.md"))
        m = self.vor()
        out = io.StringIO()
        with redirect_stdout(out):
            m.cmd_list(argparse.Namespace(missing=True))
        said = out.getvalue()
        self.assertIn("gone.md", said)
        self.assertNotIn("a.md\n", said.replace("gone.md", ""))
        self.assertIn("1 of 2 indexed documents no longer have a file", said)

    # ------------------------------------------------------------------ the command

    def run_doctor(self):
        m = self.vor()
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            m.cmd_doctor(Args())
        return cm.exception.code, out.getvalue()

    def test_exit_code_is_zero_only_when_nothing_is_wrong(self):
        self.library("Vor", files=("a.md",), index=("a.md",))
        code, said = self.run_doctor()
        self.assertEqual(code, 0, said)
        self.assertNotIn("PROBLEM", said)
        os.remove(os.path.join(self.tmp, "Vor", "vor.db"))
        code, said = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("PROBLEM  no index at", said)
        self.assertIn("1 PROBLEM(s) above", said)

    def test_a_watch_alone_does_not_fail_the_command(self):
        self.library("Vor", files=("a.md", "b.md"), index=("a.md",))
        code, said = self.run_doctor()
        self.assertEqual(code, 0, said)
        self.assertIn("WATCH    1 file(s) in the library are not in the index", said)


if __name__ == "__main__":
    unittest.main()
