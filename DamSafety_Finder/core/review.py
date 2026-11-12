"""Which candidates to put in front of a reviewer, and how to read the marks back (Finder 0.5).

Pure numpy. A review batch is the Finder's experiment: the marks are the only ground truth, so a
batch is designed to answer three questions at once, and every card carries the chance it had
of being picked, so the answers are unbiased (evaluate.ht_share):

  random control (~18 %)   a fixed random sample of the whole candidate list. The same cards stay
                           in it from one model version to the next (picked by a hash of the
                           card's id, not by the order of the list), so versions compare on
                           identical cards. It says how many real dams the WHOLE list holds.
  top-ranked (~37 %)       the highest-scored cards not reviewed before: what a user sees first.
  near the cut (~45 %)     cards ranked k to 3k (k = the top slice), or where several scores
                           disagree most (committee): the cards a better score would move in or
                           out of the list. At most per_tile_cap from one 5 km tile, so one dense
                           area does not fill the batch.
  second look (~12 %)      a share of the batch flagged for a second reviewer, blind to the first
                           marks: the first figure anyone will have for how often two reviewers
                           agree on a desk call.

Marks: yes / no / unsure / offset (a real dam just off the point - a candidate-placement miss,
not a ranking error). marks_to_labels turns them into training labels.
"""
import math
import zlib

import numpy as np


def _hash01(key, seed):
    return (zlib.crc32("{}|{}".format(seed, key).encode()) & 0xFFFFFFFF) / 4294967296.0


def pick_batch(rows, n=90, random_frac=0.18, top_frac=0.37, second_frac=0.12, score_key="score",
               committee=None, tile_m=5000.0, per_tile_cap=3, seed=0, reviewed=()):
    """Pick a review batch from candidate rows (dicts with find_id, x, y and a score).
    committee: optional list of score arrays (one per model) - the near-the-cut slice then takes
    the cards they disagree on most. reviewed: find_ids already marked (left out of the top and
    near slices; the random control keeps them, so it stays the same set).
    Returns the picked rows (copies) with 'stratum' (random / top / near), 'rank' (by score, 1 =
    highest), 'incl_prob' (the card's chance of being in a batch drawn this way) and 'second'
    ('yes' for a second reviewer), shuffled, numbered 'n' from 1."""
    rows = list(rows)
    N = len(rows)
    if N == 0:
        return []
    n = min(int(n), N)
    rng = np.random.default_rng(seed)
    s = np.array([float(r.get(score_key) or 0.0) for r in rows])
    tie = np.array([_hash01(r.get("find_id"), "tie") for r in rows])
    order = np.lexsort((tie, -s))
    rank = np.empty(N, int)
    rank[order] = np.arange(1, N + 1)
    done = set(reviewed)
    n_rand = int(round(n * random_frac))
    n_top = int(round(n * top_frac))
    n_near = n - n_rand - n_top
    # random control: the n_rand smallest hashes of the id (stable across score versions)
    h = np.array([_hash01(r.get("find_id"), seed) for r in rows])
    rand_idx = list(np.argsort(h)[:n_rand])
    picked = {int(i): "random" for i in rand_idx}
    # top slice: highest-scored not reviewed / not picked
    top_idx = []
    for i in order:
        if len(top_idx) >= n_top:
            break
        if int(i) in picked or rows[i].get("find_id") in done:
            continue
        top_idx.append(int(i))
        picked[int(i)] = "top"
    k = max(n_top, 1)
    lo_rank = max(rank[top_idx].max() if top_idx else 0, k) + 1
    hi_rank = 3 * lo_rank
    band = [int(i) for i in order if lo_rank <= rank[i] <= hi_rank and int(i) not in picked
            and rows[i].get("find_id") not in done]
    if committee is not None and len(committee) >= 2:
        cr = np.vstack([np.argsort(np.argsort(-np.asarray(c, float))) for c in committee])
        spread = cr.std(axis=0)
        band.sort(key=lambda i: -spread[i])
    else:
        band = [band[j] for j in rng.permutation(len(band))]
    tiles = {}
    for r in picked:
        key = (int(rows[r]["x"] // tile_m), int(rows[r]["y"] // tile_m))
        tiles[key] = tiles.get(key, 0) + 1
    near_idx = []
    for i in band:
        if len(near_idx) >= n_near:
            break
        key = (int(rows[i]["x"] // tile_m), int(rows[i]["y"] // tile_m))
        if tiles.get(key, 0) >= per_tile_cap:
            continue
        tiles[key] = tiles.get(key, 0) + 1
        near_idx.append(i)
        picked[i] = "near"
    p_rand = n_rand / float(N)
    band_size = max(1, sum(1 for i in range(N) if lo_rank <= rank[i] <= hi_rank and rows[i].get("find_id") not in done))
    p_band = min(1.0, len(near_idx) / float(band_size))
    out = []
    for i, st in picked.items():
        r = dict(rows[i])
        if rank[i] <= (rank[top_idx].max() if top_idx else 0) and rows[i].get("find_id") not in done:
            pi = 1.0
        elif lo_rank <= rank[i] <= hi_rank:
            pi = 1.0 - (1.0 - p_rand) * (1.0 - p_band)
        else:
            pi = p_rand
        r.update({"stratum": st, "rank": int(rank[i]), "incl_prob": round(pi, 6)})
        out.append(r)
    perm = rng.permutation(len(out))
    out = [out[j] for j in perm]
    n2 = int(round(len(out) * second_frac))
    second = set(rng.choice(len(out), n2, replace=False).tolist()) if n2 else set()
    for j, r in enumerate(out):
        r["n"] = j + 1
        r["second"] = "yes" if j in second else "no"
    return out


def marks_to_labels(marks, unsure_q=0.5):
    """{find_id: mark dict} -> {find_id: target} for training: yes 1, no 0, unsure unsure_q (a
    soft label), offset left out (the dam is real but this card is not on it: a placement miss)."""
    out = {}
    for fid, m in marks.items():
        v = (m or {}).get("mark")
        if v == "yes":
            out[fid] = 1.0
        elif v == "no":
            out[fid] = 0.0
        elif v == "unsure":
            out[fid] = float(unsure_q)
    return out


def agreement(first, second):
    """{'n', 'agree', 'kappa'} of two reviewers' yes / no marks on the same cards (Cohen's kappa;
    'unsure' / 'offset' / missing pairs left out)."""
    pairs = [(first[k].get("mark"), second[k].get("mark")) for k in first if k in second]
    pairs = [(a, b) for a, b in pairs if a in ("yes", "no") and b in ("yes", "no")]
    n = len(pairs)
    if not n:
        return {"n": 0, "agree": float("nan"), "kappa": float("nan")}
    po = sum(1 for a, b in pairs if a == b) / float(n)
    pa = sum(1 for a, _b in pairs if a == "yes") / float(n)
    pb = sum(1 for _a, b in pairs if b == "yes") / float(n)
    pe = pa * pb + (1 - pa) * (1 - pb)
    kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")
    return {"n": n, "agree": po, "kappa": kappa}


def offsets(marks, rows_by_id, chip_half_m=300.0, chip_px=480):
    """Placement misses: for each 'offset' mark with a click (px, py on the chip, 0..chip_px from
    the top-left), the map point the reviewer clicked and its distance from the card's point.
    [(find_id, x, y, distance m)]."""
    out = []
    for fid, m in marks.items():
        if (m or {}).get("mark") != "offset" or fid not in rows_by_id:
            continue
        px, py = m.get("px"), m.get("py")
        if px is None or py is None:
            continue
        r = rows_by_id[fid]
        sc = 2.0 * chip_half_m / chip_px
        x = r["x"] - chip_half_m + float(px) * sc
        y = r["y"] + chip_half_m - float(py) * sc
        out.append((fid, x, y, math.hypot(x - r["x"], y - r["y"])))
    return out
