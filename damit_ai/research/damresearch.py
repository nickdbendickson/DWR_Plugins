#!/usr/bin/env python3
# damresearch.py v1.0.3 — local research assistant for dam-safety, dam-design and
# flood-hazard publications. Runs on your Mac against Ollama; nothing leaves the machine.
#
# This is a RESEARCH ASSISTANT. Every statement it writes must be checked against
# the cited page of the source document before it is used for any dam-safety decision.
# It is kept apart from `lumo` (the code-triage model): own Ollama model (dam-research),
# own embedding model, own database (~/DamResearch/research.db).
#
#   damresearch ingest [PATH ...]      add PDFs / .md / .txt (default: ~/DamResearch/library,
#                                      the drop folder). Unchanged files are skipped.
#                                      --prune drops documents whose file is gone.
#   damresearch list                   documents, pages, chunks, pages with no text layer
#   damresearch meta DOC --title T --authors A --year Y --org O --number N
#                                      fix a document's citation (DOC = id or part of file name)
#   damresearch bib FILE.bib           take titles/authors/years from a Zotero/BibTeX export
#   damresearch search QUESTION        retrieval only (no model): best passages with page numbers
#   damresearch ask QUESTION           quick cited answer (one retrieval pass)
#   damresearch research QUESTION      deep research: plan sub-questions -> retrieve -> extract
#                                      verbatim evidence -> check every quote against the page
#                                      -> cited report in ~/DamResearch/reports/
#   damresearch chat                   ask follow-up questions in a loop
#   damresearch ocr                    OCR the scanned PDFs (needs ocrmypdf) and re-ingest them
#   damresearch remove DOC             drop a document from the index (file is not touched)
#   damresearch doctor                 check Ollama, the two models and the database
#
# Settings (environment; the installer writes them to ~/.zshrc):
#   DAMRESEARCH_HOME     ~/DamResearch   (library/, reports/, research.db)
#   DAMRESEARCH_MODEL    dam-research    (Ollama persona made by the installer)
#   DAMRESEARCH_EMBED    qwen3-embedding:0.6b
#   DAMRESEARCH_NUM_CTX  context window the persona was built with
#   DAMRESEARCH_THINK    1 = let the model reason before writing the report (slower)
#   OLLAMA_URL           http://127.0.0.1:11434
#
# 1.0.1: reads library.csv in every sub-folder of the library (fetch_asdso.py writes one per folder).
#
# Stdlib only; numpy makes search fast, PyMuPDF gives printed page labels. PDF text
# backends in order: PyMuPDF -> pdftotext -> pypdf.
import os, sys, re, json, time, math, hashlib, sqlite3, argparse, subprocess, difflib, shutil
import urllib.request, urllib.error, unicodedata
from array import array
from datetime import datetime

VERSION = "1.0.3"
HOME = os.path.expanduser(os.environ.get("DAMRESEARCH_HOME", "~/DamResearch"))
LIBRARY = os.path.join(HOME, "library")
REPORTS = os.path.join(HOME, "reports")
DB_PATH = os.environ.get("DAMRESEARCH_DB") or os.path.join(HOME, "research.db")
BASE = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
CHAT_MODEL = os.environ.get("DAMRESEARCH_MODEL", "dam-research")
EMBED_MODEL = os.environ.get("DAMRESEARCH_EMBED", "qwen3-embedding:0.6b")
NUM_CTX = int(os.environ.get("DAMRESEARCH_NUM_CTX", "16384"))
THINK = os.environ.get("DAMRESEARCH_THINK", "0") == "1"
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
BANNER = ("RESEARCH ASSISTANT OUTPUT - NOT ENGINEERING JUDGEMENT. Every statement must be "
          "checked against the cited page of the source before use in any dam-safety decision.")

try:
    import numpy as np
except ImportError:
    np = None


def die(msg, code=1):
    print("damresearch: " + msg, file=sys.stderr)
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
        raise RuntimeError("cannot reach Ollama at %s (%s). Start it: lumo-start (or open Ollama.app)"
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


def chat(messages, schema=None, think=False, stream_to=None, num_predict=None, temperature=0.2):
    """One /api/chat call. schema = JSON schema (structured output). Returns the answer text.
    stream_to = a file object to echo the answer into while it is written."""
    opts = {"num_ctx": NUM_CTX, "temperature": temperature}
    if num_predict:
        opts["num_predict"] = num_predict
    p = {"model": CHAT_MODEL, "messages": messages, "options": opts, "stream": stream_to is not None}
    if schema is not None and _format_ok[0]:
        p["format"] = schema
    if _think_ok[0]:
        p["think"] = bool(think)
    try:
        if stream_to is None:
            out = _post("/api/chat", p)
            return ((out.get("message") or {}).get("content") or "").strip()
        return _stream(p, stream_to)
    except RuntimeError as e:
        s = str(e)
        if "HTTP 400" in s and "think" in s.lower() and _think_ok[0]:
            _think_ok[0] = False          # older Ollama, or a model without a thinking switch
            return chat(messages, schema, False, stream_to, num_predict, temperature)
        if "HTTP 400" in s and schema is not None and _format_ok[0] and "format" in s.lower():
            _format_ok[0] = False         # Ollama too old for JSON-schema output
            return chat(messages, schema, think, stream_to, num_predict, temperature)
        raise


def _stream(p, out):
    req = urllib.request.Request(BASE + "/api/chat", data=json.dumps(p).encode(),
                                 headers={"Content-Type": "application/json"})
    buf, thinking_shown = [], False
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
                break
    out.write("\n")
    return "".join(buf).strip()


def parse_json(text):
    """Structured output is JSON already; older Ollama / models may wrap it in prose or fences."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
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
        die("this library was indexed with %s but DAMRESEARCH_EMBED is %s. Either set it back, "
            "or rebuild: rm %s && damresearch ingest" % (used, EMBED_MODEL, DB_PATH))


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
        die("no document matches %r (see: damresearch list)" % key)
    die("%r matches %d documents; use the id from `damresearch list`:\n  " % (key, len(rows))
        + "\n  ".join("%d  %s" % r for r in rows[:20]))


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
        say("%d document(s) have pages with no text layer (scanned). See `damresearch list`, "
            "then `damresearch ocr` to make them searchable." % scanned)


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
        say("  [%d/%d] NO TEXT (scanned? run `damresearch ocr`): %s" % (n, total, name))
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
        rows = db.execute("SELECT c.id, c.vec FROM chunks c").fetchall()
        if not rows:
            die("the library is empty. Put PDFs in %s and run: damresearch ingest" % LIBRARY)
        self.ids = [r[0] for r in rows]
        if np is not None:
            self.M = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        else:
            self.M = [array("f", r[1]) for r in rows]

    def vector(self, q, n):
        qv = embed([query_text(EMBED_MODEL, q)], EMBED_MODEL)[0]
        if np is not None:
            s = self.M @ np.asarray(qv, dtype=np.float32)
            top = np.argsort(-s)[:n]
            return [(self.ids[i], float(s[i])) for i in top]
        sc = sorted(((sum(a * b for a, b in zip(v, qv)), i) for i, v in enumerate(self.M)), reverse=True)[:n]
        return [(self.ids[i], s) for s, i in sc]

    def lexical(self, q, n):
        if not self.db.fts:
            return []
        toks = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}|\d+(?:\.\d+)?", q) if t.lower() not in STOP]
        if not toks:
            return []
        expr = " OR ".join('"%s"' % t.replace('"', "") for t in dict.fromkeys(toks))
        try:
            return [(r[0], -r[1]) for r in self.db.execute(
                "SELECT rowid, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ? "
                "ORDER BY bm25(chunks_fts) LIMIT ?", (expr, n))]
        except sqlite3.OperationalError:
            return []

    def search(self, q, k=8, per_doc=3, pool=60):
        """Hybrid: meaning (vectors) + exact words (BM25, catches 'Froehlich', 'MacDonald'), fused by rank."""
        fused = {}
        for lst in (self.vector(q, pool), self.lexical(q, pool)):
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


def cmd_search(a):
    db = connect()
    check_embed_model(db)
    idx = Index(db)
    q = " ".join(a.question)
    for n, h in enumerate(idx.search(q, k=a.k, per_doc=a.per_doc), 1):
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
    out = parse_json(chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}],
                          schema=EXTRACT_SCHEMA, num_predict=2500))
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
    return chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}],
                think=THINK, stream_to=out, num_predict=reserve)


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


def run_pipeline(question, mode, depth, k, out=sys.stdout, idx=None, save=True):
    db = connect()
    check_embed_model(db)
    idx = idx or Index(db)
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
        hits = idx.search(q, k=k, per_doc=3)
        log.append((s, hits))
        say("[%d/%d] reading %d passages for: %s" % (i, len(subs), len(hits), textwrap_short(s, 90)))
        extract(ev, s, hits)
    nf, nr = len(ev.findings), len(ev.rejected)
    say("Evidence: %d quote(s) checked against their pages, %d rejected (quote not on the page or wrong source)."
        % (nf, nr))
    say("-" * 72)
    if not ev.findings:
        body = ("The library returned no passage that answers this question, so there is nothing to cite. "
                "Try `damresearch search` with different words, or add publications that cover it.")
        out.write(body + "\n")
    else:
        body = synthesize(question, ev, mode, out)
    rendered, order, unknown, used_f = render_citations(body, ev)
    unc = uncited_sentences(body) if ev.findings else 0
    report = build_report(question, mode, subs, terms, log, ev, rendered, order, unknown, used_f, unc, time.time() - t0)
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
    if unknown or unc or any(f["numbers"] for f in ev.findings if f["F"] in used_f):
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
    return c


def md_cell(s):
    return " ".join(str(s).split()).replace("|", "\\|")


def build_report(question, mode, subs, terms, log, ev, rendered, order, unknown, used_f, unc, secs):
    L = ["# %s" % question, "",
         "> **%s**" % BANNER, "",
         "%s · model `%s` · embeddings `%s` · %s · %.0f s" % (
             "Deep research" if mode == "research" else "Quick answer", CHAT_MODEL, EMBED_MODEL,
             datetime.now().strftime("%Y-%m-%d %H:%M"), secs), "",
         rendered.strip(), "", "## References", ""]
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
    run_pipeline(" ".join(a.question), "ask", 1, a.k, save=not a.no_save)


def cmd_research(a):
    run_pipeline(" ".join(a.question), "research", a.depth, a.k)


def cmd_chat(a):
    db = connect()
    check_embed_model(db)
    idx = Index(db)
    say(damit_banner("dam-research - literature assistant"))
    say("damresearch chat — cited answers from your library (%s). Blank line or Ctrl+D quits.\n"
        "Prefix a question with '!' for deep research.\n%s" % (CHAT_MODEL, BANNER))
    last_q, last_a = None, None
    while True:
        try:
            q = input("\n? ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        deep = q.startswith("!")
        q = q.lstrip("!").strip()
        if last_q:
            # make a follow-up stand on its own so retrieval sees the whole question
            r = chat([{"role": "user", "content":
                       "Previous question: %s\nPrevious answer (start): %s\nFollow-up: %s\n\nRewrite the follow-up "
                       "as one standalone question. Reply with the question only." % (last_q, (last_a or "")[:800], q)}],
                     num_predict=200)
            r = r.strip().strip('"').splitlines()[0] if r.strip() else q
            if r and r.lower() != q.lower():
                say("(searching for: %s)" % r)
                q = r
        try:
            _, ans, _ = run_pipeline(q, "research" if deep else "ask", a.depth if deep else 1, a.k, idx=idx)
        except RuntimeError as e:
            say("error: %s" % e)
            continue
        last_q, last_a = q, ans


# --------------------------------------------------------------------------- housekeeping

def cmd_list(a):
    db = connect()
    rows = db.execute("SELECT d.id, d.title, d.year, d.pages, d.notext, d.path, d.meta_src, "
                      "(SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.id) FROM docs d ORDER BY d.id").fetchall()
    if not rows:
        say("No documents yet. Put PDFs in %s and run: damresearch ingest" % LIBRARY)
        return
    for i, t, y, pages, notext, path, src, nc in rows:
        nt = [x for x in (notext or "").split(",") if x]
        flag = ""
        if nt:
            flag = "  NO TEXT on %d/%d pages%s" % (len(nt), pages, " (scanned - run ocr)" if len(nt) > pages / 2 else "")
        say("%4d  %s%s  [%d p, %d passages]%s%s\n      %s" % (
            i, t[:90], " (%s)" % y if y else "", pages or 0, nc, flag,
            "  title from %s" % src if src in ("first-page", "file-name") else "", path))
    say("\n%d documents. Embeddings: %s. Fix a citation: damresearch meta ID --title ... --year ..."
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
            "ndresearch env), then run this again.")
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
        r = subprocess.run([exe, "--skip-text", "--output-type", "pdf", "-q", p, dst])
        if r.returncode not in (0, 6):          # 6 = already has text
            say("  ocrmypdf failed (exit %d); left as is" % r.returncode)
            continue
        delete_doc(db, i)
        db.commit()
    say("Re-indexing the OCR copies...")
    cmd_ingest(argparse.Namespace(paths=[outdir], prune=False))


def cmd_doctor(a):
    ok = True
    say(damit_banner("dam-research"))
    say("damresearch %s   home %s" % (VERSION, HOME))
    try:
        v = _get("/api/version").get("version")
        say("  Ollama %s at %s" % (v, BASE))
        tags = {m["name"] for m in _get("/api/tags").get("models", [])}
        for m in (CHAT_MODEL, EMBED_MODEL):
            have = m in tags or (m + ":latest") in tags
            say("  model %-28s %s" % (m, "present" if have else "MISSING - re-run setup-dam-research.zsh"))
            ok &= have
        if ok:
            vec = embed([query_text(EMBED_MODEL, "spillway design flood")], EMBED_MODEL)[0]
            say("  embeddings work (%d dims)" % len(vec))
            r = chat([{"role": "user", "content": "Reply with the single word: ready"}], num_predict=20)
            say("  chat model replies: %r" % r[:40])
    except Exception as e:
        say("  Ollama: NOT reachable (%s). Start it with lumo-start in its own tab." % e)
        ok = False
    db = connect()
    nd = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    nc = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    say("  library: %d documents, %d passages, indexed with %s; keyword search %s; numpy %s"
        % (nd, nc, meta_get(db, "embed_model", "-"), "on" if db.fts else "OFF (no FTS5)",
           "yes" if np is not None else "no (slower search)"))
    try:
        import pymupdf  # noqa
        say("  PDF reader: PyMuPDF (printed page labels on)")
    except ImportError:
        say("  PDF reader: %s" % ("pdftotext" if shutil.which("pdftotext") else "pypdf/none"))
    say("  OCR: %s" % ("ocrmypdf" if ocr_exe() else "not installed (installer --ocr)"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(prog="damresearch",
                                 description="Local research assistant for dam-safety literature. " + BANNER)
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest", help="index PDFs/.md/.txt (default: the library drop folder)")
    s.add_argument("paths", nargs="*")
    s.add_argument("--prune", action="store_true", help="drop documents whose file is gone")
    s.set_defaults(fn=cmd_ingest)
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
    s.add_argument("--per-doc", type=int, default=3)
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("ask", help="quick cited answer")
    s.add_argument("question", nargs="+")
    s.add_argument("-k", type=int, default=10)
    s.add_argument("--no-save", action="store_true")
    s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("research", help="deep research report with checked quotes")
    s.add_argument("question", nargs="+")
    s.add_argument("--depth", type=int, default=5, help="sub-questions incl. the original (default 5)")
    s.add_argument("-k", type=int, default=8, help="passages read per sub-question (default 8)")
    s.set_defaults(fn=cmd_research)
    s = sub.add_parser("chat", help="interactive; '!' prefix = deep research")
    s.add_argument("--depth", type=int, default=4)
    s.add_argument("-k", type=int, default=8)
    s.set_defaults(fn=cmd_chat)
    s = sub.add_parser("ocr", help="OCR scanned PDFs (needs ocrmypdf) and re-index them")
    s.add_argument("--min-fraction", type=float, default=0.3,
                   help="OCR a PDF when at least this share of its pages has no text (default 0.3)")
    s.set_defaults(fn=cmd_ocr)
    s = sub.add_parser("remove", help="drop a document from the index")
    s.add_argument("doc")
    s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("doctor", help="check Ollama, models and the library")
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
