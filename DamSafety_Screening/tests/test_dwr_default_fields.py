"""0.18.1 (fulltest 2026-10-30): tool 1 with its default DWR field boxes (the shapefile's
10-character names: structure_, max_dam_he, ...) on the suite GeoPackage (structure_id,
max_dam_height, ...) failed every read with KeyError: 'structure_'. inventory.resolve_fields
matches a chosen name ignoring case, falls back to the alias the layer has, else leaves the
role blank, and says so.

Pure Python: python3 DamSafety_Screening/tests/test_dwr_default_fields.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
import inventory as inv  # noqa: E402

FAILS = []


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


GPKG = ["fid", "structure_id", "dam_name", "max_dam_height", "normal_vol", "max_pool_vol",
        "normal_area", "max_pool_area", "hazard_class", "status", "national_id", "dam_length",
        "normal_elev", "max_pool_elev"]
SHP = ["STRUCTURE_", "DAM_NAME", "MAX_DAM_HE", "NORMAL_VOL", "MAX_POOL_V", "NORMAL_ARE",
       "HAZARD_CLA", "STATUS", "NATIONAL_I", "DAM_LENGTH"]

# 1. the suite GeoPackage with the default (shapefile) boxes
fld, notes = inv.resolve_fields(dict(inv.DWR_DEFAULTS), GPKG, inv.DWR_ALIASES)
check("GeoPackage: ID -> structure_id", fld["ID"] == "structure_id", fld)
check("GeoPackage: H -> max_dam_height, MS -> max_pool_vol, SA -> normal_area",
      (fld["H"], fld["MS"], fld["SA"]) == ("max_dam_height", "max_pool_vol", "normal_area"), fld)
check("GeoPackage: HZ / NIDREF / MA / NE / ME found",
      (fld["HZ"], fld["NIDREF"], fld["MA"], fld["NE"], fld["ME"]) ==
      ("hazard_class", "national_id", "max_pool_area", "normal_elev", "max_pool_elev"), fld)
check("GeoPackage: every resolved name is a layer field",
      all(v in GPKG for v in fld.values() if v), fld)
check("GeoPackage: a note for the swapped ID box",
      "structure_" in notes.get("ID", "") and "structure_id" in notes.get("ID", ""), notes)
rec = {"structure_id": 2801, "dam_name": "Test", "max_dam_height": 20.0, "normal_vol": 100.0,
       "max_pool_vol": 300.0, "normal_area": 10.0, "max_pool_area": 20.0,
       "hazard_class": "High", "status": "Intact", "national_id": "nd00199", "dam_length": 500.0,
       "normal_elev": 1900.0, "max_pool_elev": 1910.0}
s = inv.standardize(lambda n: rec[n], fld)          # the tool's strict getter (f[name])
check("GeoPackage: standardize reads the record (no KeyError)",
      s["id"] == "2801" and s["nidref"] == "ND00199" and abs(s["h"] - 6.096) < 1e-9, s)

# 2. the shapefile export, upper-case names: matched ignoring case, no notes
fld, notes = inv.resolve_fields(dict(inv.DWR_DEFAULTS), SHP, inv.DWR_ALIASES)
check("shapefile (upper case): ID -> STRUCTURE_", fld["ID"] == "STRUCTURE_", fld)
check("shapefile: no notes (the defaults are its names)", not notes, notes)

# 3. a layer with neither name: blank, noted, standardize still works
fld, notes = inv.resolve_fields({"ID": "structure_", "NAME": "dam_name"}, ["dam_name", "x"],
                                inv.DWR_ALIASES)
check("no ID field: ID left blank", fld["ID"] is None, fld)
check("no ID field: noted as left blank", "left blank" in notes.get("ID", ""), notes)
s = inv.standardize(lambda n: {"dam_name": "A"}[n], fld, tag="DWR", idx=7)
check("no ID field: the record gets the DWR<n> id", s["id"] == "DWR7" and s["name"] == "A", s)

# 4. an explicit good choice is kept even when an alias also exists
fld, notes = inv.resolve_fields({"ID": "dam_id"}, ["structure_id", "dam_id"], inv.DWR_ALIASES)
check("explicit existing field kept", fld["ID"] == "dam_id" and not notes, (fld, notes))

# 5. the NID defaults on the NID layer
nid = ["NIDID", "NAME", "DAM_HEIGHT", "NORMAL_STORAGE", "MAX_STORAGE", "SURFACE_AREA",
       "HAZARD_POTENTIAL", "OPERATIONAL_STATUS", "DAM_LENGTH"]
fld, notes = inv.resolve_fields(dict(inv.NID_DEFAULTS), nid, inv.NID_ALIASES, "NID")
check("NID defaults resolve unchanged", fld["ID"] == "NIDID" and fld["H"] == "DAM_HEIGHT"
      and not notes, (fld, notes))

print("\n{} failure(s)".format(len(FAILS)))
sys.exit(1 if FAILS else 0)
