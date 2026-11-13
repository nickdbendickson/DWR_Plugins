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
import hashlib
import math

import numpy as np


def _hash01(key, seed):
    """A uniform number in [0, 1) from a card id and a seed. blake2b, not crc32: crc32 is affine,
    so two seeds would pick nearly the same 'random' cards."""
    d = hashlib.blake2b("{}|{}".format(seed, key).encode(), digest_size=8).digest()
    return int.from_bytes(d, "big") / 18446744073709551616.0


def _tile(r, tile_m):
    return (int(float(r["x"]) // tile_m), int(float(r["y"]) // tile_m))


def _take(pool, n_near, tiles, cap):
    """Walk pool in order, take up to n_near cards, at most cap from one tile."""
    used, got = {}, []
    for i in pool:
        if len(got) >= n_near:
            break
        t = tiles[i]
        if used.get(t, 0) >= cap:
            continue
        used[t] = used.get(t, 0) + 1
        got.append(i)
    return got


def _near_chance(pool, n_near, tiles, cap, seed, replays=2000):
    """{card: chance a uniformly shuffled walk of pool (n_near cards, at most cap a tile) takes it}.
    Exact when the cap cannot bite (no tile holds more than cap pool cards); otherwise counted
    over replays with their own random stream."""
    pool = list(pool)
    if not pool or n_near <= 0:
        return {i: 0.0 for i in pool}
    per = {}
    for i in pool:
        per[tiles[i]] = per.get(tiles[i], 0) + 1
    if max(per.values()) <= cap:
        p = min(1.0, n_near / float(len(pool)))
        return {i: p for i in pool}
    rng = np.random.default_rng([int(_hash01("replay", seed) * 2 ** 32), len(pool)])
    hits = dict.fromkeys(pool, 0)
    for _ in range(replays):
        for i in _take([pool[j] for j in rng.permutation(len(pool))], n_near, tiles, cap):
            hits[i] += 1
    return {i: hits[i] / float(replays) for i in pool}


def pick_batch(rows, n=90, random_frac=0.18, top_frac=0.37, second_frac=0.12, score_key="score",
               committee=None, tile_m=5000.0, per_tile_cap=3, seed=0, reviewed=()):
    """Pick a review batch from candidate rows (dicts with find_id, x, y and a score).
    committee: optional list of score arrays (one per model) - the near-the-cut slice then takes
    the cards they disagree on most. reviewed: find_ids already marked (left out of the top and
    near slices; the random control keeps them, so it stays the same set). per_tile_cap limits
    the near-the-cut cards from one tile (the random and top cards do not count against it).
    Returns the picked rows (copies) with 'stratum' (random / top / near), 'rank' (by score, 1 =
    highest), 'incl_prob' (the card's chance of being in a batch drawn this way: the random
    control's n_rand / N, plus, for a card the near slice could take, (1 - n_rand / N) times its
    chance of being taken given the random control drawn, with the tile cap counted; 1 for the
    top slice and for a committee's deterministic picks) and 'second' ('yes' for a second
    reviewer), shuffled, numbered 'n' from 1."""
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
    is_done = [r.get("find_id") in done for r in rows]
    n_rand = int(round(n * random_frac))
    n_top = int(round(n * top_frac))
    n_near = n - n_rand - n_top
    # random control: the n_rand smallest hashes of the id (stable across score versions)
    h = np.array([_hash01(r.get("find_id"), seed) for r in rows])
    rand_idx = [int(i) for i in np.argsort(h)[:n_rand]]
    picked = {i: "random" for i in rand_idx}
    # top slice: highest-scored not reviewed / not picked
    top_idx = []
    for i in order:
        if len(top_idx) >= n_top:
            break
        if int(i) in picked or is_done[i]:
            continue
        top_idx.append(int(i))
        picked[int(i)] = "top"
    top_max = int(rank[top_idx].max()) if top_idx else 0
    k = max(n_top, 1)
    lo_rank = max(top_max, k) + 1
    hi_rank = 3 * lo_rank
    in_band = [lo_rank <= rank[i] <= hi_rank and not is_done[i] for i in range(N)]
    band = [int(i) for i in order if in_band[i] and int(i) not in picked]
    rand_band = [i for i in rand_idx if in_band[i]]
    tiles = {i: _tile(rows[i], tile_m) for i in band + rand_band}
    if committee is not None and len(committee) >= 2:
        cr = np.vstack([np.argsort(np.argsort(-np.asarray(c, float))) for c in committee])
        spread = cr.std(axis=0)

        def walk(pool):
            return _take(sorted(pool, key=lambda i: (-spread[i], rank[i])), n_near, tiles, per_tile_cap)
        near_idx = walk(band)
        # deterministic given the random control: a card is taken or it is not. A random-control
        # card in the band would have been taken had the hash left it out?
        chance = {i: 1.0 if i in set(near_idx) else 0.0 for i in band}
        for i in rand_band:
            chance[i] = 1.0 if i in set(walk(band + [i])) else 0.0
    else:
        near_idx = _take([band[j] for j in rng.permutation(len(band))], n_near, tiles, per_tile_cap)
        chance = _near_chance(band, n_near, tiles, per_tile_cap, seed)
        for i in rand_band:
            chance[i] = _near_chance(band + [i], n_near, tiles, per_tile_cap, seed)[i]
    for i in near_idx:
        picked[i] = "near"
    p_rand = n_rand / float(N)
    out = []
    for i, st in picked.items():
        r = dict(rows[i])
        if rank[i] <= top_max and not is_done[i]:
            pi = 1.0
        elif i in chance:
            pi = p_rand + (1.0 - p_rand) * chance[i]
        else:
            pi = p_rand
        r.update({"stratum": st, "rank": int(rank[i]), "incl_prob": round(max(pi, 1e-6), 6)})
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
