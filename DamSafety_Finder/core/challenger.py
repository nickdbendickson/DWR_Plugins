"""A challenger to the logistic fill score: bagged PU gradient-boosted shallow trees (Finder 0.5).

Pure numpy. The research behind Finder 0.5 found the score's weak point is missing evidence, not
the learner, but asked for one honest challenger before ruling learners out: shallow boosted
trees (they find interactions and thresholds a logistic cannot), trained the positive-unlabelled
way by bagging (Mordelet & Vert 2014): each bag is every listed dam against a bootstrap sample of
the unlabelled fills, and the score is the average over bags. It is adopted only if it beats the
logistic in at least 5 of the 7 held-out regions (leave-one-region-out) - see evaluate.py.

Trees are depth-limited regression trees on the logistic loss's gradient (Newton leaf values),
split on quantile bins of each feature.
"""
import json

import numpy as np


def _bins(x, n_bins):
    """Per-feature split thresholds: the distinct inner quantiles of each column."""
    out = []
    qs = np.linspace(0, 1, n_bins + 1)[1:-1]
    for j in range(x.shape[1]):
        out.append(np.unique(np.quantile(x[:, j], qs)))
    return out


def _grow(x, g, h, idx, depth, edges, min_leaf, l2):
    """A regression tree node (dict) fitted to gradient g / hessian h of rows idx."""
    G, H = g[idx].sum(), h[idx].sum()
    leaf = {"v": float(-G / (H + l2))}
    if depth == 0 or len(idx) < 2 * min_leaf:
        return leaf
    best = (0.0, None, None)
    base = G * G / (H + l2)
    for j, th in enumerate(edges):
        if not len(th):
            continue
        xj = x[idx, j]
        k = np.searchsorted(th, xj, side="left")          # bin of each row
        gs = np.bincount(k, g[idx], len(th) + 1).cumsum()[:-1]
        hs = np.bincount(k, h[idx], len(th) + 1).cumsum()[:-1]
        ns = np.bincount(k, None, len(th) + 1).cumsum()[:-1]
        ok = (ns >= min_leaf) & (len(idx) - ns >= min_leaf)
        if not ok.any():
            continue
        gain = gs * gs / (hs + l2) + (G - gs) ** 2 / (H - hs + l2) - base
        gain[~ok] = -np.inf
        t = int(np.argmax(gain))
        if gain[t] > best[0]:
            best = (float(gain[t]), j, float(th[t]))
    if best[1] is None:
        return leaf
    j, th = best[1], best[2]
    left = idx[x[idx, j] <= th]
    right = idx[x[idx, j] > th]
    return {"f": j, "t": th, "l": _grow(x, g, h, left, depth - 1, edges, min_leaf, l2),
            "r": _grow(x, g, h, right, depth - 1, edges, min_leaf, l2)}


def _tree_predict(node, x):
    out = np.empty(len(x))
    stack = [(node, np.arange(len(x)))]
    while stack:
        nd, idx = stack.pop()
        if "v" in nd:
            out[idx] = nd["v"]
            continue
        m = x[idx, nd["f"]] <= nd["t"]
        stack.append((nd["l"], idx[m]))
        stack.append((nd["r"], idx[~m]))
    return out


def fit_gbm(x, y, sw=None, n_trees=150, lr=0.1, depth=2, n_bins=32, min_leaf=20, l2=1.0,
            subsample=0.8, seed=0):
    """Gradient-boosted trees for the logistic loss. {'base', 'lr', 'trees'}."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    sw = np.ones(len(y)) if sw is None else np.asarray(sw, float)
    rng = np.random.default_rng(seed)
    edges = _bins(x, n_bins)
    p0 = min(max(float((sw * y).sum() / sw.sum()), 1e-4), 1 - 1e-4)
    base = float(np.log(p0 / (1 - p0)))
    f = np.full(len(y), base)
    trees = []
    for _ in range(int(n_trees)):
        p = 1.0 / (1.0 + np.exp(-f))
        g = sw * (p - y)
        h = sw * p * (1 - p)
        idx = np.flatnonzero(sw > 0)
        if subsample < 1.0:
            idx = idx[rng.random(len(idx)) < subsample]
        t = _grow(x, g, h, idx, depth, edges, min_leaf, l2)
        f += lr * _tree_predict(t, x)
        trees.append(t)
    return {"base": base, "lr": float(lr), "trees": trees}


def predict_gbm(model, x):
    x = np.asarray(x, float)
    f = np.full(len(x), model["base"])
    for t in model["trees"]:
        f += model["lr"] * _tree_predict(t, x)
    return f


def bagging_pu(x, y, n_bags=10, seed=0, sw=None, trusted=None, **kw):
    """Bagged PU: each bag = all positives + a bootstrap of the unlabelled rows as large as the
    positives x 5 (or all of them, when fewer). trusted: rows that are reviewers' labels (dam or
    not) - in every bag at their weight sw, never bootstrapped as unlabelled. With few rows a
    bag's trees still split: min_leaf shrinks to a tenth of the rows a tree sees (20 at most).
    {'kind': 'finder-pu-gbm', 'models': [...]}."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    sw = np.ones(len(y)) if sw is None else np.asarray(sw, float)
    tr = np.zeros(len(y), bool) if trusted is None else np.asarray(trusted, bool)
    rng = np.random.default_rng(seed)
    pos = np.flatnonzero((y > 0) & ~tr & (sw > 0))
    unl = np.flatnonzero((y == 0) & ~tr & (sw > 0))
    fix = np.flatnonzero(tr & (sw > 0))
    k = min(len(unl), 5 * len(pos))
    n_rows = int(kw.get("subsample", 0.8) * (len(pos) + k + len(fix)))
    if "min_leaf" not in kw:
        kw["min_leaf"] = max(2, min(20, n_rows // 10))
    models = []
    for b in range(int(n_bags)):
        pick = rng.choice(unl, k, replace=True) if k else np.zeros(0, int)
        idx = np.concatenate([pos, pick, fix]).astype(int)
        models.append(fit_gbm(x[idx], y[idx], sw=sw[idx], seed=seed + b, **kw))
    return {"kind": "finder-pu-gbm", "models": models}


def predict(model, x):
    """Mean raw score over the bags (a ranking, not a probability)."""
    return np.mean([predict_gbm(m, x) for m in model["models"]], axis=0)


def dumps(model):
    return json.dumps(model)
