#!/usr/bin/env python3
# rag_query.py v2.4 — query the RAG store, streaming output (stdlib only; numpy if present)
#
#   rag_query.py "question"            one question, answered from the indexed documents
#   rag_query.py -c standards "..."    from one collection
#   rag_query.py --chat                a conversation: every question is looked up first
#
# v2.4: `heim doctor`, `heim add FILE ...` and `heim chat` do what `heim-doctor`, `heim-add`
#   and `heim-chat` do. Vor takes its commands as words (`vor doctor`), Heim took them only
#   as separate hyphenated commands, so `heim doctor` put the one word "doctor" to the model
#   as a question and came back with an answer instead of the check.
# v2.3: --chat (the `heim-chat` command). heim-chat used to be `ollama run heim`: the bare
#   model, which never read a document, so the knowledge pack and anything added with
#   heim-add were invisible to it. Now each question is looked up in the index like a one-shot
#   query, a follow-up is searched together with the question before it, and a file dragged
#   into the window is indexed (rag_index --add) and readable on the next question. With
#   nothing indexed it says so before every answer: an empty index is not an error, and that
#   has cost a wrong answer more than once.
#
# v2.2 fixes (vs v2.1):
#   - nomic-embed-text query prefix (search_query:) to match the v2.4 index.
#   - Hidden per-collection stages excluded from "all".
#   - num_ctx sent per request, sized to the prompt, so Ollama never trims the
#     front of the prompt (the instructions) without saying so.
#   - qwen3 "thinking" is streamed separately and kept out of the answer.
import os, sys, json, sqlite3, urllib.request, urllib.error, math, argparse, shlex

DB = os.path.expanduser(os.environ.get("HEIM_DB", "~/local-ai/knowledge.db"))
BASE = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = "nomic-embed-text"
QUERY_PREFIX = "search_query: "
CHAT_MODEL = os.environ.get("HEIM_MODEL", "heim")
NUM_CTX = int(os.environ.get("HEIM_NUM_CTX", "16384"))
TOP_K = 10
try:
    import numpy as np
except ImportError:
    np = None


def post(url, payload, timeout=900):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Store(object):
    """The indexed passages of one collection (or all), held in memory. A chat asks many
    questions, and parsing every stored vector again for each one takes seconds on a full
    index; the file's size and time say when it has changed and must be read again."""

    def __init__(self, coll):
        self.coll = coll
        self.rows, self.M, self.stamp = [], None, None
        self.load()

    def _stamp(self):
        try:
            st = os.stat(DB)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def load(self):
        self.stamp = self._stamp()
        self.rows, self.M = [], None
        if self.stamp is None:
            return
        db = sqlite3.connect(DB)
        try:
            if self.coll == "all":
                rows = db.execute("SELECT collection,path,idx,txt,vec FROM chunks "
                                  "WHERE collection NOT LIKE '\\_\\_%' ESCAPE '\\'").fetchall()
            else:
                rows = db.execute("SELECT collection,path,idx,txt,vec FROM chunks WHERE collection=?",
                                  (self.coll,)).fetchall()
        except sqlite3.OperationalError:      # a file that is not an index yet
            rows = []
        finally:
            db.close()
        vecs = [json.loads(r[4]) for r in rows]
        self.rows = [r[:4] for r in rows]
        if np is not None and vecs:
            self.M = np.array(vecs, dtype=np.float32)
            self.norms = np.linalg.norm(self.M, axis=1)
        else:
            self.vecs = vecs

    def changed(self):
        return self._stamp() != self.stamp

    def collections(self):
        out = {}
        for r in self.rows:
            out[r[0]] = out.get(r[0], 0) + 1
        return out

    def _sims(self, qv):
        if self.M is not None:
            v = np.array(qv, dtype=np.float32)
            return ((self.M @ v) / (self.norms * np.linalg.norm(v) + 1e-9)).tolist()
        nb = math.sqrt(sum(y * y for y in qv))

        def cos(a):
            d = sum(x * y for x, y in zip(a, qv))
            na = math.sqrt(sum(x * x for x in a))
            return d / (na * nb) if na and nb else 0.0
        return [cos(a) for a in self.vecs]

    def search(self, queries, top_k):
        """Best passages for any of the queries (a follow-up is searched on its own and
        together with the question before it; each passage keeps its better score)."""
        if not self.rows:
            return []
        qvs = post(BASE + "/api/embed", {"model": EMBED_MODEL,
                                         "input": [QUERY_PREFIX + q for q in queries]})["embeddings"]
        best = None
        for qv in qvs:
            sims = self._sims(qv)
            best = sims if best is None else [max(a, b) for a, b in zip(best, sims)]
        order = sorted(range(len(best)), key=lambda i: -best[i])[:top_k]
        return [(float(best[i]),) + tuple(self.rows[i]) for i in order]


def retrieve(q, coll, top_k):
    if not os.path.exists(DB):
        sys.exit("No index at %s — run rag_index.py first." % DB)
    store = Store(coll)
    if not store.rows:
        h = (" Collection '%s' empty — index it: rag_index.py -c %s <folder>" % (coll, coll)) if coll != "all" else ""
        sys.exit("Index is empty.%s" % h)
    return store.search([q], top_k)


def chat_payload(prompt, stream, think=True):
    est_tokens = len(prompt) // 3 + 4096          # prompt + room for thinking and answer
    p = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": prompt}],
         "stream": stream, "options": {"num_ctx": max(NUM_CTX, min(est_tokens, 40960))}}
    if think:
        p["think"] = True
    return p


def stream_answer(prompt, think=True):
    req = urllib.request.Request(BASE + "/api/chat", data=json.dumps(chat_payload(prompt, True, think)).encode(),
                                 headers={"Content-Type": "application/json"})
    buf = []
    thinking_shown = False
    try:
        r = urllib.request.urlopen(req, timeout=1800)
    except urllib.error.HTTPError as e:
        if think and e.code == 400:      # older Ollama without the "think" field
            return stream_answer(prompt, think=False)
        raise
    with r:
        while True:
            line = r.readline()
            if not line:
                break
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
                sys.stdout.write("(thinking…)\n")
                sys.stdout.flush()
                thinking_shown = True
            piece = msg.get("content") or ""
            if piece:
                buf.append(piece)
                sys.stdout.write(piece)
                sys.stdout.flush()
            if ch.get("done"):
                break
    sys.stdout.write("\n")
    return "".join(buf)


INSTRUCTION = ("Answer using ONLY the context below. Cite each fact with the collection "
               "and file path shown in its bracket header. If insufficient, say so plainly.")
NO_DOCS = ("NO DOCUMENTS ARE INDEXED%s, so nothing below is looked up or cited: it is the model's "
           "built-in rules only.\nIndex the knowledge pack:  heim-index -c heim_knowledge <folder>   "
           "(the installer does this; `heim-index --list-collections` shows what is there)")


def build_prompt(q, hits, earlier=None):
    ctx = "\n\n".join("--- [%s] %s (chunk %d) ---\n%s" % (c, p, i, t) for _, c, p, i, t in hits)
    parts = [INSTRUCTION, ""]
    if earlier:
        parts += ["=== EARLIER IN THIS CONVERSATION (for what the question refers to; not a source) ===",
                  "Question: " + earlier[0], "Answer: " + earlier[1][:1200], ""]
    parts += ["=== CONTEXT ===", ctx, "=== QUESTION ===", q]
    return "\n".join(parts)


def answer(prompt):
    """Stream the answer; fall back to one non-streamed call. Returns the text ('' if none)."""
    try:
        ans = stream_answer(prompt)
    except KeyboardInterrupt:
        print("\n[interrupted]")
        return None
    except Exception as e:
        print("[stream failed (%s) — non-streaming fallback]" % e)
        out = post(BASE + "/api/chat", chat_payload(prompt, False, think=False))
        ans = (out.get("message") or {}).get("content") or ""
        print(ans)
    if not ans.strip():
        print("[empty response — check `ollama ps` and the server log]")
    return ans


def dropped_paths(line):
    """The paths in a line Terminal pasted when a file was dragged in, or [] when the line is
    a question. Every word must be an existing path that has a separator or starts with ~, so
    a question that happens to name a file in the current folder is still a question."""
    try:
        parts = shlex.split(line)
    except ValueError:
        return []
    out = []
    for p in parts:
        full = os.path.expanduser(p)
        if not (os.sep in p or p.startswith("~")) or not os.path.exists(full):
            return []
        out.append(full)
    return out


def add_dropped(paths):
    """Index dragged files with rag_index.add_files (the heim-add command). Returns the number
    of files indexed, or None when the indexer could not be used at all."""
    import importlib.util
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_index.py")
    if not os.path.isfile(src):
        print("rag_index.py is not next to this script, so nothing was added. Use: heim-add <file>")
        return None
    spec = importlib.util.spec_from_file_location("rag_index_for_chat", src)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod.add_files(paths)
    except SystemExit as e:                 # the indexer exits with its reason; keep the chat
        if e.code not in (0, None):
            print(e.code if isinstance(e.code, str) else "not added (reason above)")
        return None
    except Exception as e:                  # Ollama down, an unreadable file: say so and go on
        print("not added: %s" % e)
        return None


def chat(coll, top_k, ask=input):
    store = Store(coll)
    print("Heim chat (%s): every question is looked up in the indexed documents first.\n"
          "Blank line or Ctrl+D quits. Drag a file into this window to add it." % CHAT_MODEL)
    cols = store.collections()
    if cols:
        print("Reading %d passages: %s" % (len(store.rows), ", ".join(
            "%s (%d)" % kv for kv in sorted(cols.items()))))
    else:
        print(NO_DOCS % ((" in collection '%s'" % coll) if coll != "all" else ""))
    earlier = None
    while True:
        try:
            q = ask("\n? ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            break
        if not q:
            break
        drop = dropped_paths(q)
        if drop:
            n = add_dropped(drop)
            store.load()
            if n:
                print("Ready: ask about it.")
                if coll not in ("all", "added"):
                    print("(this chat reads only collection '%s'; a dragged file goes into 'added', "
                          "so start heim-chat without -c to read it)" % coll)
            continue
        if store.changed():                 # heim-add or heim-index ran in another window
            store.load()
        try:
            hits = store.search([q] + ([earlier[0] + "\n" + q] if earlier else []), top_k)
        except Exception as e:
            print("could not look that up: %s\n(is Ollama running? heim-start)" % e)
            continue
        if hits:
            print("Looked up %d passages:" % len(hits))
            for s, c, p, i, _ in hits:
                print("  %.3f  [%s] %s (chunk %d)" % (s, c, p, i))
            prompt = build_prompt(q, hits, earlier)
        else:
            print(NO_DOCS % ((" in collection '%s'" % coll) if coll != "all" else ""))
            prompt = q if not earlier else ("Earlier question: %s\nEarlier answer: %s\n\nQuestion: %s"
                                            % (earlier[0], earlier[1][:1200], q))
        print("-" * 70)
        ans = answer(prompt)
        if ans is None:
            continue
        if ans.strip():
            earlier = (q, ans.strip())


def command_word(argv):
    """`heim doctor`, `heim add FILE ...`, `heim chat`: the arguments to run instead, or None
    when argv is a question.

    Returns ("index", [...]) for rag_index.py beside this file, ("query", [...]) for this one.
    A question is never taken for a command: `doctor` counts only with nothing after it but
    flags, `chat` only alone, and `add` only when every word after it is a file or folder that
    exists (or there is none, which gets the usage line). "add a culvert to the run?" is a
    question; "doctor" on its own is not one."""
    if not argv or argv[0] not in ("doctor", "add", "chat"):
        return None
    word, rest = argv[0], argv[1:]
    if word == "doctor" and all(r.startswith("-") for r in rest):
        return ("index", ["--doctor"] + rest)
    if word == "chat" and not rest:
        return ("query", ["--chat"])
    if word == "add":
        if not rest:
            sys.exit("usage: heim add FILE ...   (a file dragged into the Terminal window gives its path)")
        if all(os.path.exists(os.path.expanduser(r)) for r in rest):
            return ("index", ["--add"] + rest)
    return None


def main():
    word = command_word(sys.argv[1:])
    if word and word[0] == "index":
        tool = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_index.py")
        if not os.path.exists(tool):
            sys.exit("ERROR: %s is missing; re-run the Heim installer" % tool)
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable, tool] + word[1])
    if word:
        sys.argv[1:] = word[1]
    ap = argparse.ArgumentParser(description="Ask Heim a question, answered from the indexed documents.",
                                 epilog="Also: heim doctor (is Heim reading what you think it is), "
                                        "heim add FILE ... (index a file), heim chat (a conversation).")
    ap.add_argument("-c", "--collection", default="all",
                    help="read one collection only (default: all of them)")
    ap.add_argument("-k", "--top-k", type=int, default=TOP_K, help="passages handed to the model per question")
    ap.add_argument("--chat", action="store_true",
                    help="a conversation: each question is looked up first, and a dragged file is indexed")
    ap.add_argument("question", nargs="*")
    args = ap.parse_args()
    if args.chat:
        if args.question:
            ap.error("--chat takes no question on the command line: type it at the ? prompt")
        chat(args.collection, args.top_k)
        return
    q = " ".join(args.question).strip()
    if not q:
        sys.exit("no question given")
    hits = retrieve(q, args.collection, args.top_k)
    print("Top %d context hits:" % len(hits))
    for s, c, p, i, _ in hits:
        print("  %.3f  [%s] %s (chunk %d)" % (s, c, p, i))
    print("-" * 70)
    prompt = build_prompt(q, hits)
    print("Answer (%s, streaming — Ctrl+C aborts):" % CHAT_MODEL)
    print("-" * 70)
    if answer(prompt) is None:
        sys.exit(0)


if __name__ == "__main__":
    main()
