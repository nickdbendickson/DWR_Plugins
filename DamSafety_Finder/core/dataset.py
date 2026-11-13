"""The chip + label dataset for a future image model (Finder 0.5).

Pure Python. An image-chip classifier (a small CNN on the aerial + LiDAR-hillshade chips the
review sheets show) needs about 100 confirmed dams before it can be trained and checked; there
are 32 after three county reviews. This keeps every reviewed card - its chips, its mark, the
Finder's numbers and where it came from - in one folder, so the set grows with each review round
and is ready when it is large enough:

  <folder>/manifest.jsonl   one JSON record per card: find_id, label (yes / no / unsure / offset),
                            note, county, sheet, terrain, model / engine version, the fill's
                            fields, chip file names
  <folder>/chips/<find_id>_<kind>.jpg

A card marked again (a later round, a second reviewer) is a new record; load() keeps the
latest per (find_id, reviewer).
"""
import json
import os
import time


def add(folder, find_id, label, meta=None, chips=None, reviewer=""):
    """Append one reviewed card. chips: {kind: bytes or path} (e.g. {'aerial': ..., 'hillshade':
    ...}). Returns the record."""
    os.makedirs(os.path.join(folder, "chips"), exist_ok=True)
    names = {}
    for kind, data in (chips or {}).items():
        name = "{}_{}.jpg".format(find_id, kind)
        path = os.path.join(folder, "chips", name)
        if isinstance(data, (bytes, bytearray)):
            with open(path + ".part", "wb") as fh:
                fh.write(data)
            os.replace(path + ".part", path)
        elif data and os.path.exists(str(data)):
            with open(str(data), "rb") as src, open(path + ".part", "wb") as fh:
                fh.write(src.read())
            os.replace(path + ".part", path)
        else:
            continue
        names[kind] = "chips/" + name
    rec = {"find_id": find_id, "label": label, "reviewer": reviewer, "chips": names,
           "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    rec.update(meta or {})
    with open(os.path.join(folder, "manifest.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    return rec


def _records(folder):
    """Every record in file order."""
    path = os.path.join(folder, "manifest.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load(folder):
    """The latest record per (find_id, reviewer)."""
    latest = {}
    for r in _records(folder):
        latest[(r.get("find_id"), r.get("reviewer", ""))] = r
    return list(latest.values())


def stats(folder, ready_at=100):
    """{'cards', 'yes', 'no', 'unsure', 'offset', 'second_looks', 'by_county': {county: {label:
    n}}, 'ready' (enough confirmed dams to train and check an image model)}. Each card counts once,
    by its first reviewer's mark (the reviewer of its earliest record that is not a second look;
    their latest mark, so a reviewer can correct themselves). A blind second look is counted
    apart."""
    recs = load(folder)
    who = {}
    for r in _records(folder):
        if not r.get("second_look"):
            who.setdefault(r.get("find_id"), r.get("reviewer", ""))
    first = {fid: r for r in recs for fid in [r.get("find_id")]
             if not r.get("second_look") and who.get(fid) == r.get("reviewer", "")}
    out = {"cards": len(first), "yes": 0, "no": 0, "unsure": 0, "offset": 0,
           "second_looks": sum(1 for r in recs if r.get("second_look")), "by_county": {}}
    for r in first.values():
        lab = r.get("label")
        if lab in out:
            out[lab] += 1
        c = out["by_county"].setdefault(r.get("county", ""), {})
        c[lab] = c.get(lab, 0) + 1
    out["ready"] = out["yes"] >= ready_at
    return out
