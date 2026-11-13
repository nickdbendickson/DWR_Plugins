#!/usr/bin/env python3
# rag_index.py v2.6 — multi-collection RAG indexer (stdlib only)
# Atomic rebuilds via a hidden per-collection stage; auto-migrates v1 DBs;
# PDF backends: pdftotext -> pymupdf -> pypdf -> skip.
#
# v2.6: --add FILE ... copies single files into <index folder>/added and indexes them as the
#   collection 'added' (heim-add). Indexing took folders only, so one dragged PDF had no home.
#
# v2.4 fixes (vs v2.3):
#   - A missing folder, or a run that produced 0 chunks, no longer swaps an
#     empty result over the live collection (v2.3 wiped it while saying
#     "Live index untouched").
#   - Backend detection used one flag for "detected" and "pdftotext exists".
#   - Inserts run inside one transaction per batch (autocommit was one fsync
#     per chunk).
#   - Oversized / minified files are reported, not skipped silently.
#   - nomic-embed-text task prefixes (search_document: / search_query:).
#   - TypeScript/JS added so the crest repo indexes its source, not just docs.
#   - Stage name is per collection, so two indexers cannot clobber each other.
import os, sys, json, sqlite3, argparse, subprocess, urllib.request, urllib.error, time, re
import hashlib, shutil

DB = os.path.expanduser(os.environ.get("HEIM_DB", "~/local-ai/knowledge.db"))
EMBED_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/embed"
EMBED_MODEL = "nomic-embed-text"
DOC_PREFIX = "search_document: "
CHUNK_CHARS = 1500
OVERLAP = 200            # audit_plugin.py relies on this value when rebuilding files
MAX_TEXT_BYTES = 262144
MAX_PDF_BYTES = 104857600
MINIFIED_AVG_LINE = 300  # average chars per line above this = generated/minified, skip
MINIFIABLE_EXT = (".js", ".mjs", ".cjs", ".json")
BATCH = 16
STAGE_PREFIX = "__stage__"
SKIP_DIRS = {"__MACOSX", ".git", "node_modules", "__pycache__", ".venv",
             "testhazard", "_ocr_staging_tmp", "dist", "build", ".vite"}
SKIP_PREFIX = ("._",)
SKIP_EXACT = {".DS_Store"}
TEXT_EXT = (".py", ".md", ".txt", ".qml", ".rst", ".yml", ".yaml",
            ".toml", ".cfg", ".ini", ".json", ".log", ".sql", ".sh",
            ".ts", ".tsx", ".js", ".mjs", ".cjs")
PDF_EXT = (".pdf",)

_detected = [False]
_pdftotext = [False]
_pm = [None]
_pp = [None]


def _detect():
    if _detected[0]:
        return
    _detected[0] = True
    try:
        subprocess.run(["pdftotext", "-v"], capture_output=True)
        _pdftotext[0] = True
    except (FileNotFoundError, OSError):
        _pdftotext[0] = False
    try:
        import pymupdf
        _pm[0] = pymupdf
    except ImportError:
        try:
            import fitz
            _pm[0] = fitz
        except ImportError:
            pass
    try:
        import pypdf as m
        _pp[0] = m
    except ImportError:
        pass


def pdf_backend_names():
    _detect()
    n = []
    if _pdftotext[0]:
        n.append("pdftotext")
    if _pm[0] is not None:
        n.append("pymupdf")
    if _pp[0] is not None:
        n.append("pypdf")
    return n


def extract_pdf_text(path):
    _detect()
    if _pdftotext[0]:
        try:
            r = subprocess.run(["pdftotext", "-q", "-layout", path, "-"],
                               capture_output=True, timeout=600)
            if r.returncode == 0:
                t = r.stdout.decode("utf-8", errors="replace")
                if t.strip():
                    return t
        except Exception as e:
            print("    ! pdftotext failed on %s: %s" % (path, e))
    if _pm[0] is not None:
        try:
            doc = _pm[0].open(path)
            parts = [doc.load_page(i).get_text() for i in range(len(doc))]
            doc.close()
            t = "\n".join(parts)
            if t.strip():
                return t
        except Exception as e:
            print("    ! pymupdf failed on %s: %s" % (path, e))
    if _pp[0] is not None:
        try:
            rd = _pp[0].PdfReader(path)
            parts = []
            for pg in rd.pages:
                try:
                    parts.append(pg.extract_text() or "")
                except Exception:
                    continue
            t = "\n".join(parts)
            if t.strip():
                return t
        except Exception as e:
            print("    ! pypdf failed on %s: %s" % (path, e))
    return None


def connect():
    db = sqlite3.connect(DB)
    db.isolation_level = None
    return db


def ensure_schema(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(chunks)").fetchall()]
    if not cols:
        db.execute("""CREATE TABLE chunks (id INTEGER PRIMARY KEY,
            collection TEXT NOT NULL, path TEXT NOT NULL, idx INTEGER NOT NULL,
            txt TEXT NOT NULL, vec TEXT NOT NULL)""")
    elif "collection" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN collection TEXT NOT NULL DEFAULT 'legacy'")
        print("  migrated v1 index: existing rows tagged collection='legacy'")
    db.execute("CREATE INDEX IF NOT EXISTS idx_coll ON chunks(collection)")
    db.execute("""CREATE TABLE IF NOT EXISTS roots (collection TEXT PRIMARY KEY,
        roots TEXT NOT NULL, indexed_at TEXT NOT NULL)""")
    rename_legacy_collections(db)


def rename_legacy_collections(db):
    """The code-triage AI was called pierre until 2026-11-13 (and lumo before that).

    An existing ~/local-ai/knowledge.db holds its chunks under 'pierre_knowledge', so after the
    rename `heim` would query 'heim_knowledge', find nothing, and answer from the persona alone -
    silently, because an empty collection is not an error. Renaming the rows keeps the embeddings
    that took minutes to build. Only done when the new name is not already in use.
    """
    for old, new in (("pierre_knowledge", "heim_knowledge"), ("lumo_knowledge", "heim_knowledge")):
        have_old = db.execute("SELECT 1 FROM chunks WHERE collection = ? LIMIT 1", (old,)).fetchone()
        have_new = db.execute("SELECT 1 FROM chunks WHERE collection = ? LIMIT 1", (new,)).fetchone()
        if not have_old or have_new:
            continue
        db.execute("UPDATE chunks SET collection = ? WHERE collection = ?", (new, old))
        db.execute("UPDATE OR REPLACE roots SET collection = ? WHERE collection = ?", (new, old))
        print("  renamed collection '%s' -> '%s' (nothing re-embedded)" % (old, new))


def embed(texts):
    body = {"model": EMBED_MODEL, "input": [DOC_PREFIX + t for t in texts]}
    req = urllib.request.Request(EMBED_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        vecs = json.loads(r.read())["embeddings"]
    if len(vecs) != len(texts):
        raise RuntimeError("embed returned %d vectors for %d texts" % (len(vecs), len(texts)))
    return vecs


def file_filter(name):
    if name in SKIP_EXACT or name.startswith(SKIP_PREFIX):
        return None
    lo = name.lower()
    if lo.endswith(PDF_EXT):
        return "pdf"
    if lo.endswith(TEXT_EXT):
        return "text"
    return None


def read_text(full, rel):
    """Return the file text, or None with a printed reason. Never silent."""
    try:
        size = os.path.getsize(full)
        if size > MAX_TEXT_BYTES:
            print("    [too big, skipped: %d KB > %d KB] %s" % (size // 1024, MAX_TEXT_BYTES // 1024, rel))
            return None
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print("    [unreadable, skipped: %s] %s" % (e, rel))
        return None
    if not text.strip():
        return None
    lines = text.count("\n") + 1
    if rel.lower().endswith(MINIFIABLE_EXT) and len(text) / lines > MINIFIED_AVG_LINE:
        print("    [minified/generated, skipped] %s" % rel)
        return None
    return text


def iter_chunks(kind, full, rel):
    if kind == "pdf":
        text = extract_pdf_text(full)
    else:
        text = read_text(full, rel)
    if not text:
        return
    step = CHUNK_CHARS - OVERLAP
    # Every slice is yielded (even whitespace-only) so chunk idx stays contiguous
    # and audit_plugin.py can rebuild the file exactly.
    for i in range(0, len(text), step):
        yield text[i:i + CHUNK_CHARS]
        if i + CHUNK_CHARS >= len(text):
            break


def index_collection(collection, roots, allow_empty=False):
    t0 = time.time()
    nf = nc = nnp = 0
    pending = []
    stage = STAGE_PREFIX + collection
    abs_roots = [os.path.abspath(os.path.expanduser(r)) for r in roots]
    missing = [r for r in abs_roots if not os.path.isdir(r)]
    if missing:
        sys.exit("ERROR: not a directory: %s\nLive collection '%s' untouched."
                 % (", ".join(missing), collection))
    db = connect()
    ensure_schema(db)
    db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))

    def flush():
        nonlocal nc
        if not pending:
            return
        vecs = embed([t for _, _, t in pending])
        db.execute("BEGIN")
        db.executemany("INSERT INTO chunks (collection,path,idx,txt,vec) VALUES (?,?,?,?,?)",
                       [(stage, p, i, t, json.dumps(v)) for (p, i, t), v in zip(pending, vecs)])
        db.execute("COMMIT")
        nc += len(pending)
        pending.clear()

    multi = len(abs_roots) > 1
    try:
        for root in abs_roots:
            print("  walking %s ..." % root)
            for dp, dns, fns in os.walk(root):
                dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
                for fn in sorted(fns):
                    kind = file_filter(fn)
                    if kind is None:
                        continue
                    full = os.path.join(dp, fn)
                    rel = os.path.relpath(full, root)
                    if multi:  # keep paths unique and traceable to their root
                        rel = os.path.join(os.path.basename(root), rel)
                    if kind == "pdf":
                        try:
                            if os.path.getsize(full) > MAX_PDF_BYTES:
                                print("    [PDF too big, skipped] %s" % rel)
                                continue
                        except OSError as e:
                            print("    [unreadable, skipped: %s] %s" % (e, rel))
                            continue
                    got = False
                    for i, c in enumerate(iter_chunks(kind, full, rel)):
                        pending.append((rel, i, c))
                        got = True
                        if len(pending) >= BATCH:
                            flush()
                            print("    ...%d chunks staged" % nc, flush=True)
                    if got:
                        nf += 1
                    elif kind == "pdf":
                        nnp += 1
                        # With no PDF library installed, EVERY pdf yields nothing, and saying
                        # "needs OCR" blames the file for a missing dependency. On 2026-11-13
                        # that sent seven perfectly good engine-doc PDFs to an OCR queue they
                        # did not need; the real fix was to run this with the python that has
                        # PyMuPDF. Only claim a missing text layer when we could have read one.
                        if pdf_backend_names():
                            print("    [no text layer] %s — needs OCR" % rel)
                        else:
                            print("    [skipped, no PDF library] %s" % rel)
        flush()
    except KeyboardInterrupt:
        try:
            db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))
        except Exception:
            pass
        db.close()
        sys.exit("\nInterrupted. Live collection '%s' untouched." % collection)
    except Exception as e:
        try:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))
        except Exception:
            pass
        db.close()
        sys.exit("ERROR: indexing '%s' failed (%s). Live collection untouched." % (collection, e))
    if nc == 0 and not allow_empty:
        db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))
        db.close()
        sys.exit("ERROR: no indexable files under %s. Live collection '%s' untouched "
                 "(pass --allow-empty to clear it on purpose)." % (", ".join(abs_roots), collection))
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM chunks WHERE collection = ?", (collection,))
        db.execute("UPDATE chunks SET collection = ? WHERE collection = ?", (collection, stage))
        db.execute("INSERT OR REPLACE INTO roots (collection, roots, indexed_at) VALUES (?,?,?)",
                   (collection, json.dumps(abs_roots), time.strftime("%Y-%m-%d %H:%M:%S")))
        db.execute("COMMIT")
    except Exception as e:
        try:
            db.execute("ROLLBACK")
            db.execute("DELETE FROM chunks WHERE collection = ?", (stage,))
        except Exception:
            pass
        db.close()
        sys.exit("ERROR: swap failed (%s). Live collection untouched." % e)
    db.close()
    print("Indexed collection '%s': %d files, %d chunks in %.0fs" % (collection, nf, nc, time.time() - t0))
    if nnp and pdf_backend_names():
        print("NOTE: %d PDFs had no extractable text (scanned?). OCR then re-run." % nnp)
    elif nnp:
        print("NOTE: %d PDFs were skipped because this python has no PDF library. They are not\n"
              "      necessarily scanned. Re-run with the alias, which uses the environment the\n"
              "      installer put PyMuPDF in:  heim-index -c <collection> <folder>" % nnp)


def list_collections():
    if not os.path.exists(DB):
        print("No index yet at %s" % DB)
        return
    db = connect()
    try:
        rows = db.execute("SELECT collection,COUNT(DISTINCT path),COUNT(*) FROM chunks "
                          "WHERE collection NOT LIKE '\\_\\_stage\\_\\_%' ESCAPE '\\' "
                          "GROUP BY collection ORDER BY collection").fetchall()
    except sqlite3.OperationalError:
        print("Database exists but has no usable collections yet.")
        return
    finally:
        db.close()
    if not rows:
        print("Index exists but is empty.")
        return
    print("Collections in %s:" % DB)
    print("  %-20s %8s %8s" % ("collection", "files", "chunks"))
    for c, a, b in rows:
        print("  %-20s %8d %8d" % (c, a, b))


ADDED = "added"


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def add_files(paths):
    """Copy single files into <folder of the index>/added and index that folder as 'added'.

    Copied, because a dragged file usually lies in Downloads and the index stores only its
    text: the copy is what lets the collection be rebuilt later. The whole (small) folder is
    re-indexed through the usual stage, so a failed run leaves the live collection as it was.
    Returns the number of files copied in."""
    dest = os.path.join(os.path.dirname(DB), ADDED)
    copied = 0
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        name = os.path.basename(p)
        if os.path.isdir(p):
            print("  folder, not added: %s\n    index a folder where it is: rag_index.py -c <name> %s" % (p, p))
            continue
        if not os.path.isfile(p):
            print("  not found: %s" % p)
            continue
        if file_filter(name) is None:
            print("  not added (not a text or PDF type this index reads): %s" % name)
            continue
        os.makedirs(dest, exist_ok=True)
        dst = os.path.join(dest, name)
        if os.path.abspath(dst) == p:
            continue
        if os.path.exists(dst):
            if _digest(dst) == _digest(p):
                print("  already added: %s" % name)
                continue
            stem, ext = os.path.splitext(name)          # same name, different file: keep both
            dst = os.path.join(dest, "%s-%s%s" % (stem, _digest(p)[:8], ext))
            if os.path.exists(dst):
                print("  already added: %s" % name)
                continue
        shutil.copy2(p, dst)
        copied += 1
        print("  added: %s" % os.path.basename(dst))
    if not copied:
        print("Nothing new to index.")
        return 0
    b = pdf_backend_names()
    print("PDF extraction backends: %s" % (", ".join(b) if b else "NONE — PDFs skipped"))
    index_collection(ADDED, [dest])
    list_collections()
    return copied


def main():
    ap = argparse.ArgumentParser(description="Index folders (text+PDFs) into a named collection.")
    ap.add_argument("-c", "--collection", default=None)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--list-collections", action="store_true")
    ap.add_argument("--add", action="store_true",
                    help="the paths are single FILES: copy them into <index folder>/added and "
                         "index them as the collection 'added' (type the command, drag the files in)")
    ap.add_argument("--allow-empty", action="store_true",
                    help="allow a run with 0 chunks to replace (clear) the collection")
    args = ap.parse_args()
    if args.list_collections:
        list_collections()
        return
    if not args.paths:
        sys.exit("usage: rag_index.py [-c NAME] <folder> ...\n  rag_index.py --add <file> ...\n"
                 "  rag_index.py --list-collections")
    if args.add:
        if args.collection not in (None, ADDED):
            sys.exit("ERROR: --add always uses the collection '%s'; to index a folder into "
                     "'%s' leave --add out." % (ADDED, args.collection))
        add_files(args.paths)
        return
    coll = args.collection or os.path.basename(os.path.normpath(os.path.expanduser(args.paths[0])))
    if not re.match(r"^[a-zA-Z0-9_\-]{1,32}$", coll) or coll.startswith("__"):
        sys.exit("ERROR: bad collection name '%s' (letters, digits, _ and -, max 32, no leading __)" % coll)
    b = pdf_backend_names()
    print("PDF extraction backends: %s" % (", ".join(b) if b else "NONE — PDFs skipped"))
    index_collection(coll, args.paths, args.allow_empty)
    list_collections()


if __name__ == "__main__":
    main()
