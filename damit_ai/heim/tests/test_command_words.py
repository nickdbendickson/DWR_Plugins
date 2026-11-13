"""`heim doctor`, `heim add FILE`, `heim chat` (rag_query.py 2.4).

Why this exists: Vor takes its commands as words (`vor doctor`, `vor add`, `vor chat`). Heim
had the same three only as separate commands with a hyphen (`heim-doctor`, `heim-add`,
`heim-chat`), and `heim` followed by anything was a question. So `heim doctor`, typed straight
after `vor doctor`, put the one word "doctor" to the model and printed an answer where the
check should have been. Nothing said it was not a command.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(HERE), "tools")


def load():
    spec = importlib.util.spec_from_file_location("rag_query_words_test", os.path.join(TOOLS, "rag_query.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class CommandWordTest(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.mkdtemp(prefix="heim-words-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = os.path.join(self.tmp, "a report.pdf")
        with open(self.file, "w") as f:
            f.write("x")

    def test_doctor(self):
        self.assertEqual(self.m.command_word(["doctor"]), ("index", ["--doctor"]))
        self.assertEqual(self.m.command_word(["doctor", "--library"]), ("index", ["--doctor", "--library"]))

    def test_chat(self):
        self.assertEqual(self.m.command_word(["chat"]), ("query", ["--chat"]))

    def test_add_takes_files_that_exist(self):
        self.assertEqual(self.m.command_word(["add", self.file, self.tmp]),
                         ("index", ["--add", self.file, self.tmp]))

    def test_add_alone_gets_the_usage_line(self):
        with self.assertRaises(SystemExit) as cm:
            self.m.command_word(["add"])
        self.assertIn("usage: heim add FILE", str(cm.exception))

    def test_a_question_that_starts_with_one_of_the_words_is_still_a_question(self):
        for q in (["doctor", "blade", "wear?"], ["doctor blade wear?"], ["add", "a", "culvert", "to", "the", "run?"],
                  ["add", os.path.join(self.tmp, "not-there.pdf")], ["chat", "with", "me"],
                  ["what", "does", "doctor", "check?"], ["-c", "standards", "doctor"], []):
            self.assertIsNone(self.m.command_word(q), q)


class RunsTheRealCheckTest(unittest.TestCase):
    """End to end, with no Ollama: `rag_query.py doctor --library` must print the doctor's
    report (from rag_index.py), not go looking for a model to answer "doctor"."""

    def run_tool(self, *args):
        tmp = tempfile.mkdtemp(prefix="heim-words-run-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, HOME=tmp, OLLAMA_URL="http://127.0.0.1:9")
        for k in ("HEIM_HOME", "HEIM_DB", "LOCAL_AI"):
            env.pop(k, None)
        return subprocess.run([sys.executable, os.path.join(TOOLS, "rag_query.py")] + list(args),
                              env=env, capture_output=True, text=True, timeout=60)

    def test_doctor_word_runs_the_check(self):
        r = self.run_tool("doctor", "--library")
        self.assertEqual(r.returncode, 1, (r.stdout, r.stderr))        # an empty home: no index
        self.assertIn("PROBLEM  no index at", r.stdout)
        self.assertNotIn("Top ", r.stdout)
        self.assertNotIn("Answer (", r.stdout)

    def test_help_names_the_three_words(self):
        r = self.run_tool("--help")
        for w in ("heim doctor", "heim add FILE", "heim chat"):
            self.assertIn(w, r.stdout)


if __name__ == "__main__":
    unittest.main()
