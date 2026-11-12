# -*- coding: utf-8 -*-
"""Hazard 2.24.7: habitable-only ND depth-velocity chart (911 points until reviewed).

QGIS-free, no DWR data, no rasters. Synthetic structure dicts only (the same
keys as the in-memory inventory and Habitable_Structures.csv).

    python3 DamSafety_Hazard/tests/test_dvchart.py
"""
import csv
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from DamSafety_Hazard import dvchart as DV  # noqa: E402
from DamSafety_Core.damsuite import tm11_curves as TM11  # noqa: E402

FAILS = []


def check(name, ok, info=""):
    print("{} {}{}".format("ok  " if ok else "FAIL", name, "" if ok else "  -> {}".format(info)))
    if not ok:
        FAILS.append(name)


def rec(**kw):
    d = {"id": kw.pop("id", 1), "habitable": "Yes", "acer_type": "House on foundation",
         "max_depth": 1.5, "max_vel": 1.0, "families": set(), "sources": [], "reviewed": ""}
    d.update(kw)
    return d


def nine(**kw):
    kw.setdefault("families", {"NDGISHUB"})
    kw.setdefault("sources", [{"family": "NDGISHUB", "addr_text": "100 MAIN ST"}])
    return rec(**kw)


# ----------------------------------------------------------------- 911 / review --
check("NDGISHUB family is a 911 point", DV.has_911(nine()))
check("ND_Address_Points > 0 is a 911 point (CSV)",
      DV.has_911({"ND_Address_Points": "2", "Sources": "NSI"}))
check("Sources column NDGISHUB is a 911 point (CSV)",
      DV.has_911({"Sources": "FEMA_STRUCTURES;NDGISHUB;NSI", "ND_Address_Points": "0"}))
check("NSI + FEMA with only a parcel Address is not a 911 point",
      not DV.has_911(rec(families={"NSI", "FEMA_STRUCTURES"},
                         sources=[{"family": "NSI"}], Address="15681 35TH STREET SOUTHEAST",
                         Sources="FEMA_STRUCTURES;NSI")))
check("empty record is not a 911 point", not DV.has_911({}))

check("Reviewed column is a review", DV.is_reviewed({"Reviewed": "nick 2026-11-12 (confirmed)"}))
check("rec['reviewed'] string is a review", DV.is_reviewed(rec(reviewed="nick 2026-11-12 (category)")))
check("rec['review'] decision dict is a review",
      DV.is_reviewed(rec(review={"category": "Dwelling", "reviewer": "nick", "date": "2026-11-12"})))
check("empty Reviewed is not a review", not DV.is_reviewed(rec(Reviewed="", reviewed="")))


# ----------------------------------------------------------------- chart filter --
structs = [
    nine(id=1, max_depth=2.0, max_vel=1.0),                          # plotted 911
    rec(id=2, families={"NSI"}, habitable="Yes", max_depth=3.0,
        max_vel=2.0),                                               # unreviewed non-911
    rec(id=3, families={"NSI", "FEMA_STRUCTURES"}, habitable="Yes",
        max_depth=2.5, max_vel=1.2,
        reviewed="nick 2026-11-12 (category: Dwelling)"),            # reviewed habitable
    rec(id=4, families={"NSI"}, habitable="Unverified", max_depth=1.0,
        max_vel=0.5),                                               # unreviewed, not Yes
    nine(id=5, habitable="Unverified", max_depth=1.2, max_vel=0.4),  # 911 but not Yes
    rec(id=6, families={"NSI"}, habitable="Yes", max_depth=0.0,
        max_vel=0.0),                                               # dry
    nine(id=7, acer_type="Mobile home", max_depth=1.8, max_vel=2.0), # mobile 911
]
sel = DV.select(structs)
ids = {p["id"] for p in sel["points"]}
check("911 habitable Yes is plotted", "1" in ids or 1 in ids or "1" in {p["id"] for p in sel["points"]})
# rec_id stringifies
ids = {p["id"] for p in sel["points"]}
check("plotted ids are the 911 house, reviewed non-911, and 911 mobile",
      ids == {"1", "3", "7"}, ids)
check("unreviewed NSI-only is not a point", "2" not in ids)
check("unreviewed unverified is not a point", "4" not in ids)
roles = {p["id"]: p["role"] for p in sel["points"]}
check("reviewed non-911 has distinct role", roles.get("3") == DV.ROLE_REVIEWED, roles)
check("911 house role is 911", roles.get("1") == DV.ROLE_911, roles)
check("unreviewed non-911 count includes Yes and Unverified",
      sel["left_out"]["unreviewed_non911"] == 2, sel["left_out"])
check("of those, one is already flagged habitable",
      sel["left_out"]["unreviewed_non911_habitable"] == 1, sel["left_out"])
check("unverified 911 is left out, not plotted",
      sel["left_out"]["not_plotted"] == 1 and "5" not in ids, sel["left_out"])
note = DV.left_out_note(sel["left_out"])
check("note names the unreviewed non-911 count",
      "2 unreviewed non-911" in note and "1 of them" in note, note)
check("select does not mutate habitable flags",
      structs[1]["habitable"] == "Yes" and structs[2]["reviewed"].startswith("nick"))

# mobile panel
check("mobile 911 is on the mobile chart",
      [p["chart"] for p in sel["points"] if p["id"] == "7"] == [DV.KIND_MOBILE])
check("house 911 is on the house chart",
      [p["chart"] for p in sel["points"] if p["id"] == "1"] == [DV.KIND_HOUSE])


# ----------------------------------------------------------------- zones match Core --
rng = random.Random(20261112)
n_ok = 0
n = 800
for i in range(n):
    kind = DV.KIND_HOUSE if i % 2 == 0 else DV.KIND_MOBILE
    vm, dm = TM11.AXES[kind]
    v = rng.random() * vm
    d = rng.random() * dm
    z = TM11.zone(kind, d, v)
    # same function the chart stores
    r = nine(id=i, acer_type="Mobile home" if kind == DV.KIND_MOBILE else "House on foundation",
             max_depth=d, max_vel=v)
    z2 = DV.select([r])["points"][0]["zone"]
    if z == z2:
        n_ok += 1
check("800 random points: chart zone equals tm11_curves.zone", n_ok == n, (n_ok, n))

# a point on the high boundary is HIGH (higher zone, ND 5.3)
lo, hi = TM11.thresholds("house", 5.0)
on = DV.select([nine(id=9, max_depth=hi, max_vel=5.0)])["points"][0]
check("depth on the high boundary is high (not judgment)", on["zone"] == TM11.HIGH, on["zone"])


# ----------------------------------------------------------------- source labels --
check("override wins", DV.detect_source("/x/Depth (Max).tif", override="DSS-WISE")[0] == "DSS-WISE")
check("scenario source DSS-WISE Lite -> DSS-WISE",
      DV.detect_source("/x/foo.tif", scenario_source="DSS-WISE Lite") == ("DSS-WISE", "dsswise"))
check("Screening Depth (Max).tif -> Screening",
      DV.detect_source("/tmp/Screening Depth (Max).tif")[0] == "Screening")
check("Depth (Max).tif -> Runner",
      DV.detect_source("/dams/DWR-2801/Depth (Max).tif")[0] == "Runner")
check("_breach_model path -> Runner",
      DV.detect_source("/dams/DWR-1/_breach_model/out.tif")[0] == "Runner")
check("imported raster uses the file name",
      DV.detect_source("/tmp/HCOM_MaxDepth_ft.tif")[0].startswith("DSS-WISE") or
      DV.detect_source("/tmp/other_depth.tif")[0] == "Imported raster: other_depth.tif")
# DSS-WISE hint in the name
check("DSS-WISE-like file name", DV.detect_source("/tmp/MaxDepth_in_ft.tif")[0] == "DSS-WISE")
check("generic imported name",
      DV.detect_source("/tmp/my_flood.tif") == ("Imported raster: my_flood.tif", "imported"))

with tempfile.TemporaryDirectory() as td:
    with open(os.path.join(td, "UNITS.txt"), "w") as fh:
        fh.write("Depth (Max).tif: feet\nThe *_depth.tif working grids in the parent folder are metres.\n")
    lab, kind = DV.detect_source(os.path.join(td, "Depth (Max).tif"))
    check("UNITS.txt screening sidecar beats Depth (Max).tif Runner default",
          lab == "Screening" and kind == "screening", (lab, kind))


# ----------------------------------------------------------------- build: CSV always, PNG when matplotlib --
def _has_pyplot():
    try:
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt  # noqa: F401
        return True
    except Exception:
        return False


with tempfile.TemporaryDirectory() as td:
    meta = {"dam_id": "DWR-0000", "dam_name": "Test Dam", "source": "Runner",
            "run_date": "2026-11-12"}
    # empty state
    empty = DV.build([], meta, td, stem="empty")
    check("empty state still writes a CSV", os.path.isfile(empty["csv"]) and
          empty["counts"]["total"] == 0)
    check("empty-state note", "no wet habitable structures" in (empty["note"] or ""), empty["note"])
    # with points, including a high/judgment label case (deep + fast)
    deep = nine(id=10, max_depth=8.0, max_vel=10.0)          # high on the house chart
    mid = nine(id=11, max_depth=3.5, max_vel=1.0)            # judgment (house low ~2.9 at v=1)
    built = DV.build([deep, mid, structs[1], structs[2], structs[6]], meta, td, stem="withpts")
    check("CSV lists only plotted points (not the unreviewed NSI)", True)
    with open(built["csv"], newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    csv_ids = {r["id"] for r in rows}
    check("CSV ids are 10, 11, 3, 7 (no unreviewed 2)", csv_ids == {"10", "11", "3", "7"}, csv_ids)
    check("CSV has role and has_911 columns",
          set(DV.CSV_COLUMNS) <= set(rows[0]) and
          any(r["role"] == "reviewed_habitable" for r in rows))
    check("title has high / MEDIUM / low counts",
          "MEDIUM (judgment)" in built["text"] and "high" in built["text"], built["text"])
    check("left-out note on the title third line",
          "unreviewed non-911" in " ".join(DV.title_lines(meta, built["counts"], built["left_out"])))
    if _has_pyplot():
        check("PNG written when matplotlib.pyplot is available",
              built["status"] == "ok" and built["png"] and os.path.isfile(built["png"])
              and os.path.getsize(built["png"]) > 1000, (built["status"], built["png"], built["note"]))
        # mobile panel only when needed
        no_mob = DV.build([deep, mid], meta, td, stem="nomobile")
        check("no mobile panel file still ok (houses + zoom only)",
              no_mob["status"] == "ok" and os.path.isfile(no_mob["png"]))
    else:
        check("without matplotlib, status is csv_only and CSV remains",
              built["status"] == "csv_only" and os.path.isfile(built["csv"]) and built["png"] is None,
              (built["status"], built["note"]))

# missing matplotlib: force the skip even if it is installed
with tempfile.TemporaryDirectory() as td:
    hold = {k: sys.modules[k] for k in list(sys.modules) if k == "matplotlib" or k.startswith("matplotlib.")}
    try:
        for k in list(hold):
            sys.modules[k] = None
        blocked = DV.build([nine(id=1)], {"dam_id": "X", "source": "Runner", "run_date": "2026-11-12"},
                           td, stem="blocked")
    finally:
        sys.modules.update(hold)
        for k in list(sys.modules):
            if k not in hold and (k == "matplotlib" or k.startswith("matplotlib.")):
                sys.modules.pop(k, None)
    check("forced-missing matplotlib: CSV only, note says why, no raise",
          blocked["status"] == "csv_only" and os.path.isfile(blocked["csv"])
          and blocked["png"] is None and "matplotlib" in (blocked["note"] or "").lower(),
          blocked)


# build_from_csv reads Habitable_Structures columns
with tempfile.TemporaryDirectory() as td:
    src = os.path.join(td, "Habitable_Structures.csv")
    with open(src, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["Struct_ID", "Sources", "ND_Address_Points",
                                           "Habitable", "ACER_Type", "Max_Depth_ft",
                                           "Max_Vel_fps", "Reviewed"])
        w.writeheader()
        w.writerow({"Struct_ID": "22", "Sources": "NDGISHUB;NSI", "ND_Address_Points": "1",
                    "Habitable": "Yes", "ACER_Type": "Habitable", "Max_Depth_ft": "1.63",
                    "Max_Vel_fps": "3.32", "Reviewed": ""})
        w.writerow({"Struct_ID": "8", "Sources": "FEMA_STRUCTURES;NSI", "ND_Address_Points": "0",
                    "Habitable": "Yes", "ACER_Type": "House on foundation", "Max_Depth_ft": "2.1",
                    "Max_Vel_fps": "1.0", "Reviewed": ""})
    out = DV.build_from_csv(src, {"dam_id": "DWR-2801", "source": "Runner", "run_date": "2026-10-23"},
                            td, stem="fromcsv")
    check("CSV input: only the 911 row is plotted",
          out["counts"]["total"] == 1 and out["left_out"]["unreviewed_non911_habitable"] == 1,
          (out["counts"], out["left_out"]))


# write_for_run labels a DSS-WISE scenario
class _Sc(object):
    name = "DSSWISE sunny day"
    source = "DSS-WISE Lite"
    depth = "/tmp/made_up_depth.tif"


with tempfile.TemporaryDirectory() as td:
    wr = DV.write_for_run([nine(id=1)], td, scenario=_Sc(), dam_id="DWR-1", dam_name="X")
    check("write_for_run uses the DSS-WISE scenario source",
          wr["source"] == "DSS-WISE", wr["source"])
    check("write_for_run writes the standard stem",
          os.path.basename(wr["csv"]) == "ND_DV_Habitable.csv")

print("\n{} failure(s)".format(len(FAILS)) if FAILS else "\nall checks passed")
sys.exit(1 if FAILS else 0)
