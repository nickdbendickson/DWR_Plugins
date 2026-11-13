"""A score for each fill, learned from the fills that are already on a dam list (Finder 0.1).

Pure numpy. Run the fill finder over areas with known dams; the fills it matched to a dam list
(status 'listed' on a layer whose name holds one of `positive_words`) are the examples of what a
dam looks like in the LiDAR; every other fill is unlabelled - mostly road fills and natural
humps, some real dams no list has. A logistic regression (L2, Newton steps) of listed against the
rest ranks the unlabelled fills by how much they look like listed dams; under the usual
assumption that the listed dams are a random sample of all dams (Elkan & Noto 2008), that ranking
is the ranking by the chance of being a dam. The score is that ranking scaled 0-1: a way to look
at the likeliest new candidates first, not a probability and not a determination.

The model is a small JSON file (features, scaling, weights, what it was trained on), so it can be
kept with the project and used on the next county.

FINDER 0.5 (version 2 models; version 1 models still score):
  * evidence features from the 0.5 engine (outlet, original ground, pool shape, contributing
    area, terrain) - FEATURES_05; a version 1 model keeps its 11 features.
  * CLEAN POSITIVES (multiple-instance): a listed dam's 300 ft buffer often holds several fills
    (the dam, its spillway saddle, a road fill below it). Only the most dam-like fill in each
    listed dam's bag is the positive - first the nearest to the listed point, then (bag_iters
    rounds) the highest-scored; the others are left out, not counted against.
  * TRUSTED LABELS: reviewers' marks (yes / no / unsure) enter with their own weight - an
    'unsure' as both, weight q and 1 - q (cross-entropy with a soft target); background
    (unlabelled fills) can be limited to tiles whose inventory is trusted.
  * TERRAIN-AWARE: hinge (piecewise-linear) terms at the training quartiles of the continuous
    features, and each feature copied once per terrain group (flat / rolling / cut, from the
    relief round the fill: shared + group weights, Daume 2007), so a weight can differ between
    pothole prairie and badlands where the data say so and is shared where they do not.
  * OUTSIDE TRAINING CONDITIONS: score_detail() says when a fill's features fall outside what the
    model was trained on, or its terrain group had few listed dams.
  * BOOSTED TREES (learner='trees', version 3): the bagged positive-unlabelled boosted shallow
    trees of core/challenger.py on the same features and clean positives, in the same model
    file. Adopted for Finder 0.5 because it beat the logistic in 6 of the 7 held-out regions on
    the statewide tiles (the rule set beforehand: at least 5 of 7).
"""
import json
import math

import numpy as np

from . import challenger as ch

FEATURES = (
    ("log_height", lambda r: math.log1p(max(0.0, _f(r, "height_ft")))),
    ("log_crest_height", lambda r: math.log1p(max(0.0, _f(r, "crest_height_ft")))),
    ("net_head", lambda r: max(-5.0, min(20.0, _f(r, "head_ft") - _f(r, "valley_drop_ft")))),
    ("log_storage", lambda r: math.log1p(max(0.0, _f(r, "storage_af")))),
    ("log_area", lambda r: math.log1p(max(0.0, _f(r, "area_ac")))),
    ("log_length", lambda r: math.log1p(max(0.0, _f(r, "length_ft")))),
    ("pool", lambda r: 1.0 if r.get("pool") == "yes" else 0.0),
    ("on_road", lambda r: 1.0 if r.get("on_road") == "yes" else 0.0),
    ("open_pool", lambda r: 1.0 if r.get("open_pool") == "yes" else 0.0),
    ("spill_beside", lambda r: 1.0 if str(r.get("spillway") or "").startswith("beside") else 0.0),
    # length of the raised fill (Finder 0.3): farm dams are short, highways / canal banks run on
    ("log_ridge_len", lambda r: math.log1p(max(0.0, _f(r, "ridge_len_ft")))),
)
_FN = dict(FEATURES)
BASE_FEATURES = tuple(n for n, _f in FEATURES)


def _fv(r, k, default):
    """A float field, or `default` when missing / not a number (not measured)."""
    try:
        v = float(r.get(k))
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


# Finder 0.5 evidence (core/embankments.py steps 7-10). Not measured -> a neutral value.
FEATURES_05 = (
    ("ag_head", lambda r: max(-5.0, min(20.0, _fv(r, "ag_head_ft", _fv(r, "height_ft", 0.0))))),
    ("below_frac", lambda r: max(0.0, min(1.0, _fv(r, "below_frac", 0.0)))),
    ("spill_on_fill", lambda r: 1.0 if r.get("spill_on_fill") == "yes" else 0.0),
    ("log_outlet", lambda r: math.log1p(max(0.0, _fv(r, "outlet_dist_ft", _fv(r, "spill_dist_ft", 500.0))))),
    ("log_drain", lambda r: math.log1p(max(0.0, _fv(r, "drain_ac", 0.0)))),
    ("log_drain_ratio", lambda r: math.log1p(max(0.0, _fv(r, "drain_ratio", 0.0)))),
    ("rect", lambda r: max(0.0, min(1.0, _fv(r, "rect", 0.5)))),
    ("log_elong", lambda r: math.log(max(1.0, min(20.0, _fv(r, "elong", 1.5))))),
    ("wall_slope", lambda r: max(0.0, min(2.0, _fv(r, "wall_slope", 0.1)))),
    ("mounds", lambda r: max(0.0, min(5.0, _fv(r, "mounds", 0.0)))),
    ("log_top_w", lambda r: math.log1p(max(0.0, _fv(r, "top_w_ft", 10.0)))),
    ("log_side_sum", lambda r: math.log1p(max(0.0, min(200.0, _fv(r, "side_sum", 5.0))))),
    ("fill_road", lambda r: 1.0 if r.get("fill_road") == "yes" else 0.0),
    ("same_pool", lambda r: 1.0 if _fv(r, "same_pool", 0.0) > 0 else 0.0),
    ("log_relief", lambda r: math.log1p(max(0.0, _fv(r, "relief_ft", 0.0)))),
    ("slope_pct", lambda r: max(0.0, min(60.0, _fv(r, "slope_pct", 0.0)))),
)
_FN.update(dict(FEATURES_05))
ALL_FEATURES = BASE_FEATURES + tuple(n for n, _f in FEATURES_05)
# hinge terms go on these (continuous, enough spread); terrain copies on every feature
SPLINE_FEATURES = ("log_height", "net_head", "log_storage", "log_area", "log_ridge_len", "ag_head",
                   "below_frac", "log_outlet", "log_drain_ratio")
# terrain groups from the relief (95th - 5th percentile ground within 600 m of the crest, ft)
TERRAIN_EDGES_FT = (40.0, 120.0)
TERRAIN_NAMES = ("flat", "rolling", "cut")


def terrain_group(r, edges=TERRAIN_EDGES_FT):
    """'flat' / 'rolling' / 'cut' from the relief round the fill ('' when not measured)."""
    v = _fv(r, "relief_ft", float("nan"))
    if not math.isfinite(v):
        return ""
    k = sum(1 for e in edges if v >= e)
    return TERRAIN_NAMES[min(k, len(TERRAIN_NAMES) - 1)]


def _f(r, k):
    try:
        v = float(r.get(k))
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def matrix(rows, names=None):
    names = names or [n for n, _f in FEATURES]
    return np.array([[_FN[n](r) for n in names] for r in rows], float).reshape(len(rows), len(names))


# the dam list's own status of a listed fill's structure (known_state): only these are a dam on
# the ground at the point ('' = the layer has no status, e.g. the NID)
POSITIVE_STATES = ("", "intact")


def off_ground(r, positive_words=("dam",)):
    """True for a fill listed on a dam layer whose structure the list itself says is not there as
    a dam (the DWR list: Breached, Not Built, Removed, Submerged, Location Inconclusive ...):
    neither a dam nor background - left out of training and of every check."""
    words = [w.lower() for w in positive_words]
    return (r.get("status") == "listed" and any(w in str(r.get("known_layer") or "").lower() for w in words)
            and str(r.get("known_state") or "").strip().lower() not in POSITIVE_STATES)


def labels(rows, positive_words=("dam",)):
    """1 for a fill listed on a layer whose name holds one of the words (and, when the list gives
    a status, an intact one), else 0. Drop off_ground rows before using these as truth."""
    words = [w.lower() for w in positive_words]
    return np.array([1.0 if r.get("status") == "listed"
                     and any(w in str(r.get("known_layer") or "").lower() for w in words)
                     and not off_ground(r, positive_words) else 0.0
                     for r in rows])


def fit(xs, y, sw=None, l2=1.0, iters=50):
    """Weights of an L2 logistic regression (Newton) on a design matrix xs (first column the
    intercept, not penalised), targets y in [0, 1] (soft targets allowed), sample weights sw."""
    y = np.asarray(y, float)
    sw = np.ones(len(y)) if sw is None else np.asarray(sw, float)
    w = np.zeros(xs.shape[1])
    reg = np.eye(xs.shape[1]) * l2
    reg[0, 0] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(xs @ w, -30, 30)))
        g = xs.T @ (sw * (p - y)) + reg @ w
        hmat = (xs * (sw * p * (1 - p))[:, None]).T @ xs + reg
        step = np.linalg.solve(hmat, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def _design(x, names, basis, rows=None):
    """Raw feature matrix -> model design columns (names of the columns too): the features, their
    hinge terms max(0, x - knot), and (with groups) a copy of every column per terrain group."""
    cols, cn = [x], list(names)
    knots = (basis or {}).get("knots") or {}
    for n, ks in knots.items():
        j = names.index(n)
        for k in ks:
            cols.append(np.maximum(0.0, x[:, j] - k)[:, None])
            cn.append("{}>{:.3g}".format(n, k))
    base = np.hstack(cols)
    groups = (basis or {}).get("groups")
    if not groups:
        return base, cn
    gnames = groups["names"]
    g = np.array([terrain_group(r, groups["edges"]) for r in rows]) if rows is not None else np.array([""] * len(x))
    out, on = [base], list(cn)
    for gn in gnames:
        m = (g == gn).astype(float)[:, None]
        out.append(base * m)
        on += ["{}@{}".format(c, gn) for c in cn]
    return np.hstack(out), on


def _without_marked(rows, background, extra, positive_words=("dam",), bags=False):
    """rows (and background) less every row a reviewer marked: the mark replaces the row's own
    label (listed or not) instead of joining it. Matched by identity or find_id. With bags, a
    'yes' or 'unsure' on a bag's own positive (its listed fill nearest the listed point) stands
    for the listed dam: the bag's other listed fills are left out too (else the next of them
    would become a second positive for the same dam); a 'no' on it leaves the rest of the bag
    in, so the next fill is promoted; a mark on any other fill of the bag replaces that fill
    alone."""
    if not extra:
        return list(rows), background
    ids = {id(r) for r, _t, _w in extra}
    fids = {r.get("find_id") for r, _t, _w in extra if r.get("find_id") not in (None, "")}
    pos_ids = {id(r) for r, t, _w in extra if float(t) > 0.0}
    pos_f = {r.get("find_id") for r, t, _w in extra if float(t) > 0.0 and r.get("find_id") not in (None, "")}

    def marked(r):
        return id(r) in ids or (r.get("find_id") not in (None, "") and r.get("find_id") in fids)

    def bag(r):
        return (r.get("known_layer"), r.get("known_id"))
    y = labels(rows, positive_words)
    held = set()
    if bags:
        # the bag's own positive: its listed fill nearest the listed point (as the bags pick it)
        near = {}
        for r, yy in zip(rows, y):
            if yy > 0 and r.get("known_id") not in (None, ""):
                b = bag(r)
                if b not in near or _fv(r, "known_ft", 1e9) < _fv(near[b], "known_ft", 1e9):
                    near[b] = r
        held = {b for b, r in near.items()
                if id(r) in pos_ids or (r.get("find_id") not in (None, "") and r.get("find_id") in pos_f)}
    keep = [not (marked(r) or (yy > 0 and bag(r) in held)) for r, yy in zip(rows, y)]
    rows = [r for r, k in zip(rows, keep) if k]
    if background is not None:
        background = [b for b, k in zip(background, keep) if k]
    return rows, background


def train(rows, positive_words=("dam",), l2=1.0, iters=50, note="", features=None, bags=False,
          bag_iters=2, background=None, extra=None, splines=False, groups=False, group_l2=None,
          learner="logistic", n_bags=10, seed=0):
    """A model (dict) from embankment rows. Needs at least 3 positives and 3 others.
    features: names (default the 11 of version 1; ALL_FEATURES for the 0.5 evidence too).
    bags: clean positives - one fill per listed dam (known_id): nearest first, then the
      highest-scored, bag_iters rounds; the rest of each bag left out.
    background: bool per row - unlabelled fills used as background (e.g. only tiles whose
      inventory is trusted); others left out. Listed (positive) rows are always used.
    extra: trusted labels [(row, target 0..1, weight)] - reviewers' marks; an 'unsure' with q
      becomes (row, 1, w q) and (row, 0, w (1 - q)).
    splines / groups: the terrain-aware model (version 2). group_l2: a heavier penalty on the
      per-group copies (default 3 x l2) so they move only where the data say so.
      A marked row's own entry in rows is dropped: the mark replaces its label.
    A fill listed on a dam whose list status says it is not there (off_ground: Breached, Not Built,
      Removed ...) is left out altogether: model['not_on_ground'] counts them.
    learner: 'logistic' or 'trees' (bagged PU boosted trees, version 3: the clean positives are
      the fill nearest each listed point; marks are in every bag at their weight, an unsure one
      as both; splines / groups do not apply)."""
    keep = [not off_ground(r, positive_words) for r in rows]
    n_off = len(keep) - sum(keep)
    rows = [r for r, k in zip(rows, keep) if k]
    if background is not None:
        background = [b for b, k in zip(background, keep) if k]
    rows, background = _without_marked(rows, background, extra, positive_words, bags)
    model = _train(rows, positive_words, l2, iters, note, features, bags, bag_iters, background, extra, splines,
                   groups, group_l2, learner, n_bags, seed)
    model["not_on_ground"] = n_off               # listed dams the list says are not there: left out
    return model


def _train(rows, positive_words, l2, iters, note, features, bags, bag_iters, background, extra, splines, groups,
           group_l2, learner, n_bags, seed):
    if learner == "trees":
        return _train_trees(rows, positive_words, note, features, bags, background, extra, n_bags, seed)
    if learner != "logistic":
        raise ValueError("learner {} is not 'logistic' or 'trees'".format(learner))
    names = list(features or BASE_FEATURES)
    x = matrix(rows, names)
    y = labels(rows, positive_words)
    sw = np.ones(len(y))
    if background is not None:
        sw[(y == 0) & ~np.asarray(background, bool)] = 0.0
    bag_of = None
    if bags:
        bag_of = {}
        for i, r in enumerate(rows):
            if y[i] > 0:                            # a listed fill with no id is a bag of its own
                k = (r.get("known_layer"), r.get("known_id")) if r.get("known_id") not in (None, "") else ("", i)
                bag_of.setdefault(k, []).append(i)
        for ids in bag_of.values():                 # nearest to the listed point first
            best = min(ids, key=lambda i: _fv(rows[i], "known_ft", 1e9))
            for i in ids:
                sw[i] = 1.0 if i == best else 0.0
    ex_rows, ex_y, ex_w = [], [], []
    for r, t, wt in (extra or []):
        t = float(t)
        if 0.0 < t < 1.0:
            ex_rows += [r, r]
            ex_y += [1.0, 0.0]
            ex_w += [wt * t, wt * (1.0 - t)]
        else:
            ex_rows.append(r)
            ex_y.append(t)
            ex_w.append(wt)
    all_rows = list(rows) + ex_rows
    xa = np.vstack([x, matrix(ex_rows, names)]) if ex_rows else x
    ya = np.concatenate([y, np.asarray(ex_y, float)]) if ex_rows else y
    wa = np.concatenate([sw, np.asarray(ex_w, float)]) if ex_rows else sw
    npos = int(((ya > 0) & (wa > 0)).sum())
    nneg = int(((ya == 0) & (wa > 0)).sum())
    if npos < 3 or nneg < 3:
        raise ValueError("need at least 3 listed dams and 3 other fills to learn from "
                         "({} listed, {} other)".format(npos, nneg))
    used = wa > 0
    basis = {}
    if splines:
        basis["knots"] = {n: sorted(set(float(round(q, 4)) for q in np.quantile(xa[used, names.index(n)], [0.25, 0.5, 0.75])))
                          for n in SPLINE_FEATURES if n in names}
    if groups:
        basis["groups"] = {"by": "relief_ft", "edges": list(TERRAIN_EDGES_FT), "names": list(TERRAIN_NAMES)}
    d, cn = _design(xa, names, basis, all_rows)
    mu = d[used].mean(axis=0)
    sd = d[used].std(axis=0)
    sd[sd < 1e-9] = 1.0
    xs = np.hstack([np.ones((len(ya), 1)), (d - mu) / sd])
    pen = np.full(xs.shape[1], float(l2))
    pen[0] = 0.0
    if groups:
        nb = len(cn) // (1 + len(TERRAIN_NAMES))
        pen[1 + nb:] = float(group_l2 if group_l2 is not None else 3.0 * l2)

    def solve(wv):
        w = np.zeros(xs.shape[1])
        for _ in range(iters):
            p = 1.0 / (1.0 + np.exp(-np.clip(xs @ w, -30, 30)))
            g = xs.T @ (wv * (p - ya)) + pen * w
            hmat = (xs * (wv * p * (1 - p))[:, None]).T @ xs + np.diag(pen)
            step = np.linalg.solve(hmat, g)
            w -= step
            if np.abs(step).max() < 1e-8:
                break
        return w

    w = solve(wa)
    if bags:
        for _ in range(int(bag_iters)):             # re-pick the most dam-like fill in each bag
            raw = xs @ w
            for ids in bag_of.values():
                best = max(ids, key=lambda i: raw[i])
                for i in ids:
                    wa[i] = 1.0 if i == best else 0.0
            w = solve(wa)
    raw = xs[:len(rows)] @ w
    lo = np.quantile(xa[used], 0.005, axis=0)
    hi = np.quantile(xa[used], 0.995, axis=0)
    gpos = {}
    for r, yy, ww in zip(all_rows, ya, wa):
        if yy > 0 and ww > 0:
            g = terrain_group(r)
            gpos[g] = gpos.get(g, 0) + 1
    model = {"kind": "finder-fill-score", "version": 2 if (basis or features) else 1, "features": names,
             "mean": mu.tolist(), "std": sd.tolist(), "weights": w.tolist(),
             "positive_words": list(positive_words), "n_listed": npos, "n_other": nneg,
             "raw_min": float(raw.min()), "raw_max": float(raw.max()), "note": note,
             "train_lo": lo.tolist(), "train_hi": hi.tolist(), "group_positives": gpos}
    if basis:
        model["basis"] = basis
        model["columns"] = cn
    if bags:
        model["clean_positives"] = {"bags": len(bag_of), "rounds": int(bag_iters)}
    if ex_rows:
        model["trusted_labels"] = len(extra)
    return model


def _train_trees(rows, positive_words, note, features, bags, background, extra, n_bags, seed):
    names = list(features or ALL_FEATURES)
    y = labels(rows, positive_words)
    keep = np.ones(len(y), bool)
    if background is not None:
        keep[(y == 0) & ~np.asarray(background, bool)] = False
    nbags = None
    if bags:
        best = {}
        for i, r in enumerate(rows):
            if y[i] > 0:
                k = (r.get("known_layer"), r.get("known_id")) if r.get("known_id") not in (None, "") else ("", i)
                if k not in best or _fv(r, "known_ft", 1e9) < _fv(rows[best[k]], "known_ft", 1e9):
                    best[k] = i
        nbags = len(best)
        pos = set(best.values())
        keep &= ~((y > 0) & ~np.isin(np.arange(len(y)), list(pos)))
    use = [rows[i] for i in np.flatnonzero(keep)]
    yu = y[keep]
    wu = np.ones(len(yu))
    tr = np.zeros(len(yu), bool)
    ex = []
    for r, t, wt in (extra or []):
        t = float(t)
        if 0.0 < t < 1.0:
            ex += [(r, 1.0, wt * t), (r, 0.0, wt * (1.0 - t))]
        else:
            ex.append((r, t, wt))
    if ex:
        use = use + [r for r, _t, _w in ex]
        yu = np.concatenate([yu, [t for _r, t, _w in ex]])
        wu = np.concatenate([wu, [w for _r, _t, w in ex]])
        tr = np.concatenate([tr, np.ones(len(ex), bool)])
    npos, nneg = int(((yu > 0) & (wu > 0)).sum()), int(((yu == 0) & (wu > 0)).sum())
    if npos < 3 or nneg < 3:
        raise ValueError("need at least 3 listed dams and 3 other fills to learn from "
                         "({} listed, {} other)".format(npos, nneg))
    x = matrix(use, names)
    gbm = ch.bagging_pu(x, yu, n_bags=n_bags, seed=seed, sw=wu, trusted=tr)
    for m in gbm["models"]:                      # a smaller file: 4 decimals are plenty
        for t in m["trees"]:
            _round_tree(t)
    raw = ch.predict(gbm, x)
    gpos = {}
    for r, yy in zip(use, yu):
        if yy > 0:
            g = terrain_group(r)
            gpos[g] = gpos.get(g, 0) + 1
    model = {"kind": "finder-fill-score", "version": 3, "learner": "pu-gbm", "features": names, "gbm": gbm,
             "positive_words": list(positive_words), "n_listed": npos, "n_other": nneg,
             "raw_min": float(raw.min()), "raw_max": float(raw.max()), "note": note,
             "train_lo": np.quantile(x, 0.005, axis=0).tolist(), "train_hi": np.quantile(x, 0.995, axis=0).tolist(),
             "group_positives": gpos}
    if bags:
        model["clean_positives"] = {"bags": nbags, "rounds": 0}
    if extra:
        model["trusted_labels"] = len(extra)
    return model


def _round_tree(t):
    if "v" in t:
        t["v"] = round(t["v"], 4)
        return
    _round_tree(t["l"])                          # split points kept exact (a row on one stays put)
    _round_tree(t["r"])


def _raw(model, rows):
    names = model.get("features") or []
    if not names or any(n not in _FN for n in names):      # an older model still scores
        raise ValueError("the score model was made for other features: learn it again")
    x = matrix(rows, names)
    if model.get("learner") == "pu-gbm":
        return ch.predict(model["gbm"], x), x
    d, _cn = _design(x, names, model.get("basis"), rows)
    xs = np.hstack([np.ones((len(rows), 1)), (d - np.array(model["mean"])) / np.array(model["std"])])
    return xs @ np.array(model["weights"]), x


def score(model, rows):
    """0-1 scores of rows (embankment rows) under a model."""
    if not rows:
        return np.zeros(0)
    raw, _x = _raw(model, rows)
    return 1.0 / (1.0 + np.exp(-np.clip(raw, -30, 30)))


def score_detail(model, rows, min_group_positives=30):
    """[(score, outside training conditions: '' or why)] - a fill whose features fall outside the
    middle 99 % of what the model was trained on, or whose terrain group had fewer than
    min_group_positives listed dams, is scored by extrapolation: say so beside its score."""
    if not rows:
        return []
    raw, x = _raw(model, rows)
    s = 1.0 / (1.0 + np.exp(-np.clip(raw, -30, 30)))
    names = model["features"]
    lo = np.array(model.get("train_lo") or [-np.inf] * len(names))
    hi = np.array(model.get("train_hi") or [np.inf] * len(names))
    gall = model.get("group_positives") or {}
    unk = int(gall.get("", 0))                      # listed dams whose terrain was not measured
    gp = {k: v for k, v in gall.items() if k}       # measured terrain only
    out = []
    for i, r in enumerate(rows):
        why = [n for j, n in enumerate(names) if x[i, j] < lo[j] or x[i, j] > hi[j]]
        msg = "outside the training range: " + ", ".join(why) if why else ""
        g = terrain_group(r)
        if gp and g and gp.get(g, 0) + unk < min_group_positives:   # the unmeasured ones may be in g
            msg = (msg + "; " if msg else "") + "{} terrain: only {} listed dam(s) to learn from".format(
                g, gp.get(g, 0) + unk)
        out.append((float(s[i]), msg))
    return out


def save(model, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(model, fh, indent=1)


def load(path):
    with open(path, encoding="utf-8") as fh:
        m = json.load(fh)
    if m.get("kind") != "finder-fill-score":
        raise ValueError("{} is not a Finder score model".format(path))
    return m


def ranked_auc(scores, y):
    """Area under the ROC curve of scores against 0 / 1 labels (rank form; ties count half)."""
    s = np.asarray(scores, float)
    y = np.asarray(y, float)
    pos, neg = s[y > 0], s[y == 0]
    if not len(pos) or not len(neg):
        return float("nan")
    gt = (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
    return float(gt) / (len(pos) * len(neg))
