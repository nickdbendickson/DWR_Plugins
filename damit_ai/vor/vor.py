#!/usr/bin/env python3
# vor.py v1.3.0 — local research assistant for dam-safety, dam-design and
# flood-hazard publications. Runs on your Mac against Ollama; nothing leaves the machine.
#
# This is a RESEARCH ASSISTANT. Every statement it writes must be checked against
# the cited page of the source document before it is used for any dam-safety decision.
# It is kept apart from `heim` (the code-triage model): own Ollama model (vor),
# own embedding model, own database (~/Vor/vor.db).
#
#   vor ingest [PATH ...]      add PDFs / .md / .txt (default: ~/Vor/library,
#                                      the drop folder). Unchanged files are skipped.
#                                      --prune drops documents whose file is gone.
#   vor add PATH [PATH ...]    copy the files into ~/Vor/library/added and index them. In
#                                      Terminal, type `vor add ` and drag the files in.
#   vor list                   documents, pages, chunks, pages with no text layer
#   vor meta DOC --title T --authors A --year Y --org O --number N
#                                      fix a document's citation (DOC = id or part of file name)
#   vor bib FILE.bib           take titles/authors/years from a Zotero/BibTeX export
#   vor search QUESTION        retrieval only (no model): best passages with page numbers
#   vor ask QUESTION           quick cited answer (one retrieval pass)
#   vor research QUESTION      deep research: plan sub-questions -> retrieve -> extract
#                                      verbatim evidence -> check every quote against the page
#                                      -> cited report in ~/Vor/reports/
#   vor chat                   ask follow-up questions in a loop. Drag a file or folder into
#                                      the window to add it without leaving the chat.
#
#   search / ask / research / chat also take:
#     --doc KEY        only read this publication. KEY is the id from `vor list`,
#                      or part of its file name, title or report number; punctuation does
#                      not matter, so --doc P-946 finds the file named "... P946.pdf".
#                      Several keys, comma separated, are read together. Naming the
#                      publication in the QUESTION instead hurts: "FEMA" is just another
#                      search word and matches every FEMA document.
#     --per-doc N      passages allowed from any one publication (default 3; with --doc
#                      the cap is lifted unless you set it, so a long guideline can give
#                      up all -k passages).
#   vor ocr                    OCR the scanned PDFs (needs ocrmypdf) and re-ingest them
#   vor remove DOC             drop a document from the index (file is not touched)
#   vor doctor                 check Ollama, the two models and the database
#
# Settings (environment; the installer writes them to ~/.zshrc):
#   VOR_HOME     ~/Vor   (library/, reports/, vor.db)
#   VOR_MODEL    vor    (Ollama persona made by the installer)
#   VOR_EMBED    qwen3-embedding:0.6b
#   VOR_NUM_CTX  context window the persona was built with
#   VOR_THINK    1 = let the model reason before writing the report (slower)
#   OLLAMA_URL           http://127.0.0.1:11434
#
# 1.0.1: reads library.csv in every sub-folder of the library (fetch_asdso.py writes one per folder).
# 1.3.0: an answer is never empty. A reply with no text in it (a reasoning model spending its
#   whole allowance before the first word of the answer) is asked for once more with reasoning
#   off and more room; if that is empty too, the answer is the checked findings themselves,
#   each with its quote and page, and the report says which of the three it was and why.
# 1.2.0: `vor add`, and `vor chat` takes a dragged file: the line Terminal pastes is a path, which
#        used to be sent to the model as a question. A chat that is running keeps its vectors in
#        memory, so before this a document ingested from another tab was invisible to it until
#        the chat was restarted, and nothing said so.
#
# Stdlib only; numpy makes search fast, PyMuPDF gives printed page labels. PDF text
# backends in order: PyMuPDF -> pdftotext -> pypdf.
import os, sys, re, json, time, math, hashlib, sqlite3, argparse, subprocess, difflib, shutil, shlex
import urllib.request, urllib.error, unicodedata
from array import array
from datetime import datetime, timedelta

VERSION = "1.3.0"


def _home():
    """Where the library and its index live: the folder that HOLDS THE INDEX.

    The tool was called damresearch until 2026-11-13 and kept everything in ~/DamResearch
    (research.db). setup-vor.zsh moves that to ~/Vor (vor.db). Until it has run, both names
    can exist: fetch_asdso.py or download-library.zsh run first would create ~/Vor to hold
    the downloads, and a rule of "use ~/Vor if it is there" then opens a new, empty index
    beside the real one. Nothing errors: `vor ask` simply finds no passages. So the index
    decides, not the folder. fetch_asdso.py carries the same rule (vor_home), and
    tests/test_home.py holds the two to the same answer.
    """
    if os.environ.get("VOR_HOME"):
        return os.path.expanduser(os.environ["VOR_HOME"])
    new = os.path.expanduser("~/Vor")
    old = os.path.expanduser("~/DamResearch")
    # DAMRESEARCH_HOME: the old installers exported it, so a library kept somewhere else is
    # still named by it in a shell whose rc file has not been rewritten yet.
    legacy = os.path.expanduser(os.environ.get("DAMRESEARCH_HOME") or old)
    for d in (new, legacy, old):
        if any(os.path.isfile(os.path.join(d, f)) for f in ("vor.db", "research.db")):
            return d
    if not os.path.isdir(new) and os.path.isdir(old):
        return old
    return new


HOME = _home()
LIBRARY = os.path.join(HOME, "library")
REPORTS = os.path.join(HOME, "reports")


def _db_path():
    """vor.db, or the pre-rename research.db beside it when vor.db does not exist yet."""
    if os.environ.get("VOR_DB"):
        return os.environ["VOR_DB"]
    new = os.path.join(HOME, "vor.db")
    old = os.path.join(HOME, "research.db")
    if not os.path.exists(new) and os.path.exists(old):
        return old
    return new


DB_PATH = _db_path()
BASE = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
CHAT_MODEL = os.environ.get("VOR_MODEL", "vor")
EMBED_MODEL = os.environ.get("VOR_EMBED", "qwen3-embedding:0.6b")
NUM_CTX = int(os.environ.get("VOR_NUM_CTX", "16384"))
THINK = os.environ.get("VOR_THINK", "0") == "1"
CHARS_PER_TOKEN = 3.2
CHUNK_TARGET = 1100
CHUNK_MAX = 1600
CHUNK_OVERLAP = 200
MIN_PAGE_CHARS = 40
EMBED_BATCH = 16
MAX_FILE_BYTES = 300 * 1024 * 1024
TEXT_EXT = (".md", ".txt")
DOC_EXT = (".pdf",) + TEXT_EXT
SKIP_DIRS = {"__MACOSX", ".git", "_ocr_tmp", "node_modules"}
# The one line of character either AI gets. It greets; it never answers, so it cannot change
# what the model asserts. Everything that shapes an answer is in prompts/manner.txt.
VOICE = "Vor - literature assistant. Every sentence traces to a page, or it says it cannot."
BANNER = ("RESEARCH ASSISTANT OUTPUT - NOT ENGINEERING JUDGEMENT. Every statement must be "
          "checked against the cited page of the source before use in any dam-safety decision.")

try:
    import numpy as np
except ImportError:
    np = None


def die(msg, code=1):
    print("vor: " + msg, file=sys.stderr)
    sys.exit(code)



def damit_banner(subtitle=""):
    """DaMiT wordmark for the terminal (brand kit colours); plain text when piped or NO_COLOR is set."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return "DaMiT" + ("  " + subtitle if subtitle else "")
    c = lambda rgb, s: "\x1b[1;38;2;%d;%d;%dm%s\x1b[0m" % (rgb + (s,))
    teal, cream, amber = (48, 196, 200), (250, 250, 240), (244, 158, 24)
    word = c(teal, "D") + c(cream, "a") + c(teal, "M") + c(amber, "iT")
    return word + ("  \x1b[2m" + subtitle + "\x1b[0m" if subtitle else "") + "\n \x1b[38;2;31;188;188m~\x1b[0m"

def say(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- Ollama

def _post(path, payload, timeout=3600):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("Ollama %s -> HTTP %d: %s" % (path, e.code, body)) from None
    except urllib.error.URLError as e:
        raise RuntimeError("cannot reach Ollama at %s (%s). Start it: heim-start (or open Ollama.app)"
                           % (BASE, e.reason)) from None


def _get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read())


def is_qwen_embed(model):
    return "qwen3-embedding" in model


def doc_prefix(model):
    return "search_document: " if "nomic" in model else ""


def query_text(model, q):
    if is_qwen_embed(model):
        return ("Instruct: Given a question about dam safety, dam design or flood hazards, "
                "retrieve passages from technical publications that answer it\nQuery: " + q)
    if "nomic" in model:
        return "search_query: " + q
    if "mxbai" in model:
        return "Represent this sentence for searching relevant passages: " + q
    return q


def embed(texts, model):
    out = _post("/api/embed", {"model": model, "input": texts, "truncate": True})
    vecs = out.get("embeddings") or []
    if len(vecs) != len(texts):
        raise RuntimeError("embed returned %d vectors for %d texts" % (len(vecs), len(texts)))
    res = []
    for v in vecs:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        res.append([x / n for x in v])
    return res


_think_ok = [True]
_format_ok = [True]

# How the most recent chat() reply ended: done_reason ("stop", "length", ...), tokens written
# (eval_count) and how many characters of them were reasoning rather than answer. 1.3.0: a
# reply can come back EMPTY with nothing wrong at the HTTP level - a reasoning model spends its
# whole num_predict allowance in message.thinking and writes no content - and before this
# nothing recorded which of those happened.
LAST_REPLY = {"done_reason": "", "eval_count": 0, "thinking_chars": 0, "content_chars": 0}


def _note_reply(done_reason, eval_count, thinking_chars, content_chars):
    LAST_REPLY.update(done_reason=str(done_reason or ""), eval_count=int(eval_count or 0),
                      thinking_chars=int(thinking_chars or 0), content_chars=int(content_chars or 0))


def strip_think(text):
    """Remove a model's inline reasoning from a reply. Closed <think>..</think> blocks go; an
    opening tag that is never closed means the reply ran out while still reasoning, so
    everything after it goes; a lone closing tag means the renderer dropped the opening one,
    so everything before it goes."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


def empty_reply_reason():
    """Why the last reply had no answer in it, in words a report can carry."""
    r = LAST_REPLY
    if r["done_reason"] == "length":
        if r["thinking_chars"]:
            return ("it reached its length limit (%d tokens) while still reasoning, before writing "
                    "an answer" % r["eval_count"])
        return "it reached its length limit (%d tokens) without writing an answer" % r["eval_count"]
    if r["thinking_chars"]:
        return "it reasoned (%d characters) and then stopped without writing an answer" % r["thinking_chars"]
    if r["eval_count"]:
        return "it stopped after %d token(s) with no text (done_reason: %s)" % (
            r["eval_count"], r["done_reason"] or "not given")
    return "the reply was empty (done_reason: %s)" % (r["done_reason"] or "not given")


def chat(messages, schema=None, think=False, stream_to=None, num_predict=None, temperature=0.2):
    """One /api/chat call. schema = JSON schema (structured output). Returns the answer text.
    stream_to = a file object to echo the answer into while it is written."""
    opts = {"num_ctx": NUM_CTX, "temperature": temperature}
    if num_predict:
        opts["num_predict"] = num_predict
    p = {"model": CHAT_MODEL, "messages": messages, "options": opts, "stream": stream_to is not None}
    if schema is not None and _format_ok[0]:
        p["format"] = schema
    elif schema is not None:
        # 1.0.4: no structured output (older Ollama, or a model whose renderer has none, e.g.
        # qwen3.6 on Ollama 0.40: HTTP 501 "structured output is unavailable"): ask for the
        # same JSON in words; parse_json takes it out of the reply.
        msgs = [dict(m) for m in messages]
        msgs[-1]["content"] = (msgs[-1]["content"] + "\n\nReply with ONE JSON object only, no "
                               "other text, matching this JSON schema:\n" + json.dumps(schema))
        p["messages"] = msgs
    if _think_ok[0]:
        p["think"] = bool(think)
    try:
        if stream_to is None:
            out = _post("/api/chat", p)
            msg = out.get("message") or {}
            content = (msg.get("content") or "").strip()
            _note_reply(out.get("done_reason"), out.get("eval_count"), len(msg.get("thinking") or ""),
                        len(content))
            return content
        return _stream(p, stream_to)
    except RuntimeError as e:
        s = str(e)
        if "HTTP 400" in s and "think" in s.lower() and _think_ok[0]:
            _think_ok[0] = False          # older Ollama, or a model without a thinking switch
            return chat(messages, schema, False, stream_to, num_predict, temperature)
        if (schema is not None and _format_ok[0]
                and (("HTTP 400" in s and "format" in s.lower())
                     or ("HTTP 501" in s and "structured output" in s.lower()))):
            _format_ok[0] = False         # no JSON-schema output here: ask for JSON in words
            return chat(messages, schema, think, stream_to, num_predict, temperature)
        raise


def _stream(p, out):
    req = urllib.request.Request(BASE + "/api/chat", data=json.dumps(p).encode(),
                                 headers={"Content-Type": "application/json"})
    buf, thinking_shown, thinking_chars, done_reason, eval_count = [], False, 0, "", 0
    try:
        r = urllib.request.urlopen(req, timeout=3600)
    except urllib.error.HTTPError as e:
        raise RuntimeError("Ollama /api/chat -> HTTP %d: %s"
                           % (e.code, e.read().decode("utf-8", "replace")[:400])) from None
    except urllib.error.URLError as e:
        raise RuntimeError("cannot reach Ollama at %s (%s)" % (BASE, e.reason)) from None
    with r:
        for line in r:
            line = line.strip()
            if not line:
                continue
            try:
                ch = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ch.get("error"):
                raise RuntimeError(ch["error"])
            msg = ch.get("message") or {}
            thinking_chars += len(msg.get("thinking") or "")
            if msg.get("thinking") and not thinking_shown:
                out.write("(thinking...)\n")
                out.flush()
                thinking_shown = True
            piece = msg.get("content") or ""
            if piece:
                buf.append(piece)
                out.write(piece)
                out.flush()
            if ch.get("done"):
                done_reason, eval_count = ch.get("done_reason") or "", ch.get("eval_count") or 0
                break
    out.write("\n")
    text = "".join(buf).strip()
    _note_reply(done_reason, eval_count, thinking_chars, len(text))
    return text


def parse_json(text):
    """Structured output is JSON already; older Ollama / models may wrap it in prose or fences."""
    text = strip_think(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# --------------------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS docs(
  id INTEGER PRIMARY KEY, sha1 TEXT UNIQUE, path TEXT, title TEXT, authors TEXT, year TEXT,
  org TEXT, number TEXT, pages INTEGER, notext TEXT, backend TEXT, added TEXT, meta_src TEXT);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, doc_id INTEGER, page INTEGER, label TEXT, idx INTEGER, txt TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
"""


class _Conn(sqlite3.Connection):
    fts = False


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = sqlite3.connect(DB_PATH, factory=_Conn)
    db.executescript(SCHEMA)
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                   "txt, title, content='', tokenize='porter unicode61')")
        fts = True
    except sqlite3.OperationalError:
        fts = False                     # SQLite without FTS5: vector search only
    db.fts = fts
    return db


def meta_get(db, k, default=None):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))


def check_embed_model(db):
    used = meta_get(db, "embed_model")
    if used and used != EMBED_MODEL:
        die("this library was indexed with %s but VOR_EMBED is %s. Either set it back, "
            "or rebuild: rm %s && vor ingest" % (used, EMBED_MODEL, DB_PATH))


def delete_doc(db, doc_id):
    if db.fts:
        for cid, txt, title in db.execute(
                "SELECT c.id, c.txt, d.title FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE c.doc_id=?",
                (doc_id,)).fetchall():
            db.execute("INSERT INTO chunks_fts(chunks_fts, rowid, txt, title) VALUES('delete',?,?,?)",
                       (cid, txt, title or ""))
    db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    db.execute("DELETE FROM docs WHERE id=?", (doc_id,))


def find_doc(db, key):
    if str(key).isdigit():
        r = db.execute("SELECT id FROM docs WHERE id=?", (int(key),)).fetchone()
        if r:
            return r[0]
    rows = db.execute("SELECT id, path FROM docs WHERE path LIKE ?", ("%" + key + "%",)).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        die("no document matches %r (see: vor list)" % key)
    die("%r matches %d documents; use the id from `vor list`:\n  " % (key, len(rows))
        + "\n  ".join("%d  %s" % r for r in rows[:20]))


def squash(s):
    """Lower-case, punctuation removed: 'FEMA P-946' and 'P946.pdf' both become ...p946."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def find_docs(db, key):
    """Document ids a --doc filter names, as (ids, labels).

    KEY is an id, or part of a file name, title or report number. Punctuation is ignored,
    so --doc P-946 matches a file called "... P946.pdf". Comma-separated keys are read
    together. Unlike find_doc this allows several matches on purpose: the point is to scope
    a question to one publication, or to a family of them.
    """
    rows = db.execute("SELECT id, title, path, number FROM docs").fetchall()
    ids, labels = [], []
    for part in [p.strip() for p in str(key).split(",") if p.strip()]:
        if part.isdigit():
            hit = [r for r in rows if r[0] == int(part)]
        else:
            if len(squash(part)) < 2:
                die("--doc %r is too short to match on; use the id from `vor list`" % part)
            low = part.lower()
            hit = [r for r in rows if low in (r[2] or "").lower() or low in (r[1] or "").lower()
                   or low in (r[3] or "").lower()]
            if not hit:                                   # 'P-946' against a file named P946
                sq = squash(part)
                hit = [r for r in rows if sq in squash(r[2]) or sq in squash(r[1]) or sq in squash(r[3])]
        if not hit:
            die("--doc %r matches no document (see: vor list)" % part)
        for r in hit:
            if r[0] not in ids:
                ids.append(r[0])
                labels.append("%d  %s" % (r[0], (r[1] or r[2])[:80]))
    return ids, labels


def scope_note(labels):
    """One line naming what --doc scoped the search to, so a silent over-match is visible."""
    if not labels:
        return ""
    if len(labels) <= 6:
        return "Scoped to %d document(s):\n  %s" % (len(labels), "\n  ".join(labels))
    return "Scoped to %d documents: %s, ... (%d more)" % (
        len(labels), "; ".join(l.split("  ", 1)[-1][:40] for l in labels[:3]), len(labels) - 3)


# --------------------------------------------------------------------------- text extraction

def clean(t):
    t = unicodedata.normalize("NFKC", t)
    t = t.replace("­", "")                                  # soft hyphen
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)                       # embank-\nment
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def extract_pdf(path):
    """[(page_no, label, text)], backend, pdf_meta"""
    try:
        import pymupdf as pm
    except ImportError:
        try:
            import fitz as pm
        except ImportError:
            pm = None
    if pm is not None:
        try:
            d = pm.open(path)
            pages = []
            for i, pg in enumerate(d):
                try:
                    label = pg.get_label() or ""
                except Exception:
                    label = ""
                pages.append((i + 1, label, pg.get_text("text") or ""))
            md = dict(d.metadata or {})
            d.close()
            return pages, "pymupdf", md
        except Exception as e:
            say("    PyMuPDF failed on this file (%s); trying pdftotext" % e)
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-enc", "UTF-8", path, "-"], capture_output=True)
        if r.returncode == 0:
            parts = r.stdout.decode("utf-8", "replace").split("\f")
            if parts and not parts[-1].strip():
                parts = parts[:-1]
            md = {}
            info = subprocess.run(["pdfinfo", path], capture_output=True)
            for line in info.stdout.decode("utf-8", "replace").splitlines():
                k, _, v = line.partition(":")
                if k.strip() in ("Title", "Author", "CreationDate"):
                    md[{"Title": "title", "Author": "author", "CreationDate": "creationDate"}[k.strip()]] = v.strip()
            return [(i + 1, "", t) for i, t in enumerate(parts)], "pdftotext", md
    try:
        import pypdf
        rd = pypdf.PdfReader(path)
        pages = []
        for i, pg in enumerate(rd.pages):
            try:
                label = rd.page_labels[i]
            except Exception:
                label = ""
            pages.append((i + 1, label, pg.extract_text() or ""))
        md = {}
        if rd.metadata:
            md = {"title": rd.metadata.get("/Title") or "", "author": rd.metadata.get("/Author") or "",
                  "creationDate": rd.metadata.get("/CreationDate") or ""}
        return pages, "pypdf", md
    except ImportError:
        raise RuntimeError("no PDF reader: install PyMuPDF (pip install pymupdf) or poppler (pdftotext)")


def extract_text_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        t = f.read()
    # Split long notes at headings so a citation points at a section, not a whole file.
    parts = re.split(r"\n(?=#{1,3} )", t) if path.endswith(".md") else [t]
    pages, acc = [], ""
    for p in parts:
        if len(acc) + len(p) > 6000 and acc:
            pages.append(acc)
            acc = ""
        acc += ("\n" if acc else "") + p
    if acc:
        pages.append(acc)
    return [(i + 1, "sec %d" % (i + 1), x) for i, x in enumerate(pages)], "text", {}


BAD_TITLE = re.compile(r"^(microsoft word|untitled|document\d*|\S+\.(docx?|pdf|indd|tmp))", re.I)


def guess_meta(path, pages, md):
    title = (md.get("title") or "").strip()
    src = "pdf-metadata"
    if not title or len(title) < 6 or BAD_TITLE.match(title):
        title, src = "", "first-page"
        for _, _, t in pages[:2]:
            for line in clean(t).splitlines():
                s = line.strip()
                if 12 <= len(s) <= 180 and sum(c.isalpha() for c in s) > 0.6 * len(s):
                    title = s
                    break
            if title:
                break
    if not title:
        title, src = os.path.splitext(os.path.basename(path))[0].replace("_", " "), "file-name"
    authors = (md.get("author") or "").strip()
    year = ""
    m = re.search(r"(19|20)\d\d", md.get("creationDate") or "")
    first = clean(pages[0][2])[:3000] if pages else ""
    m2 = re.search(r"\b(19[3-9]\d|20[0-4]\d)\b", first)
    if m2:
        year = m2.group(1)
    elif m:
        year = m.group(0)
    return {"title": title[:300], "authors": authors[:300], "year": year, "meta_src": src}


def chunk_page(text):
    """Chunks never cross a page, so every citation points at one page."""
    text = clean(text)
    if len(text) < MIN_PAGE_CHARS:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[A-Z0-9][^\n]{0,80}\n)", text) if p.strip()]
    pieces = []
    for p in paras:
        p = p.replace("\n", " ")
        while len(p) > CHUNK_MAX:
            cut = p.rfind(". ", 0, CHUNK_TARGET + 200)
            cut = cut + 1 if cut > CHUNK_TARGET // 2 else CHUNK_TARGET
            pieces.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            pieces.append(p)
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > CHUNK_TARGET:
            chunks.append(cur)
            tail = cur[-CHUNK_OVERLAP:]
            k = tail.find(". ")
            cur = (tail[k + 2:] if k >= 0 else "") + " " + p
            cur = cur.strip()
        else:
            cur = (cur + " " + p).strip()
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------- ingest

def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def walk(paths):
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isfile(p):
            if p.lower().endswith(DOC_EXT):
                yield p
            continue
        if not os.path.isdir(p):
            say("  not found: %s" % p)
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for f in sorted(files):
                if f.startswith(("._", ".")) or not f.lower().endswith(DOC_EXT):
                    continue
                yield os.path.join(root, f)


def load_sidecar(paths):
    """library.csv next to the files: file,title,authors,year,org,number (any subset of columns)."""
    import csv
    out = {}
    files = []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isdir(p):
            continue
        # every library.csv under the folder (fetch_asdso.py writes one per sub-folder)
        for root, dirs, fs in os.walk(p):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if "library.csv" in fs:
                files.append(os.path.join(root, "library.csv"))
    for f in files:
        with open(f, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("file") or "").strip()
                if name:
                    out[os.path.basename(name)] = {k: (v or "").strip() for k, v in row.items() if k != "file"}
    return out


def cmd_ingest(a):
    db = connect()
    check_embed_model(db)
    paths = a.paths or [LIBRARY]
    os.makedirs(LIBRARY, exist_ok=True)
    side = load_sidecar(paths)
    files = list(walk(paths))
    say("Scanning %d file(s) under %s" % (len(files), ", ".join(paths)))
    known = {r[0]: (r[1], r[2]) for r in db.execute("SELECT sha1, id, path FROM docs")}
    by_path = {r[1]: (r[0], r[2]) for r in db.execute("SELECT id, path, sha1 FROM docs")}
    added = skipped = failed = notext = 0
    t0 = time.time()
    for n, path in enumerate(files, 1):
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                say("  [%d/%d] SKIP (over %d MB): %s" % (n, len(files), MAX_FILE_BYTES >> 20, path))
                skipped += 1
                continue
            h = sha1_of(path)
            if h in known:
                did, old = known[h]
                if old != path and not os.path.exists(old):
                    db.execute("UPDATE docs SET path=? WHERE id=?", (path, did))   # file was moved
                    db.commit()
                skipped += 1
                continue
            if path in by_path and by_path[path][1] != h:
                say("  [%d/%d] changed, re-indexing: %s" % (n, len(files), os.path.basename(path)))
                delete_doc(db, by_path[path][0])
                db.commit()
            ok = ingest_one(db, path, h, side.get(os.path.basename(path)), n, len(files))
            known[h] = (None, path)
            if ok:
                added += 1
            else:
                notext += 1
        except KeyboardInterrupt:
            say("\nStopped. Finished documents are saved; run ingest again to continue.")
            break
        except RuntimeError as e:
            if "cannot reach Ollama" in str(e):
                db.close()
                die(str(e))
            say("  [%d/%d] FAILED %s: %s" % (n, len(files), os.path.basename(path), e))
            failed += 1
    if a.prune:
        roots = [os.path.abspath(os.path.expanduser(p)) for p in paths]
        gone = [(i, p) for i, p in db.execute("SELECT id, path FROM docs")
                if any(p.startswith(r) for r in roots) and not os.path.exists(p)]
        for i, p in gone:
            delete_doc(db, i)
            say("  pruned (file gone): %s" % p)
        db.commit()
    nd, nc = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0], db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    say("Done in %.0f s: %d added, %d with no text, %d unchanged/skipped, %d failed. Library: %d documents, %d passages."
        % (time.time() - t0, added, notext, skipped, failed, nd, nc))
    scanned = db.execute("SELECT COUNT(*) FROM docs WHERE notext!='' AND notext IS NOT NULL").fetchone()[0]
    if scanned:
        say("%d document(s) have pages with no text layer (scanned). See `vor list`, "
            "then `vor ocr` to make them searchable." % scanned)
    db.close()


def dropped_paths(line):
    """The existing files/folders a line names, when it names nothing else.

    Dragging a file into a terminal pastes its path: spaces backslash-escaped in Terminal
    and iTerm, the whole path quoted in some others, several files space-separated. A token
    must look like a path (a separator or a leading ~) AND exist, so an ordinary question is
    never mistaken for a drop, even one whose only word matches a file in the current folder."""
    try:
        parts = shlex.split(line)
    except ValueError:                  # an unbalanced quote: a question, not a path
        return []
    out = []
    for p in parts:
        full = os.path.abspath(os.path.expanduser(p))
        if not (os.sep in p or p.startswith("~")) or not os.path.exists(full):
            return []
        out.append(full)
    return out


def add_paths(paths):
    """Copy files into LIBRARY/added and index them; returns (documents, passages) added.

    Both numbers, because a scanned PDF adds a document and no passages: it is in the
    library and nothing can be asked of it until `vor ocr` has run.

    Copied, not indexed where they lie: a dragged file usually sits in Downloads or on the
    Desktop, and the library should not lose its source when that copy is tidied away. A
    file already inside the library, and a whole folder, are indexed in place."""
    db = connect()
    have = {r[0]: r[1] for r in db.execute("SELECT sha1, path FROM docs")}
    count = "SELECT (SELECT COUNT(*) FROM docs), (SELECT COUNT(*) FROM chunks)"
    before = db.execute(count).fetchone()
    lib = os.path.join(os.path.abspath(LIBRARY), "")
    dest_dir = os.path.join(LIBRARY, "added")
    targets = []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p):
            say("  folder, indexed where it is: %s" % p)
            targets.append(p)
            continue
        if not os.path.isfile(p):
            say("  not found: %s" % p)
            continue
        if not p.lower().endswith(DOC_EXT):
            say("  not added (%s files are what the library reads): %s"
                % (" ".join(DOC_EXT), os.path.basename(p)))
            continue
        if p.startswith(lib):
            targets.append(p)
            continue
        h = sha1_of(p)
        if h in have:
            say("  already in the library: %s  (as %s)" % (os.path.basename(p), have[h]))
            continue
        os.makedirs(dest_dir, exist_ok=True)
        dst = os.path.join(dest_dir, os.path.basename(p))
        if os.path.exists(dst) and sha1_of(dst) != h:      # same name, different file
            stem, ext = os.path.splitext(os.path.basename(p))
            dst = os.path.join(dest_dir, "%s-%s%s" % (stem, h[:8], ext))
        if not os.path.exists(dst):
            shutil.copy2(p, dst)
        targets.append(dst)
    db.close()
    if not targets:
        return 0, 0
    cmd_ingest(argparse.Namespace(paths=targets, prune=False))
    db = connect()
    after = db.execute(count).fetchone()
    db.close()
    return after[0] - before[0], after[1] - before[1]


def cmd_add(a):
    add_paths(a.paths)


def ingest_one(db, path, h, side, n, total):
    name = os.path.basename(path)
    if path.lower().endswith(".pdf"):
        pages, backend, md = extract_pdf(path)
    else:
        pages, backend, md = extract_text_file(path)
    meta = guess_meta(path, pages, md)
    if side:
        for k in ("title", "authors", "year", "org", "number"):
            # a library.csv with an empty authors column means "cite the organisation", so it
            # replaces whatever the PDF's own Author field says (often a typist's login)
            if side.get(k) or (k == "authors" and k in side):
                meta[k] = side[k]
        meta["meta_src"] = "library.csv"
    notext = [str(p) for p, _, t in pages if len(clean(t)) < MIN_PAGE_CHARS] if backend != "text" else []
    items = []
    for p, label, t in pages:
        for i, c in enumerate(chunk_page(t)):
            items.append((p, label, i, c))
    if not items:
        say("  [%d/%d] NO TEXT (scanned? run `vor ocr`): %s" % (n, total, name))
        db.execute("INSERT INTO docs(sha1,path,title,authors,year,org,number,pages,notext,backend,added,meta_src)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (h, path, meta["title"], meta.get("authors", ""), meta.get("year", ""), meta.get("org", ""),
                    meta.get("number", ""), len(pages), ",".join(notext), backend,
                    datetime.now().isoformat(timespec="seconds"), meta["meta_src"]))
        db.commit()
        return False
    vecs = []
    pre = doc_prefix(EMBED_MODEL)
    for b in range(0, len(items), EMBED_BATCH):
        batch = items[b:b + EMBED_BATCH]
        vecs += embed([pre + "%s (p.%s)\n%s" % (meta["title"], lab or p, c) for p, lab, _, c in batch], EMBED_MODEL)
    dim = len(vecs[0])
    old = meta_get(db, "embed_dim")
    if old and int(old) != dim:
        raise RuntimeError("embedding size %d differs from the library's %s" % (dim, old))
    cur = db.cursor()
    cur.execute("BEGIN")
    cur.execute("INSERT INTO docs(sha1,path,title,authors,year,org,number,pages,notext,backend,added,meta_src)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (h, path, meta["title"], meta.get("authors", ""), meta.get("year", ""), meta.get("org", ""),
                 meta.get("number", ""), len(pages), ",".join(notext), backend,
                 datetime.now().isoformat(timespec="seconds"), meta["meta_src"]))
    did = cur.lastrowid
    for (p, lab, i, c), v in zip(items, vecs):
        cur.execute("INSERT INTO chunks(doc_id,page,label,idx,txt,vec) VALUES(?,?,?,?,?,?)",
                    (did, p, lab, i, c, array("f", v).tobytes()))
        if db.fts:
            cur.execute("INSERT INTO chunks_fts(rowid, txt, title) VALUES(?,?,?)", (cur.lastrowid, c, meta["title"]))
    meta_set(db, "embed_model", EMBED_MODEL)
    meta_set(db, "embed_dim", dim)
    db.commit()
    say("  [%d/%d] %s  (%d pages, %d passages%s, %s)" % (
        n, total, name, len(pages), len(items),
        ", %d page(s) without text" % len(notext) if notext else "", backend))
    return True


# --------------------------------------------------------------------------- retrieval

class Index:
    """Vectors held in memory once per process (chat keeps it warm)."""

    def __init__(self, db):
        self.db = db
        rows = db.execute("SELECT c.id, c.doc_id, c.vec FROM chunks c").fetchall()
        if not rows:
            die("the library is empty. Put PDFs in %s and run: vor ingest" % LIBRARY)
        self.ids = [r[0] for r in rows]
        self.doc_of = [r[1] for r in rows]
        self._sel_cache = {}
        if np is not None:
            self.M = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        else:
            self.M = [array("f", r[2]) for r in rows]

    def rows_for(self, keep):
        """Row positions belonging to the documents in `keep` (cached: chat reuses the Index)."""
        key = frozenset(keep)
        hit = self._sel_cache.get(key)
        if hit is None:
            sel = [i for i, d in enumerate(self.doc_of) if d in key]
            hit = np.asarray(sel, dtype=np.int64) if np is not None else sel
            self._sel_cache[key] = hit
        return hit

    def vector(self, q, n, keep=None):
        qv = embed([query_text(EMBED_MODEL, q)], EMBED_MODEL)[0]
        # With --doc the filter is applied BEFORE ranking, not after: a pool taken from the
        # whole library can easily hold no chunk of the document that was asked for.
        sel = self.rows_for(keep) if keep else None
        if np is not None:
            if sel is None:
                s = self.M @ np.asarray(qv, dtype=np.float32)
                top = np.argsort(-s)[:n]
                return [(self.ids[i], float(s[i])) for i in top]
            if len(sel) == 0:
                return []
            s = self.M[sel] @ np.asarray(qv, dtype=np.float32)
            return [(self.ids[sel[i]], float(s[i])) for i in np.argsort(-s)[:n]]
        rows = range(len(self.M)) if sel is None else sel
        sc = sorted(((sum(a * b for a, b in zip(self.M[i], qv)), i) for i in rows), reverse=True)[:n]
        return [(self.ids[i], s) for s, i in sc]

    def lexical(self, q, n, keep=None):
        if not self.db.fts:
            return []
        toks = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}|\d+(?:\.\d+)?", q) if t.lower() not in STOP]
        if not toks:
            return []
        expr = " OR ".join('"%s"' % t.replace('"', "") for t in dict.fromkeys(toks))
        sql = "SELECT rowid, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ?"
        args = [expr]
        if keep:
            ids = sorted(keep)
            sql += " AND rowid IN (SELECT id FROM chunks WHERE doc_id IN (%s))" % ",".join("?" * len(ids))
            args += ids
        sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
        args.append(n)
        try:
            return [(r[0], -r[1]) for r in self.db.execute(sql, args)]
        except sqlite3.OperationalError:
            return []

    def search(self, q, k=8, per_doc=3, pool=60, keep=None):
        """Hybrid: meaning (vectors) + exact words (BM25, catches 'Froehlich', 'MacDonald'), fused by rank.

        keep: document ids to read, or None for the whole library (see find_docs).
        per_doc: passages allowed from any one document; None lifts the cap, which is what
        a question scoped to a single long guideline needs.
        """
        if per_doc is None:
            per_doc = k
        fused = {}
        for lst in (self.vector(q, pool, keep), self.lexical(q, pool, keep)):
            for rank, (cid, _) in enumerate(lst):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank)
        order = sorted(fused, key=lambda c: -fused[c])
        hits, seen_doc = [], {}
        for cid in order:
            row = self.db.execute(
                "SELECT c.id, c.doc_id, c.page, c.label, c.txt, d.title, d.path, d.authors, d.year, d.org, d.number "
                "FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE c.id=?", (cid,)).fetchone()
            if not row:
                continue
            if seen_doc.get(row[1], 0) >= per_doc:
                continue
            seen_doc[row[1]] = seen_doc.get(row[1], 0) + 1
            hits.append(Hit(row, fused[cid]))
            if len(hits) >= k:
                break
        return hits


STOP = set("""a an and are as at be by can could did do does for from has have how if in into is it its
of on or should than that the their there these this those to was were what when where which who why
will with would about between during under over per using used use based any all also more most
""".split())


class Hit:
    def __init__(self, row, score):
        (self.cid, self.doc_id, self.page, self.label, self.txt, self.title, self.path,
         self.authors, self.year, self.org, self.number) = row
        self.score = score

    @property
    def pg(self):
        if self.path.lower().endswith(".pdf"):
            if self.label and self.label != str(self.page):
                return "p.%s (PDF p.%d)" % (self.label, self.page)
            return "p.%d" % self.page
        return self.label or "sec %d" % self.page

    def short(self):
        who = (self.authors.split(",")[0].split(";")[0].strip() if self.authors else self.org) or ""
        t = self.title if len(self.title) <= 70 else self.title[:67] + "..."
        return "%s%s%s" % (who + " " if who else "", "(%s) " % self.year if self.year else "", t)

    def link(self):
        u = "file://" + urllib.request.pathname2url(self.path)
        return u + ("#page=%d" % self.page if self.path.lower().endswith(".pdf") else "")


def doc_scope(db, a):
    """Turn --doc / --per-doc into (document ids or None, per_doc or None, a line to print).

    Without --doc the per-document cap stays at 3, which keeps one chatty source from
    filling the window. With --doc the cap is lifted unless it was asked for: scoping to a
    145-page guideline and then reading 3 of its passages is the thing that sends you away
    empty-handed.
    """
    keep, labels = (find_docs(db, a.doc) if getattr(a, "doc", None) else ([], []))
    per_doc = getattr(a, "per_doc", None)
    if per_doc is None:
        per_doc = None if keep else 3
    return (keep or None), per_doc, scope_note(labels)


def cmd_search(a):
    db = connect()
    check_embed_model(db)
    idx = Index(db)
    q = " ".join(a.question)
    keep, per_doc, note = doc_scope(db, a)
    if note:
        say(note)
    hits = idx.search(q, k=a.k, per_doc=per_doc, keep=keep)
    if not hits:
        say("\nNo passage matched%s. %s" % (
            " in that document" if keep else "",
            "Try other words, or drop --doc." if keep else "Try other words."))
        return
    for n, h in enumerate(hits, 1):
        say("\n[%d] %s  %s   (score %.4f)\n    %s" % (n, h.short(), h.pg, h.score, h.path))
        say("    " + textwrap_short(h.txt, 420))


def textwrap_short(t, n):
    t = " ".join(t.split())
    return t if len(t) <= n else t[:n - 3] + "..."


# --------------------------------------------------------------------------- quote checking

def norm(s):
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.translate({0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"', 0x2013: "-", 0x2014: "-",
                     0x2212: "-", 0x00d7: "x"})
    s = re.sub(r"(\w)- (\w)", r"\1\2", s)
    s = re.sub(r"[^\w%.,;:()/=+\-'\" ]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" .,;:")


def quote_check(quote, source_txt):
    """'exact', 'close' (>= 90% of the quote's characters found in order), or 'not found'."""
    q, t = norm(quote), norm(source_txt)
    if len(q) < 12:
        return "too short"
    if q in t:
        return "exact"
    sm = difflib.SequenceMatcher(None, q, t, autojunk=False)
    m = sum(b.size for b in sm.get_matching_blocks() if b.size >= 4)
    # must also be one contiguous region of the page, not letters scattered across it
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 4]
    span = (blocks[-1].b + blocks[-1].size - blocks[0].b) if blocks else 0
    if m >= 0.9 * len(q) and span <= 1.3 * len(q):
        # a near match must not hide a changed number ("2 and 5 times" -> "3 and 7 times")
        region = t[blocks[0].b: blocks[-1].b + blocks[-1].size]
        if NUM.findall(q) != NUM.findall(region):
            return "numbers differ"
        return "close"
    return "not found"


NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")


def numbers_outside(claim, quote, source_txt):
    """Numbers in the paraphrase that appear in neither the quote nor the page — likely invented."""
    allowed = set(NUM.findall(norm(quote))) | set(NUM.findall(norm(source_txt)))
    bad = [n for n in NUM.findall(norm(claim)) if n not in allowed and n.replace(",", "") not in allowed]
    return bad


# --------------------------------------------------------------------------- research pipeline

SYSTEM = """You are a research assistant for dam-safety engineers (dam safety, dam and spillway design,
breach parameters, inundation mapping, hydrology and flood hazards). You work ONLY from the numbered
source passages you are given, which come from the user's own library of publications.
Rules:
- Every factual statement must come from a given passage. Never use outside knowledge to fill a gap.
- Quotes must be copied character-for-character from the passage. Do not fix or reword them.
- Keep numbers, units, equations, coefficients and their applicability limits exactly as written.
- Say which kind of source a statement comes from when it matters: federal guidance/standard
  (FEMA, USBR, USACE, NRCS, FERC, state rules) versus a research paper or a case study.
- If passages disagree, report both sides; do not pick one silently.
- If the passages do not answer something, say so plainly. "The library does not cover this" is a
  good answer. Never guess."""

PLAN_SCHEMA = {"type": "object", "properties": {
    "subquestions": {"type": "array", "items": {"type": "string"}},
    "search_terms": {"type": "array", "items": {"type": "string"}}},
    "required": ["subquestions", "search_terms"]}

EXTRACT_SCHEMA = {"type": "object", "properties": {
    "findings": {"type": "array", "items": {"type": "object", "properties": {
        "source": {"type": "string"},
        "quote": {"type": "string"},
        "claim": {"type": "string"},
        "kind": {"type": "string", "enum": ["value", "equation", "method", "recommendation",
                                            "requirement", "finding", "definition", "limitation", "case"]}},
        "required": ["source", "quote", "claim", "kind"]}},
    "not_covered": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings", "not_covered"]}


def plan(question, depth):
    if depth <= 1:
        return [question], []
    msg = ("Break this research question into at most %d focused sub-questions that together answer it, "
           "the way an engineer would search a library of dam-safety publications. Also give up to 8 exact "
           "search terms (method names, equation authors, agency document numbers, technical terms).\n\n"
           "Question: %s" % (depth - 1, question))
    out = parse_json(chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}],
                          schema=PLAN_SCHEMA, num_predict=800)) or {}
    subs = [s.strip() for s in out.get("subquestions", []) if isinstance(s, str) and s.strip()][:depth - 1]
    terms = [s.strip() for s in out.get("search_terms", []) if isinstance(s, str) and s.strip()][:8]
    return [question] + [s for s in subs if s.lower() != question.lower()], terms


class Evidence:
    def __init__(self):
        self.sources = []        # Hit, index = S number - 1
        self.by_cid = {}
        self.findings = []       # dict(F, source, quote, claim, kind, check, numbers, sub)
        self.rejected = []
        self.not_covered = []
        self.answer_note = ""    # set when the model's own write-up could not be used
        self.answer_from = "model"   # "model" | "model, second attempt" | "findings"

    def add_source(self, h):
        if h.cid not in self.by_cid:
            self.sources.append(h)
            self.by_cid[h.cid] = len(self.sources)
        return self.by_cid[h.cid]


def budget_chars(reserve_tokens):
    return max(4000, int((NUM_CTX - reserve_tokens) * CHARS_PER_TOKEN))


def extract(ev, sub, hits):
    """Ask the model for verbatim evidence from these passages, then check every quote ourselves."""
    budget = budget_chars(2500)
    blocks, used = [], 0
    for h in hits:
        s = ev.add_source(h)
        b = "[S%d] %s, %s\n%s" % (s, h.short(), h.pg, h.txt)
        if used + len(b) > budget:
            break
        blocks.append((s, b))
        used += len(b)
    if not blocks:
        return
    msg = ("Sub-question: %s\n\nSource passages:\n\n%s\n\n"
           "List every finding in these passages that helps answer the sub-question. For each: the source "
           "label (like S3), an exact quote copied from that passage (one or two sentences, under 300 "
           "characters), a one-sentence plain claim that says no more than the quote, and its kind. "
           "Skip passages that are not relevant. In not_covered, list parts of the sub-question that "
           "none of the passages answer." % (sub, "\n\n".join(b for _, b in blocks)))
    reply = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}],
                 schema=EXTRACT_SCHEMA, num_predict=2500)
    out = parse_json(reply)
    if out is None and not strip_think(reply):
        # 1.3.0: nothing came back at all. Once more with twice the room, because the usual
        # cause is the allowance being spent before the first character of the answer.
        why = empty_reply_reason()
        say("  (the model returned nothing for these passages: %s. Asking once more.)" % why)
        reply = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}],
                     schema=EXTRACT_SCHEMA, num_predict=5000)
        out = parse_json(reply)
        if out is None and not strip_think(reply):
            ev.rejected.append({"sub": sub, "why": "model returned nothing for these passages, twice (%s; "
                                                    "then %s)" % (why, empty_reply_reason())})
            return
    if out is None:
        ev.rejected.append({"sub": sub, "why": "model reply was not valid JSON"})
        return
    allowed = {s for s, _ in blocks}
    for f in out.get("findings") or []:
        if not isinstance(f, dict):
            continue
        m = re.search(r"\d+", str(f.get("source", "")))
        s = int(m.group(0)) if m else -1
        quote, claim = str(f.get("quote", "")).strip().strip('"'), str(f.get("claim", "")).strip()
        if s not in allowed:
            ev.rejected.append({"sub": sub, "why": "cites S%s, which was not in its passages" % s,
                                "claim": claim, "quote": quote})
            continue
        h = ev.sources[s - 1]
        chk = quote_check(quote, h.txt)
        if chk != "exact" and chk != "close":
            ev.rejected.append({"sub": sub, "why": "quote %s on %s" % (chk, h.pg), "claim": claim,
                                "quote": quote, "S": s})
            continue
        if any(quote == g["quote"] and s == g["source"] for g in ev.findings):
            continue
        ev.findings.append({"F": len(ev.findings) + 1, "source": s, "quote": quote, "claim": claim,
                            "kind": f.get("kind", ""), "check": chk, "sub": sub,
                            "numbers": numbers_outside(claim, quote, h.txt)})
    for g in out.get("not_covered") or []:
        if isinstance(g, str) and g.strip() and g.strip().lower() not in {x.lower() for _, x in ev.not_covered}:
            ev.not_covered.append((sub, g.strip()))


def synthesize(question, ev, mode, out):
    lines = []
    for f in ev.findings:
        h = ev.sources[f["source"] - 1]
        lines.append('[F%d] (%s; %s; %s) %s\n     quote: "%s"' % (
            f["F"], h.short(), h.pg, f["kind"], f["claim"], f["quote"]))
    gaps = "\n".join("- %s" % g for _, g in ev.not_covered) or "- (none reported)"
    if mode == "ask":
        shape = ("Write a direct answer of one to four short paragraphs. Put the finding labels you rely on "
                 "after each sentence, like [F2] or [F2][F5]. End with one line starting 'Not covered:' if "
                 "part of the question is unanswered.")
    else:
        shape = ("Write a research brief in Markdown with these sections:\n"
                 "## Answer  (the short answer, 1-2 paragraphs)\n"
                 "## What the sources say  (grouped by theme; methods, equations and values exactly as quoted)\n"
                 "## Where sources differ  (or 'No disagreement found in these passages.')\n"
                 "## Gaps and uncertainty  (what the library does not answer; what an engineer should check)\n"
                 "Put the finding labels after every sentence that states a fact, like [F2] or [F2][F5].")
    msg = ("Question: %s\n\nVerified findings (each quote has been checked against its page):\n\n%s\n\n"
           "Parts the sources did not cover:\n%s\n\n%s\nUse ONLY these findings. Do not add facts, numbers or "
           "references that are not in them. If they do not answer the question, say so."
           % (question, "\n".join(lines) or "(no findings)", gaps, shape))
    reserve = 3000 if mode == "ask" else 5000
    if len(msg) > budget_chars(reserve):
        msg = msg[:budget_chars(reserve)] + "\n[findings list cut to fit the context window]"
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}]
    body = strip_think(chat(msgs, think=THINK, stream_to=out, num_predict=reserve))
    if body:
        return body
    # 1.3.0. On the first real run (2026-11-12, qwen3.6:27b) four of sixteen questions ended
    # here with one to four checked findings and an empty answer: the right page found, the
    # quote verified, and nothing written. The reply being empty is not an error to Ollama, so
    # the empty string went into the report as the answer.
    first = empty_reply_reason()
    say("The model wrote no answer: %s. Asking once more, with reasoning off and more room." % first)
    room = int(NUM_CTX - len(msg) / CHARS_PER_TOKEN - 600)
    again = [msgs[0], {"role": "user", "content": msg + "\n\nWrite the answer itself now, beginning "
                       "with its first sentence. Do not reason or plan first."}]
    body = strip_think(chat(again, think=False, stream_to=out,
                            num_predict=max(reserve, min(reserve * 2, room)), temperature=0.5))
    if body:
        ev.answer_from = "model, second attempt"
        ev.answer_note = ("the model wrote no answer at first (%s); this is its second attempt" % first)
        return body
    second = empty_reply_reason()
    ev.answer_from = "findings"
    ev.answer_note = ("the model wrote no answer twice (%s; then %s), so the answer above is the checked "
                      "findings themselves, not a written summary" % (first, second))
    body = findings_answer(ev)
    out.write(body + "\n")
    return body


def no_findings_answer(ev, keep):
    """What to say when no quote survived. 1.3.0: two different things used to get one sentence,
    "The library returned no passage that answers this question". On 2026-11-12 one model gave
    that for seven of sixteen questions whose right page was in the passages it had just been
    handed: the library was not empty, the model drew nothing from it. Saying the library has
    nothing sends the reader off to find a document they already own."""
    if not ev.sources:
        return ("That document returned no passage that answers this question, so there is nothing to "
                "cite. Try `vor search --doc ...` with different words, or drop --doc and search "
                "the whole library." if keep else
                "The library returned no passage that answers this question, so there is nothing to cite. "
                "Try `vor search` with different words, or add publications that cover it.")
    seen, pages = set(), []
    for h in ev.sources:
        key = (h.short(), h.pg)
        if key not in seen:
            seen.add(key)
            pages.append("- %s, %s" % key)
    quoted = [r for r in ev.rejected if r.get("quote")]
    other = [r for r in ev.rejected if not r.get("quote")]
    if quoted:
        drew = "the model offered %d quote(s), and none of them passed the check against its page" % len(quoted)
    elif other:
        drew = "the model's reply could not be used (%s)" % other[0].get("why", "no reason recorded")
    else:
        drew = "the model drew no quote from them"
    return ("There is nothing to cite: %d passage(s) were read and %s. That is not the same as the "
            "library having nothing. These are the passages the search ranked closest to the question, "
            "unchecked:\n\n%s\n\nOpen those pages, or ask again in different words%s."
            % (len(ev.sources), drew, "\n".join(pages[:8]),
               "" if keep else ", or name the publication with --doc"))


def findings_answer(ev):
    """An answer made only of what was verified: each finding's claim, its quote and its label.
    Used when the model writes nothing. Every line carries its [F] label, so it renders with
    page citations like any other answer, and nothing in it is the model's unchecked prose."""
    L = ["The model did not write an answer, so these are the checked findings as they stand. "
         "Each quote was found on the page cited."]
    L.append("")
    for f in ev.findings:
        claim = f["claim"].rstrip()
        if claim and claim[-1] not in ".!?":
            claim += "."
        L.append('- %s "%s" [F%d]' % (claim, f["quote"], f["F"]))
    if ev.not_covered:
        L.append("")
        L.append("Not covered: " + "; ".join(g for _, g in ev.not_covered))
    return "\n".join(L)


def render_citations(text, ev):
    """[F3] -> [n, p.12]; n numbers the references list in order of first use."""
    order, unknown = [], []

    def ref_no(doc_id):
        if doc_id not in order:
            order.append(doc_id)
        return order.index(doc_id) + 1

    fmap = {f["F"]: f for f in ev.findings}

    def sub(m):
        out = []
        for n in re.findall(r"\d+", m.group(0)):
            f = fmap.get(int(n))
            if not f:
                unknown.append(n)
                out.append("[F%s: NO SUCH FINDING]" % n)
                continue
            h = ev.sources[f["source"] - 1]
            c = "[%d, %s]" % (ref_no(h.doc_id), h.pg)
            if c not in out:
                out.append(c)
        return "".join(out)
    rendered = re.sub(r"\[F\d+(?:\]\s*\[F\d+)*\]|\[F\d+(?:\s*[,;]\s*F?\d+)+\]", sub, text)
    used_f = set(int(n) for n in re.findall(r"F(\d+)", text))
    return rendered, order, unknown, used_f


def uncited_sentences(text):
    bad = 0
    for para in text.split("\n"):
        p = para.strip()
        if not p or p.startswith("#") or p.lower().startswith(("not covered", "no disagreement")):
            continue
        for s in re.split(r"(?<=[.!?])\s+", p):
            if len(s) > 60 and "[F" not in s and not re.search(r"not (covered|answer)|does not|no (finding|source)", s, re.I):
                bad += 1
    return bad


def run_pipeline(question, mode, depth, k, out=sys.stdout, idx=None, save=True,
                 keep=None, per_doc=None, scope=""):
    db = connect()
    try:
        check_embed_model(db)
        return _run_pipeline(db, question, mode, depth, k, out, idx or Index(db), save, keep, per_doc, scope)
    finally:
        db.close()          # one connection per question was left open for the life of a chat


def _run_pipeline(db, question, mode, depth, k, out, idx, save, keep, per_doc, scope):
    if scope:
        say(scope)
    t0 = time.time()
    ev = Evidence()
    if mode == "research":
        say("Planning sub-questions...")
    subs, terms = plan(question, depth)
    if mode == "research":
        for s in subs[1:]:
            say("  - " + s)
        if terms:
            say("  search terms: " + "; ".join(terms))
    log = []
    for i, s in enumerate(subs, 1):
        q = s + ((" " + " ".join(terms)) if (i == 1 and terms) else "")
        hits = idx.search(q, k=k, per_doc=per_doc if per_doc is not None else 3, keep=keep)
        log.append((s, hits))
        say("[%d/%d] reading %d passages for: %s" % (i, len(subs), len(hits), textwrap_short(s, 90)))
        extract(ev, s, hits)
    nf, nr = len(ev.findings), len(ev.rejected)
    say("Evidence: %d quote(s) checked against their pages, %d rejected (quote not on the page or wrong source)."
        % (nf, nr))
    say("-" * 72)
    if not ev.findings:
        body = no_findings_answer(ev, keep)
        out.write(body + "\n")
    else:
        body = synthesize(question, ev, mode, out)
    rendered, order, unknown, used_f = render_citations(body, ev)
    unc = uncited_sentences(body) if (ev.findings and ev.answer_from != "findings") else 0
    report = build_report(question, mode, subs, terms, log, ev, rendered, order, unknown, used_f, unc,
                          time.time() - t0, scope=scope)
    path = None
    if save:
        os.makedirs(REPORTS, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", question.lower())[:60].strip("-") or "question"
        path = os.path.join(REPORTS, "%s_%s_%s.md" % (datetime.now().strftime("%Y-%m-%d_%H%M"), mode, slug))
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
    say("-" * 72)
    say("With references:\n")
    say(rendered)
    say("\nReferences:")
    for n, did in enumerate(order, 1):
        h = next(s for s in ev.sources if s.doc_id == did)
        say("  [%d] %s" % (n, cite_line(h)))
    if unknown or unc or ev.answer_note or any(f["numbers"] for f in ev.findings if f["F"] in used_f):
        say("\nChecks: %s" % "; ".join(checks_summary(ev, unknown, used_f, unc)))
    if path:
        say("\nReport (evidence table, quotes, rejected items): %s" % path)
    say(BANNER)
    return path, rendered, ev


def cite_line(h):
    parts = []
    if h.authors:
        parts.append(h.authors)
    elif h.org:
        parts.append(h.org)
    if h.year:
        parts.append("(%s)" % h.year)
    parts.append(h.title + ".")
    if h.number:
        parts.append(h.number + ".")
    if h.org and h.authors:
        parts.append(h.org + ".")
    return " ".join(parts) + "  " + os.path.basename(h.path)


def checks_summary(ev, unknown, used_f, unc):
    c = []
    if unknown:
        c.append("%d citation(s) to findings that do not exist (marked NO SUCH FINDING)" % len(unknown))
    nums = [f for f in ev.findings if f["F"] in used_f and f["numbers"]]
    if nums:
        c.append("%d cited finding(s) state numbers that are not on the page: %s" % (
            len(nums), ", ".join("F%d (%s)" % (f["F"], ", ".join(f["numbers"])) for f in nums)))
    close = [f for f in ev.findings if f["F"] in used_f and f["check"] == "close"]
    if close:
        c.append("%d quote(s) match the page only closely (PDF text extraction or a small slip): %s"
                 % (len(close), ", ".join("F%d" % f["F"] for f in close)))
    if unc:
        c.append("%d long sentence(s) carry no citation - treat them as the model's own words" % unc)
    if not c:
        c.append("all cited quotes found on their pages; no uncited sentences")
    if ev.answer_note:
        c.insert(0, ev.answer_note)
    return c


def md_cell(s):
    return " ".join(str(s).split()).replace("|", "\\|")


def build_report(question, mode, subs, terms, log, ev, rendered, order, unknown, used_f, unc, secs,
                 scope=""):
    L = ["# %s" % question, "",
         "> **%s**" % BANNER, "",
         "%s · model `%s` · embeddings `%s` · %s · %.0f s" % (
             "Deep research" if mode == "research" else "Quick answer", CHAT_MODEL, EMBED_MODEL,
             datetime.now().strftime("%Y-%m-%d %H:%M"), secs), ""]
    if scope:
        L += ["> " + scope.replace("\n", "  \n> "), ""]           # --doc: say what was read
    L += [rendered.strip(), "", "## References", ""]
    for n, did in enumerate(order, 1):
        hs = [s for s in ev.sources if s.doc_id == did]
        pages = sorted({s.pg for s in hs})
        L.append("%d. %s  \n   Pages used: %s · [open](%s)" % (n, cite_line(hs[0]), ", ".join(pages), hs[0].link()))
    L += ["", "## Automatic checks", ""] + ["- " + c for c in checks_summary(ev, unknown, used_f, unc)]
    L += ["", "## Evidence (every quote checked against its page)", "",
          "| F | Used | Claim | Quote | Source | Page | Check |", "|---|---|---|---|---|---|---|"]
    for f in ev.findings:
        h = ev.sources[f["source"] - 1]
        chk = f["check"] + ("; numbers not on page: " + ", ".join(f["numbers"]) if f["numbers"] else "")
        L.append("| F%d | %s | %s | \"%s\" | %s | %s | %s |" % (
            f["F"], "yes" if f["F"] in used_f else "", md_cell(f["claim"]), md_cell(f["quote"]),
            md_cell(h.short()), h.pg, chk))
    if ev.rejected:
        L += ["", "## Rejected by the checks (not used)", ""]
        seen = set()
        for r in ev.rejected:
            key = (r.get("why"), r.get("quote"))
            if key in seen:
                continue
            seen.add(key)
            L.append("- %s: %s%s" % (md_cell(r.get("why")), md_cell(r.get("claim", "")),
                                    (' — "%s"' % md_cell(r["quote"][:200])) if r.get("quote") else ""))
    if ev.not_covered:
        L += ["", "## Not covered by the passages read", ""] + ["- %s" % md_cell(g) for _, g in ev.not_covered]
    L += ["", "## Search log", ""]
    if terms:
        L.append("Search terms: " + "; ".join(terms))
    for s, hits in log:
        L.append("- **%s**" % md_cell(s))
        for h in hits:
            L.append("  - %s, %s (score %.4f)" % (md_cell(h.short()), h.pg, h.score))
    return "\n".join(L) + "\n"


def cmd_ask(a):
    keep, per_doc, note = doc_scope(connect(), a)
    run_pipeline(" ".join(a.question), "ask", 1, a.k, save=not a.no_save,
                 keep=keep, per_doc=per_doc, scope=note)


def cmd_vor(a):
    keep, per_doc, note = doc_scope(connect(), a)
    run_pipeline(" ".join(a.question), "research", a.depth, a.k,
                 keep=keep, per_doc=per_doc, scope=note)


def cmd_chat(a):
    db = connect()
    check_embed_model(db)
    idx = Index(db)
    keep, per_doc, note = doc_scope(db, a)
    say(damit_banner(VOICE))
    if note:
        say(note)
    say("vor chat — cited answers from your library (%s). Blank line or Ctrl+D quits.\n"
        "Prefix a question with '!' for deep research. Drag a file or folder into this window\n"
        "to add it to the library.\n%s" % (CHAT_MODEL, BANNER))
    last_q, last_a = None, None
    while True:
        try:
            q = input("\n? ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        drop = dropped_paths(q)
        if drop:
            try:
                ndocs, npass = add_paths(drop)
            except SystemExit:              # cmd_ingest dies when Ollama is down: keep the chat
                say("not indexed (reason above). Anything copied is in %s and the next "
                    "`vor ingest` picks it up." % os.path.join(LIBRARY, "added"))
                continue
            if npass:
                idx = Index(db)             # the vectors are held in memory: reload, or the new
                say("Ready: ask about it.") # document is invisible to this chat
                if keep:
                    say("(this chat was started with --doc, so answers still come only from that "
                        "selection; start `vor chat` without it to read the new document)")
            elif ndocs:
                say("Added, but it has no readable text, so there is nothing to ask it yet. "
                    "A scanned PDF needs `vor ocr` first.")
            continue
        deep = q.startswith("!")
        q = q.lstrip("!").strip()
        if last_q:
            # make a follow-up stand on its own so retrieval sees the whole question
            r = chat([{"role": "user", "content":
                       "Previous question: %s\nPrevious answer (start): %s\nFollow-up: %s\n\nRewrite the follow-up "
                       "as one standalone question. Reply with the question only." % (last_q, (last_a or "")[:800], q)}],
                     num_predict=200)
            r = strip_think(r)
            r = r.strip().strip('"').splitlines()[0] if r.strip() else q
            if r and r.lower() != q.lower():
                say("(searching for: %s)" % r)
                q = r
        try:
            _, ans, _ = run_pipeline(q, "research" if deep else "ask", a.depth if deep else 1, a.k,
                                     idx=idx, keep=keep, per_doc=per_doc)
        except RuntimeError as e:
            say("error: %s" % e)
            continue
        last_q, last_a = q, ans
    db.close()


# --------------------------------------------------------------------------- housekeeping

def cmd_list(a):
    db = connect()
    rows = db.execute("SELECT d.id, d.title, d.year, d.pages, d.notext, d.path, d.meta_src, "
                      "(SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.id) FROM docs d ORDER BY d.id").fetchall()
    if not rows:
        say("No documents yet. Put PDFs in %s and run: vor ingest" % LIBRARY)
        return
    for i, t, y, pages, notext, path, src, nc in rows:
        nt = [x for x in (notext or "").split(",") if x]
        flag = ""
        if nt:
            flag = "  NO TEXT on %d/%d pages%s" % (len(nt), pages, " (scanned - run ocr)" if len(nt) > pages / 2 else "")
        say("%4d  %s%s  [%d p, %d passages]%s%s\n      %s" % (
            i, t[:90], " (%s)" % y if y else "", pages or 0, nc, flag,
            "  title from %s" % src if src in ("first-page", "file-name") else "", path))
    say("\n%d documents. Embeddings: %s. Fix a citation: vor meta ID --title ... --year ..."
        % (len(rows), meta_get(db, "embed_model", "?")))


def cmd_meta(a):
    db = connect()
    did = find_doc(db, a.doc)
    sets = {k: getattr(a, k) for k in ("title", "authors", "year", "org", "number") if getattr(a, k) is not None}
    if not sets:
        r = db.execute("SELECT title, authors, year, org, number, path FROM docs WHERE id=?", (did,)).fetchone()
        say("title:   %s\nauthors: %s\nyear:    %s\norg:     %s\nnumber:  %s\nfile:    %s" % r)
        return
    msg = ", ".join("%s=%s" % kv for kv in sets.items())
    if "title" in sets:
        set_title(db, did, sets.pop("title"))
    for k, v in sets.items():
        db.execute("UPDATE docs SET %s=?, meta_src='manual' WHERE id=?" % k, (v, did))
    db.execute("UPDATE docs SET meta_src='manual' WHERE id=?", (did,))
    db.commit()
    say("updated document %d: %s" % (did, msg))


def set_title(db, did, title):
    """Change a title and keep the keyword index (which also searches titles) in step."""
    old = db.execute("SELECT title FROM docs WHERE id=?", (did,)).fetchone()[0] or ""
    if db.fts and title != old:
        for cid, txt in db.execute("SELECT id, txt FROM chunks WHERE doc_id=?", (did,)).fetchall():
            db.execute("INSERT INTO chunks_fts(chunks_fts, rowid, txt, title) VALUES('delete',?,?,?)", (cid, txt, old))
            db.execute("INSERT INTO chunks_fts(rowid, txt, title) VALUES(?,?,?)", (cid, txt, title))
    db.execute("UPDATE docs SET title=? WHERE id=?", (title, did))


def parse_bib(text):
    entries = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,]*),(.*?)\n\}", text, re.S):
        fields = {}
        for fm in re.finditer(r"(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\"|\d+)", m.group(3)):
            v = fm.group(2).strip()
            if v[:1] in "{\"":
                v = v[1:-1]
            fields[fm.group(1).lower()] = re.sub(r"[{}]", "", " ".join(v.split()))
        entries.append(fields)
    return entries


def cmd_bib(a):
    db = connect()
    with open(os.path.expanduser(a.bibfile), encoding="utf-8", errors="replace") as f:
        entries = parse_bib(f.read())
    docs = db.execute("SELECT id, path FROM docs").fetchall()
    by_name = {}
    for i, p in docs:
        by_name.setdefault(os.path.basename(p).lower(), []).append(i)
    hit = 0
    for e in entries:
        files = re.split(r";", e.get("file", ""))
        for fl in files:
            # Zotero: "Title:files/12/name.pdf:application/pdf" or a plain path
            parts = fl.split(":")
            cand = [x for x in parts if x.lower().endswith(".pdf")]
            for c in cand:
                for did in by_name.get(os.path.basename(c).lower(), []):
                    authors = "; ".join(x.strip() for x in e.get("author", "").split(" and ") if x.strip())
                    if e.get("title"):
                        set_title(db, did, e["title"])
                    db.execute("UPDATE docs SET authors=?, year=?, org=?, number=?, meta_src='bibtex' WHERE id=?",
                               (authors, e.get("year", ""),
                                e.get("institution") or e.get("publisher") or e.get("journal") or "",
                                e.get("number") or e.get("doi") or "", did))
                    hit += 1
    db.commit()
    say("%d BibTeX entries read, %d library documents updated." % (len(entries), hit))


def cmd_remove(a):
    db = connect()
    did = find_doc(db, a.doc)
    p = db.execute("SELECT path FROM docs WHERE id=?", (did,)).fetchone()[0]
    delete_doc(db, did)
    db.commit()
    say("removed from the index (file left in place): %s" % p)


def ocr_exe():
    """ocrmypdf, looking in this Python's env first: the launcher runs the env's python without
    activating the env, so its bin (ocrmypdf, tesseract, ghostscript) goes on PATH."""
    for envbin in (os.path.dirname(os.path.realpath(sys.executable)), os.path.dirname(sys.executable)):
        if envbin not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = envbin + os.pathsep + os.environ.get("PATH", "")
    return shutil.which("ocrmypdf")


def cmd_ocr(a):
    exe = ocr_exe()
    if not exe:
        die("ocrmypdf is not installed. Re-run the installer with --ocr (adds ocrmypdf + tesseract to the "
            "ndvor env), then run this again.")
    db = connect()
    rows = db.execute("SELECT id, path, pages, notext FROM docs WHERE notext!='' AND notext IS NOT NULL").fetchall()
    todo = [(i, p) for i, p, n, nt in rows if p.lower().endswith(".pdf") and os.path.exists(p)
            and len([x for x in nt.split(",") if x]) >= max(1, (n or 1) * a.min_fraction)]
    if not todo:
        say("No scanned PDFs to OCR.")
        return
    outdir = os.path.join(LIBRARY, "_ocr")
    os.makedirs(outdir, exist_ok=True)
    for i, p in todo:
        dst = os.path.join(outdir, os.path.splitext(os.path.basename(p))[0] + ".ocr.pdf")
        say("OCR: %s" % os.path.basename(p))
        r = subprocess.run([exe, "--skip-text", "--output-type", "pdf", "-q", p, dst])  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- local OCR on user-provided PDF paths, fixed argv
        if r.returncode not in (0, 6):          # 6 = already has text
            say("  ocrmypdf failed (exit %d); left as is" % r.returncode)
            continue
        delete_doc(db, i)
        db.commit()
    say("Re-indexing the OCR copies...")
    cmd_ingest(argparse.Namespace(paths=[outdir], prune=False))


def home_reason():
    """Why HOME is the folder it is, in a few words (see _home)."""
    if os.environ.get("VOR_HOME"):
        return "VOR_HOME is set"
    if any(os.path.isfile(os.path.join(HOME, f)) for f in ("vor.db", "research.db")):
        return "it holds the index"
    return "the default; no index found anywhere yet"


def _index_counts(path):
    """(documents, passages) in an index file, or None when it cannot be read as one."""
    try:
        db = sqlite3.connect("file:%s?mode=ro" % urllib.request.pathname2url(path), uri=True)
        try:
            return (db.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        finally:
            db.close()
    except sqlite3.Error:
        return None


def other_libraries():
    """Folders that look like a Vor library and are NOT the one in use: (folder, index counts
    or None, number of document files). Two libraries is how Vor has gone quiet before: the
    one being read was empty and the full one was a folder away."""
    here = os.path.realpath(HOME)
    seen, out = {here}, []
    cands = [os.path.expanduser("~/Vor"), os.path.expanduser("~/DamResearch")]
    if os.environ.get("DAMRESEARCH_HOME"):
        cands.append(os.path.expanduser(os.environ["DAMRESEARCH_HOME"]))
    parent = os.path.expanduser("~")
    try:
        cands += sorted(os.path.join(parent, d) for d in os.listdir(parent)
                        if d.startswith("Vor.before-rename-"))
    except OSError:
        pass
    for d in cands:
        real = os.path.realpath(d)
        if real in seen or not os.path.isdir(d):
            continue
        seen.add(real)
        counts = None
        for f in ("vor.db", "research.db"):
            if os.path.isfile(os.path.join(d, f)):
                counts = _index_counts(os.path.join(d, f)) or (0, 0)
                break
        nfiles = sum(1 for _ in walk([os.path.join(d, "library")])) if os.path.isdir(os.path.join(d, "library")) else 0
        if counts is not None or nfiles:
            out.append((d, counts, nfiles))
    return out


def library_checks():
    """The checks that need no model: is Vor reading the library you think it is, and is the
    index level with the files. Returns [(level, text)], level one of OK / WATCH / PROBLEM.
    Every PROBLEM here is a state in which Vor runs without error and answers from nothing."""
    out = []
    add = lambda level, text: out.append((level, text))
    add("OK", "library folder %s (%s)" % (HOME, home_reason()))
    on_disk = list(walk([LIBRARY])) if os.path.isdir(LIBRARY) else []
    if not os.path.exists(DB_PATH):
        nd = nc = 0
        add("PROBLEM", "no index at %s%s" % (DB_PATH, (": %d document file(s) are waiting in the library. "
            "Run: vor ingest" % len(on_disk)) if on_disk else ". Put PDFs in %s and run: vor ingest" % LIBRARY))
        rows, used = [], None
    else:
        counts = _index_counts(DB_PATH)
        if counts is None:
            add("PROBLEM", "%s is not a readable index" % DB_PATH)
            return out
        nd, nc = counts
        db = connect()
        try:
            used = meta_get(db, "embed_model")
            rows = db.execute("SELECT path, notext, added FROM docs").fetchall()
            fts = db.fts
        finally:
            db.close()
        if not nc:
            add("PROBLEM", "the index is empty (%d documents, 0 passages): every question will get "
                "'no passage'. %s" % (nd, ("%d file(s) are in the library. Run: vor ingest" % len(on_disk))
                                       if on_disk else "Put PDFs in %s and run: vor ingest" % LIBRARY))
        else:
            add("OK", "index: %d documents, %d passages" % (nd, nc))
        if used and used != EMBED_MODEL:
            add("PROBLEM", "indexed with %s but VOR_EMBED is %s: every search is refused until one is "
                           "set back or the index is rebuilt" % (used, EMBED_MODEL))
        if not fts:
            add("WATCH", "keyword search is off (this Python's SQLite has no FTS5): names like "
                         "'Froehlich' are found by meaning only")
    for d, counts, nfiles in other_libraries():
        what = ("an index with %d documents" % counts[0]) if counts is not None else "no index"
        text = ("a second library exists at %s (%s, %d document file(s)); Vor is reading %s"
                % (d, what, nfiles, HOME))
        fuller = (counts[0] if counts else 0) > nd or (nd == 0 and nfiles)
        add("PROBLEM" if fuller else "WATCH",
            text + (". It holds MORE than the one in use: run `zsh setup-vor.zsh` to bring it over, or set "
                    "VOR_HOME to it" if fuller else ". Nothing reads it; remove it once you are sure"))
    if rows:
        indexed = {os.path.normpath(r[0]) for r in rows}
        new = [f for f in on_disk if os.path.normpath(f) not in indexed]
        if new:
            add("WATCH", "%d file(s) in the library are not in the index (first: %s). Run: vor ingest"
                % (len(new), os.path.relpath(new[0], LIBRARY)))
        gone = [r[0] for r in rows if not os.path.exists(r[0])]
        if gone:
            add("WATCH", "%d indexed document(s) no longer have a file (first: %s). They are still "
                "answered from. To drop them: vor ingest --prune" % (len(gone), os.path.basename(gone[0])))
        changed = []
        for path, _, added in rows:
            try:
                when = datetime.fromisoformat((added or "")[:19])
                if os.path.exists(path) and datetime.fromtimestamp(os.path.getmtime(path)) > when + timedelta(seconds=2):
                    changed.append(path)
            except ValueError:
                continue
        if changed:
            add("WATCH", "%d file(s) changed after they were indexed (first: %s), so answers come from "
                "the older text. Run: vor ingest" % (len(changed), os.path.basename(changed[0])))
        scanned = [r for r in rows if (r[1] or "").strip()]
        if scanned:
            pages = sum(len(r[1].split(",")) for r in scanned)
            add("WATCH", "%d document(s) have %d page(s) with no text layer: Vor cannot read those pages. %s"
                % (len(scanned), pages, "Run: vor ocr, then vor ingest" if ocr_exe() else
                   "OCR is not installed: re-run the installer with --ocr, then vor ocr and vor ingest"))
        if not (new or gone or changed):
            add("OK", "every file in the library is indexed, and none changed since")
    return out


def print_checks(checks):
    """Print [(level, text)] and return the number of PROBLEMs."""
    for level, text in checks:
        say("  %-8s %s" % ({"OK": "ok", "WATCH": "WATCH", "PROBLEM": "PROBLEM"}[level], text))
    return sum(1 for level, _ in checks if level == "PROBLEM")


def cmd_doctor(a):
    ok = True
    say(damit_banner("vor"))
    say("vor %s   home %s" % (VERSION, HOME))
    if getattr(a, "library_only", False):
        say("  (models not checked: --library)")
    else:
        try:
            v = _get("/api/version").get("version")
            say("  Ollama %s at %s" % (v, BASE))
            tags = {m["name"] for m in _get("/api/tags").get("models", [])}
            for m in (CHAT_MODEL, EMBED_MODEL):
                have = m in tags or (m + ":latest") in tags
                say("  model %-28s %s" % (m, "present" if have else "MISSING - re-run setup-vor.zsh"))
                ok &= have
            if ok:
                vec = embed([query_text(EMBED_MODEL, "spillway design flood")], EMBED_MODEL)[0]
                say("  embeddings work (%d dims)" % len(vec))
                r = strip_think(chat([{"role": "user", "content": "Reply with the single word: ready"}],
                                     num_predict=20))
                if r:
                    say("  chat model replies: %r" % r[:40])
                else:
                    # 1.3.0: an empty reply used to print as '' and count as working
                    say("  chat model replied with NOTHING to a one-word request: %s" % empty_reply_reason())
                    ok = False
        except Exception as e:
            say("  Ollama: NOT reachable (%s). Start it with heim-start in its own tab." % e)
            ok = False
    # 1.3.0: the library itself. These need no model, and each PROBLEM is a state in which Vor
    # runs without an error and answers from nothing.
    problems = print_checks(library_checks())
    say("  numpy %s" % ("yes" if np is not None else "no (slower search)"))
    try:
        import pymupdf  # noqa
        say("  PDF reader: PyMuPDF (printed page labels on)")
    except ImportError:
        say("  PDF reader: %s" % ("pdftotext" if shutil.which("pdftotext") else "pypdf/none"))
    say("  OCR: %s" % ("ocrmypdf" if ocr_exe() else "not installed (installer --ocr)"))
    if problems:
        say("\n%d PROBLEM(s) above: until fixed, Vor will run and answer from less than you think." % problems)
    sys.exit(0 if (ok and not problems) else 1)


def add_scope_args(s):
    """--doc / --per-doc, on every command that retrieves."""
    s.add_argument("--doc", metavar="KEY",
                   help="only read this publication: the id from `vor list`, or part of its "
                        "file name, title or report number (punctuation is ignored, so P-946 finds a "
                        "file named P946.pdf). Comma-separated for several. Naming the publication in "
                        "the question instead does not scope anything - 'FEMA' is just a search word "
                        "and matches every FEMA document.")
    s.add_argument("--per-doc", type=int, default=None, metavar="N",
                   help="passages allowed from any one publication (default 3; with --doc the cap is "
                        "lifted unless you set it)")


def main():
    ap = argparse.ArgumentParser(prog="vor",
                                 description="Local research assistant for dam-safety literature. " + BANNER)
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest", help="index PDFs/.md/.txt (default: the library drop folder)")
    s.add_argument("paths", nargs="*")
    s.add_argument("--prune", action="store_true", help="drop documents whose file is gone")
    s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("add", help="copy files into the library and index them (drag them in)")
    s.add_argument("paths", nargs="+")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("list", help="documents in the library")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("meta", help="show or fix a document's citation")
    s.add_argument("doc")
    for k in ("title", "authors", "year", "org", "number"):
        s.add_argument("--" + k)
    s.set_defaults(fn=cmd_meta)
    s = sub.add_parser("bib", help="take citations from a Zotero/BibTeX export")
    s.add_argument("bibfile")
    s.set_defaults(fn=cmd_bib)
    s = sub.add_parser("search", help="retrieval only: best passages with pages")
    s.add_argument("question", nargs="+")
    s.add_argument("-k", type=int, default=10)
    add_scope_args(s)
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("ask", help="quick cited answer")
    s.add_argument("question", nargs="+")
    s.add_argument("-k", type=int, default=10)
    s.add_argument("--no-save", action="store_true")
    add_scope_args(s)
    s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("research", help="deep research report with checked quotes")
    s.add_argument("question", nargs="+")
    s.add_argument("--depth", type=int, default=5, help="sub-questions incl. the original (default 5)")
    s.add_argument("-k", type=int, default=8, help="passages read per sub-question (default 8)")
    add_scope_args(s)
    s.set_defaults(fn=cmd_vor)
    s = sub.add_parser("chat", help="interactive; '!' prefix = deep research")
    s.add_argument("--depth", type=int, default=4)
    s.add_argument("-k", type=int, default=8)
    add_scope_args(s)
    s.set_defaults(fn=cmd_chat)
    s = sub.add_parser("ocr", help="OCR scanned PDFs (needs ocrmypdf) and re-index them")
    s.add_argument("--min-fraction", type=float, default=0.3,
                   help="OCR a PDF when at least this share of its pages has no text (default 0.3)")
    s.set_defaults(fn=cmd_ocr)
    s = sub.add_parser("remove", help="drop a document from the index")
    s.add_argument("doc")
    s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("doctor", help="check Ollama, the models, and that the library being read is "
                                      "the full one and level with its files")
    s.add_argument("--library", dest="library_only", action="store_true",
                   help="check the library and index only (no Ollama needed)")
    s.set_defaults(fn=cmd_doctor)
    a = ap.parse_args()
    try:
        a.fn(a)
    except RuntimeError as e:
        die(str(e))
    except KeyboardInterrupt:
        die("interrupted", 130)


if __name__ == "__main__":
    main()
