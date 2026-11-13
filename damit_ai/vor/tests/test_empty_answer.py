"""vor 1.3.0: an answer is never empty.

Why this exists: on the first real run (2026-11-12, qwen3.6:27b, 16 questions) four questions
came back with one to four findings, each quote verified on the right page, and an EMPTY answer.
A reply with no text in it is not an error to Ollama, so the empty string was written into the
report as the answer and the question scored as a failure of the model.

These tests talk to a real HTTP stand-in for Ollama's /api/chat (streamed and not), because
the fault sits at that interface: what a reply looks like when a reasoning model spends its
whole allowance in `message.thinking` and never writes `message.content`.
"""
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from array import array
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "vor.py")
_TMP = tempfile.mkdtemp(prefix="vor-empty-")
os.environ["VOR_HOME"] = _TMP
os.environ["VOR_DB"] = os.path.join(_TMP, "vor.db")

_spec = importlib.util.spec_from_file_location("vor_empty_answer_test", SRC)
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)
dr.embed = lambda texts, model=None: [[1.0, 0.0] for _ in texts]
dr.query_text = lambda model, q: q

PAGE = ("Dams assigned the significant hazard potential classification are those dams where failure "
        "or mis-operation results in no probable loss of human life but can cause economic loss.")
QUOTE = "failure or mis-operation results in no probable loss of human life but can cause economic loss"
FINDINGS = json.dumps({"findings": [{"source": "S1", "quote": QUOTE, "kind": "definition",
                                     "claim": "A significant hazard dam is one whose failure costs money but probably no lives"}],
                       "not_covered": []})
THINK_ONLY = dict(thinking="Let me work through the definition. " * 40, content="", done_reason="length",
                  eval_count=3000)


class Ollama(BaseHTTPRequestHandler):
    script, seen = [], []

    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Ollama.seen.append(req)
        r = dict(content="", thinking="", done_reason="stop", eval_count=5)
        r.update(Ollama.script.pop(0) if Ollama.script else {})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        if not req.get("stream"):
            self.wfile.write(json.dumps({"message": {"role": "assistant", "content": r["content"],
                                                     "thinking": r["thinking"]},
                                         "done": True, "done_reason": r["done_reason"],
                                         "eval_count": r["eval_count"]}).encode())
            return
        for key in ("thinking", "content"):
            text = r[key]
            for i in range(0, len(text), 40):
                self.wfile.write((json.dumps({"message": {"role": "assistant", key: text[i:i + 40]},
                                              "done": False}) + "\n").encode())
        self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                                      "done_reason": r["done_reason"], "eval_count": r["eval_count"]})
                          + "\n").encode())


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Ollama)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls._base = dr.BASE
        dr.BASE = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        dr.BASE = cls._base
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(5)

    def setUp(self):
        Ollama.script, Ollama.seen = [], []
        dr._think_ok[0] = dr._format_ok[0] = True
        self._say = dr.say
        self.said = []
        dr.say = lambda m: self.said.append(str(m))

    def tearDown(self):
        dr.say = self._say

    def evidence(self):
        ev = dr.Evidence()
        row = (1, 1, 9, "9", PAGE, "Federal Guidelines for Dam Safety: Hazard Potential Classification",
               "/lib/FEMA 333.pdf", "", "2004", "FEMA", "FEMA 333")
        s = ev.add_source(dr.Hit(row, 1.0))
        ev.findings.append({"F": 1, "source": s, "quote": QUOTE, "kind": "definition", "check": "exact",
                            "claim": "A significant hazard dam is one whose failure costs money but probably no lives",
                            "sub": "q", "numbers": []})
        return ev


class ReplyRecordTest(Base):
    def test_a_reply_that_is_all_reasoning_is_empty_and_says_why(self):
        Ollama.script = [THINK_ONLY]
        out = io.StringIO()
        text = dr.chat([{"role": "user", "content": "q"}], stream_to=out, num_predict=3000)
        self.assertEqual(text, "")
        self.assertEqual(dr.LAST_REPLY["done_reason"], "length")
        self.assertEqual(dr.LAST_REPLY["eval_count"], 3000)
        self.assertGreater(dr.LAST_REPLY["thinking_chars"], 1000)
        why = dr.empty_reply_reason()
        self.assertIn("length limit (3000 tokens)", why)
        self.assertIn("still reasoning", why)

    def test_the_same_reply_without_streaming(self):
        Ollama.script = [THINK_ONLY]
        self.assertEqual(dr.chat([{"role": "user", "content": "q"}], num_predict=2500), "")
        self.assertEqual((dr.LAST_REPLY["done_reason"], dr.LAST_REPLY["eval_count"]), ("length", 3000))
        self.assertGreater(dr.LAST_REPLY["thinking_chars"], 1000)

    def test_a_stop_with_nothing_written_is_reported_as_that(self):
        Ollama.script = [dict(content="", done_reason="stop", eval_count=1)]
        dr.chat([{"role": "user", "content": "q"}], stream_to=io.StringIO())
        self.assertIn("stopped after 1 token(s) with no text", dr.empty_reply_reason())

    def test_inline_reasoning_is_not_an_answer(self):
        self.assertEqual(dr.strip_think("<think>plan</think>The answer."), "The answer.")
        self.assertEqual(dr.strip_think("<think>ran out while still thinking"), "")
        self.assertEqual(dr.strip_think("the tail of some reasoning</think>\nThe answer."), "The answer.")
        self.assertEqual(dr.strip_think("No tags at all."), "No tags at all.")
        self.assertEqual(dr.strip_think(None), "")
        self.assertEqual(dr.parse_json('<think>hm</think>{"a": 1}'), {"a": 1})


class SynthesizeTest(Base):
    def test_a_normal_answer_is_one_request_and_carries_no_note(self):
        Ollama.script = [dict(content="It is a dam whose failure costs money but no lives [F1].")]
        ev = self.evidence()
        body = dr.synthesize("How is significant hazard defined?", ev, "ask", io.StringIO())
        self.assertEqual(body, "It is a dam whose failure costs money but no lives [F1].")
        self.assertEqual(len(Ollama.seen), 1)
        self.assertEqual((ev.answer_from, ev.answer_note), ("model", ""))
        self.assertEqual(dr.checks_summary(ev, [], {1}, 0), ["all cited quotes found on their pages; no uncited sentences"])

    def test_an_empty_answer_is_asked_for_again_with_reasoning_off_and_more_room(self):
        Ollama.script = [THINK_ONLY, dict(content="Failure causes economic loss but no probable loss of life [F1].")]
        ev = self.evidence()
        body = dr.synthesize("How is significant hazard defined?", ev, "ask", io.StringIO())
        self.assertEqual(body, "Failure causes economic loss but no probable loss of life [F1].")
        first, second = Ollama.seen
        self.assertIs(second["think"], False)
        self.assertGreater(second["options"]["num_predict"], first["options"]["num_predict"])
        self.assertLessEqual(second["options"]["num_predict"], dr.NUM_CTX)
        self.assertIn("Write the answer itself now", second["messages"][-1]["content"])
        self.assertTrue(second["messages"][-1]["content"].startswith(first["messages"][-1]["content"]))
        self.assertEqual(ev.answer_from, "model, second attempt")
        self.assertIn("length limit (3000 tokens) while still reasoning", ev.answer_note)
        self.assertTrue(any("wrote no answer" in m for m in self.said), self.said)

    def test_empty_twice_gives_the_checked_findings_never_nothing(self):
        Ollama.script = [THINK_ONLY, dict(content="<think>still going", done_reason="length", eval_count=6000)]
        ev = self.evidence()
        ev.not_covered.append(("q", "what counts as a lifeline"))
        out = io.StringIO()
        body = dr.synthesize("How is significant hazard defined?", ev, "ask", out)
        self.assertEqual(len(Ollama.seen), 2, "one retry, not a loop")
        self.assertEqual(ev.answer_from, "findings")
        self.assertIn(QUOTE, body)
        self.assertIn("probably no lives.", body)
        self.assertIn("[F1]", body)
        self.assertIn("Not covered: what counts as a lifeline", body)
        self.assertIn(QUOTE, out.getvalue(), "the person watching the terminal sees it too")
        rendered, order, unknown, used = dr.render_citations(body, ev)
        self.assertIn("[1, p.9]", rendered)
        self.assertEqual((order, unknown, used), ([1], [], {1}))
        checks = dr.checks_summary(ev, unknown, used, 0)
        self.assertIn("wrote no answer twice", checks[0])
        self.assertIn("then it reached its length limit (6000 tokens) without writing an answer", checks[0])
        report = dr.build_report("How is significant hazard defined?", "ask", ["q"], [], [], ev, rendered,
                                 order, unknown, used, 0, 12.0)
        self.assertIn("not a written summary", report)
        self.assertIn(QUOTE, report.split("## References")[0])


class PipelineTest(Base):
    """The whole of `vor ask`, against the stand-in: retrieve, extract, check the quote, write."""

    def setUp(self):
        super().setUp()
        if os.path.exists(dr.DB_PATH):
            os.remove(dr.DB_PATH)
        db = dr.connect()
        db.execute("INSERT INTO docs(id, sha1, path, title, number, pages) VALUES(1,'s','/lib/FEMA 333.pdf',"
                   "'Hazard Potential Classification','FEMA 333',21)")
        cur = db.execute("INSERT INTO chunks(doc_id, page, label, idx, txt, vec) VALUES(1,9,'9',0,?,?)",
                         (PAGE, array("f", [1.0, 0.0]).tobytes()))
        if db.fts:
            db.execute("INSERT INTO chunks_fts(rowid, txt, title) VALUES(?,?,?)",
                       (cur.lastrowid, PAGE, "Hazard Potential Classification"))
        dr.meta_set(db, "embed_model", dr.EMBED_MODEL)
        db.commit()
        db.close()

    def ask(self):
        out = io.StringIO()
        _, rendered, ev = dr.run_pipeline("How does FEMA 333 define a significant hazard potential dam?",
                                          "ask", 1, 8, out=out, save=False)
        return rendered, ev

    def test_the_2026_11_12_failure_now_ends_in_a_cited_answer(self):
        Ollama.script = [dict(content=FINDINGS), THINK_ONLY, THINK_ONLY]
        rendered, ev = self.ask()
        self.assertEqual(len(ev.findings), 1)
        self.assertTrue(rendered.strip(), "the answer was empty: the bug this file exists for")
        self.assertRegex(rendered, r"no probable loss of human life")
        self.assertIn("[1, p.9]", rendered)
        self.assertEqual(ev.answer_from, "findings")
        self.assertTrue(any(m.startswith("\nChecks: ") and "wrote no answer twice" in m for m in self.said),
                        self.said)

    def test_an_extraction_that_returns_nothing_is_asked_for_again(self):
        Ollama.script = [dict(THINK_ONLY, eval_count=2500), dict(content=FINDINGS),
                         dict(content="No probable loss of life, but economic loss [F1].")]
        rendered, ev = self.ask()
        self.assertEqual(len(ev.findings), 1)
        self.assertEqual(Ollama.seen[1]["options"]["num_predict"], 5000)
        self.assertEqual(ev.rejected, [])
        self.assertIn("[1, p.9]", rendered)

    def test_an_extraction_that_returns_nothing_twice_says_that_not_invalid_json(self):
        Ollama.script = [dict(THINK_ONLY, eval_count=2500), dict(THINK_ONLY, eval_count=5000)]
        rendered, ev = self.ask()
        self.assertEqual(ev.findings, [])
        self.assertEqual(len(ev.rejected), 1)
        why = ev.rejected[0]["why"]
        self.assertIn("returned nothing for these passages, twice", why)
        self.assertIn("2500 tokens", why)
        self.assertIn("5000 tokens", why)
        self.assertNotIn("JSON", why)
        self.assertEqual(len(Ollama.seen), 2, "no write-up is asked for when there is nothing to write up")
        # a passage WAS read, so the answer must not say the library returned none
        self.assertNotIn("returned no passage", rendered)
        self.assertIn("nothing to cite: 1 passage(s) were read", rendered)
        self.assertIn("the model's reply could not be used (model returned nothing for these passages, twice",
                      rendered)
        self.assertIn("- Hazard Potential Classification, p.9", rendered)

    def test_a_quote_that_fails_its_check_is_counted_in_the_no_answer_message(self):
        bad = json.dumps({"findings": [{"source": "S1", "quote": "failure will probably cause loss of human life",
                                        "claim": "x", "kind": "definition"}], "not_covered": []})
        Ollama.script = [dict(content=bad)]
        rendered, ev = self.ask()
        self.assertEqual(ev.findings, [])
        self.assertIn("the model offered 1 quote(s), and none of them passed the check against its page", rendered)
        self.assertNotIn("returned no passage", rendered)

    def test_no_passage_at_all_still_says_the_library_returned_nothing(self):
        self.assertIn("The library returned no passage that answers this question",
                      dr.no_findings_answer(dr.Evidence(), None))
        self.assertIn("That document returned no passage", dr.no_findings_answer(dr.Evidence(), [1]))

    def test_a_reply_that_is_text_but_not_json_is_still_called_that(self):
        Ollama.script = [dict(content="I could not find anything relevant.")]
        _, ev = self.ask()
        self.assertEqual(ev.rejected[0]["why"], "model reply was not valid JSON")
        self.assertEqual(len(Ollama.seen), 1)


if __name__ == "__main__":
    unittest.main()
