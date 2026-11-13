#!/usr/bin/env python3
# rag_index.py v2.7 — multi-collection RAG indexer (stdlib only)
# Atomic rebuilds via a hidden per-collection stage; auto-migrates v1 DBs;
# PDF backends: pdftotext -> pymupdf -> pypdf -> skip.
#
# v2.7: --doctor (heim-doctor). Says, without changing anything, whether Heim is reading what
#   you think it is: no index, an empty index, no knowledge pack (Heim then answers from its
#   built-in rules alone, and an empty collection is not an error), a collection still under
#   its pre-rename name, an interrupted run's leftovers, a folder that moved, files that
#   changed or arrived after the folder was indexed, and files the index silently skips for
#   size. Exits 1 on a PROBLEM.
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


KNOWLEDGE = "heim_knowledge"
LEGACY_KNOWLEDGE = ("pierre_knowledge", "lumo_knowledge")


def _walk_indexable(roots):
    """(rel path as the index stores it, full path, kind) for every file a run over these
    roots would look at. The same walk as index_collection, so the two cannot disagree."""
    multi = len(roots) > 1
    for root in roots:
        for dp, dns, fns in os.walk(root):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
            for fn in sorted(fns):
                kind = file_filter(fn)
                if kind is None:
                    continue
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, root)
                if multi:
                    rel = os.path.join(os.path.basename(root), rel)
                yield rel, full, kind


def doctor_checks(ollama=True):
    """[(level, text)], level one of OK / WATCH / PROBLEM. Reads the index; never writes it
    (not even the schema upgrade a normal run does), so running it cannot hide what it finds."""
    out = []
    add = lambda level, text: out.append((level, text))
    if not os.path.exists(DB):
        add("PROBLEM", "no index at %s: heim and heim-chat answer from the model's built-in rules alone. "
                       "Index the knowledge pack: heim-index -c %s <heim/knowledge folder>" % (DB, KNOWLEDGE))
        return out
    try:
        db = sqlite3.connect("file:%s?mode=ro" % urllib.request.pathname2url(DB), uri=True)
        counts = dict((c, (f, n)) for c, f, n in db.execute(
            "SELECT collection, COUNT(DISTINCT path), COUNT(*) FROM chunks GROUP BY collection"))
        try:
            roots = dict((c, (json.loads(r), t)) for c, r, t in db.execute(
                "SELECT collection, roots, indexed_at FROM roots"))
        except sqlite3.Error:
            roots = {}
        paths = {}
        for c, p in db.execute("SELECT DISTINCT collection, path FROM chunks"):
            paths.setdefault(c, set()).add(p)
        db.close()
    except sqlite3.Error as e:
        add("PROBLEM", "%s is not a readable index (%s)" % (DB, e))
        return out
    live = dict((c, v) for c, v in counts.items() if not c.startswith("__"))
    add("OK", "index %s: %d collection(s), %d passages" % (DB, len(live), sum(n for _, n in live.values())))
    if not live:
        add("PROBLEM", "the index is empty: heim and heim-chat answer from the model's built-in rules alone")
    legacy = [c for c in LEGACY_KNOWLEDGE if c in live]
    if KNOWLEDGE not in live:
        if legacy:
            add("PROBLEM", "the knowledge pack is still under its old name '%s', and Heim asks for '%s': it "
                           "finds nothing and says nothing. Indexing the pack renames it and brings it up to date: "
                           "heim-index -c %s <heim/knowledge folder>" % (legacy[0], KNOWLEDGE, KNOWLEDGE))
        elif live:
            add("PROBLEM", "no '%s' collection: Heim has code and documents but not the knowledge pack (known "
                           "behaviours, failure casebook, tool help). Index it: heim-index -c %s "
                           "<heim/knowledge folder>" % (KNOWLEDGE, KNOWLEDGE))
    elif legacy:
        add("WATCH", "old collection(s) %s are still in the index beside '%s' and are searched too: a stale "
                     "copy of the pack can out-rank the current one" % (", ".join(legacy), KNOWLEDGE))
    stages = [c for c in counts if c.startswith(STAGE_PREFIX)]
    if stages:
        add("WATCH", "an indexing run was interrupted and left %s (%d passages, not searched). Run that "
                     "heim-index again; it clears its own leftovers"
            % (", ".join(c[len(STAGE_PREFIX):] for c in stages), sum(counts[c][1] for c in stages)))
    for c in sorted(live):
        nfiles, nchunks = live[c]
        if c not in roots:
            add("OK", "%-16s %5d files, %6d passages (folder not recorded: indexed by an older version)"
                % (c, nfiles, nchunks))
            continue
        rts, when = roots[c]
        missing = [r for r in rts if not os.path.isdir(r)]
        if missing:
            add("WATCH", "%s was indexed from %s, which is not there now: it still answers from the text "
                         "as it was on %s, and cannot be refreshed until the folder is back or it is "
                         "re-indexed from where it moved" % (c, ", ".join(missing), when))
            continue
        try:
            stamp = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            stamp = None
        new, changed, big = [], [], []
        on_disk = set()
        for rel, full, kind in _walk_indexable(rts):
            on_disk.add(rel)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size > (MAX_PDF_BYTES if kind == "pdf" else MAX_TEXT_BYTES):
                big.append(rel)
                continue
            if rel not in paths.get(c, ()):
                # a file with no text (an empty __init__.py, a scanned PDF) is never in the
                # index, so "not indexed" only means something when it arrived after the run
                if stamp is not None and st.st_mtime > stamp + 2:
                    new.append(rel)
            elif stamp is not None and st.st_mtime > stamp + 2:
                changed.append(rel)
        gone = sorted(paths.get(c, set()) - on_disk)
        notes = []
        if changed:
            notes.append("%d file(s) changed since (first: %s)" % (len(changed), changed[0]))
        if new:
            notes.append("%d new file(s) since (first: %s)" % (len(new), new[0]))
        if gone:
            notes.append("%d indexed file(s) are gone from the folder (first: %s)" % (len(gone), gone[0]))
        if notes:
            add("WATCH", "%s is older than its files (indexed %s): %s. Heim answers from the text as it "
                         "was. Refresh: heim-index -c %s %s"
                % (c, when, "; ".join(notes), c, " ".join("'%s'" % r for r in rts)))
        else:
            add("OK", "%-16s %5d files, %6d passages, level with its folder (indexed %s)"
                % (c, nfiles, nchunks, when))
        if big:
            add("WATCH", "%s: %d file(s) are over the size limit (%d KB for text) and are never indexed, "
                         "however often it is re-run (first: %s). Split them if Heim should read them"
                % (c, len(big), MAX_TEXT_BYTES // 1024, big[0]))
    if ollama:
        base = EMBED_URL.rsplit("/api/", 1)[0]
        chat_model = os.environ.get("HEIM_MODEL", "heim")
        try:
            with urllib.request.urlopen(base + "/api/tags", timeout=10) as r:
                tags = {m.get("name", "") for m in json.loads(r.read()).get("models", [])}
            for m_ in (chat_model, EMBED_MODEL):
                if m_ in tags or (m_ + ":latest") in tags:
                    add("OK", "model %s is present" % m_)
                else:
                    add("PROBLEM", "model %s is missing from Ollama: re-run the installer" % m_)
        except Exception as e:
            add("PROBLEM", "Ollama is not reachable at %s (%s). Start it: heim-start" % (base, e))
    return out


def doctor(ollama=True):
    checks = doctor_checks(ollama)
    for level, text in checks:
        print("  %-8s %s" % ({"OK": "ok", "WATCH": "WATCH", "PROBLEM": "PROBLEM"}[level], text))
    problems = sum(1 for level, _ in checks if level == "PROBLEM")
    watches = sum(1 for level, _ in checks if level == "WATCH")
    if problems:
        print("\n%d PROBLEM(s): until fixed, Heim runs and answers from less than you think." % problems)
    elif watches:
        print("\nNo problem; %d thing(s) to look at." % watches)
    else:
        print("\nAll checks passed.")
    return 1 if problems else 0


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
    ap.add_argument("--doctor", action="store_true",
                    help="check, without changing anything, that the index is there, holds the "
                         "knowledge pack and is level with the folders it was built from")
    ap.add_argument("--library", action="store_true",
                    help="with --doctor: check the index and folders only (no Ollama needed)")
    args = ap.parse_args()
    if args.doctor:
        sys.exit(doctor(ollama=not args.library))
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
