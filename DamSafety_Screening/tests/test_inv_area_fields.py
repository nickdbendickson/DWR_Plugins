"""0.17.49: the normal-pool surface area (tool 1's inv_area_m2) comes only from normal-pool fields.

Before, DWR_ALIASES["SA"] also listed max_pool_area and match_fields took the LAST alias a layer
has, so on the suite GeoPackage (which has both normal_area and max_pool_area) inv_area_m2 held
the MAXIMUM-pool area. The DWR shapefile export (10-character names: normal_are, max_pool_a) was
not affected. Checked against the real DWR_dams.gpkg column names (read-only, when the data pack
is on this machine; else the names recorded below), the shapefile variant (the same names cut to
10 characters), the chain's step-1 field map and tool 1 itself.

Run: python3 tests/test_inv_area_fields.py
"""
import json, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
from qgis.core import QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, make_layer  # noqa: E402
import importlib  # noqa: E402
PKG = os.path.basename(PLUGIN)
inv = importlib.import_module(PKG + ".core.inventory")
wf = importlib.import_module(PKG + ".core.workflow")
PrepareDams = importlib.import_module(PKG + ".algorithms.prepare_dams").PrepareDams
AC = inv.AC_M2

# the columns of the DWR_dams layer of the suite pack (dwr/DWR_dams.gpkg), in table order
GPKG_RECORDED = [
    'fid', 'geom', 'cp_index', 'national_id', 'hu_basin', 'site_location', 'longitude', 'latitude',
    'dam_name', 'other_reservoir_name', 'primary_owner_name', 'other_responsible_parties',
    'nearest_affected_town', 'distance_town', 'federal_dam_type', 'design_class', 'hazard_class',
    'max_dam_height', 'dam_length', 'upstream_slope', 'max_pool_area', 'max_pool_vol',
    'max_pool_elev', 'streambed_elev', 'emergency_vol', 'emergency_area', 'emergency_elev',
    'normal_vol', 'normal_area', 'normal_elev', 'fed_river_class', 'eap_last_updated', 'stream',
    'contrib_area', 'tot_drainage_area', 'principal_inlet', 'principal_length',
    'emerg_spillway_const', 'structure_height', 'eap_date_prepared', 'normal_depth',
    'max_discharge', 'low_level_type', 'emerg_spillway_width', 'valve_type', 'valve_loc',
    'low_head_dam', 'low_level_res_ext', 'low_level_ext_install', 'year_built', 'outlet_gates',
    'owner_type', 'private_dam', 'emergency_plan', 'phase_i_report', 'inspection_date', 'info_date',
    'former_name', 'year_modified', 'core', 'foundation', 'inspection_freq', 'regulating_agency',
    'embankment_volume', 'emerg_spillway_type', 'coord_type', 'vertical_datum', 'purpose',
    'purpose_alt', 'county', 'top_width', 'es_level_length', 'es_slope_length', 'es_percent_slope',
    'downstream_slope', 'remarks', 'structure_id', 'design_firm', 'project_no', 'status',
    'drawdown_elevation', 'drawdown_area', 'drawdown_vol', 'streambed_area', 'streambed_vol',
    'conduit_type', 'conduit_size', 'receive_date', 'water_permit_no', 'x_coord', 'y_coord',
    'hazard_review_date', 'geometry', 'hu_sub_basin', 'hu_watershed', 'hu_sub_watershed',
    'civil_township', 'set_geometry_null', 'eap_date_lastexercise', 'eap_prepared_by', 'eap_notes',
    'completion_notice_sent', 'completion_notice_received', 'date_status_verified',
    'eap_access_string', 'reservoir_name', 'satus_verified_in', 'separate_structure_no',
    'other_structure_id', 'modified_low_head_dam', 'modifications_pending']
PACK = os.environ.get("ND_PACK_DWR_DAMS", "/home/box/DamSafety/ND_Dam_Suite_Pack/dwr/DWR_dams.gpkg")
gpkg_names = GPKG_RECORDED
if os.path.isfile(PACK):
    con = sqlite3.connect("file:{}?mode=ro".format(PACK), uri=True)      # read-only
    gpkg_names = [r[1] for r in con.execute('PRAGMA table_info("DWR_dams")')]
    con.close()
    assert gpkg_names == GPKG_RECORDED, sorted(set(gpkg_names) ^ set(GPKG_RECORDED))
    print("real pack columns: {} ({} names)".format(PACK, len(gpkg_names)))
shp_names = list(dict.fromkeys(n[:10] for n in gpkg_names))           # the shapefile export's names

# ---- 1. the bug: the old rule on the real names
OLD_SA = ("normal_area", "normal_are", "max_pool_area", "surface_area")
old_have = {n.lower(): n for n in gpkg_names}
old_pick = [old_have[a] for a in OLD_SA if a in old_have][-1]
assert old_pick == "max_pool_area", old_pick                          # what 0.17.46 put in inv_area_m2

# ---- 2. the fix: normal pool -> SA, max pool -> MA only, on both variants
g = inv.match_fields(gpkg_names, inv.DWR_ALIASES)
assert g["SA"] == "normal_area" and g["MA"] == "max_pool_area", g
s = inv.match_fields(shp_names, inv.DWR_ALIASES)
assert s["SA"] == "normal_are" and s["MA"] == "max_pool_a", s
for n in ("max_pool_area", "max_pool_a", "MAX_POOL_AREA"):
    assert n.lower() not in inv.DWR_ALIASES["SA"] and inv.is_max_pool_area(n)
assert not inv.is_max_pool_area("normal_area") and not inv.is_max_pool_area(None)
# a layer with ONLY a max-pool area has no normal-pool area
assert "SA" not in inv.match_fields(["structure_id", "max_pool_area"], inv.DWR_ALIASES)
# priority, not last match: normal_area beats surface_area
assert inv.match_fields(["surface_area", "normal_area"], inv.DWR_ALIASES)["SA"] == "normal_area"
assert inv.match_fields(["SURFACE_AREA"], inv.NID_ALIASES)["SA"] == "SURFACE_AREA"
# the other roles are unchanged (the last alias the layer has still wins)
nid = ["NIDID", "FEDERAL_ID", "DAM_HEIGHT", "NID_HEIGHT", "MAX_STORAGE", "NID_STORAGE",
       "OPERATIONAL_STATUS", "CONDITION_ASSESSMENT", "SURFACE_AREA"]
m = inv.match_fields(nid, inv.NID_ALIASES)
assert (m["ID"], m["H"], m["MS"], m["STATUS"]) == ("FEDERAL_ID", "NID_HEIGHT", "NID_STORAGE",
                                                   "CONDITION_ASSESSMENT"), m
for role in ("ID", "H", "NS", "MS", "HZ", "NIDREF", "LEN"):
    assert g.get(role) == {n.lower(): n for n in gpkg_names}.get(
        [a for a in inv.DWR_ALIASES[role] if a in old_have][-1]), role
row = {"structure_id": 7, "normal_area": 4.0, "max_pool_area": 10.0}
st = inv.standardize(row.get, g)
assert abs(st["sa"] - 4 * AC) < 1e-6 and abs(st["ma"] - 10 * AC) < 1e-6, st
print("OK (match_fields: gpkg normal_area / max_pool_area, shapefile normal_are / max_pool_a)")

# ---- 3. the chain's step 1: detected names, and an old saved field map with max_pool_area
W = tempfile.mkdtemp(prefix="invarea_")
gp = os.path.join(W, "dwr.gpkg")
con = sqlite3.connect(gp)
con.execute('CREATE TABLE dwr_dams ({})'.format(", ".join('"{}"'.format(n) for n in gpkg_names)))
con.commit(); con.close()
d = wf.defaults("1", W, {"dwr": gp})
assert d["DWR_SA"] == "normal_area" and d["DWR_MA"] == "max_pool_area", d
d = wf.defaults("1", W, {"dwr": gp, "field_map": {"DWR_SA": "max_pool_area", "DWR_H": "max_dam_height"}})
assert d["DWR_SA"] == "normal_area" and d["DWR_H"] == "max_dam_height", d
json.dump({"DWR_SA": "max_pool_area"}, open(os.path.join(W, "1_fields.json"), "w"))
assert wf.defaults("1", W, {"dwr": gp})["DWR_SA"] == "normal_area"
json.dump({"DWR_SA": "emergency_area"}, open(os.path.join(W, "1_fields.json"), "w"))
assert wf.defaults("1", W, {"dwr": gp})["DWR_SA"] == "emergency_area"     # another choice is kept
os.remove(os.path.join(W, "1_fields.json"))
print("OK (workflow step 1: normal_area; an old saved max_pool_area is replaced)")

# ---- 4. tool 1 on a GeoPackage-named layer: inv_area_m2 = normal pool, inv_maxa_m2 = max pool
D, S_ = "double", "string"
FL = [("structure_id", D), ("dam_name", S_), ("max_dam_height", D), ("normal_vol", D),
      ("max_pool_vol", D), ("normal_area", D), ("max_pool_area", D), ("hazard_class", S_),
      ("status", S_), ("national_id", S_), ("dam_length", D)]
lay = make_layer(FL, [(QgsGeometry.fromPointXY(QgsPointXY(-100.0, 47.0)),
                       {"structure_id": 5289.0, "dam_name": "Test", "max_dam_height": 20.0,
                        "normal_vol": 100.0, "max_pool_vol": 300.0, "normal_area": 10.0,
                        "max_pool_area": 25.0, "hazard_class": "Low", "status": "Intact",
                        "national_id": "ND00123", "dam_length": 300.0})], "point", "dwr")
blank = {"DWR_" + k: "" for k in ("ID", "NAME", "H", "NS", "MS", "SA", "HZ", "STATUS", "NIDREF", "LEN", "MA")}
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
r = PrepareDams().run_mock(dict(blank, DWR=ctx.add(lay), OUT="memory:"), ctx, fb)
f = list(ctx.layers[r["OUT"]].getFeatures())[0]
assert abs(f["inv_area_m2"] - 10 * AC) < 1e-6 and abs(f["inv_maxa_m2"] - 25 * AC) < 1e-6, (
    f["inv_area_m2"], f["inv_maxa_m2"])
assert not any("MAXIMUM-pool" in m for m in fb.log)
# a max-pool field picked for the surface area box: used as asked, with a warning
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
r = PrepareDams().run_mock(dict(blank, DWR=ctx.add(lay), DWR_SA="max_pool_area", OUT="memory:"), ctx, fb)
f = list(ctx.layers[r["OUT"]].getFeatures())[0]
assert abs(f["inv_area_m2"] - 25 * AC) < 1e-6
assert any("max_pool_area, the MAXIMUM-pool area" in m for m in fb.log), fb.log
print("OK (tool 1: inv_area_m2 normal pool, inv_maxa_m2 max pool; a max-pool pick is warned)")
print("ALL OK")
