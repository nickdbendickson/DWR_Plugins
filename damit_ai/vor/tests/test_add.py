"""vor add, and a file dragged into `vor chat`.

Why this exists: `vor chat` holds its vectors in memory, so a document ingested from a
second terminal tab was invisible to a chat that was already open, and a path dragged into
the chat window was sent to the model as a question. Both failed without saying anything.

No Ollama: embed() is replaced by a keyword vector and run_pipeline by a recorder.
"""
import builtins
import importlib.util
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "vor.py")

WORDS = ["pond", "culvert", "breach", "spillway"]


def vec(text):
    t = text.lower()
    v = [1.0 if w in t else 0.0 for w in WORDS] + [0.5]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def load(home):
    """A private copy of vor.py bound to its own library, so tests cannot see each other."""
    old = {k: os.environ.get(k) for k in ("VOR_HOME", "VOR_DB")}
    os.environ["VOR_HOME"] = home
    os.environ["VOR_DB"] = os.path.join(home, "vor.db")
    try:
        spec = importlib.util.spec_from_file_location("vor_add_under_test", SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    mod.embed = lambda texts, model=None: [vec(t) for t in texts]
    mod.query_text = lambda model, q: q
    mod.say = lambda msg: SAID.append(str(msg))
    return mod


SAID = []


class Base(unittest.TestCase):
    def setUp(self):
        del SAID[:]
        self.tmp = tempfile.mkdtemp(prefix="vor-add-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "Vor")
        self.outside = os.path.join(self.tmp, "Down loads")       # a space, as in real paths
        os.makedirs(self.outside)
        self.vor = load(self.home)

    def write(self, name, text, folder=None):
        p = os.path.join(folder or self.outside, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def docs(self):
        db = self.vor.connect()
        try:
            return sorted(r[0] for r in db.execute("SELECT path FROM docs"))
        finally:
            db.close()


class DroppedPathsTest(Base):
    def test_backslash_escaped_path_is_what_terminal_pastes(self):
        p = self.write("Pond 378.md", "# Pond\n\nA pond is a water impoundment.\n")
        self.assertEqual(self.vor.dropped_paths(p.replace(" ", "\\ ")), [p])

    def test_quoted_path_and_several_files(self):
        a = self.write("a b.md", "# a\n\npond\n")
        b = self.write("c.txt", "culvert\n")
        self.assertEqual(self.vor.dropped_paths('"%s" %s' % (a, b.replace(" ", "\\ "))), [a, b])

    def test_a_question_is_not_a_drop(self):
        for q in ("What depth defines the inundation boundary?",
                  "does TR-60 apply to a class (a) dam",
                  "what's the freeboard",                       # unbalanced quote
                  "compare 2008/2016 Froehlich widths"):        # has a separator, names no file
            self.assertEqual(self.vor.dropped_paths(q), [], q)

    def test_a_bare_word_matching_a_file_in_the_current_folder_is_a_question(self):
        self.write("breach", "x")
        here = os.getcwd()
        os.chdir(self.outside)
        self.addCleanup(os.chdir, here)
        self.assertEqual(self.vor.dropped_paths("breach"), [])

    def test_one_missing_path_makes_the_whole_line_a_question(self):
        p = self.write("a.md", "# a\n\npond\n")
        self.assertEqual(self.vor.dropped_paths("%s /no/such/file.pdf" % p.replace(" ", "\\ ")), [])


class AddPathsTest(Base):
    def test_file_is_copied_into_the_library_and_indexed(self):
        p = self.write("Pond 378.md", "# Pond\n\nA pond is a water impoundment made by an embankment.\n")
        self.assertEqual(self.vor.add_paths([p]), (1, 1))
        want = os.path.join(self.vor.LIBRARY, "added", "Pond 378.md")
        self.assertEqual(self.docs(), [want])
        os.remove(p)                                   # the Downloads copy can go
        self.assertTrue(os.path.isfile(want))

    def test_adding_the_same_file_twice_adds_nothing_and_says_so(self):
        p = self.write("p.md", "# Pond\n\npond text long enough to make a passage.\n")
        self.assertEqual(self.vor.add_paths([p])[0], 1)
        with open(p, encoding="utf-8") as f:
            q = self.write("renamed copy.md", f.read())
        self.assertEqual(self.vor.add_paths([q]), (0, 0))
        self.assertTrue(any("already in the library" in m for m in SAID), SAID)
        self.assertEqual(os.listdir(os.path.join(self.vor.LIBRARY, "added")), ["p.md"])

    def test_same_name_different_content_keeps_both(self):
        a = self.write("notes.md", "# one\n\npond notes, first version of the file.\n")
        self.assertEqual(self.vor.add_paths([a])[0], 1)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        b = self.write("notes.md", "# two\n\nculvert notes, a different document.\n", other)
        self.assertEqual(self.vor.add_paths([b])[0], 1)
        self.assertEqual(len(os.listdir(os.path.join(self.vor.LIBRARY, "added"))), 2)

    def test_a_file_the_library_cannot_read_is_refused_not_copied(self):
        p = self.write("model.docx", "not text")
        self.assertEqual(self.vor.add_paths([p]), (0, 0))
        self.assertFalse(os.path.exists(os.path.join(self.vor.LIBRARY, "added", "model.docx")))
        self.assertTrue(any("not added" in m for m in SAID), SAID)

    def test_file_already_inside_the_library_is_indexed_in_place(self):
        os.makedirs(self.vor.LIBRARY)
        p = self.write("in.md", "# in\n\nspillway text that sits in the library.\n", self.vor.LIBRARY)
        self.assertEqual(self.vor.add_paths([p])[0], 1)
        self.assertEqual(self.docs(), [p])
        self.assertFalse(os.path.exists(os.path.join(self.vor.LIBRARY, "added")))


class ChatDropTest(Base):
    def chat(self, lines):
        """Run cmd_chat over scripted input; returns the list of (question, n_docs_seen)."""
        asked = []

        def fake_pipeline(q, mode, depth, k, idx=None, keep=None, per_doc=None, **kw):
            asked.append((q, len(set(idx.doc_of))))
            return None, "an answer", None

        self.vor.run_pipeline = fake_pipeline
        self.vor.chat = lambda *a, **k: ""                   # the follow-up rewriter
        feed = iter(lines)

        def fake_input(prompt=""):
            try:
                return next(feed)
            except StopIteration:
                raise EOFError

        real = builtins.input
        builtins.input = fake_input
        try:
            a = self.vor.argparse.Namespace(depth=4, k=8, doc=None, per_doc=None)
            self.vor.cmd_chat(a)
        finally:
            builtins.input = real
        return asked

    def test_dragged_file_is_added_and_the_next_question_can_see_it(self):
        first = self.write("first.md", "# first\n\nbreach text for the document already there.\n")
        self.vor.add_paths([first])
        new = self.write("New Pond.md", "# pond\n\npond text for the document dragged in.\n")
        asked = self.chat(["what about breach", new.replace(" ", "\\ "), "what about pond"])
        # the dragged path was never sent to the model as a question
        self.assertEqual([q for q, _ in asked], ["what about breach", "what about pond"])
        # and the chat that was already open now reads two documents, not one
        self.assertEqual([n for _, n in asked], [1, 2])

    def test_ollama_down_during_a_drop_does_not_end_the_chat(self):
        first = self.write("first.md", "# first\n\nbreach text for the document already there.\n")
        self.vor.add_paths([first])
        new = self.write("n.md", "# n\n\nculvert text for a document that Ollama never gets to embed.\n")

        def down(texts, model=None):
            raise RuntimeError("cannot reach Ollama at http://127.0.0.1:11434")

        good = self.vor.embed
        self.vor.embed = down
        try:
            asked = self.chat([new.replace(" ", "\\ "), "still here"])
        finally:
            self.vor.embed = good
        self.assertEqual([q for q, _ in asked], ["still here"])
        self.assertTrue(any(m.startswith("not indexed") for m in SAID), SAID)

    def test_a_file_with_no_readable_text_is_not_announced_as_ready(self):
        first = self.write("first.md", "# first\n\nbreach text for the document already there.\n")
        self.vor.add_paths([first])
        scan = self.write("scan.md", "x\n")              # stands in for a scanned PDF
        asked = self.chat([scan.replace(" ", "\\ "), "what about breach"])
        self.assertEqual([n for _, n in asked], [1])
        self.assertFalse(any("Ready" in m for m in SAID), SAID)
        self.assertTrue(any("vor ocr" in m and "no readable text" in m for m in SAID), SAID)


if __name__ == "__main__":
    unittest.main()
