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
"""
import json
import math

import numpy as np

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
)


def _f(r, k):
    try:
        v = float(r.get(k))
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def matrix(rows):
    return np.array([[fn(r) for _n, fn in FEATURES] for r in rows], float).reshape(len(rows), len(FEATURES))


def labels(rows, positive_words=("dam",)):
    """1 for a fill listed on a layer whose name holds one of the words, else 0."""
    words = [w.lower() for w in positive_words]
    return np.array([1.0 if r.get("status") == "listed"
                     and any(w in str(r.get("known_layer") or "").lower() for w in words) else 0.0
                     for r in rows])


def train(rows, positive_words=("dam",), l2=1.0, iters=50, note=""):
    """A model (dict) from embankment rows. Needs at least 3 positives and 3 others."""
    x = matrix(rows)
    y = labels(rows, positive_words)
    npos, nneg = int(y.sum()), int(len(y) - y.sum())
    if npos < 3 or nneg < 3:
        raise ValueError("need at least 3 listed dams and 3 other fills to learn from "
                         "({} listed, {} other)".format(npos, nneg))
    mu = x.mean(axis=0)
    sd = x.std(axis=0)
    sd[sd < 1e-9] = 1.0
    xs = np.hstack([np.ones((len(y), 1)), (x - mu) / sd])
    w = np.zeros(xs.shape[1])
    reg = np.eye(xs.shape[1]) * l2
    reg[0, 0] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(xs @ w, -30, 30)))
        g = xs.T @ (p - y) + reg @ w
        hmat = (xs * (p * (1 - p))[:, None]).T @ xs + reg
        step = np.linalg.solve(hmat, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    raw = xs @ w
    return {"kind": "finder-fill-score", "version": 1, "features": [n for n, _f in FEATURES],
            "mean": mu.tolist(), "std": sd.tolist(), "weights": w.tolist(),
            "positive_words": list(positive_words), "n_listed": npos, "n_other": nneg,
            "raw_min": float(raw.min()), "raw_max": float(raw.max()), "note": note}


def score(model, rows):
    """0-1 scores of rows (embankment rows) under a model."""
    if not rows:
        return np.zeros(0)
    if model.get("features") != [n for n, _f in FEATURES]:
        raise ValueError("the score model was made for other features: learn it again")
    x = matrix(rows)
    xs = np.hstack([np.ones((len(rows), 1)), (x - np.array(model["mean"])) / np.array(model["std"])])
    return 1.0 / (1.0 + np.exp(-np.clip(xs @ np.array(model["weights"]), -30, 30)))


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
