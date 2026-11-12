#!/usr/bin/env python3
# eval_damresearch.py v1.0 - score damresearch's answers against questions with known answers.
#
#   python3 eval_damresearch.py                         # the dam-research model, all questions
#   python3 eval_damresearch.py --model qwen3.6:27b     # any Ollama model (run once per model)
#   python3 eval_damresearch.py --only td39-froehlich2008-k,p64-exercises
#   python3 eval_damresearch.py --list                  # questions + whether their source is indexed
#
# Each question (eval/questions.json) names the source file (regex), the PDF page(s) the answer is on,
# and the facts (regexes) that must be in the answer. A question passes when
#   1. facts:    every fact appears in the answer, and
#   2. grounded: at least one quote that the code checked against its page comes from that file
#                on one of those pages (+/-1, for printed vs PDF numbering).
# Questions whose source is not indexed are skipped, except "if_missing": "not_covered" ones, which
# then pass only if the tool says the library does not cover it and invents none of the "forbid" numbers.
#
# Writes ~/DamResearch/reports/eval_<model>_<date>.md (and .json) with the score, the time per
# question and every answer, so two models can be compared side by side. Nothing is sent off the Mac.
import os, re, sys, io, json, time, argparse, contextlib
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
QFILE = os.path.join(HERE, "eval", "questions.json")
NOT_COVERED = re.compile(r"(no passage|nothing to cite|not (cover|address|found|contain|in the library)|"
                         r"does not (cover|address|say|contain|include)|doesn't (cover|say)|no (information|evidence|mention))", re.I)


def main():
    ap = argparse.ArgumentParser(description="Score damresearch answers against known answers.")
    ap.add_argument("--model", help="Ollama model to test (default: dam-research, or $DAMRESEARCH_MODEL)")
    ap.add_argument("--questions", default=QFILE)
    ap.add_argument("--only", help="comma list of question ids")
    ap.add_argument("-k", type=int, default=8, help="passages read per question (as damresearch ask)")
    ap.add_argument("--list", action="store_true", help="list questions and whether their source is indexed")
    a = ap.parse_args()
    if a.model:
        os.environ["DAMRESEARCH_MODEL"] = a.model          # read by damresearch at import
    sys.path.insert(0, HERE)
    import damresearch as dr

    qs = json.load(open(a.questions, encoding="utf-8"))["questions"]
    if a.only:
        want = set(x.strip() for x in a.only.split(","))
        qs = [q for q in qs if q["id"] in want]
    db = dr.connect()
    files = [os.path.basename(r[0]) for r in db.execute("SELECT path FROM docs")]

    def indexed(q):
        pat = re.compile(q["doc"], re.I)
        return [f for f in files if pat.search(f)]

    if a.list:
        for q in qs:
            hit = indexed(q)
            print("%-28s %-9s %s" % (q["id"], "indexed" if hit else "MISSING", hit[0] if hit else q["doc"]))
        return

    model = dr.CHAT_MODEL
    say = print
    say("Testing model %s on %d question(s); library has %d documents.\n" % (model, len(qs), len(files)))
    results = []
    t_all = time.time()
    for n, q in enumerate(qs, 1):
        docs = indexed(q)
        missing = not docs
        if missing and q.get("if_missing") != "not_covered":
            say("[%d/%d] %-28s SKIP  source not in the library (%s)" % (n, len(qs), q["id"], q["doc"]))
            results.append(dict(id=q["id"], status="skip", why="source not indexed: " + q["doc"]))
            continue
        t0 = time.time()
        log = io.StringIO()
        try:
            with contextlib.redirect_stdout(log):
                _, rendered, ev = dr.run_pipeline(q["q"], "ask", 1, a.k, out=log, save=False)
        except Exception as e:
            say("[%d/%d] %-28s ERROR %s" % (n, len(qs), q["id"], e))
            results.append(dict(id=q["id"], status="error", why=str(e)))
            continue
        secs = time.time() - t0
        text = rendered or ""
        r = dict(id=q["id"], q=q["q"], secs=round(secs, 1), answer=text, findings=len(ev.findings),
                 rejected=len(ev.rejected))
        if missing:
            invented = [f for f in q.get("forbid", []) if re.search(f, text, re.I)]
            ok = (not ev.findings or bool(NOT_COVERED.search(text))) and not invented
            r.update(status="pass" if ok else "fail", test="not covered",
                     why="" if ok else ("invented: %s" % ", ".join(invented) if invented else "answered instead of saying not covered"))
        else:
            facts_missing = [f for f in q["facts"] if not re.search(f, text, re.I)]
            pages = set()
            for p in q.get("pages") or []:
                pages.update((p - 1, p, p + 1))
            pat = re.compile(q["doc"], re.I)
            grounded = []
            for f in ev.findings:
                h = ev.sources[f["source"] - 1]
                if pat.search(os.path.basename(h.path)) and (not pages or h.page in pages):
                    grounded.append("PDF p.%d" % h.page)
            ok = not facts_missing and bool(grounded)
            why = []
            if facts_missing:
                why.append("missing fact(s): " + ", ".join(facts_missing))
            if not grounded:
                why.append("no checked quote from %s p.%s" % (docs[0], "/".join(map(str, q.get("pages") or ["any"]))))
            r.update(status="pass" if ok else "fail", test="answer", grounded=sorted(set(grounded)), why="; ".join(why))
        results.append(r)
        say("[%d/%d] %-28s %s  %5.0f s  %s" % (n, len(qs), q["id"], r["status"].upper(), secs, r["why"]))

    scored = [r for r in results if r["status"] in ("pass", "fail", "error")]
    passed = sum(r["status"] == "pass" for r in scored)
    total_s = time.time() - t_all
    say("\nScore: %d / %d passed (%d skipped), %.1f min.  Model: %s"
        % (passed, len(scored), len(results) - len(scored), total_s / 60, model))

    os.makedirs(dr.REPORTS, exist_ok=True)
    stem = os.path.join(dr.REPORTS, "eval_%s_%s" % (re.sub(r"[^\w.-]+", "_", model), datetime.now().strftime("%Y-%m-%d_%H%M")))
    with open(stem + ".json", "w", encoding="utf-8") as f:
        json.dump(dict(model=model, when=datetime.now().isoformat(timespec="seconds"), passed=passed,
                       scored=len(scored), minutes=round(total_s / 60, 1), results=results), f, indent=1)
    L = ["# damresearch answer test: %s" % model, "",
         "%s. **%d / %d passed**, %d skipped, %.1f min." % (datetime.now().strftime("%Y-%m-%d %H:%M"), passed,
                                                          len(scored), len(results) - len(scored), total_s / 60), "",
         "| Question | Result | Time | Why |", "|---|---|---|---|"]
    for r in results:
        L.append("| %s | %s | %s | %s |" % (r["id"], r["status"], "%s s" % r.get("secs", ""), (r.get("why") or "").replace("|", "/")))
    L.append("")
    for r in results:
        if r.get("answer") is not None:
            L += ["## %s (%s)" % (r["id"], r["status"]), "", "**Q:** " + r["q"], "", r["answer"].strip(), ""]
    with open(stem + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    say("Report: %s.md" % stem)


if __name__ == "__main__":
    main()
