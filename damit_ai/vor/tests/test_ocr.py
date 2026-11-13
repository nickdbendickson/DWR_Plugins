"""vor ocr, and the ingest that follows it.

Why this exists: `vor ocr` wrote a searchable copy to library/_ocr, indexed the copy and
dropped the scanned original from the index, and left the original file where it was. The
next plain `vor ingest` saw a file that was not in the index and indexed it again. From then
on the document was in the library twice (the copy, and the original with its unreadable
pages), the "pages with no text layer" count was back, and `vor ocr` would do the same
documents again on every run. Nothing failed. Stopping `vor ocr` with Ctrl-C was worse:
every document finished so far had already been dropped and its copy was never indexed.

No Ollama, no PyMuPDF, no ocrmypdf. A ".pdf" here is a text file whose pages are separated
by a form feed, a page that says SCAN is a scanned page with no text layer, and the stand-in
ocrmypdf turns each SCAN page into words.
"""
import argparse
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "vor.py")
SAID = []

FAKE_OCR = r'''#!%s
import os, sys
src, dst = sys.argv[-2], sys.argv[-1]
stop = os.environ.get("FAKE_OCR_FAIL_ON", "")
if stop and stop in os.path.basename(src):
    sys.exit(2)
pages = open(src, encoding="utf-8").read().split("\f")
if not any(p.strip() == "SCAN" for p in pages):
    sys.exit(6)
out = [("recognised words about the spillway and the breach on page %%d of %%s, enough to index"
        %% (i, os.path.basename(src))) if p.strip() == "SCAN" else p for i, p in enumerate(pages, 1)]
open(dst, "w", encoding="utf-8").write("\f".join(out))
''' % sys.executable

TEXT = "This page has a text layer about the embankment, the spillway and the breach width. "


def pdf(*pages):
    return "\f".join(pages)


def fake_extract_pdf(path):
    with open(path, encoding="utf-8") as f:
        pages = f.read().split("\f")
    return ([(i, str(i), "" if p.strip() == "SCAN" else p) for i, p in enumerate(pages, 1)], "fake", {})


def load(home):
    old = {k: os.environ.get(k) for k in ("VOR_HOME", "VOR_DB")}
    os.environ["VOR_HOME"] = home
    os.environ["VOR_DB"] = os.path.join(home, "vor.db")
    try:
        spec = importlib.util.spec_from_file_location("vor_ocr_under_test", SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    mod.embed = lambda texts, model=None: [[1.0, 0.0] for _ in texts]
    mod.extract_pdf = fake_extract_pdf
    mod.say = lambda msg: SAID.append(str(msg))
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        del SAID[:]
        self.tmp = tempfile.mkdtemp(prefix="vor-ocr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "Vor")
        self.vor = load(self.home)
        self.lib = self.vor.LIBRARY
        os.makedirs(self.lib)
        exe = os.path.join(self.tmp, "ocrmypdf")
        with open(exe, "w") as f:
            f.write(FAKE_OCR)
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR)
        self.vor.ocr_exe = lambda: exe
        os.environ.pop("FAKE_OCR_FAIL_ON", None)
        self.addCleanup(os.environ.pop, "FAKE_OCR_FAIL_ON", None)

    def write(self, rel, text):
        p = os.path.join(self.lib, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def ingest(self, *paths):
        self.vor.cmd_ingest(argparse.Namespace(paths=list(paths), prune=False))

    def ocr(self):
        self.vor.cmd_ocr(argparse.Namespace(min_fraction=0.3))

    def docs(self):
        db = self.vor.connect()
        try:
            return {os.path.relpath(p, self.lib): (nt or "") for p, nt in db.execute("SELECT path, notext FROM docs")}
        finally:
            db.close()

    def checks(self):
        return self.vor.library_checks()


class OcrThenIngestTest(Base):
    def setUp(self):
        Base.setUp(self)
        self.scan = self.write("reports/scan.pdf", pdf(TEXT * 3, "SCAN", "SCAN"))
        self.plain = self.write("reports/plain.pdf", pdf(TEXT * 3, TEXT * 2))
        self.ingest()
        self.assertEqual(self.docs()[os.path.join("reports", "scan.pdf")], "2,3")

    def test_ocr_replaces_the_scanned_original_in_the_index(self):
        self.ocr()
        self.assertEqual(self.docs(), {os.path.join("_ocr", "scan.ocr.pdf"): "",
                                       os.path.join("reports", "plain.pdf"): ""})
        self.assertTrue(os.path.exists(self.scan), "the original file must be left alone")

    def test_the_next_ingest_does_not_bring_the_original_back(self):
        self.ocr()
        before = self.docs()
        self.ingest()
        self.assertEqual(self.docs(), before)
        self.ingest()
        self.assertEqual(self.docs(), before)

    def test_a_second_ocr_has_nothing_to_do(self):
        self.ocr()
        self.ingest()
        del SAID[:]
        self.ocr()
        self.assertTrue(any("No scanned PDFs" in m for m in SAID), SAID)
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "_ocr"))), ["ocr_sources.json", "scan.ocr.pdf"])

    def test_the_doctor_does_not_send_you_to_ingest_the_original(self):
        self.ocr()
        got = self.checks()
        self.assertFalse([t for lvl, t in got if "not in the index" in t], got)
        self.assertFalse([t for lvl, t in got if "no text layer" in t], got)
        self.assertTrue([t for lvl, t in got if lvl == "OK" and "every file" in t], got)

    def test_an_index_rebuilt_from_nothing_still_takes_the_copy_not_the_original(self):
        self.ocr()
        want = self.docs()
        os.remove(self.vor.DB_PATH)
        self.ingest()
        self.assertEqual(self.docs(), want)

    def test_an_original_indexed_again_by_the_old_code_is_dropped(self):
        self.ocr()
        db = self.vor.connect()                       # what vor <= 1.3.0 did on the next ingest
        self.vor.ingest_one(db, self.scan, self.vor.sha1_of(self.scan), None, 1, 1)
        db.commit()
        db.close()
        self.assertIn(os.path.join("reports", "scan.pdf"), self.docs())
        self.ingest()
        self.assertEqual(sorted(self.docs()), [os.path.join("_ocr", "scan.ocr.pdf"),
                                               os.path.join("reports", "plain.pdf")])

    def test_a_copy_made_before_the_record_existed_is_adopted(self):
        self.ocr()
        os.remove(os.path.join(self.lib, "_ocr", "ocr_sources.json"))
        want = self.docs()
        self.ingest()
        self.assertEqual(self.docs(), want)
        with open(os.path.join(self.lib, "_ocr", "ocr_sources.json")) as f:
            rec = json.load(f)
        self.assertEqual([v["copy"] for v in rec.values()], ["scan.ocr.pdf"])

    def test_a_library_the_old_code_doubled_is_repaired_by_one_ingest(self):
        self.ocr()
        os.remove(os.path.join(self.lib, "_ocr", "ocr_sources.json"))     # 1.3.0 kept no record
        db = self.vor.connect()
        self.vor.ingest_one(db, self.scan, self.vor.sha1_of(self.scan), None, 1, 1)
        db.commit()
        db.close()
        self.assertEqual(len(self.docs()), 3)                             # the document, twice
        self.ingest()
        self.assertEqual(self.docs(), {os.path.join("_ocr", "scan.ocr.pdf"): "",
                                       os.path.join("reports", "plain.pdf"): ""})
        self.assertTrue(any("left out" in m for m in SAID), SAID)

    def test_an_original_that_changed_is_a_new_document_and_its_old_copy_goes(self):
        self.ocr()
        self.write("reports/scan.pdf", pdf(TEXT * 3, TEXT + "a new second page with its own text layer now. ", "SCAN"))
        self.ingest()
        got = self.docs()
        self.assertEqual(sorted(got), [os.path.join("reports", "plain.pdf"), os.path.join("reports", "scan.pdf")])
        self.assertFalse(os.path.exists(os.path.join(self.lib, "_ocr", "scan.ocr.pdf")))
        self.assertTrue(any("scan.ocr.pdf" in m and "older version" in m for m in SAID), SAID)
        self.ocr()
        self.assertEqual(sorted(self.docs()), [os.path.join("_ocr", "scan.ocr.pdf"),
                                               os.path.join("reports", "plain.pdf")])


class OcrRunTest(Base):
    def test_two_originals_with_one_file_name_get_two_copies(self):
        self.write("a/report.pdf", pdf(TEXT * 3, "SCAN", "SCAN"))
        self.write("b/report.pdf", pdf(TEXT * 2 + "the other report. ", "SCAN", "SCAN", "SCAN"))
        self.ingest()
        self.ocr()
        got = sorted(self.docs())
        self.assertEqual(len(got), 2, got)
        self.assertTrue(all(g.startswith("_ocr") for g in got), got)
        self.ingest()
        self.assertEqual(sorted(self.docs()), got)

    def test_a_document_ocr_cannot_do_stays_indexed_as_it_was(self):
        self.write("good.pdf", pdf(TEXT * 3 + "the good one. ", "SCAN"))
        self.write("locked.pdf", pdf(TEXT * 3 + "the locked one. ", "SCAN"))
        self.ingest()
        os.environ["FAKE_OCR_FAIL_ON"] = "locked"
        self.ocr()
        self.assertEqual(self.docs(), {os.path.join("_ocr", "good.ocr.pdf"): "", "locked.pdf": "2"})
        self.assertFalse(os.path.exists(os.path.join(self.lib, "_ocr", "locked.ocr.pdf")))

    def test_stopping_part_way_loses_nothing_and_the_next_run_finishes(self):
        self.write("one.pdf", pdf(TEXT * 3 + "document one. ", "SCAN"))
        self.write("two.pdf", pdf(TEXT * 3 + "document two. ", "SCAN"))
        self.ingest()
        real_run = self.vor.subprocess.run
        calls = []

        def run(cmd, *a, **k):
            calls.append(cmd)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_run(cmd, *a, **k)
        self.vor.subprocess.run = run
        try:
            self.ocr()
        finally:
            self.vor.subprocess.run = real_run
        self.assertEqual(self.docs(), {os.path.join("_ocr", "one.ocr.pdf"): "", "two.pdf": "2"})
        self.assertTrue(any("Stopped" in m for m in SAID), SAID)
        self.ocr()
        self.assertEqual(sorted(self.docs()), [os.path.join("_ocr", "one.ocr.pdf"), os.path.join("_ocr", "two.ocr.pdf")])

    def test_a_copy_is_not_sent_through_ocr_again(self):
        self.write("blank.pdf", pdf(TEXT * 3, "SCAN", "SCAN"))
        self.ingest()
        self.ocr()
        # a page OCR could make nothing of stays without text in the copy
        copy = os.path.join(self.lib, "_ocr", "blank.ocr.pdf")
        with open(copy, "w", encoding="utf-8") as f:
            f.write(pdf(TEXT * 3, "SCAN", "SCAN"))
        self.ingest()
        self.assertEqual(self.docs(), {os.path.join("_ocr", "blank.ocr.pdf"): "2,3"})
        del SAID[:]
        self.ocr()
        self.assertTrue(any("No scanned PDFs" in m for m in SAID), SAID)
        self.assertEqual(sorted(f for f in os.listdir(os.path.join(self.lib, "_ocr")) if f.endswith(".pdf")),
                         ["blank.ocr.pdf"])


if __name__ == "__main__":
    unittest.main()
