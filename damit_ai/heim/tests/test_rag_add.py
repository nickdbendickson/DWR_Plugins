"""rag_index.py --add (heim-add): one dragged file into Heim's index.

Why this exists: rag_index.py took folders only ("ERROR: not a directory"), so a single
document had no way in, and heim-chat is the bare model with no document lookup at all.
Embeddings are stubbed; no Ollama.
"""
import importlib.util
import os
import shutil
import sqlite3
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "tools", "rag_index.py")


class AddTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="heim-add-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "local-ai", "knowledge.db")
        os.makedirs(os.path.dirname(self.db))
        self.inbox = os.path.join(self.tmp, "Down loads")
        os.makedirs(self.inbox)
        old = os.environ.get("HEIM_DB")
        os.environ["HEIM_DB"] = self.db
        try:
            spec = importlib.util.spec_from_file_location("rag_index_add_test", SRC)
            self.rag = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.rag)
        finally:
            if old is None:
                os.environ.pop("HEIM_DB", None)
            else:
                os.environ["HEIM_DB"] = old
        self.rag.embed = lambda texts: [[1.0, 0.0] for _ in texts]
        self.rag.print = lambda *a, **k: None            # keep the test output readable

    def write(self, name, text, folder=None):
        p = os.path.join(folder or self.inbox, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def rows(self):
        db = sqlite3.connect(self.db)
        try:
            return sorted(db.execute("SELECT collection, path FROM chunks GROUP BY collection, path").fetchall())
        finally:
            db.close()

    def test_file_is_copied_beside_the_index_and_lands_in_the_added_collection(self):
        p = self.write("Run notes.md", "# notes\n\nThe 2801 run used the kit composite.\n")
        self.assertEqual(self.rag.add_files([p]), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "local-ai", "added", "Run notes.md")))
        self.assertEqual(self.rows(), [("added", "Run notes.md")])

    def test_second_file_keeps_the_first(self):
        self.rag.add_files([self.write("a.md", "# a\n\nfirst document.\n")])
        self.rag.add_files([self.write("b.txt", "second document.\n")])
        self.assertEqual(self.rows(), [("added", "a.md"), ("added", "b.txt")])

    def test_same_file_again_is_not_indexed_twice(self):
        p = self.write("a.md", "# a\n\nfirst document.\n")
        self.assertEqual(self.rag.add_files([p]), 1)
        self.assertEqual(self.rag.add_files([p]), 0)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "local-ai", "added")), ["a.md"])

    def test_same_name_different_content_keeps_both(self):
        self.rag.add_files([self.write("a.md", "# a\n\nfirst document.\n")])
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.assertEqual(self.rag.add_files([self.write("a.md", "# a\n\na different document.\n", other)]), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_folder_and_unreadable_type_are_refused_and_other_collections_untouched(self):
        kn = os.path.join(self.tmp, "kn")
        os.makedirs(kn)
        self.write("K.md", "# k\n\nknowledge pack text.\n", kn)
        self.rag.index_collection("heim_knowledge", [kn])
        self.assertEqual(self.rag.add_files([self.inbox, self.write("model.docx", "x")]), 0)
        self.assertEqual(self.rows(), [("heim_knowledge", "K.md")])
        self.rag.add_files([self.write("a.md", "# a\n\nfirst document.\n")])
        self.assertEqual(self.rows(), [("added", "a.md"), ("heim_knowledge", "K.md")])

    def test_installers_expose_it_as_heim_add(self):
        root = os.path.dirname(os.path.dirname(HERE))
        self.assertTrue(os.path.isfile(os.path.join(root, "heim", "setup-local-heim.sh")))
        for name in ("heim/setup-local-heim.sh", "install-local-ai-linux.sh", "install-local-ai-windows.ps1"):
            if not os.path.isfile(os.path.join(root, name)):
                continue        # the DamSafety copy of damit_ai ships the Mac installer only
            with open(os.path.join(root, name), encoding="utf-8") as f:
                self.assertRegex(f.read(), r"heim-add\b.*rag_index\.py.*--add", name)


if __name__ == "__main__":
    unittest.main()
