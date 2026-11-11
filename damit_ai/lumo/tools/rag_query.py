#!/usr/bin/env python3
# rag_query.py v2.2 — query the RAG store, streaming output (stdlib only; numpy if present)
#
# v2.2 fixes (vs v2.1):
#   - nomic-embed-text query prefix (search_query:) to match the v2.4 index.
#   - Hidden per-collection stages excluded from "all".
#   - num_ctx sent per request, sized to the prompt, so Ollama never trims the
#     front of the prompt (the instructions) without saying so.
#   - qwen3 "thinking" is streamed separately and kept out of the answer.
import os, sys, json, sqlite3, urllib.request, urllib.error, math, argparse

DB = os.path.expanduser(os.environ.get("LUMO_DB", "~/local-ai/knowledge.db"))
BASE = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = "nomic-embed-text"
QUERY_PREFIX = "search_query: "
CHAT_MODEL = os.environ.get("LUMO_MODEL", "lumo")
NUM_CTX = int(os.environ.get("LUMO_NUM_CTX", "16384"))
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


def retrieve(q, coll, top_k):
    if not os.path.exists(DB):
        sys.exit("No index at %s — run rag_index.py first." % DB)
    db = sqlite3.connect(DB)
    if coll == "all":
        rows = db.execute("SELECT collection,path,idx,txt,vec FROM chunks "
                          "WHERE collection NOT LIKE '\\_\\_%' ESCAPE '\\'").fetchall()
    else:
        rows = db.execute("SELECT collection,path,idx,txt,vec FROM chunks WHERE collection=?",
                          (coll,)).fetchall()
    db.close()
    if not rows:
        h = (" Collection '%s' empty — index it: rag_index.py -c %s <folder>" % (coll, coll)) if coll != "all" else ""
        sys.exit("Index is empty.%s" % h)
    qv = post(BASE + "/api/embed", {"model": EMBED_MODEL, "input": [QUERY_PREFIX + q]})["embeddings"][0]
    if np is not None:
        M = np.array([json.loads(r[4]) for r in rows], dtype=np.float32)
        v = np.array(qv, dtype=np.float32)
        sims = (M @ v) / (np.linalg.norm(M, axis=1) * np.linalg.norm(v) + 1e-9)
        return [(float(sims[i]), rows[i][0], rows[i][1], rows[i][2], rows[i][3])
                for i in np.argsort(-sims)[:top_k]]

    def cos(a, b):
        d = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return d / (na * nb) if na and nb else 0.0
    sc = sorted(((cos(qv, json.loads(r[4])), r) for r in rows), key=lambda x: -x[0])[:top_k]
    return [(s, r[0], r[1], r[2], r[3]) for s, r in sc]


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--collection", default="all")
    ap.add_argument("-k", "--top-k", type=int, default=TOP_K)
    ap.add_argument("question", nargs="+")
    args = ap.parse_args()
    q = " ".join(args.question).strip()
    if not q:
        sys.exit("no question given")
    hits = retrieve(q, args.collection, args.top_k)
    print("Top %d context hits:" % len(hits))
    for s, c, p, i, _ in hits:
        print("  %.3f  [%s] %s (chunk %d)" % (s, c, p, i))
    print("-" * 70)
    ctx = "\n\n".join("--- [%s] %s (chunk %d) ---\n%s" % (c, p, i, t) for _, c, p, i, t in hits)
    prompt = ("Answer using ONLY the context below. Cite each fact with the collection "
              "and file path shown in its bracket header. If insufficient, say so plainly.\n\n"
              "=== CONTEXT ===\n" + ctx + "\n=== QUESTION ===\n" + q)
    print("Answer (%s, streaming — Ctrl+C aborts):" % CHAT_MODEL)
    print("-" * 70)
    ans = ""
    try:
        ans = stream_answer(prompt)
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(0)
    except Exception as e:
        print("[stream failed (%s) — non-streaming fallback]" % e)
        out = post(BASE + "/api/chat", chat_payload(prompt, False, think=False))
        ans = (out.get("message") or {}).get("content") or ""
        print(ans)
    if not ans.strip():
        print("[empty response — check `ollama ps` and the server log]")


if __name__ == "__main__":
    main()
