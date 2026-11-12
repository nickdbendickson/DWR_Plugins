# -*- coding: utf-8 -*-
"""Hazard 2.24.7: habitable_review.json is written only by an explicit sign-off.

QGIS-free, synthetic keys only.

    python3 DamSafety_Hazard/tests/test_habitable_review.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from DamSafety_Hazard import habitable_review as HR  # noqa: E402
from DamSafety_Hazard import dvchart as DV  # noqa: E402

FAILS = []


def check(name, ok, info=""):
    print("{} {}{}".format("ok  " if ok else "FAIL", name, "" if ok else "  -> {}".format(info)))
    if not ok:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, HR.REVIEW_FILE)
    # classify / chart path must not create the file
    recs = [{"id": 1, "families": {"NSI"}, "habitable": "Yes", "max_depth": 1.0, "max_vel": 0.5}]
    DV.build(recs, {"dam_id": "X", "source": "Runner", "run_date": "2026-11-12"}, td)
    check("dvchart.build does not write habitable_review.json",
          not os.path.isfile(path) and HR.load(path) == ({}, []))

    try:
        HR.sign_off(path, ["fd_id:99"], "yes", "")
        check("sign_off requires decided_by", False)
    except ValueError:
        check("sign_off requires decided_by", True)
    check("failed sign_off left no file", not os.path.isfile(path))

    HR.sign_off(path, ["fd_id:99", "Struct_ID:2"], "yes", "A. Engineer",
                note="site visit", decided_on="2026-11-12")
    check("sign_off writes the file", os.path.isfile(path))
    reviews, probs = HR.load(path)
    check("load has both keys as yes, no problems",
          set(reviews) == {"fd_id:99", "Struct_ID:2"} and
          all(e["reviewed_habitable"] == "yes" for e in reviews.values()) and not probs,
          (reviews, probs))
    data = json.loads(open(path, encoding="utf-8").read())
    check("schema is dwr-habitable-review-v1 and history has 2 entries",
          data["schema"] == HR.SCHEMA and len(data["history"]) == 2, data)

    # apply matches fd_id
    recs = [{"id": 7, "fd_id": 99, "families": {"NSI"}, "habitable": "Yes",
             "max_depth": 2.0, "max_vel": 1.0}]
    n = HR.apply(recs, reviews)
    check("apply matches fd_id:99 and sets reviewed_habitable=yes",
          n == 1 and recs[0]["reviewed_habitable"] == "yes" and recs[0]["review_by"] == "A. Engineer",
          recs[0])
    check("applied non-911 is now chart-habitable as reviewed",
          DV.habitability_status(recs[0])[0] == "yes" and DV.role_of(recs[0]) == DV.ROLE_REVIEWED)

    HR.sign_off(path, ["fd_id:99"], "no", "A. Engineer", note="shed", decided_on="2026-11-13")
    reviews, _ = HR.load(path)
    recs[0].pop("reviewed_habitable", None)
    HR.apply(recs, reviews)
    check("later no decision overwrites yes", recs[0]["reviewed_habitable"] == "no")
    data = json.loads(open(path, encoding="utf-8").read())
    check("history keeps the earlier yes",
          len(data["history"]) == 3 and data["history"][0]["reviewed_habitable"] == "yes")

    HR.sign_off(path, ["fd_id:99"], None, "A. Engineer", decided_on="2026-11-14")
    reviews, _ = HR.load(path)
    check("remove drops the key (back to unverified)", "fd_id:99" not in reviews)

    # DamIT boolean schema is accepted
    dpath = os.path.join(td, "damit.json")
    with open(dpath, "w", encoding="utf-8") as fh:
        json.dump({"schema": "damit-habitable-review-v1",
                   "reviews": {"nguid:ABC": {"reviewed_habitable": True, "decided_by": "B",
                                            "decided_on": "2026-11-12", "note": ""}},
                   "history": []}, fh)
    reviews, probs = HR.load(dpath)
    check("DamIT boolean true loads as yes",
          reviews.get("nguid:ABC", {}).get("reviewed_habitable") == "yes" and not probs,
          (reviews, probs))

    check("find walks up from a raster path",
          HR.find([os.path.join(td, "Depth (Max).tif")]) == path)

    keys = HR.keys_from("a, b\nc", None)
    check("keys_from splits commas and lines", keys == ["a", "b", "c"], keys)

print("\n{} failure(s)".format(len(FAILS)) if FAILS else "\nall checks passed")
sys.exit(1 if FAILS else 0)
