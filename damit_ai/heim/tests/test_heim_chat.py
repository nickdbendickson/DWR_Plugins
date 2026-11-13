"""heim-chat looks each question up in the indexed documents (rag_query.py --chat).

Why this exists: heim-chat was `ollama run heim`, the bare model. It never read a document,
so the knowledge pack, and anything added with heim-add, was invisible in a conversation.

A real HTTP stand-in answers /api/embed and /api/chat, so what is tested is the request the
model would actually receive: is the passage in it, with its source header.
"""
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HEIM = os.path.dirname(HERE)
AI = os.path.dirname(HEIM)
TOOLS = os.path.join(HEIM, "tools")
DIMS = ["refine", "velocity", "terrain", "kit", "culvert"]


def vec(text):
    t = text.lower()
    v = [1.0 if d in t else 0.0 for d in DIMS] + [0.05]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


class Ollama(BaseHTTPRequestHandler):
    embeds, chats, replies = [], [], []

    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if self.path == "/api/embed":
            Ollama.embeds.append(req)
            self.wfile.write(json.dumps({"embeddings": [vec(t) for t in req["input"]]}).encode())
            return
        Ollama.chats.append(req)
        text = Ollama.replies.pop(0) if Ollama.replies else "It is off by default [heim_knowledge] K.md."
        self.wfile.write((json.dumps({"message": {"content": text}, "done": False}) + "\n").encode())
        self.wfile.write((json.dumps({"message": {"content": ""}, "done": True}) + "\n").encode())


def setUpModule():
    global SRV, THREAD, TMP, rq, rag, _ENV
    SRV = HTTPServer(("127.0.0.1", 0), Ollama)
    THREAD = threading.Thread(target=SRV.serve_forever, daemon=True)
    THREAD.start()
    TMP = tempfile.mkdtemp(prefix="heim-chat-")
    _ENV = {k: os.environ.get(k) for k in ("HEIM_DB", "OLLAMA_URL")}
    os.environ["HEIM_DB"] = os.path.join(TMP, "local-ai", "knowledge.db")
    os.environ["OLLAMA_URL"] = "http://127.0.0.1:%d" % SRV.server_address[1]
    os.makedirs(os.path.join(TMP, "local-ai"))
    mods = []
    for name in ("rag_query", "rag_index"):
        spec = importlib.util.spec_from_file_location(name + "_chat_test", os.path.join(TOOLS, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        mods.append(m)
    rq, rag = mods


def tearDownModule():
    SRV.shutdown()
    SRV.server_close()
    THREAD.join(5)
    for k, v in _ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(TMP, True)


PACK = [("heim_knowledge", "K.md", "REFINE_NEAR is off by default because near-dam refine zeroes velocity."),
        ("heim_knowledge", "T.md", "A DEM in metres overrides WORK_CRS: a terrain swap with no error."),
        ("engine_docs", "anuga.md", "A culvert is routed with the Boyd method.")]


class Base(unittest.TestCase):
    def setUp(self):
        Ollama.embeds, Ollama.chats, Ollama.replies = [], [], []
        if os.path.exists(rq.DB):
            os.remove(rq.DB)
        shutil.rmtree(os.path.join(TMP, "local-ai", "added"), True)
        self._np = rq.np

    def tearDown(self):
        rq.np = self._np

    def index(self, rows=PACK):
        db = sqlite3.connect(rq.DB)
        rag.ensure_schema(db)
        db.executemany("INSERT INTO chunks (collection,path,idx,txt,vec) VALUES (?,?,?,?,?)",
                       [(c, p, 0, t, json.dumps(vec(t))) for c, p, t in rows])
        db.commit()
        db.close()

    def talk(self, lines, coll="all", top_k=2):
        it = iter(lines)

        def ask(prompt):
            try:
                return next(it)
            except StopIteration:
                raise EOFError
        out = io.StringIO()
        with redirect_stdout(out):
            rq.chat(coll, top_k, ask=ask)
        return out.getvalue()

    def prompt(self, n=-1):
        return Ollama.chats[n]["messages"][-1]["content"]


class ChatLooksThingsUpTest(Base):
    def test_the_passage_reaches_the_model_with_its_source(self):
        self.index()
        said = self.talk(["why is refine off by default?"])
        p = self.prompt()
        self.assertIn("--- [heim_knowledge] K.md (chunk 0) ---", p)
        self.assertIn("near-dam refine zeroes velocity", p)
        self.assertNotIn("Boyd", p, "top_k=2: the unrelated passage stays out")
        self.assertIn("Answer using ONLY the context below", p)
        self.assertIn("Reading 3 passages: engine_docs (1), heim_knowledge (2)", said)
        self.assertIn("[heim_knowledge] K.md (chunk 0)", said)
        self.assertNotIn("NO DOCUMENTS", said)

    def test_a_follow_up_is_searched_with_the_question_before_it(self):
        self.index()
        Ollama.replies = ["Because it zeroes velocity."]
        self.talk(["why is refine off by default?", "what does it do to the result?"])
        self.assertEqual(len(Ollama.embeds[1]["input"]), 2)
        self.assertIn("why is refine off by default?", Ollama.embeds[1]["input"][1])
        p = self.prompt()
        self.assertIn("--- [heim_knowledge] K.md (chunk 0) ---", p,
                      "'it' has no search word of its own; the earlier question carries the lookup")
        self.assertIn("EARLIER IN THIS CONVERSATION", p)
        self.assertIn("Answer: Because it zeroes velocity.", p)
        self.assertTrue(p.rstrip().endswith("what does it do to the result?"))

    def test_one_collection_only(self):
        self.index()
        said = self.talk(["how is a culvert routed?"], coll="engine_docs")
        self.assertIn("Reading 1 passages: engine_docs (1)", said)
        self.assertIn("Boyd", self.prompt())
        self.assertNotIn("K.md", self.prompt())

    def test_pure_python_ranks_the_same_as_numpy(self):
        self.index()
        ranked = {}
        for label, np_ in (("numpy", self._np), ("python", None)):
            rq.np = np_
            if label == "numpy" and np_ is None:
                continue
            ranked[label] = [(c, p) for _, c, p, _, _ in rq.Store("all").search(["terrain swap"], 3)]
        self.assertEqual(ranked["python"][0], ("heim_knowledge", "T.md"))
        if "numpy" in ranked:
            self.assertEqual(ranked["numpy"], ranked["python"])


class NothingIndexedTest(Base):
    def test_no_index_is_said_at_the_start_and_before_every_answer(self):
        said = self.talk(["why is refine off by default?", "and velocity?"])
        self.assertEqual(said.count("NO DOCUMENTS ARE INDEXED"), 3)
        self.assertEqual(Ollama.embeds, [], "nothing to search, so nothing is embedded")
        self.assertEqual(Ollama.chats[0]["messages"][-1]["content"], "why is refine off by default?")
        self.assertNotIn("CONTEXT", self.prompt())

    def test_an_empty_collection_is_named(self):
        self.index()
        said = self.talk(["anything"], coll="standards")
        self.assertIn("NO DOCUMENTS ARE INDEXED in collection 'standards'", said)


class DragAFileTest(Base):
    def drop(self, name, text):
        folder = os.path.join(TMP, "Down loads")
        os.makedirs(folder, exist_ok=True)
        p = os.path.join(folder, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_a_dragged_file_is_indexed_and_the_next_question_reads_it(self):
        self.index()
        p = self.drop("Kit notes.md", "# notes\n\nThe kit composite keeps the box run's terrain.\n")
        said = self.talk([p.replace(" ", "\\ "), "what does the kit composite keep?"])
        self.assertIn("Ready: ask about it.", said)
        self.assertEqual(len(Ollama.chats), 1, "the pasted path is not sent to the model as a question")
        self.assertIn("--- [added] Kit notes.md (chunk 0) ---", self.prompt())
        self.assertIn("keeps the box run's terrain", self.prompt())

    def test_a_dragged_file_into_a_chat_on_one_collection_says_it_will_not_be_read(self):
        self.index()
        p = self.drop("x.md", "# x\n\nculvert kit notes.\n")
        said = self.talk(["'%s'" % p], coll="heim_knowledge")
        self.assertIn("this chat reads only collection 'heim_knowledge'", said)

    def test_a_question_that_names_a_file_is_still_a_question(self):
        self.index()
        self.talk(["what is in K.md about velocity?"])
        self.assertEqual(len(Ollama.chats), 1)
        self.assertEqual(rq.dropped_paths("README.md"), [])
        self.assertEqual(rq.dropped_paths("/no/such/file.pdf"), [])
        self.assertEqual(rq.dropped_paths('an "unclosed quote'), [])

    def test_an_index_changed_from_another_window_is_reloaded(self):
        self.index(PACK[:1])
        store = rq.Store("all")
        self.assertEqual(len(store.rows), 1)
        self.assertFalse(store.changed())
        self.index([("added", "new.md", "kit terrain notes")])
        self.assertTrue(store.changed())
        store.load()
        self.assertEqual(len(store.rows), 2)


class OneShotUnchangedTest(Base):
    def test_the_one_shot_prompt_is_what_it_was(self):
        hits = [(0.9, "heim_knowledge", "K.md", 0, "text one"), (0.5, "engine_docs", "a.md", 3, "text two")]
        self.assertEqual(
            rq.build_prompt("the question", hits),
            "Answer using ONLY the context below. Cite each fact with the collection "
            "and file path shown in its bracket header. If insufficient, say so plainly.\n\n"
            "=== CONTEXT ===\n--- [heim_knowledge] K.md (chunk 0) ---\ntext one\n\n"
            "--- [engine_docs] a.md (chunk 3) ---\ntext two\n=== QUESTION ===\nthe question")

    def test_one_shot_still_refuses_an_empty_index(self):
        with self.assertRaises(SystemExit) as cm:
            rq.retrieve("q", "all", 3)
        self.assertIn("No index at", str(cm.exception))
        self.index()
        with self.assertRaises(SystemExit) as cm:
            rq.retrieve("q", "standards", 3)
        self.assertIn("Collection 'standards' empty", str(cm.exception))
        self.assertEqual(rq.retrieve("refine velocity", "all", 1)[0][1:3], ("heim_knowledge", "K.md"))


class InstalledCommandTest(unittest.TestCase):
    def slurp(self, *parts):
        with open(os.path.join(AI, *parts), encoding="utf-8") as f:
            return f.read()

    def test_mac_heim_chat_is_the_lookup_chat_and_bare_is_still_there(self):
        sh = self.slurp("heim", "setup-local-heim.sh")
        fn = sh[sh.index("function heim-chat {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertRegex(fn, r"rag_query\.py --chat")
        self.assertRegex(fn, r"--bare")
        self.assertRegex(fn, r"ollama run heim")
        self.assertTrue(re.search(r'(?m)^alias heim="[^"\n]*rag_query\.py"$', sh),
                        "the docs say `heim \"question\"`; the Mac installer never defined it")

    def test_linux_and_windows_have_it_too(self):
        for name in ("install-local-ai-linux.sh", "install-local-ai-windows.ps1"):
            if not os.path.isfile(os.path.join(AI, name)):
                continue        # the DamSafety copy of damit_ai ships the Mac installer only
            self.assertTrue(re.search(r"heim-chat\b.*rag_query\.py.*--chat", self.slurp(name)), name)

    def test_windows_replaces_its_profile_block_the_same_way(self):
        name = "install-local-ai-windows.ps1"
        if not os.path.isfile(os.path.join(AI, name)):
            self.skipTest("no Windows installer in this copy")
        ps = self.slurp(name)
        self.assertNotIn("profile block already present", ps)
        self.assertIn("'^# >>> dam local AI \\(.*\\) >>>$'", ps)
        self.assertIn("if ($inBlock) { $kept.AddRange($buf) }", ps, "an unterminated block must be kept")
        self.assertIn("if ($DoHeim -and $DoVor)", ps)


LINUX = os.path.join(AI, "install-local-ai-linux.sh")
OLD_BLOCK = """# >>> dam local AI (pierre + dam-research) >>>
export DAMRESEARCH_HOME="/home/u/DamResearch"
alias pierre='"/home/u/local-ai/python" "/home/u/local-ai/rag_query.py"'
# <<< dam local AI <<<
"""
STALE_BLOCK = """# >>> dam local AI (heim + vor) >>>
alias heim='"/home/u/local-ai/python" "/home/u/local-ai/rag_query.py"'
# <<< dam local AI <<<
"""


@unittest.skipUnless(os.path.isfile(LINUX), "this copy of damit_ai ships no Linux installer")
@unittest.skipUnless(shutil.which("bash"), "bash not installed")
class LinuxShellBlockTest(unittest.TestCase):
    """Step 5 of the Linux installer, cut out and run under bash. It wrote its block once and
    then said "already present" for ever, so a machine installed before heim-add or heim-chat
    existed never got them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="heim-rc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rc = os.path.join(self.tmp, ".bashrc")
        with open(LINUX, encoding="utf-8") as f:
            text = f.read()
        a = text.index('say "Step 5 ')
        b = text.index('say "Done"', a)
        self.step = text[a:b]

    def run_step(self, before, heim=1, vor=1):
        if before is not None:
            with open(self.rc, "w", encoding="utf-8") as f:
                f.write(before)
        code = ('set -euo pipefail\nsay(){ :; }; ok(){ echo "OK $*"; }; warn(){ echo "WARN $*"; }\n'
                'RC="%s"; MODELS_DIR=/m; NUM_CTX=8192; LOCAL_AI=/home/u/local-ai; VOR_DIR=/home/u/vor\n'
                'RHOME=/home/u/Vor; WATCH_MODEL=qwen3:4b; DO_HEIM=%d; DO_VOR=%d\n' % (self.rc, heim, vor)) + self.step
        r = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        with open(self.rc, encoding="utf-8") as f:
            return f.read(), r.stdout

    def test_a_block_from_before_the_rename_is_replaced_and_the_rest_is_kept(self):
        rc, _ = self.run_step("export EDITOR=vim\n\n" + OLD_BLOCK + "alias ll='ls -l'\n")
        self.assertNotIn("pierre", rc)
        self.assertNotIn("DAMRESEARCH_HOME", rc)
        self.assertIn("export EDITOR=vim\n", rc)
        self.assertIn("alias ll='ls -l'\n", rc)
        self.assertEqual(rc.count("# >>> dam local AI"), 1)
        self.assertEqual(rc.count("# <<< dam local AI <<<"), 1)
        self.assertRegex(rc, r"alias heim-chat='.*rag_query\.py\" --chat'")
        self.assertRegex(rc, r"alias heim-add='.*rag_index\.py\" --add'")
        self.assertIn('export VOR_HOME="/home/u/Vor"', rc)
        with open(self.rc + ".bak-localai", encoding="utf-8") as f:
            self.assertIn("pierre", f.read(), "the backup is the file as it was")

    def test_a_current_block_without_the_new_commands_gets_them(self):
        rc, _ = self.run_step("export EDITOR=vim\n" + STALE_BLOCK)
        self.assertEqual(rc.count("# >>> dam local AI (heim + vor) >>>"), 1)
        self.assertIn("alias heim-chat=", rc)

    def test_running_it_again_changes_nothing(self):
        once, _ = self.run_step("export EDITOR=vim\n")
        twice, _ = self.run_step(None)
        thrice, _ = self.run_step(None)
        self.assertEqual(twice.count("# >>> dam local AI"), 1)
        self.assertEqual(twice, thrice, "no blank line or block accumulates per run")
        self.assertEqual(once.count("alias heim-chat="), 1)

    def test_no_rc_file_yet(self):
        rc, _ = self.run_step(None)
        self.assertTrue(rc.startswith("# >>> dam local AI (heim + vor) >>>\n"))

    def test_a_block_someone_cut_the_end_off_is_not_eaten_to_the_end_of_the_file(self):
        before = "# >>> dam local AI (heim + vor) >>>\nalias heim=mine\nexport KEEP_ME=1\n"
        rc, _ = self.run_step(before)
        self.assertIn("export KEEP_ME=1\n", rc)
        self.assertIn("alias heim=mine\n", rc)
        self.assertIn("alias heim-chat=", rc)

    def test_installing_one_ai_leaves_the_block_for_both_alone(self):
        before = "export EDITOR=vim\n" + STALE_BLOCK.replace("# <<<", "alias vor=x\n# <<<")
        rc, out = self.run_step(before, vor=0)
        self.assertEqual(rc, before)
        self.assertIn("WARN shell block", out)

    def test_installing_one_ai_first_writes_only_its_commands(self):
        rc, _ = self.run_step("", heim=0)
        self.assertIn("alias vor=", rc)
        self.assertNotIn("heim-chat", rc)


if __name__ == "__main__":
    unittest.main()
