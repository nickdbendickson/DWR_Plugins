"""How good is a ranking, with honest error bars (Finder 0.5).

Pure numpy, no QGIS. The Finder's review rounds give small label sets (a 90-card county sheet
with 4 to 16 real dams), and at that size a ranking's numbers swing a lot from one sheet to the
next. Every number here comes with its 95 % interval, and two rankings of the same cards are
compared PAIRED (the same cards, each scored both ways), which is far tighter than comparing two
separate intervals:

  wilson(k, n)                 a share k / n with its Wilson interval (good at small n and at 0)
  precision_at(s, y, k)        real dams among the top k by score, with its interval
  auc / auc_ci(s, y)           ranked AUC (ties half) and its Hanley-McNeil interval
  delong(s1, s2, y)            paired AUC difference of two scores on the same cards (DeLong)
  paired_bootstrap(...)        paired difference of any metric (precision at k, AUC), resampling
                               dams and non-dams separately, optionally within groups (counties)
  listed_recall(...)           how many already-listed dams the candidate generator finds at all
  ht_share(...)                the share of real dams in a whole list, from a reviewed sample whose
                               cards each had a known chance of being picked (Horvitz-Thompson)
  platt(s, y)                  a two-number calibration (Platt): score -> chance it is a dam
  group_folds(groups)          leave-one-group-out folds (region, county, terrain)

Labels: 1 = a real dam, 0 = not, NaN = not reviewed / unsure (left out).
"""
import math

import numpy as np

Z95 = 1.959963984540054


def _clean(scores, labels):
    s = np.asarray(scores, float).ravel()
    y = np.asarray(labels, float).ravel()
    if s.shape != y.shape:
        raise ValueError("scores and labels differ in length")
    ok = np.isfinite(s) & np.isfinite(y)
    return s[ok], (y[ok] > 0).astype(float)


def wilson(k, n, z=Z95):
    """(share, low, high) of k successes in n trials: the Wilson score interval. (nan, 0, 1) for
    n = 0."""
    if n <= 0:
        return float("nan"), 0.0, 1.0
    p = k / float(n)
    d = 1.0 + z * z / n
    mid = (p + z * z / (2.0 * n)) / d
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / d
    return p, max(0.0, mid - half), min(1.0, mid + half)


def top_k(scores, k, seed=0):
    """Indices of the k highest scores; ties broken at random (seeded), not by input order, so a
    tied block at the cut does not favour whichever cards came first."""
    s = np.asarray(scores, float)
    tie = np.random.default_rng(seed).random(len(s))
    order = np.lexsort((tie, -s))
    return order[:max(0, min(int(k), len(s)))]


def precision_at(scores, labels, k, z=Z95, seed=0):
    """{'k', 'hits', 'p', 'lo', 'hi'}: real dams among the k highest-scored reviewed cards."""
    s, y = _clean(scores, labels)
    idx = top_k(s, k, seed)
    hits = int(y[idx].sum())
    p, lo, hi = wilson(hits, len(idx), z)
    return {"k": int(len(idx)), "hits": hits, "p": p, "lo": lo, "hi": hi}


def _midrank(x):
    order = np.argsort(x, kind="mergesort")
    xs = x[order]
    n = len(x)
    t = np.zeros(n)
    i = 0
    while i < n:
        j = i
        while j < n and xs[j] == xs[i]:
            j += 1
        t[i:j] = 0.5 * (i + j - 1) + 1.0
        i = j
    out = np.empty(n)
    out[order] = t
    return out


def auc(scores, labels):
    """Area under the ROC curve (Mann-Whitney; ties count half). NaN without both classes."""
    s, y = _clean(scores, labels)
    m, n = int(y.sum()), int(len(y) - y.sum())
    if not m or not n:
        return float("nan")
    r = _midrank(s)
    return float((r[y > 0].sum() - m * (m + 1) / 2.0) / (m * n))


def auc_ci(scores, labels, z=Z95):
    """(auc, low, high, se): the Hanley-McNeil (1982) standard error. With few dams it is wide -
    32 dams over three counties give about +/- 0.15 per county."""
    s, y = _clean(scores, labels)
    a = auc(s, y)
    m, n = int(y.sum()), int(len(y) - y.sum())
    if not math.isfinite(a):
        return a, float("nan"), float("nan"), float("nan")
    q1 = a / (2.0 - a)
    q2 = 2.0 * a * a / (1.0 + a)
    var = (a * (1 - a) + (m - 1) * (q1 - a * a) + (n - 1) * (q2 - a * a)) / (m * n)
    se = math.sqrt(max(var, 0.0))
    return a, max(0.0, a - z * se), min(1.0, a + z * se), se


def _norm_sf(x):
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def delong(s1, s2, labels, z=Z95):
    """Paired comparison of two scores on the same cards (DeLong, DeLong & Clarke-Pearson 1988,
    fast form of Sun & Xu 2014): {'auc1', 'auc2', 'diff' (2 - 1), 'se', 'lo', 'hi', 'p'
    (two-sided)}."""
    a = np.asarray(s1, float).ravel()
    b = np.asarray(s2, float).ravel()
    y = np.asarray(labels, float).ravel()
    ok = np.isfinite(a) & np.isfinite(b) & np.isfinite(y)
    a, b, y = a[ok], b[ok], y[ok] > 0
    m, n = int(y.sum()), int((~y).sum())
    if m < 2 or n < 2:
        return {"auc1": float("nan"), "auc2": float("nan"), "diff": float("nan"), "se": float("nan"),
                "lo": float("nan"), "hi": float("nan"), "p": float("nan")}
    v10, v01, au = [], [], []
    for s in (a, b):
        x, w = s[y], s[~y]
        tx, ty, tz = _midrank(x), _midrank(w), _midrank(np.concatenate([x, w]))
        au.append((tz[:m].sum() - m * (m + 1) / 2.0) / (m * n))
        v10.append((tz[:m] - tx) / n)
        v01.append(1.0 - (tz[m:] - ty) / m)
    sx = np.cov(np.vstack(v10)) / m
    sy = np.cov(np.vstack(v01)) / n
    cov = sx + sy
    var = cov[0, 0] + cov[1, 1] - 2.0 * cov[0, 1]
    d = au[1] - au[0]
    se = math.sqrt(max(var, 0.0))
    p = 1.0 if se == 0 and d == 0 else (0.0 if se == 0 else 2.0 * _norm_sf(abs(d) / se))
    return {"auc1": float(au[0]), "auc2": float(au[1]), "diff": float(d), "se": se,
            "lo": d - z * se, "hi": d + z * se, "p": float(p)}


def paired_bootstrap(s1, s2, labels, metric=None, n=2000, seed=0, groups=None, alpha=0.05):
    """Paired bootstrap of metric(s2) - metric(s1) on the same cards: dams and non-dams are
    resampled separately (so every draw has both), and within each group when `groups` (e.g. the
    county of each card) is given. `metric(scores, labels)` defaults to AUC; use
    lambda s, y: precision_at(s, y, 30)['p'] for precision at 30.
    {'diff', 'lo', 'hi', 'p_le0' (share of draws with no gain), 'n'}."""
    metric = metric or auc
    a = np.asarray(s1, float).ravel()
    b = np.asarray(s2, float).ravel()
    y = np.asarray(labels, float).ravel()
    ok = np.isfinite(a) & np.isfinite(b) & np.isfinite(y)
    g = np.asarray(groups if groups is not None else np.zeros(len(y)), dtype=object).ravel()[ok]
    a, b, y = a[ok], b[ok], (y[ok] > 0).astype(float)
    base = metric(b, y) - metric(a, y)
    cells = [np.flatnonzero((g == k) & (y == c)) for k in sorted(set(g.tolist()), key=str) for c in (0.0, 1.0)]
    cells = [c for c in cells if len(c)]
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(int(n)):
        idx = np.concatenate([rng.choice(c, len(c)) for c in cells])
        v = metric(b[idx], y[idx]) - metric(a[idx], y[idx])
        if math.isfinite(v):
            out.append(v)
    if not out:
        return {"diff": base, "lo": float("nan"), "hi": float("nan"), "p_le0": float("nan"), "n": 0}
    out = np.asarray(out)
    return {"diff": float(base), "lo": float(np.quantile(out, alpha / 2)),
            "hi": float(np.quantile(out, 1 - alpha / 2)), "p_le0": float((out <= 0).mean()),
            "n": int(len(out))}


def listed_recall(dams, cands, match_m, z=Z95):
    """How many listed dams the candidate generator finds AT ALL (a fill within match_m), before
    any score - the ceiling on what a ranking can show. `dams`, `cands`: [(x, y)] in metres.
    {'found', 'total', 'p', 'lo', 'hi'}."""
    found = 0
    c = np.asarray(cands, float).reshape(-1, 2)
    for x, y in dams:
        if len(c) and float(np.hypot(c[:, 0] - x, c[:, 1] - y).min()) <= match_m:
            found += 1
    p, lo, hi = wilson(found, len(dams), z)
    return {"found": found, "total": len(dams), "p": p, "lo": lo, "hi": hi}


def ht_share(labels, incl_prob, z=Z95):
    """Share of real dams in a whole candidate list, from a reviewed sample in which each card had
    a known chance incl_prob of being picked (a random control, a top-k slice taken with chance 1,
    a near-cut-off slice ...): the Horvitz-Thompson estimate. The cards that share one chance form
    a stratum (a top slice taken whole has no sampling error; a random sample of the rest has it
    all); each sampled stratum's interval sits on its Agresti-Coull share (k + z^2/2) / (n + z^2), so
    a random control with no dams in it still gives an honest upper bound instead of 0 +/- 0.
    {'share', 'lo', 'hi', 'n_list' (estimated list size), 'dams' (estimated real dams in the
    list)}."""
    y = np.asarray(labels, float).ravel()
    pi = np.asarray(incl_prob, float).ravel()
    ok = np.isfinite(y) & np.isfinite(pi) & (pi > 0)
    y, pi = (y[ok] > 0).astype(float), np.minimum(pi[ok], 1.0)
    if not len(y):
        return {"share": float("nan"), "lo": float("nan"), "hi": float("nan"), "n_list": 0.0, "dams": 0.0}
    w = 1.0 / pi
    n_hat, d_hat = float(w.sum()), float((w * y).sum())
    r = d_hat / n_hat
    var = 0.0
    centre = float(y[pi >= 1.0].sum())                 # the bounds sit on the Agresti-Coull shares
    for p in set(np.round(pi, 9).tolist()):
        if p >= 1.0:
            continue                                   # taken whole: no sampling error
        m = np.round(pi, 9) == p
        n_h = int(m.sum())
        big_n = n_h / p
        nt = n_h + z * z
        pt = (y[m].sum() + z * z / 2.0) / nt
        centre += big_n * pt
        var += big_n * big_n * (1.0 - n_h / big_n) * pt * (1.0 - pt) / nt
    se = math.sqrt(var) / n_hat
    c = centre / n_hat
    lo = max(float((y[pi >= 1.0]).sum()) / n_hat, c - z * se)
    return {"share": r, "lo": max(0.0, min(lo, r)), "hi": min(1.0, max(c + z * se, r)), "n_list": n_hat,
            "dams": d_hat}


def platt(scores, labels, l2=1e-3, iters=100):
    """(a, b) of P(dam) = 1 / (1 + exp(-(a * score + b))): a two-number calibration fitted to
    reviewed cards (use the random-control cards: the top-k cards over-represent dams). With
    Platt's smoothed targets, so a handful of cards does not give 0 or 1."""
    s, y = _clean(scores, labels)
    m, n = y.sum(), len(y) - y.sum()
    t = np.where(y > 0, (m + 1.0) / (m + 2.0), 1.0 / (n + 2.0))
    x = np.c_[s, np.ones(len(s))]

    def loss(w):
        f = x @ w
        return float(np.sum(np.logaddexp(0.0, f) - t * f) + 0.5 * l2 * (w @ w))
    # start flat at the base rate, Newton steps halved until the loss falls (Lin, Lin & Weng 2007):
    # full steps diverge on scores far outside 0-1
    w = np.array([0.0, -math.log((n + 1.0) / (m + 1.0))])
    f0 = loss(w)
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(x @ w, -30, 30)))
        g = x.T @ (p - t) + l2 * w
        h = (x * (p * (1 - p))[:, None]).T @ x + l2 * np.eye(2)
        st = np.linalg.solve(h, g)
        step = 1.0
        while step > 1e-8:
            f1 = loss(w - step * st)
            if f1 <= f0 + 1e-4 * step * float(g @ -st):
                break
            step *= 0.5
        if step <= 1e-8:
            break
        w, f0 = w - step * st, f1
        if np.abs(step * st).max() < 1e-10:
            break
    return float(w[0]), float(w[1])


def platt_apply(ab, scores):
    a, b = ab
    return 1.0 / (1.0 + np.exp(-np.clip(a * np.asarray(scores, float) + b, -30, 30)))


def group_folds(groups):
    """[(group, train indices, test indices)] leaving one group out at a time."""
    g = np.asarray(groups, dtype=object).ravel()
    return [(k, np.flatnonzero(g != k), np.flatnonzero(g == k)) for k in sorted(set(g.tolist()), key=str)]


def summary(scores, labels, ks=(30, 60, 100), z=Z95):
    """{'n', 'dams', 'auc', 'auc_lo', 'auc_hi', 'p@k': {k: precision_at}} of one ranking."""
    s, y = _clean(scores, labels)
    a, lo, hi, _se = auc_ci(s, y, z)
    return {"n": int(len(y)), "dams": int(y.sum()), "auc": a, "auc_lo": lo, "auc_hi": hi,
            "p@k": {k: precision_at(s, y, k, z) for k in ks if k <= len(y)}}


def describe(name, sm):
    """One line of text for a summary()."""
    parts = ["{}: {} cards, {} dams, AUC {:.3f} ({:.3f}-{:.3f})".format(
        name, sm["n"], sm["dams"], sm["auc"], sm["auc_lo"], sm["auc_hi"])]
    for k, pk in sorted(sm["p@k"].items()):
        parts.append("top-{} {}/{} = {:.0f} % ({:.0f}-{:.0f})".format(
            k, pk["hits"], pk["k"], 100 * pk["p"], 100 * pk["lo"], 100 * pk["hi"]))
    return "; ".join(parts)
