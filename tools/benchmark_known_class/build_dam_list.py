"""Build dams.csv, the known-class benchmark set, from the official inventories.

The class of every benchmark dam is the class an official source states; nothing here guesses
or computes a class. Two sources are read and both are kept:

* ND DWR dam database (the regulator): hazard_class and hazard_review_date. The offline pack's
  dwr/DWR_dams.gpkg, or the mapservice export (Dams.shp, short field names) - either works.
* USACE National Inventory of Dams: HAZARD_POTENTIAL (the pack's hydro/NID_dams.gpkg).

The expected class is DWR's (ND is the regulator of record); NID is a cross-check, and a dam on
which they disagree is kept but flagged (nid_agrees = no).

    python3 build_dam_list.py --dwr <DWR_dams.gpkg or Dams.shp> --nid <NID_dams.gpkg> [--out dams.csv]

Needs the GDAL Python bindings (osgeo.ogr) only to read the inventories; the benchmark check
itself (check_classes.py) does not.
"""
import argparse
import csv
import os

# The selection (2026-11-23). Every dam is Intact in the DWR database and states a class.
# High and Significant: every reviewed (hazard_review_date set) dam with a dam height and a
# maximum storage of at most 12,000 acre-feet, which a cloud screening run finishes, plus
# 2801 / 2662 (Swan Buffalo 12 / 8, the suite's reference dams; class stated, no review date).
# Low: one reviewed Low dam per county from 14 random counties (seed 20261123) of the 123
# reviewed Low dams 20 ft or higher holding 50 to 5,000 acre-feet, plus 3034 / 2865 (reference
# runs of the accuracy envelope).
# Left out: 2659 Tewaukon WS-T-7 (reviewed Significant): its DWR dam line is 7 km from the dam
# (batch-44 run1118 review, 2026-11-23), so a run measures the bad line, not the classifier.
# tier "mac": large reservoirs whose screening flood takes hours (Nick's Mac, not the cloud).
SELECTION = {
    "cloud": [
        # High
        6122, 2642, 5728, 3048, 2641, 2668, 2692, 3061, 6001, 2636, 2639, 4980, 10551, 2669,
        2649, 2656, 2904, 2973, 2978, 4575, 2975, 2979, 2647,
        # Significant
        2633, 2663, 2695, 2722, 6111, 2983, 4441, 2640, 3037, 2621, 10727, 3007, 2665,
        10633, 2654, 2655, 2791, 2801, 2662,
        # Low
        2629, 4620, 5668, 4628, 2898, 3122, 2870, 3033, 2794, 11409, 3000, 2724, 2617, 5765,
        3034, 2865,
    ],
    "mac": [2756, 2914, 3008, 2667, 2651, 2708],
}

FIELDS = ("dam_id", "structure_id", "name", "county", "height_ft", "max_storage_acft", "tier",
          "dwr_hazard_class", "dwr_hazard_review_date", "nid_id", "nid_hazard_potential",
          "nid_agrees", "expected_nd_class", "source")

# ND REG_05.2026 says Medium where the DWR database (and NID) still say Significant
ND_NAME = {"High": "High", "Significant": "Medium", "Medium": "Medium", "Low": "Low"}

ALIASES = {"structure_id": ("structure_id", "structure_"), "name": ("dam_name",),
           "county": ("county",), "height": ("max_dam_height", "max_dam_he", "structure_height"),
           "storage": ("max_pool_vol", "max_pool_volume", "max_pool_v", "max_storage"),
           "hazard": ("hazard_class", "hazard_cla"), "review": ("hazard_review_date", "hazard_rev"),
           "nid": ("national_id", "national_i", "nid_id"), "status": ("status",)}


def _pick(names, key):
    low = {n.lower(): n for n in names}
    return next((low[a] for a in ALIASES[key] if a in low), None)


def read_layer(path):
    from osgeo import ogr
    ogr.UseExceptions()
    ds = ogr.Open(path)
    if ds is None:
        raise SystemExit("cannot open {}".format(path))
    lyr = ds.GetLayer(0)
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    rows = [{n: f.GetField(n) for n in names} for f in lyr]
    return names, rows


def nid_classes(path):
    names, rows = read_layer(path)
    low = {n.lower(): n for n in names}
    idf, hz = low.get("nidid"), low.get("hazard_potential")
    return {str(r[idf]).strip(): (r[hz] or "").strip() for r in rows if r.get(idf)}


def build(dwr_path, nid_path, out):
    names, rows = read_layer(dwr_path)
    f = {k: _pick(names, k) for k in ALIASES}
    by_id = {int(r[f["structure_id"]]): r for r in rows if r.get(f["structure_id"]) is not None}
    nid = nid_classes(nid_path) if nid_path else {}
    src = "ND DWR dam database ({}); NID {}".format(os.path.basename(dwr_path),
                                                    os.path.basename(nid_path) if nid_path else "not read")
    out_rows = []
    for tier, ids in SELECTION.items():
        for sid in ids:
            r = by_id.get(sid)
            if r is None:
                raise SystemExit("structure {} not in {}".format(sid, dwr_path))
            cls = (r[f["hazard"]] or "").strip()
            if cls not in ND_NAME:
                raise SystemExit("structure {}: the database states no class ({!r})".format(sid, cls))
            if (r[f["status"]] or "").strip() != "Intact":
                raise SystemExit("structure {}: status {!r}, not Intact".format(sid, r[f["status"]]))
            nid_id = (r[f["nid"]] or "").strip() if f["nid"] else ""
            nid_cls = nid.get(nid_id, "")
            agrees = "" if nid_cls not in ND_NAME else ("yes" if ND_NAME.get(nid_cls) == ND_NAME[cls] else "no")
            out_rows.append({
                "dam_id": "DWR-{}".format(sid), "structure_id": sid, "name": (r[f["name"]] or "").strip(),
                "county": (r[f["county"]] or "").strip(),
                "height_ft": round(float(r[f["height"]] or 0), 1),
                "max_storage_acft": round(float(r[f["storage"]] or 0), 1), "tier": tier,
                "dwr_hazard_class": cls, "dwr_hazard_review_date": (str(r[f["review"]] or "")).strip()[:10],
                "nid_id": nid_id, "nid_hazard_potential": nid_cls, "nid_agrees": agrees,
                "expected_nd_class": ND_NAME[cls], "source": src})
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, FIELDS)
        w.writeheader()
        w.writerows(out_rows)
    return out_rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dwr", required=True)
    ap.add_argument("--nid", default="")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "dams.csv"))
    a = ap.parse_args(argv)
    rows = build(a.dwr, a.nid, a.out)
    tally = {}
    for r in rows:
        tally[(r["tier"], r["expected_nd_class"])] = tally.get((r["tier"], r["expected_nd_class"]), 0) + 1
    print("{} dams -> {}".format(len(rows), a.out))
    for k in sorted(tally):
        print("  {} {}: {}".format(k[0], k[1], tally[k]))
    bad = [r["dam_id"] for r in rows if r["nid_agrees"] == "no"]
    if bad:
        print("  DWR and NID disagree on: " + ", ".join(bad))


if __name__ == "__main__":
    main()
