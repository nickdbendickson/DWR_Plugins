"""0.17.43: surface_area_ac and dam_length_ft in the dam.json tool 7b writes (damrecord_io).

Both are filled when the screening has them (the top-of-dam area tool 7b sends as AREA_AC; the
dam length from tool 4's mapped dam line, else the inventory) and are otherwise recorded as
null with the reason in the source note - never in place of a value another tool recorded.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_handoff_record_fields.py
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
from nd_dam_screening.core import damrecord_io as dri  # noqa: E402
from nd_dam_screening.damsuite import dam_record as dr  # noqa: E402

FT, AC = 0.3048, 4046.8564224
work = tempfile.mkdtemp(prefix="hofields_")
attrs = {"dam_id": "DWR-77", "name": "Test", "height_m": 6.096, "crest_len_m": 152.4,
         "inv_area_m2": 40 * AC, "source": "DWR+NID"}
P = {"HEIGHT_FT": 20.0, "STORAGE_ACFT": 300.0, "AREA_AC": 55.25}

# ---- the values: area only from AREA_AC, length line > inventory, nothing from 0 / blank
v = dri.handoff_values(attrs, P, (1.0, 2.0))
assert v["surface_area_ac"] == 55.25 and v["dam_length_ft"] == 500.0, v
assert dri.handoff_values(attrs, P, None, line_len_m=91.44)["dam_length_ft"] == 300.0
assert dri.dam_length(attrs, 91.44)[1] == "length of the mapped dam line (tool 4)"
assert dri.dam_length(attrs)[1] == "dam inventory (DWR+NID: DWR dam_length / NID DAM_LENGTH)"
assert dri.dam_length(attrs, 0.0)[0] == 500.0                          # 0 = no line -> inventory
for blank in (None, "", 0, 0.0, -3, "x"):
    assert dri.dam_length({"crest_len_m": blank})[0] is None, blank
v0 = dri.handoff_values(dict(attrs, crest_len_m=0.0), {"HEIGHT_FT": 20.0}, None)
assert "surface_area_ac" not in v0 and "dam_length_ft" not in v0, v0   # no inventory area used
assert "dam_length_ft" not in dri.tool8_values(attrs)                  # tool 8: one source only
assert dri.missing_notes(v, attrs) == {}

# ---- the reasons
m = dri.missing_notes(v0, attrs, ["no surface area at the top of dam (normal-pool areas are not passed)",
                                   "no surface area sent: the stage-storage curve spans 1.0 ft"])
assert m["surface_area_ac"].startswith("no surface area sent: the stage-storage curve spans 1.0 ft"), m
assert "normal-pool surface area is 40.0 ac" in m["surface_area_ac"], m
assert "no dam length in the inventory" in m["dam_length_ft"], m
assert "normal-pool surface area" not in dri.missing_notes({}, {"inv_area_m2": 0})["surface_area_ac"]

# ---- write: explicit nulls, filled later, never over another tool's value
d1 = os.path.join(work, "DWR-77")
dri.write(d1, v0, {}, "0.17.43", field_notes=dri.source_notes(attrs, []), missing=m)
raw = json.load(open(os.path.join(d1, "dam.json")))
assert raw["fields"]["surface_area_ac"] is None and raw["fields"]["dam_length_ft"] is None, raw["fields"]
assert raw["sources"]["dam_length_ft"]["note"].startswith("not known to the screening: no mapped dam line")
assert raw["sources"]["surface_area_ac"]["version"] == "0.17.43"
rec = dr.load(d1)
assert rec.get("surface_area_ac", "dflt") == "dflt"                    # readers see "no value"
assert rec.set("dam_length_ft", 500.0, "ndbreach", "1.0") and not rec.conflicts   # the Runner fills it
rec.save()
dri.write(d1, v0, {}, "0.17.43", missing=dri.missing_notes(v0, attrs, []))
rec = dr.load(d1)
assert rec.fields["dam_length_ft"] == 500.0 and rec.sources["dam_length_ft"]["tool"] == "ndbreach"
# a later 7b run that has the values replaces its own nulls; the Runner's same length is no conflict
dri.write(d1, v, {}, "0.17.43", field_notes=dri.source_notes(attrs, []), missing=dri.missing_notes(v, attrs))
rec = dr.load(d1)
assert rec.fields["surface_area_ac"] == 55.25 and rec.sources["surface_area_ac"]["tool"] == "ndscreen"
assert rec.fields["dam_length_ft"] == 500.0 and not rec.conflicts, rec.conflicts
# another tool's null (a field it cleared on purpose) keeps its source
rec.fields["storage_acft"] = None; rec.sources["storage_acft"] = {"tool": "engineer", "note": "cleared"}
rec.save()
dri.write(d1, {"dam_id": "DWR-77"}, {}, "0.17.43", missing={"storage_acft": "x"})
assert dr.load(d1).sources["storage_acft"]["tool"] == "engineer"
print("OK (7b dam.json: surface_area_ac / dam_length_ft)")
