# -*- coding: utf-8 -*-
"""Dam hazard classification from the structure, road and rail results.

ND  - North Dakota Dam Safety Standards Policy REG_05.2026 (effective
      04/01/2026), Section 5.2: High if loss of human life is probable;
      otherwise Medium if economic losses / lifeline impacts (habitable
      structures inundated to any degree, roads with AADT >= 500 or US/State
      highways, any railroad, sole-access roads, lifeline facilities);
      otherwise Low. Borderline -> higher class (5.3(f)).
TM 11 - USBR ACER TM 11 (1988) Table 1 by lives in jeopardy (LIJ):
      Low 0, Significant 1-6, High > 6.

The result is a screening classification with the reasons behind it; it
does not replace the engineer's review (ND 5.2.1.1 judgment zone, sole
access roads, lifeline facilities, imminent future development).
"""

import math
import os
import time

from qgis.core import QgsField, QgsFeature, QgsCoordinateReferenceSystem

from .compat import WKB_NOGEOMETRY, FT_STRING
from .constants import (
    ND_POLICY_REF, TM11_REF, ND_HIGH, ND_MEDIUM, ND_LOW, ND_RANK,
    TM11_SIGNIFICANT_MAX_LIJ, PLUGIN_NAME, PLUGIN_VERSION, CLASS_HIGH, ND_AADT_LOW_BELOW,
    CLASS_JUDGMENT, vehicle_curve_available,
)
from .helpers import create_memory_layer, export_layer_to_formats, prepare_output_path
from .structures import OUT_PROBABLE, OUT_JUDGMENT, OUT_ECONOMIC, TYPE_MOBILE


JUDGMENT_RULE_PREFIX = "MEDIUM - "     # 2.22.0: judgment-zone rule reason starts so
PROVISIONAL_PREFIX = "PROVISIONAL - "   # FF.1: formfill skips this reason (not a criterion)


def governing_reason(reasons):
    """FF.1: the first ND reason that is a criterion (the provisional note skipped)."""
    return next((r for r in reasons or [] if not str(r).startswith(PROVISIONAL_PREFIX)), None)


def _raise(level, new):
    # M1: an empty inventory classifies as "Not classified", a value not in
    # ND_RANK. Treat an unknown class as below Low (rank -1) so a criterion
    # added afterwards (flooded lifeline facility, sole-access cut-off) can
    # still raise it, instead of a KeyError here.
    return new if ND_RANK.get(new, -1) > ND_RANK.get(level, -1) else level


def _drop_low_reason(nd):
    """A criterion added after classify_nd raised the class: the "LOW - no
    ..." reason no longer applies."""
    nd["reasons"] = [r for r in nd["reasons"] if not r.startswith("LOW -")]


def classify_nd(structs, roads, rails, judgment_as_probable=False, one_d=False,
                roads_evaluated=True, rail_evaluated=True, structs_evaluated=True):
    """Returns dict(cls, reasons[], judgment[], counts{}). v2.19.0: `structs_evaluated`
    False (no structure layer was given or downloaded) shows the structure rows as
    "not evaluated" instead of "not met" and marks the class incomplete.

    2.22.0 (Nick, 10/30/2026): a habitable structure in the ACER judgment zone makes
    the dam MEDIUM by rule - not probable loss of life, not a provisional High.
    `judgment_as_probable` True is the old conservative option (judgment zone counted
    as probable loss of life, HIGH); it is kept as a non-default option."""
    # An empty inventory is not a Low dam. Low is only after structures,
    # roads and rail were actually tested.
    if not structs and not roads and not rails:
        return {"cls": "Not classified",
                "reasons": ["NOT CLASSIFIED - no structure, road or rail inventory was loaded. "
                            "Low is used only after those layers are tested."],
                "judgment": ["Load the suite folder or a structure layer and run again."],
                "criteria": [], "struct_items": 0,
                "incomplete": [n for n, ok in (("structures", structs_evaluated),
                                               ("roads", roads_evaluated),
                                               ("railroads", rail_evaluated)) if not ok],
                "counts": {"habitable_probable": 0, "habitable_judgment": 0,
                           "habitable_inundated": 0, "lifeline": 0,
                           "roads_high": 0, "roads_medium": 0, "rail": 0, "rail_high": 0,
                           "unverified": 0, "persons_probable": 0, "persons_judgment": 0}}
    cls = ND_LOW
    reasons, judgment = [], []
    hab = [s for s in structs if s["habitable"] == "Yes" and not s.get("dwelling_extra")]
    probable = [s for s in hab if s.get("outcome") == OUT_PROBABLE]
    judg = [s for s in hab if s.get("outcome") == OUT_JUDGMENT]
    econ = [s for s in hab if s.get("outcome") in (OUT_PROBABLE, OUT_JUDGMENT, OUT_ECONOMIC)]
    lifeline = [s for s in econ if s.get("lifeline")]
    unverified = [s for s in structs if s["habitable"] == "Unverified"
                  and not s.get("dwelling_extra")
                  and s.get("outcome") in (OUT_PROBABLE, OUT_JUDGMENT)]

    if probable:
        cls = ND_HIGH
        how = ("depth > 2 ft (1D model rule)" if one_d
               else "high danger zone, Figs 5.1/5.2 (Reclamation 1988)")
        reasons.append("HIGH - {} habitable structure(s) with lethal flood conditions: {} "
                       "[5.2.1.1]".format(len(probable), how))
    if judg:
        msg = ("{} habitable structure(s) in the ACER judgment zone - engineering judgment "
               "required [5.2.1.1]".format(len(judg)))
        if judgment_as_probable:
            cls = _raise(cls, ND_HIGH)
            # 2.21.1 (DWR-4031): this is the judgment-zone setting, not the 5.3(f)
            # borderline count (which can be 0); extra_criteria adds the 5.3(f) citation
            # only when that count is above 0
            reasons.append("HIGH - " + msg + "; counted as probable loss of life "
                           "(conservative judgment-zone setting)")
        else:
            # 2.22.0: the ND judgment-zone rule - MEDIUM, automatically
            cls = _raise(cls, ND_MEDIUM)
            reasons.append(JUDGMENT_RULE_PREFIX + "{} habitable structure(s) in the ACER "
                           "judgment zone - classed MEDIUM by rule (judgment-zone houses "
                           "are Medium, not probable loss of life) [5.2.1.1]".format(
                               len(judg)))
    for r in roads:
        if r["nd_class"] == ND_HIGH:
            cls = ND_HIGH
    road_high = [r for r in roads if r["nd_class"] == ND_HIGH]
    road_med = [r for r in roads if r["nd_class"] == ND_MEDIUM]
    road_low = [r for r in roads if r["nd_class"] == ND_LOW and not r.get("nd_judgment")]
    # 2.18.52: US / State highways, REG_05.2026 5.2.1.2(b): at least Medium whatever the AADT
    road_us_state = [r for r in roads if r.get("road_class") in ("US Highway", "State Highway")]
    if road_high:
        reasons.append("HIGH - {} overtopped road stretch(es) on Interstates or with "
                       "AADT > 2,000 [5.2.1.2]: {}".format(len(road_high), _road_list(road_high)))
    rail_high = [r for r in rails if r["nd_class"] == ND_HIGH]
    if rail_high:
        cls = ND_HIGH
        reasons.append("HIGH - {} inundated rail stretch(es) on passenger lines or freight "
                       "lines with >= 10 trains/day [5.2.1.3]: {}".format(
                           len(rail_high), _rail_list(rail_high)))

    if econ and not probable:
        cls = _raise(cls, ND_MEDIUM)
        reasons.append("MEDIUM - {} habitable structure(s) inundated (economic loss) "
                       "[5.2.2]".format(len(econ)))
    if road_med:
        cls = _raise(cls, ND_MEDIUM)
        reasons.append("MEDIUM - {} overtopped stretch(es) of US/State highways or roads "
                       "with AADT 500-2,000 [5.2.1.2]: {}".format(len(road_med),
                                                                 _road_list(road_med)))
    if rails and not rail_high:
        cls = _raise(cls, ND_MEDIUM)
        reasons.append("MEDIUM - railroad impacted ({} stretch(es)) [5.2.1.3]: {}".format(
            len(rails), _rail_list(rails)))
    if lifeline:
        cls = _raise(cls, ND_MEDIUM)
        reasons.append("MEDIUM (at least) - {} possible lifeline facilities inundated "
                       "(hospital / emergency response) [5.2.2] - verify".format(len(lifeline)))

    # structure items first (they matter most); roads / rail as one line each
    # so a large network cannot push them off the report page
    if unverified:
        judgment.append("{} unverified structure(s) (no habitability data) in high/judgment "
                        "zones - confirm whether any are habitable".format(len(unverified)))
    n_struct_items = len(judgment)

    def examples(items, fmt, n=4):
        names = [fmt(r) for r in items[:n]]
        return "; ".join(names) + ("" if len(items) <= n else " and {} more".format(
            len(items) - n))
    no_aadt_roads = [r for r in roads if r["nd_judgment"]]
    if no_aadt_roads:
        judgment.append("{} overtopped road stretch(es) without AADT - verify traffic: "
                        "{}".format(len(no_aadt_roads), examples(
                            no_aadt_roads, lambda r: "'{}' ({:.0f} ft)".format(
                                r["name"] or "unnamed", r["length_ft"]))))
    bridges = [r for r in roads if r["bridge"]]
    if bridges:
        judgment.append("{} bridge stretch(es) - confirm the deck is overtopped: {}".format(
            len(bridges), examples(bridges, lambda r: "'{}'".format(r["name"] or "unnamed"))))
    rail_unknown = [r for r in rails if r["nd_judgment"]]
    if rail_unknown:
        judgment.append("{} rail stretch(es) with trains/day unknown or unconfirmed (or "
                        "out of service) - high hazard if >= 10/day or passenger service: "
                        "{}".format(
                            len(rail_unknown), examples(rail_unknown, lambda r: "{} {} ({})".format(
                                r["railroad"], r["subdivision"], r.get("nd_basis", "")).strip())))
    if not structs_evaluated:
        judgment.append("No structure layer was evaluated (none given, none downloaded) - the "
                        "habitable-structure criteria (5.2.1.1, 5.2.2) were not applied, so "
                        "the class is a minimum and may be higher")
    if not roads_evaluated:
        judgment.append("No road layer was evaluated - ND road criteria (5.2.1.2) not applied")
    if not rail_evaluated:
        judgment.append("No rail layer was evaluated - ND railroad criteria (5.2.1.3) not "
                        "applied")
    judgment.append("Sole-access roads to occupied locations (5.2.1.2; network screening "
                    "below when a road layer was given), lifeline facilities (5.2.2; screened "
                    "from agency / OSM data when downloaded), imminent future development "
                    "(5.3(b)) and dams in series (5.3(c); screened from the National "
                    "Inventory of Dams when downloaded) must be reviewed by the engineer")
    if not reasons:
        reasons.append("LOW - no habitable structures, roads (AADT >= 500 / highways) or "
                       "railroads affected" if structs_evaluated else
                       "LOW SO FAR - no roads (AADT >= 500 / highways) or railroads affected "
                       "in the layers given; habitable structures were NOT evaluated")

    # ---- criteria table (report / summary): one row per ND 5.2 criterion ----
    def persons(items):
        return sum(int(s.get("persons") or 0) for s in items)

    def met(n, result):
        return result if n else "not met"
    rail_other = [r for r in rails if r["nd_class"] != ND_HIGH]
    no_aadt = [r for r in roads if r.get("nd_judgment")]
    se = structs_evaluated
    crit = [
        ("5.2.1.1", "Probable loss of life - habitable structures {}".format(
            "with depth > 2 ft (1D model)" if one_d else "in the ACER high danger zone"),
         "{} ({} persons)".format(len(probable), persons(probable)) if se else "not evaluated",
         met(probable, ND_HIGH) if se else "?"),
        ("5.2.1.1", "Habitable structures in the ACER judgment zone",
         "not evaluated" if not se else
         "n/a (1D model)" if one_d else "{} ({} persons)".format(len(judg), persons(judg)),
         "?" if not se else
         "not met" if not judg else (ND_HIGH + " (judgment counted)" if judgment_as_probable
                                     else ND_MEDIUM + " (by rule)")),
        ("5.2.1.2", "Overtopped roads - Interstate or AADT > 2,000",
         "{} stretch(es)".format(len(road_high)) if roads_evaluated else "not evaluated",
         met(road_high, ND_HIGH) if roads_evaluated else "?"),
        ("5.2.1.2", "Overtopped roads - US / State highway or AADT 500 - 2,000",
         "{} stretch(es)".format(len(road_med)) if roads_evaluated else "not evaluated",
         met(road_med, ND_MEDIUM) if roads_evaluated else "?"),
        ("5.2.1.2(b)", "US / State highways overtopped - at least Medium whatever the AADT",
         "{} stretch(es)".format(len(road_us_state)) if roads_evaluated else "not evaluated",
         met(road_us_state, ND_MEDIUM + " (at least)") if roads_evaluated else "?"),
        ("5.2.1.2", "Overtopped roads - private or AADT < 500 (no probable loss of life)",
         "{} stretch(es)".format(len(road_low)) if roads_evaluated else "not evaluated",
         met(road_low, ND_LOW) if roads_evaluated else "?"),
        ("5.2.1.2", "Overtopped roads without AADT data",
         "{} stretch(es)".format(len(no_aadt)) if roads_evaluated else "not evaluated",
         met(no_aadt, "Judgment") if roads_evaluated else "?"),
        ("5.2.1.3", "Railroad - passenger line or >= 10 trains/day",
         "{} stretch(es)".format(len(rail_high)) if rail_evaluated else "not evaluated",
         met(rail_high, ND_HIGH) if rail_evaluated else "?"),
        ("5.2.1.3", "Railroad - other lines",
         "{} stretch(es)".format(len(rail_other)) if rail_evaluated else "not evaluated",
         met(rail_other, ND_MEDIUM) if rail_evaluated else "?"),
        ("5.2.2", "Habitable structures inundated (economic loss)",
         "{} ({} persons)".format(len(econ), persons(econ)) if se else "not evaluated",
         met(econ, ND_MEDIUM) if se else "?"),
        ("5.2.2", "Possible lifeline facilities inundated",
         "{}".format(len(lifeline)) if se else "not evaluated",
         met(lifeline, ND_MEDIUM + " (verify)") if se else "?"),
        ("5.2.1.1", "Unverified structures (habitability unknown) in high / judgment zones",
         "{}".format(len(unverified)) if se else "not evaluated",
         met(unverified, "Judgment") if se else "?"),
    ]
    criteria = [{"section": a, "criterion": b, "found": c, "result": d} for a, b, c, d in crit]
    incomplete = [n for n, ok in (("structures", structs_evaluated), ("roads", roads_evaluated),
                                  ("railroads", rail_evaluated)) if not ok]
    return {"cls": cls, "reasons": reasons, "judgment": judgment, "criteria": criteria,
            "struct_items": n_struct_items, "incomplete": incomplete,
            "counts": {
                "habitable_probable": len(probable), "habitable_judgment": len(judg),
                "habitable_inundated": len(econ), "lifeline": len(lifeline),
                "roads_high": len(road_high), "roads_medium": len(road_med),
                "rail": len(rails), "rail_high": len(rail_high),
                "unverified": len(unverified), "persons_probable": persons(probable),
                "persons_judgment": persons(judg)},
            # 2.22.0: which judgment-zone rule classified this ("medium" = ND rule)
            "judgment_rule": "probable" if judgment_as_probable else "medium"}


def tm11_rows(tm11):
    """ACER TM 11 Table 1 rows for the report / summary:
    [(item, value)]."""
    g = tm11.get
    inc = "included" if g("judgment_included", True) else "not included"
    return [
        ("Persons in habitable structures - high danger zone",
         "{:,.0f}".format(g("lij_structures_high") or 0)),
        ("Persons in habitable structures - judgment zone ({})".format(inc),
         "{:,.0f}".format(g("lij_structures_judgment") or 0)),
        ("Motorists - high danger zone (estimate)",
         "{:.1f}".format(g("lij_vehicles_high") or 0)),
        ("Motorists - judgment zone ({})".format(inc),
         "{:.1f}".format(g("lij_vehicles_judgment") or 0)),
        ("Lives in jeopardy (LIJ)", "{:.1f}".format(g("lij") or 0)),
        ("Table 1: Low 0 / Significant 1 - 6 / High > 6", str(g("cls", "")).upper()),
        ("Without judgment-zone items (TM 11 Table 1)", str(g("cls_without_judgment", ""))),
    ]


def classify_tm11(structs, roads, judgment_as_probable=True):
    """ACER TM 11 Table 1 by lives in jeopardy."""
    hab = [s for s in structs if s["habitable"] == "Yes" and not s.get("dwelling_extra")]
    lij_high = sum(s["persons"] for s in hab if s.get("zone") == CLASS_HIGH)
    lij_judg = sum(s["persons"] for s in hab if s.get("zone") == CLASS_JUDGMENT)
    veh = sum(r["persons"] or 0 for r in roads if r.get("vehicle_zone") == CLASS_HIGH)
    veh_j = sum(r["persons"] or 0 for r in roads if r.get("vehicle_zone") == CLASS_JUDGMENT)
    lij = lij_high + veh + (lij_judg + veh_j if judgment_as_probable else 0)
    lij_round = int(math.ceil(lij - 1e-9))

    def table1(n):
        return "Low" if n <= 0 else ("Significant" if n <= TM11_SIGNIFICANT_MAX_LIJ else "High")
    notes = []
    if not vehicle_curve_available():
        notes.append("Motorists not counted - ACER Figure 4 (passenger vehicle) curve not "
                     "configured")
    mobiles = sum(1 for s in hab if s["acer_type"] == TYPE_MOBILE and s.get("zone"))
    return {"cls": table1(lij_round), "lij": lij, "lij_structures_high": lij_high,
            "lij_structures_judgment": lij_judg, "lij_vehicles_high": veh,
            "lij_vehicles_judgment": veh_j, "judgment_included": judgment_as_probable,
            "cls_without_judgment": table1(int(math.ceil(lij_high + veh - 1e-9))),
            "mobile_homes": mobiles, "notes": notes}


def _road_list(rs, n=4):
    names = []
    for r in rs[:n]:
        a = "AADT {:,.0f}{}".format(r["aadt"], " ({})".format(r["aadt_year"])
                                   if r["aadt_year"] else "") if r["aadt"] else "no AADT"
        names.append("{} [{}, {}]".format(r["name"] or "unnamed", r["road_class"], a))
    more = "" if len(rs) <= n else " and {} more".format(len(rs) - n)
    return "; ".join(names) + more


def _rail_list(rs, n=3):
    out = []
    for r in rs[:n]:
        t = "{:.0f} trains/day".format(r["trains"]) if r["trains"] is not None else "trains ?"
        out.append("{} {} [{}{}]".format(r["railroad"], r["subdivision"], t,
                                         ", passenger" if r["passenger"] else "").strip())
    return "; ".join(out) + ("" if len(rs) <= n else " and {} more".format(len(rs) - n))


def zone_table(structs):
    """Counts of habitable structures by ACER type and zone."""
    rows = {}
    for s in structs:
        if s["habitable"] != "Yes" or not s.get("zone"):
            continue
        key = (s["acer_type"], s["zone"])
        rows[key] = rows.get(key, 0) + 1
    return rows


def facility_criteria(nd, res, cfg, total=False):
    """v2.2 (in place): other dams in the inundated area (dams in series,
    5.3(c) - judgment) and lifeline / critical facilities from agency / OSM
    data (5.2.2 - at least Medium unless switched off). `total`: the result
    without the baseline (every flooded facility counts)."""
    from . import facilities as FAC
    crit = nd.setdefault("criteria", [])
    recs = res.get("facility_records")
    ev = res.get("fac_eval") or {}
    charged = None if total else res.get("fac_charged")
    dams_on = res.get("dams_evaluated", False)
    life_on = res.get("lifelines_evaluated", False)
    at = nd.get("struct_items", 0)
    # ---- dams in series ----
    dams = FAC.flooded(recs or [], ev, charged, kind=FAC.KIND_DAM, charged_only=False)
    high = [d for d in dams if FAC.is_high_hazard(d)]
    crit.append({"section": "5.3(c)", "criterion": "Dams in series - other dams in the "
                 "inundated area (National Inventory of Dams)",
                 "found": ("{} (High hazard: {})".format(len(dams), len(high))
                           if dams_on else "not evaluated"),
                 "result": ("Judgment" if dams else "not met") if dams_on else "?"})
    if dams:
        nd["judgment"].insert(at, "{} other dam(s) in the inundated area - dams in series "
                              "(5.3(c)): check whether the breach flood overtops or fails "
                              "them and what their failure adds: {}".format(
                                  len(dams), FAC.describe(dams, ev, dam=True)))
        at += 1
    # ---- lifelines ----
    wet = FAC.flooded(recs or [], ev, charged, kind=FAC.KIND_LIFELINE)
    life = [r for r in wet if r.get("lifeline")]
    schools = [r for r in wet if not r.get("lifeline") and r.get("category") in FAC.SCHOOLS]
    care = [r for r in wet if not r.get("lifeline") and r.get("category") not in FAC.SCHOOLS]
    as_medium = cfg.get("lifeline_as_medium", True)
    crit.append({"section": "5.2.2", "criterion": "Lifeline facilities inundated - hospital, "
                 "emergency response, utilities (agency / OSM data)",
                 "found": "{}".format(len(life)) if life_on else "not evaluated",
                 "result": ((ND_MEDIUM + " (verify)" if as_medium else "Judgment")
                            if life else "not met") if life_on else "?"})
    if life:
        msg = "{} lifeline facilit(ies) inundated [5.2.2]: {}".format(
            len(life), FAC.describe(life, ev))
        if as_medium:
            nd["cls"] = _raise(nd["cls"], ND_MEDIUM)
            _drop_low_reason(nd)
            nd["reasons"].append("MEDIUM (at least) - " + msg + " - verify")
        else:
            nd["judgment"].insert(at, msg + " - verify")
            at += 1
    # ---- bridges / culverts / crossings ----
    cr_on = res.get("crossings_evaluated", False)
    cross = sorted(FAC.flooded(recs or [], ev, charged, kind=FAC.KIND_CROSSING),
                   key=FAC.crossing_priority)
    busy = [r for r in cross if (r.get("adt") or 0) >= ND_AADT_LOW_BELOW]
    crit.append({"section": "5.2.1.2", "criterion": "Bridges, culverts, low-water crossings "
                 "and rail bridges in the inundated area (NBI / OSM)",
                 "found": ("{} (ADT >= {:,}: {})".format(len(cross), ND_AADT_LOW_BELOW,
                                                         len(busy))
                           if cr_on else "not evaluated"),
                 "result": ("Judgment" if cross else "not met") if cr_on else "?"})
    if cross:
        nd["judgment"].append("{} bridge(s) / culvert(s) / crossing(s) in the inundated area "
                              "- confirm whether the road or track is overtopped or washed "
                              "out (ADT >= {:,}: {}): {}".format(
                                  len(cross), ND_AADT_LOW_BELOW, len(busy),
                                  FAC.describe(cross, ev)))
    if care:
        nd["judgment"].insert(at, "{} care facilit(ies) (nursing home / assisted living) "
                              "inundated - vulnerable occupants; confirm they are in the "
                              "structure inventory: {}".format(len(care),
                                                               FAC.describe(care, ev)))
        at += 1
    if schools:
        nd["judgment"].insert(at, "{} school(s) / child-care facilit(ies) inundated - "
                              "vulnerable occupants (daytime); confirm they are in the "
                              "structure inventory and first on the EAP notification list: "
                              "{}".format(len(schools), FAC.describe(schools, ev)))
        at += 1
    nd["struct_items"] = at
    return nd


def extra_criteria(nd, structs, res, cfg, total=False):
    """v2.0 additions to the ND result (in place): sole-access roads
    (5.2.1.2), borderline structures (5.3(f)), economic-loss figures; v2.2:
    dams in series (5.3(c)) and lifeline facilities (5.2.2)."""
    from .structures import OUT_DRY, OUT_TRACE
    # (the incremental view has already cleared cut-offs the breach does
    # not cause; the "without baseline" class must count them all)
    cut = [s for s in structs if s.get("cut_off") and s.get("habitable") == "Yes"
           and s.get("outcome") in (None, OUT_DRY)]
    crit = nd.setdefault("criteria", [])
    access_on = res.get("access") is not None if isinstance(res, dict) else True
    persons = sum(s.get("persons") or 0 for s in cut)
    if cut:
        msg = ("{} dry habitable structure(s) ({} persons) lose all mapped road access - "
               "sole-access roads overtopped [5.2.1.2]".format(len(cut), persons))
        if cfg.get("access_as_medium", True):
            nd["cls"] = _raise(nd["cls"], ND_MEDIUM)
            _drop_low_reason(nd)
            nd["reasons"].append("MEDIUM (at least) - " + msg + " (network screening)")
        else:
            at = nd.get("struct_items", 0)
            nd["struct_items"] = at + 1
            nd["judgment"].insert(at, msg + " - verify")
    crit.insert(5 if len(crit) >= 5 else len(crit), {
        "section": "5.2.1.2", "criterion": "Sole-access roads - dry habitable structures "
        "cut off (network screening)",
        "found": "{} ({} persons)".format(len(cut), persons) if access_on else "not evaluated",
        "result": ("not met" if not cut else ND_MEDIUM if cfg.get("access_as_medium", True)
                   else "Judgment") if access_on else "?"})
    # v2.5: campgrounds / RV parks (overnight recreation) - counted like any
    # habitable structure above; their occupancy is seasonal and uncertain
    camps = [s for s in structs if s.get("camp") and s.get("habitable") == "Yes"
             and s.get("outcome") not in (None, OUT_DRY)]
    if camps or any(s.get("camp") for s in structs):
        crit.append({"section": "5.2.1.1", "criterion": "Campgrounds / RV parks inundated "
                     "(overnight recreation, OpenStreetMap)",
                     "found": "{} ({} persons)".format(len(camps), sum(
                         int(s.get("persons") or 0) for s in camps)),
                     "result": "not met" if not camps else "counted (verify)"})
    if camps:
        at = nd.get("struct_items", 0)
        nd["struct_items"] = at + 1
        nd["judgment"].insert(at, "{} campground(s) / RV park(s) inundated - overnight "
                              "recreation (5.2.1.1); occupancy is seasonal: verify the "
                              "number of sites, the season and the ACER type: {}".format(
                                  len(camps), "; ".join(
                                      "{} ({}, {})".format(
                                          (s["camp"].get("name") or "unnamed"),
                                          s.get("outcome"), "{:.1f} ft".format(
                                              s["max_depth"]) if s.get("max_depth")
                                          else "-") for s in camps[:4]) +
                                  ("" if len(camps) <= 4 else " and {} more".format(
                                      len(camps) - 4))))
    near = [s for s in structs if s.get("near") and s.get("habitable") == "Yes"]
    crit.append({"section": "5.3(f)", "criterion": "Borderline structures within {:g} ft of "
                 "the next boundary ({})".format(cfg.get("near_ft", 0.5),
                                                 "1D 2-ft rule" if cfg.get("one_d")
                                                 else "ACER curve"),
                 "found": "{}".format(len(near)),
                 "result": "not met" if not near else "counted higher"
                 if cfg.get("borderline_higher") else "Judgment"})
    if near:
        for i, r in enumerate(nd.get("reasons") or []):     # 2.21.1: only when counted
            if r.endswith("(conservative judgment-zone setting)"):
                nd["reasons"][i] = r[:-1] + "; borderline rule 5.3(f))"
        hi = sum(1 for s in near if s["near"] == "High")
        at = nd.get("struct_items", 0)
        nd["struct_items"] = at + 1
        nd["judgment"].insert(
            at,
            "{} habitable structure(s) within {:g} ft below the next boundary ({} below "
            "High) - borderline, ND 5.3(f){}".format(
                len(near), cfg.get("near_ft", 0.5), hi,
                ": already counted in the higher zone" if cfg.get("borderline_higher")
                else ""))
    wet = [s for s in structs if s.get("outcome") not in (None, OUT_DRY, OUT_TRACE)
           and s.get("habitable") == "Yes" and not s.get("dwelling_extra")]   # as totals
    dmg = sum(s.get("damage_usd") or 0 for s in wet)
    if dmg:
        for i, r in enumerate(nd["reasons"]):
            if "economic loss" in r:
                nd["reasons"][i] = r + "; estimated residential damage ${:,.0f}".format(dmg)
                break
        for c in crit:
            if c["criterion"].startswith("Habitable structures inundated"):
                c["found"] += "; damage ${:,.0f}".format(dmg)
    # 2.18.53: structure-inventory checks (FEMA scene offsets, points moved from a wet cell
    # onto a dry outline) - judgment items, they never change the class themselves
    try:
        from .structures import inventory_judgment
        inv = inventory_judgment(structs)
    except Exception:                                   # noqa - never fails the result
        inv = []
    geo = (cfg or {}).get("building_geometry_fallback") if isinstance(cfg, dict) else None
    if geo and structs:                                 # 2.18.53 (review): FEMA-only geometry
        inv = list(inv) + [geo]
    img = (cfg or {}).get("imagery_dates_fallback") if isinstance(cfg, dict) else None
    if img and structs:                                 # 2.18.54 review 3: assumed date used
        inv = list(inv) + [img]
    for msg in inv:
        at = nd.get("struct_items", 0)
        nd["judgment"].insert(at, msg)
        nd["struct_items"] = at + 1
    if isinstance(res, dict):
        b = res.get("boundary")
        if b and b.get("flagged"):
            from .boundary import judgment_text
            at = nd.get("struct_items", 0)
            nd["judgment"].insert(at, judgment_text(b))
            nd["struct_items"] = at + 1
        facility_criteria(nd, res, cfg, total=total)
    # A High that exists only because judgment-zone dwellings clear the ACER
    # line by less than a foot is inside lidar and cell-size error.
    counts = nd.get("counts") or {}
    judgment_only = (nd["cls"] == ND_HIGH and counts.get("habitable_judgment")
                     and not counts.get("habitable_probable")
                     and not counts.get("roads_high") and not counts.get("rail_high"))
    if judgment_only:
        raw_margins = [float(s["margin_judg"]) for s in structs
                       if s.get("habitable") == "Yes" and not s.get("dwelling_extra")
                       and s.get("outcome") == OUT_JUDGMENT
                       and s.get("margin_judg") is not None]
        margins = [abs(m) for m in raw_margins]
        # Provisional only when every judgment-zone dwelling is within 1 ft of the
        # line. The class rests on the most-exposed house, not the least.
        if margins and max(margins) < 1.0:
            nd["provisional"] = True
            nd["provisional_margin_ft"] = max(margins)
            # CL.2: raised-to-Judgment (positive margin) are still below the line
            if raw_margins and max(raw_margins) <= 0:
                where = "{:.2f} ft past the ACER judgment line".format(max(margins))
            else:
                where = "within {:.2f} ft of the ACER judgment line".format(max(margins))
            # 2.21.1 (DWR-4031): cite the 5.3(f) borderline rule only when it counted
            # something; with 0 borderline structures High comes from the judgment zone
            if near:
                why = ("borderline goes to the higher class, 5.3(f): {} borderline "
                       "structure(s)".format(len(near)))
            else:
                why = "the judgment zone is counted as probable loss of life"
            msg = ("HIGH is provisional: the most-exposed judgment-zone dwelling is only "
                   "{}, inside the vertical error of a "
                   "lidar crest and a coarse grid. The class stays High ({}); confirm the "
                   "buildings are habitable and the depths before the class is "
                   "final.".format(where, why))
            nd["judgment"].insert(0, msg)
            nd["reasons"].insert(0, PROVISIONAL_PREFIX + msg)
    result = "HIGH (provisional)" if nd.get("provisional") else nd["cls"].upper()
    crit.append({"section": "Result", "criterion": "ND hazard classification (screening)",
                 "found": "", "result": result})
    return nd


def nd_class_without_judgment(structs, roads, rails, res, cfg, total=False, **kw):
    """2.21.1 (DWR-4031): the ND class with the ACER judgment-zone dwellings NOT counted
    as probable loss of life (they still count as inundated, 5.2.2) - the ND counterpart
    of TM 11's "without judgment-zone items". Same inputs as classify_nd + extra_criteria;
    nothing passed in is changed."""
    kw = dict(kw)
    kw["judgment_as_probable"] = False
    nd0 = classify_nd(structs, roads, rails, **kw)
    if nd0.get("cls") in ND_RANK:
        extra_criteria(nd0, structs, res if res is not None else {}, dict(cfg or {}),
                       total=total)
    return nd0["cls"]


def without_judgment_text(nd, tm11):
    """The summary's "Without judgment-zone items" value, both classes labelled."""
    t = "{} (TM 11 Table 1)".format(tm11.get("cls_without_judgment", ""))
    ndw = nd.get("cls_without_judgment")
    if ndw:
        t += "; ND class: {}".format(str(ndw).upper())
    return t


def lone_point_judgment(nd, tm11, structs, roads, rails, **kw):
    """2.18.54 (Docs & QA, H1): when the ND or TM 11 class rests on 911 address points with
    no building (counted habitable by the ND site address rule - H1 default), say so in the
    judgment items and the TM 11 notes. The class is not changed. Returns the message."""
    from .structures import lone_911_counted
    lone = lone_911_counted(structs)
    if not lone:
        return ""
    ids = {id(r) for r in lone}
    rest = [r for r in structs if id(r) not in ids]
    # (raw structure classes - the access / facility raises of extra_criteria do not
    # depend on these points)
    kw = dict(kw)
    tm11_j = kw.pop("tm11_judgment", True)     # 2.22.0: TM 11 has its own setting
    nd1 = classify_nd(structs, roads, rails, **kw)
    nd2 = classify_nd(rest, roads, rails, **kw)
    tm2 = classify_tm11(rest, roads, judgment_as_probable=tm11_j)
    nd_rests = nd2["cls"] != nd1["cls"] and ND_RANK.get(nd2["cls"], -1) < \
        ND_RANK.get(nd["cls"], -1)
    if not nd_rests and tm2["cls"] == tm11["cls"]:
        # 2.18.54 review (Docs & QA #134 nit 8): a lighter, always-on line - how much of
        # the LIJ comes from these points, even when the class does not depend on them
        extra = (tm11.get("lij") or 0.0) - (tm2.get("lij") or 0.0)
        if extra <= 1e-9:
            return ""
        parcel = sum(1 for r in lone if any((s.get("placement") or "") == "parcel"
                                            for s in r.get("sources", ())))
        persons = sum(float(r.get("persons") or 0) for r in lone)
        msg = ("LIJ {:.1f} includes {:.1f} (zone-weighted) from {} 911 address point(s) with no "
               "building ({:g} persons; H1 default{}): {}; the class is the same without them - "
               "confirm on imagery".format(
                   tm11.get("lij") or 0.0, extra, len(lone), persons,
                   "; {} at Placement=Parcel".format(parcel) if parcel else "",
                   "; ".join("structure {} ({}, {:.1f} ft)".format(
                       r["id"], (r["sources"][0].get("addr_text") or "no address"),
                       r.get("max_depth") or 0.0) for r in lone[:4])
                   + ("" if len(lone) <= 4 else " and {} more".format(len(lone) - 4))))
        nd["judgment"].append(msg)
        tm11.setdefault("notes", []).append(msg)
        return msg
    what = []
    if nd_rests:
        what.append("ND {} (without them {})".format(nd["cls"], nd2["cls"]))
    if tm2["cls"] != tm11["cls"]:
        what.append("TM 11 {} (without them {}, LIJ {:.1f})".format(
            tm11["cls"], tm2["cls"], tm2["lij"]))
    msg = ("The class rests on {} 911 address point(s) with no building (H1 default - "
           "counted habitable by the ND site address rule): {} - {}; confirm on imagery "
           "that these are dwellings".format(
               len(lone), " and ".join(what), "; ".join(
                   "structure {} ({}, {}, {:.1f} ft)".format(
                       r["id"], (r["sources"][0].get("addr_text") or "no address"),
                       r.get("outcome"), r.get("max_depth") or 0.0) for r in lone[:4])
               + ("" if len(lone) <= 4 else " and {} more".format(len(lone) - 4))))
    at = nd.get("struct_items", 0)
    nd["judgment"].insert(at, msg)
    nd["struct_items"] = at + 1
    tm11.setdefault("notes", []).append(msg)
    return msg


def ms_rule_judgment(nd, tm11, structs, roads, rails, clip_fn=None, **kw):
    """2.18.54 review 2/3 (Docs & QA should-fix; Nick): when the 300 ft no-building rule
    dropped or kept lone 911 points, a judgment item says how many and the imagery dates
    used (NAIP 2025 flight dates, or the assumed fallback and why); every point it dropped
    that is wet in a High or Judgment zone is named, with the ND / TM 11 class and LIJ it
    would give if counted, and so is every one it kept as possibly newer than the imagery.
    Lone 911 points that are possibly misplaced (far from their address on their own street)
    and wet get their own item. clip_fn(rec) -> a NAIP 2025 clip path ('' = none). The class
    is not changed. Returns the messages joined ('' when there is nothing to say)."""
    from .structures import (ms_rule_dropped, ms_rule_counted_copy, ms_rule_source_line,
                             ms_rule_kept, lone_misplaced, OUT_DRY, OUT_TRACE)
    from .constants import CLASS_HIGH, CLASS_JUDGMENT
    from .lone911 import misplaced_text, MISPLACED_911_M
    misplaced_m = kw.pop("misplaced_m", None) or MISPLACED_911_M
    tm11_j = kw.pop("tm11_judgment", True)     # 2.22.0: TM 11 has its own setting

    def wet(r):
        return r.get("outcome") not in (None, OUT_DRY, OUT_TRACE)

    def hz(r):
        return wet(r) and r.get("zone") in (CLASS_HIGH, CLASS_JUDGMENT)

    def clip(r):
        try:
            c = clip_fn(r) if clip_fn else ""
        except Exception:                               # noqa - never fails the result
            c = ""
        return "; NAIP 2025 clip {}".format(c) if c else ""

    def name(r, d):
        return "structure {} ({}, {:.1f} ft, {} zone, 911 record {}, imagery {}{}{})".format(
            r["id"], (r["sources"][0].get("addr_text") or "no address"),
            r.get("max_depth") or 0.0,
            "High" if r.get("zone") == CLASS_HIGH else "Judgment",
            d.get("upd") or "undated", d.get("img") or "?",
            "; REVIEW: possibly misplaced, a Microsoft footprint is near its address location"
            if d.get("review") else "", clip(r))

    msgs = []
    dropped = ms_rule_dropped(structs)
    kept = ms_rule_kept(structs)
    line = ms_rule_source_line(structs)
    if line:
        msg = line
        wd = [r for r in dropped if hz(r)]
        if wd:
            ids = {id(r) for r in wd}
            alt = [ms_rule_counted_copy(r) if id(r) in ids else r for r in structs]
            nd2 = classify_nd(alt, roads, rails, **kw)
            tm2 = classify_tm11(alt, roads, judgment_as_probable=tm11_j)
            msg += (" Wet in a High or Judgment zone and not counted: {}. Counted, they would "
                    "give ND {} / TM 11 {} (LIJ {:.1f}); now ND {} / TM 11 {} (LIJ {:.1f}) - "
                    "confirm on imagery that no dwelling stands there.".format(
                        "; ".join(name(r, r["ms_dropped"]) for r in wd),
                        nd2["cls"], tm2["cls"], tm2.get("lij") or 0.0, nd["cls"], tm11["cls"],
                        tm11.get("lij") or 0.0))
        wk = [r for r in kept if hz(r)]
        if wk:
            msg += (" Wet in a High or Judgment zone and counted only because the 911 record "
                    "is newer than the imagery: {} - confirm the dwelling.".format(
                        "; ".join(name(r, r["ms_kept_newer"]) for r in wk)))
        msgs.append(msg)
    mis = [r for r in lone_misplaced(structs) if wet(r)]
    if mis:
        mis.sort(key=lambda r: -(r["misplaced"].get("dist_m") or 0))
        shown = mis[:15]
        msgs.append("Lone 911 points possibly misplaced (wet; more than {:.0f} m from the "
                    "location of their address on their own street, NG9-1-1 centerlines): "
                    "{}{} - the depth was read at the 911 point, not at the address; "
                    "verify.".format(
                        float(misplaced_m),
                        "; ".join("structure {} ({}, {:.1f} ft, {}): {}{}{}".format(
                            r["id"], r["sources"][0].get("addr_text") or "no address",
                            r.get("max_depth") or 0.0,
                            "counted" if r.get("habitable") == "Yes" else "not counted",
                            misplaced_text(r["misplaced"]),
                            "; a Microsoft footprint is within 300 ft of the expected location"
                            if r["misplaced"].get("ms_near_expected") else "",
                            clip(r) if hz(r) else "")
                            for r in shown),
                        "; and {} more (Notes column)".format(len(mis) - len(shown))
                        if len(mis) > len(shown) else ""))
    for msg in msgs:
        at = nd.get("struct_items", 0)
        nd["judgment"].insert(at, msg)
        nd["struct_items"] = at + 1
        tm11.setdefault("notes", []).append(msg)
    return " ".join(msgs)

def criteria_text(nd):
    """The criteria table as fixed-width text lines."""
    out = ["  {:<8} {:<74} {:<30} {}".format("Section", "Criterion", "Found", "Result")]
    for c in nd.get("criteria", []):
        out.append("  {:<8} {:<74} {:<30} {}".format(c["section"], c["criterion"][:74],
                                                    c["found"][:30], c["result"]))
    return out


def summary_lines(nd, tm11, structs, roads, rails, dam_name, sources, one_d, extra=None):
    zt = zone_table(structs)
    L = []
    L.append("{} v{} - Hazard Classification Summary".format(PLUGIN_NAME, PLUGIN_VERSION))
    L.append("Dam: {}    Generated: {}".format(dam_name, time.strftime("%Y-%m-%d %H:%M")))
    L.append("Hydraulic model: {}".format("1D (depth > 2 ft rule)" if one_d
                                          else "2D (depth-velocity curves)"))
    L.append("")
    unv_flood = sum(1 for s in structs if s.get("habitable") == "Unverified" and s.get("zone"))
    unv_hi = int((nd.get("counts") or {}).get("unverified") or 0)
    bits = []
    if unv_flood:
        bits.append("{} unverified in the flood (not in the class until chart sign-off)".format(unv_flood))
    if unv_hi:
        bits.append("{} unverified in a high or judgment zone".format(unv_hi))
    cut_off = (extra or {}).get("cut_off") or (extra or {}).get("flood_cut_off")
    if cut_off:
        if isinstance(cut_off, dict):
            reason = cut_off.get("reason") or "cut off"
            bits.append("incomplete: flood cut off ({})".format(reason))
        else:
            bits.append("incomplete: flood still advancing at the time cap")
    suffix = ("  |  " + "; ".join(bits)) if bits else ""
    L.append("ND HAZARD CLASSIFICATION (screening): {}{}".format(nd["cls"].upper(), suffix))
    L.append("  Basis: " + ND_POLICY_REF)
    for r in nd["reasons"]:
        L.append("  - " + r)
    if nd.get("criteria"):
        L.append("")
        L.append("ND STANDARDS SECTION 5.2 CRITERIA")
        L.extend(criteria_text(nd))
    L.append("")
    L.append("ACER TM 11 CLASSIFICATION: {}  (lives in jeopardy = {:.1f})".format(
        tm11["cls"].upper(), tm11["lij"]))
    L.append("  Basis: {} Table 1 (Low 0, Significant 1-6, High > 6)".format(TM11_REF))
    L.append("  Structures, high danger zone: {} persons; judgment zone: {} persons ({})".format(
        tm11["lij_structures_high"], tm11["lij_structures_judgment"],
        "included" if tm11["judgment_included"] else "not included"))
    L.append("  Motorists (estimate): high {:.1f}, judgment {:.1f}".format(
        tm11["lij_vehicles_high"], tm11["lij_vehicles_judgment"]))
    L.append("  Without judgment-zone items: {}".format(without_judgment_text(nd, tm11)))
    for n in tm11["notes"]:
        L.append("  Note: " + n)
    L.append("")
    L.append("HABITABLE STRUCTURES BY ACER ZONE")
    for t in ("Habitable", "House on foundation", "Mobile home"):
        L.append("  {:<20} High {:>4}   Judgment {:>4}   Low (wet) {:>4}".format(
            t, zt.get((t, 1), 0), zt.get((t, 2), 0), zt.get((t, 3), 0)))
    unv = sum(1 for s in structs if s["habitable"] == "Unverified" and s.get("zone"))
    L.append("  Unverified structures in flooded area: {}".format(unv))
    L.append("")
    L.append("ROADS: {} overtopped stretch(es) - High {}, Medium {}, Low {}".format(
        len(roads), sum(r["nd_class"] == ND_HIGH for r in roads),
        sum(r["nd_class"] == ND_MEDIUM for r in roads),
        sum(r["nd_class"] == ND_LOW for r in roads)))
    L.append("RAIL: {} inundated stretch(es)".format(len(rails)))
    L.append("")
    extra = extra or {}
    for key, title in (("proposed", "PROPOSED DAM"),
                       ("scenarios", "SCENARIOS"), ("margin", "CLASSIFICATION MARGIN (5.3(f))"),
                       ("nid", "NATIONAL INVENTORY OF DAMS - THIS DAM"),
                       ("changes", "CHANGES SINCE THE LAST RUN"),
                       ("method", "ACER ZONE METHOD"),
                       ("model_extent", "MODEL EXTENT CHECK"),
                       ("datum", "VERTICAL DATUM OF THE DAM ELEVATIONS"),
                       ("consequences", "CONSEQUENCES (screening)"),
                       ("hcom", "DSS-WISE HCOM CROSS-CHECK (population at risk)"),
                       ("popsource", "SECOND POPULATION SOURCE (census / gridded)"),
                       ("evacuation", "EVACUATION TIMING (screening)"),
                       ("sensitivity", "SENSITIVITY (depth / velocity scaling)"),
                       ("conseq_sensitivity", "CONSEQUENCE SENSITIVITY (screening range)"),
                       ("termination", "DOWNSTREAM TERMINATION OF THE INCREMENTAL HAZARD"),
                       ("access", "SOLE-ACCESS ROADS"),
                       ("dams", "DAMS IN SERIES (5.3(c))"),
                       ("lifelines", "LIFELINE / CRITICAL FACILITIES (5.2.2)"),
                       ("crossings", "BRIDGES, CULVERTS AND CROSSINGS"),
                       ("future", "FUTURE DEVELOPMENT (5.3(b))"),
                       ("eap", "EMERGENCY ACTION PLAN OUTPUTS"),
                       ("vintage", "DATA VINTAGE"),
                       ("qa", "INVENTORY COMPLETENESS")):
        if extra.get(key):
            L.append(title)
            for x in extra[key]:
                L.append("  - " + x)
            L.append("")
    L.append("ITEMS REQUIRING ENGINEERING JUDGMENT")
    for j in nd["judgment"]:
        L.append("  - " + j)
    L.append("")
    L.append("DATA SOURCES")
    for s in sources:
        L.append("  - " + s)
    L.append("")
    L.append("This is a screening result produced from the listed data. The engineer "
             "remains responsible for the hazard classification.")
    return L


def write_summary(output_folder, lines, nd, tm11, transform_ctx):
    """Write Hazard_Classification_Summary.txt and a one-table XLSX/CSV."""
    txt = prepare_output_path(os.path.join(output_folder, "Hazard_Classification_Summary.txt"))
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    lyr = create_memory_layer("Summary", WKB_NOGEOMETRY, QgsCoordinateReferenceSystem(),
                              [QgsField("Item", FT_STRING), QgsField("Value", FT_STRING)])
    rows = [("ND classification (screening)", nd["cls"]), ("ND basis", ND_POLICY_REF)]
    rows += [("ND reason", r) for r in nd["reasons"]]
    rows += [("ND {} {}".format(c["section"], c["criterion"]),
              "{} -> {}".format(c["found"], c["result"]) if c["found"] else c["result"])
             for c in nd.get("criteria", [])]
    # 2.18.51: REG_05.2026 classes are High / Medium / Low; the ACER TM 11 Table 1 class
    # (Low / Significant / High) is a reference, said so in the item, never the ND class
    rows += [("TM 11 classification (ACER TM 11 Table 1 reference, not the ND class)",
              tm11["cls"]),
             ("TM 11 lives in jeopardy", "{:.1f}".format(tm11["lij"])),
             ("TM 11 without judgment-zone items", tm11["cls_without_judgment"])]
    if nd.get("cls_without_judgment"):                  # 2.21.1 (DWR-4031)
        rows.append(("ND classification without judgment-zone items",
                     nd["cls_without_judgment"]))
    rows += [("Judgment item", j) for j in nd["judgment"]]
    feats = []
    for k, v in rows:
        f = QgsFeature(lyr.fields())
        f.setAttributes([k, v])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    # v2.3.1: only csv/xlsx - no orphan GPKG left in outputs
    _ok, _res, paths = export_layer_to_formats(lyr, output_folder,
                                               "Hazard_Classification_Summary", transform_ctx,
                                               formats=("csv", "xlsx"))
    paths["txt"] = txt
    return paths

