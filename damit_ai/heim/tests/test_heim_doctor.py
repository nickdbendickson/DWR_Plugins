"""heim-doctor (rag_index.py --doctor): is Heim reading what you think it is.

Why this exists: every one of these states is silent. An index with no knowledge pack, a
pack still under its pre-rename name, a folder re-arranged since it was indexed, a page too
big to index: Heim starts, answers, and cites nothing or cites the old text. The doctor
reads the index and the folders, changes neither, and exits 1 on the ones that matter.
"""
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "tools", "rag_index.py")
KN = os.path.join(os.path.dirname(HERE), "knowledge")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="heim-doctor-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "local-ai", "knowledge.db")
        os.makedirs(os.path.dirname(self.db))
        old = os.environ.get("HEIM_DB")
        os.environ["HEIM_DB"] = self.db
        try:
            spec = importlib.util.spec_from_file_location("rag_index_doctor_test", SRC)
            self.rag = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.rag)
        finally:
            if old is None:
                os.environ.pop("HEIM_DB", None)
            else:
                os.environ["HEIM_DB"] = old
        self.rag.embed = lambda texts: [[1.0, 0.0] for _ in texts]
        self.quiet = io.StringIO()

    def folder(self, name, files):
        root = os.path.join(self.tmp, name)
        for rel, text in files.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        return root

    def index(self, coll, root):
        with redirect_stdout(self.quiet):
            self.rag.index_collection(coll, [root])

    def backdate(self, coll, seconds=3600):
        """Pretend the collection was indexed an hour ago, and its files written before that."""
        db = sqlite3.connect(self.db)
        roots = json.loads(db.execute("SELECT roots FROM roots WHERE collection=?", (coll,)).fetchone()[0])
        db.execute("UPDATE roots SET indexed_at=? WHERE collection=?",
                   (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - seconds)), coll))
        db.commit()
        db.close()
        t = time.time() - 2 * seconds
        for root in roots:
            for dp, _, fns in os.walk(root):
                for fn in fns:
                    os.utime(os.path.join(dp, fn), (t, t))

    def checks(self):
        return self.rag.doctor_checks(ollama=False)

    def of(self, checks, level):
        return [t for l, t in checks if l == level]

    def pack(self):
        root = self.folder("knowledge", {"KNOWN.md": "# Known\n\nREFINE_NEAR is off by default.\n",
                                         "sub/CASES.md": "# Cases\n\nA DEM in metres overrides WORK_CRS.\n"})
        self.index("heim_knowledge", root)
        self.backdate("heim_knowledge")
        return root


class NothingToReadTest(Base):
    def test_no_index(self):
        c = self.checks()
        self.assertEqual([l for l, _ in c], ["PROBLEM"])
        self.assertIn("answer from the model's built-in rules alone", c[0][1])
        self.assertFalse(os.path.exists(self.db), "the doctor must not create the index it reports missing")

    def test_an_index_with_code_but_no_knowledge_pack(self):
        self.index("dwr_plugins", self.folder("repo", {"a.py": "x = 1\n"}))
        p = self.of(self.checks(), "PROBLEM")
        self.assertEqual(len(p), 1)
        self.assertIn("no 'heim_knowledge' collection", p[0])

    def test_the_pack_still_under_its_old_name(self):
        """What a rename leaves behind: the rows exist, under a name nothing asks for."""
        self.pack()
        db = sqlite3.connect(self.db)
        db.execute("UPDATE chunks SET collection='pierre_knowledge' WHERE collection='heim_knowledge'")
        db.execute("UPDATE roots SET collection='pierre_knowledge' WHERE collection='heim_knowledge'")
        db.commit()
        db.close()
        p = self.of(self.checks(), "PROBLEM")
        self.assertEqual(len(p), 1)
        self.assertIn("still under its old name 'pierre_knowledge'", p[0])
        db = sqlite3.connect(self.db)
        names = {r[0] for r in db.execute("SELECT DISTINCT collection FROM chunks")}
        db.close()
        self.assertEqual(names, {"pierre_knowledge"}, "reading it must not rename it")

    def test_an_old_copy_beside_the_new_one_is_a_watch(self):
        self.pack()
        db = sqlite3.connect(self.db)
        db.execute("INSERT INTO chunks (collection,path,idx,txt,vec) VALUES ('lumo_knowledge','K.md',0,'old','[1,0]')")
        db.commit()
        db.close()
        c = self.checks()
        self.assertEqual(self.of(c, "PROBLEM"), [])
        self.assertTrue(any("lumo_knowledge" in t and "out-rank" in t for t in self.of(c, "WATCH")), c)


class LevelWithItsFolderTest(Base):
    def test_a_fresh_index_is_clean(self):
        self.pack()
        c = self.checks()
        self.assertEqual(self.of(c, "PROBLEM") + self.of(c, "WATCH"), [], c)
        self.assertTrue(any("heim_knowledge" in t and "level with its folder" in t for t in self.of(c, "OK")), c)
        code, said = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertIn("All checks passed.", said)

    def run_doctor(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.rag.doctor(ollama=False)
        return code, out.getvalue()

    def test_changed_new_and_removed_files_since_indexing(self):
        root = self.pack()
        with open(os.path.join(root, "KNOWN.md"), "a", encoding="utf-8") as f:
            f.write("\nA new rule added after indexing.\n")
        with open(os.path.join(root, "NEW.md"), "w", encoding="utf-8") as f:
            f.write("# New page\n")
        os.remove(os.path.join(root, "sub", "CASES.md"))
        w = self.of(self.checks(), "WATCH")
        self.assertEqual(len(w), 1, w)
        self.assertIn("heim_knowledge is older than its files", w[0])
        self.assertIn("1 file(s) changed since (first: KNOWN.md)", w[0])
        self.assertIn("1 new file(s) since (first: NEW.md)", w[0])
        self.assertIn("1 indexed file(s) are gone from the folder (first: %s)" % os.path.join("sub", "CASES.md"), w[0])
        self.assertIn("heim-index -c heim_knowledge '%s'" % root, w[0])
        code, said = self.run_doctor()
        self.assertEqual(code, 0, "stale is worth saying, not worth failing a script on")
        self.assertIn("No problem; 1 thing(s) to look at.", said)

    def test_a_file_with_no_text_is_not_reported_as_new_for_ever(self):
        root = self.folder("repo", {"a.py": "x = 1\n", "pkg/__init__.py": ""})
        self.index("heim_knowledge", root)
        self.backdate("heim_knowledge")
        c = self.checks()
        self.assertEqual(self.of(c, "WATCH"), [], c)

    def test_a_file_over_the_size_limit_is_named(self):
        """The first DamSafety algorithm dump: one 790 KB page against a 256 KB limit."""
        root = self.pack()
        with open(os.path.join(root, "ALGORITHM-HELP.md"), "w", encoding="utf-8") as f:
            f.write("# big\n" + "parameter line\n" * 20000)
        t = time.time() - 7200
        os.utime(os.path.join(root, "ALGORITHM-HELP.md"), (t, t))
        w = self.of(self.checks(), "WATCH")
        self.assertEqual(len(w), 1, w)
        self.assertIn("1 file(s) are over the size limit (256 KB for text) and are never indexed", w[0])
        self.assertIn("first: ALGORITHM-HELP.md", w[0])

    def test_a_folder_that_moved(self):
        root = self.pack()
        os.rename(root, root + "-moved")
        w = self.of(self.checks(), "WATCH")
        self.assertEqual(len(w), 1)
        self.assertIn("which is not there now", w[0])

    def test_leftovers_of_an_interrupted_run(self):
        self.pack()
        db = sqlite3.connect(self.db)
        db.execute("INSERT INTO chunks (collection,path,idx,txt,vec) VALUES ('__stage__crest','a.ts',0,'x','[1,0]')")
        db.commit()
        db.close()
        c = self.checks()
        self.assertTrue(any("interrupted and left crest (1 passages, not searched)" in t
                            for t in self.of(c, "WATCH")), c)
        self.assertIn("1 collection(s)", self.of(c, "OK")[0], "a stage is not counted as a collection")

    def test_exit_code_one_on_a_problem(self):
        code, said = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("1 PROBLEM(s)", said)


class ThisRepoTest(Base):
    @unittest.skipUnless(os.path.isdir(KN), "this copy of damit_ai ships no knowledge pack (the DamSafety one)")
    def test_the_shipped_knowledge_pack_indexes_clean(self):
        """The real pack, through the real indexer: nothing over the limit, nothing left out."""
        self.index("heim_knowledge", KN)
        c = self.checks()
        self.assertEqual(self.of(c, "PROBLEM"), [], c)
        self.assertFalse([t for t in self.of(c, "WATCH") if "size limit" in t], c)
        self.assertNotIn("too big, skipped", self.quiet.getvalue())

    def test_installers_expose_heim_doctor(self):
        root = os.path.dirname(os.path.dirname(HERE))
        self.assertTrue(os.path.isfile(os.path.join(root, "heim", "setup-local-heim.sh")))
        for name in ("heim/setup-local-heim.sh", "install-local-ai-linux.sh", "install-local-ai-windows.ps1"):
            if not os.path.isfile(os.path.join(root, name)):
                continue        # the DamSafety copy of damit_ai ships the Mac installer only
            with open(os.path.join(root, name), encoding="utf-8") as f:
                self.assertTrue(__import__("re").search(r"heim-doctor\b.*rag_index\.py.*--doctor", f.read()), name)


if __name__ == "__main__":
    unittest.main()
