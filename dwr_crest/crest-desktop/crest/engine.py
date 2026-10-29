"""Desk screening engine. Port of src/lib/crest/engine.ts — same formulas."""

from __future__ import annotations

import math

FT = 0.3048
ACFT_TO_M3 = 1233.48184
CFS_PER_CMS = 35.3146667
G = 9.80665
TIE_BAND = 100

STEPS = [
    {"id": "record", "label": "Record"},
    {"id": "breach", "label": "Breach"},
    {"id": "flood", "label": "Flood"},
    {"id": "screen", "label": "Screen"},
    {"id": "report", "label": "Report"},
]

ENGINE_DECAY = {
    "lisflood": {"k": 1.85, "label": "LISFLOOD-style spread"},
    "anuga": {"k": 2.25, "label": "ANUGA-style mesh"},
    "routing": {"k": 3.15, "label": "1D routing"},
}

METHOD_LABEL = {
    "froehlich": "Froehlich 2008",
    "macdonald": "MacDonald & Langridge-Monopolis",
    "tr60": "TR-60 screening width",
}


def clamp(n: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, n))


def finite(n, fallback: float) -> float:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return fallback
    return v if math.isfinite(v) else fallback


def is_local_elev(ft: float) -> bool:
    return math.isfinite(ft) and 0 <= ft <= 300


def fmt_ft(n: float) -> str:
    return f"{n:,.1f}"


def tie_value(label, stated, offset, crest_navd, local_record):
    if not math.isfinite(stated):
        return {"navd": None, "warning": None}
    if local_record and offset is not None:
        navd = stated + offset
        if abs(navd - crest_navd) > TIE_BAND and label != "Top of dam":
            return {
                "navd": None,
                "warning": (
                    f"{label} {fmt_ft(stated)} ft ties to {fmt_ft(navd)} ft, "
                    f"{fmt_ft(abs(navd - crest_navd))} ft from the DEM crest. Left out (100 ft guard)."
                ),
            }
        return {"navd": navd, "warning": None}
    if not local_record and is_local_elev(stated):
        return {
            "navd": None,
            "warning": f"{label} {fmt_ft(stated)} ft is in the 0–300 ft band on a real-datum record. Not used as given.",
        }
    if not local_record and offset is not None:
        return {"navd": stated + offset, "warning": None}
    return {"navd": stated, "warning": None}


def froehlich(volume_m3, height_m, mode):
    ko = 1.3 if mode == "overtopping" else 1.0
    width_m = 0.27 * ko * volume_m3**0.32 * height_m**0.04
    time_s = 63.2 * math.sqrt(volume_m3 / (G * height_m * height_m))
    peak_cms = 0.607 * volume_m3**0.295 * height_m**1.24
    return width_m, time_s, peak_cms


def macdonald(storage_acft, height_ft, length_ft):
    eroded = 0.0261 * (storage_acft * height_ft) ** 0.769
    through_ft = 16 + 3 * height_ft
    width_ft = (eroded * 43560) / max(height_ft * through_ft, 1)
    capped = width_ft > length_ft
    width_ft = min(width_ft, length_ft)
    time_hr = clamp(0.0035 * height_ft * (storage_acft / 200) ** 0.15, 0.15, 4)
    return width_ft, time_hr, eroded, capped


def tr60(height_ft, length_ft, mode):
    mult = 3 if mode == "overtopping" else 2
    width_ft = mult * height_ft
    capped = width_ft > length_ft
    width_ft = min(width_ft, length_ft)
    time_hr = clamp(height_ft / 90, 0.15, 3)
    return width_ft, time_hr, capped


def hydrograph(peak_cms, time_s, volume_m3):
    n = 56
    t_end = time_s * 2.4
    shape = []
    for i in range(n + 1):
        t = (i / n) * t_end
        if t <= time_s:
            p = 0.5 * (1 - math.cos(math.pi * (t / time_s)))
            s = math.sin((math.pi * p) / 2)
        else:
            s = math.exp(-(t - time_s) / (0.42 * time_s))
        shape.append((t, s))
    area = 0.0
    for i in range(1, len(shape)):
        area += 0.5 * (shape[i][1] + shape[i - 1][1]) * (shape[i][0] - shape[i - 1][0])
    scale = volume_m3 / area if area > 0 else peak_cms
    fitted_cms = max(p[1] for p in shape) * scale
    return {
        "empiricalPeakCfs": peak_cms * CFS_PER_CMS,
        "fittedPeakCfs": fitted_cms * CFS_PER_CMS,
        "series": [{"tHr": t / 3600, "qCfs": q * scale * CFS_PER_CMS} for t, q in shape],
    }


def analyze(dam: dict) -> dict:
    height_ft = clamp(finite(dam.get("heightFt"), 10), 4, 200)
    storage = clamp(finite(dam.get("storageAcFt"), 10), 1, 500000)
    length_ft = clamp(finite(dam.get("lengthFt"), 100), 40, 20000)
    height_m = height_ft * FT
    volume_m3 = storage * ACFT_TO_M3
    crest_ft = finite(dam.get("crestFt"), 1000)
    local_crest = is_local_elev(crest_ft)
    warnings: list[str] = []
    offset_ft = None
    crest_navd = crest_ft
    rule = "Stated elevations used as NAVD88."

    if local_crest:
        dem = finite(dam.get("demCrestFt"), 1000)
        if dem < 500 or dem > 3600:
            rule = "0–300 ft crest, but the DEM crest is outside 500–3,600 ft. Flagged for review."
            warnings.append("No DEM tie. Crest left unconverted.")
            crest_navd = dem
        else:
            offset_ft = dem - crest_ft
            crest_navd = dem
            rule = f"0–300 ft is local. Offset {fmt_ft(offset_ft)} ft from the DEM crest. Datum choice is not applied."
    elif dam.get("statedDatum") == "NGVD29":
        offset_ft = 1
        crest_navd = crest_ft + 1
        rule = "NGVD29 desk shift +1.0 ft toward NAVD88. Not the VERTCON grid."
    elif dam.get("statedDatum") == "Local" and not local_crest:
        rule = "Marked Local, but the crest is outside 0–300 ft. Treated as NAVD88 and flagged."
        warnings.append("Stated datum Local does not match a 0–300 ft crest.")
        crest_navd = crest_ft
    else:
        crest_navd = crest_ft
        rule = "Elevations treated as NAVD88."

    local_record = local_crest and offset_ft is not None
    bed = tie_value("Streambed", finite(dam.get("streambedFt"), float("nan")), offset_ft, crest_navd, local_record)
    if not local_record and is_local_elev(finite(dam.get("streambedFt"), float("nan"))):
        toe = crest_navd - height_ft
        bed = {
            "navd": toe,
            "warning": f"Streambed was in the 0–300 ft band. Replaced with the desk toe, {fmt_ft(toe)} ft NAVD88 (crest minus height).",
        }
    spill = tie_value("Spillway", finite(dam.get("spillwayFt"), float("nan")), offset_ft, crest_navd, local_record)
    pool = tie_value("Normal pool", finite(dam.get("normalPoolFt"), float("nan")), offset_ft, crest_navd, local_record)
    for item in (bed, spill, pool):
        if item["warning"]:
            warnings.append(item["warning"])

    mode = dam.get("failureMode") or "overtopping"
    width_m, time_s_fr, peak_cms = froehlich(volume_m3, height_m, mode)
    width_ft = width_m / FT
    time_hr = time_s_fr / 3600
    capped = False
    notes: list[str] = []
    method = dam.get("method") or "froehlich"

    if method == "macdonald":
        width_ft, time_hr, eroded, capped = macdonald(storage, height_ft, length_ft)
        notes.append(f"Eroded embankment about {eroded:.0f} ac-ft (Wahl form of MacDonald, earthfill).")
    elif method == "tr60":
        width_ft, time_hr, capped = tr60(height_ft, length_ft, mode)
        notes.append("Average width is a screening multiple of height, not a fitted regression.")
    else:
        notes.append("Width, time, and the reference peak are Froehlich 2008 / 1995 in SI, shown in feet and cfs.")

    if width_ft > length_ft:
        width_ft = length_ft
        capped = True
    if capped:
        notes.append("Breach width was capped at the dam length.")

    time_s = max(time_hr, 0.05) * 3600
    hydro = hydrograph(peak_cms, time_s, volume_m3)
    notes.append(
        "The plotted hydrograph is sine-shaped, then scaled so its volume matches the stored water. "
        "The Froehlich peak is shown beside it and is not forced to that volume."
    )

    decay = ENGINE_DECAY.get(dam.get("engine") or "lisflood", ENGINE_DECAY["lisflood"])
    max_depth = clamp((height_ft * 0.42 * (hydro["fittedPeakCfs"] / 8000) ** 0.18), 0.4, height_ft * 0.9)
    length_m = clamp(900 + math.sqrt(storage) * 80, 1200, 6000)
    stations = []
    for i in range(41):
        s = i / 40
        half_m = 28 + 340 * s**0.72
        depth_ft = max_depth * math.exp(-decay["k"] * s)
        stations.append({"s": s, "halfM": half_m, "depthFt": depth_ft})

    area_m2 = 0.0
    for i in range(1, len(stations)):
        a, b = stations[i - 1], stations[i]
        if a["depthFt"] > 0.1 or b["depthFt"] > 0.1:
            dx = (b["s"] - a["s"]) * length_m
            area_m2 += dx * (a["halfM"] + b["halfM"])
    acres = area_m2 / 4046.8564224

    receptors = []
    for r in dam.get("receptors") or []:
        s = clamp(finite(r.get("station"), 0), 0, 1)
        idx = min(len(stations) - 1, round(s * (len(stations) - 1)))
        st = stations[idx]
        depth = max(0.0, st["depthFt"] - finite(r.get("padFt"), 0))
        flooded = depth >= 0.1
        width_ch = st["halfM"] * 2 * 3.28084
        q_here = hydro["fittedPeakCfs"] * math.exp(-decay["k"] * s)
        velocity = clamp(q_here / (width_ch * depth), 0.1, 14) if flooded and depth > 0.05 else 0
        arrival = (time_hr * 0.85 + s * (length_m / max(velocity * FT, 0.3)) / 3600) if flooded else None
        receptors.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "kind": r.get("kind"),
                "station": s,
                "lateral": r.get("lateral"),
                "depthFt": depth if flooded else 0,
                "velocityFps": velocity if flooded else 0,
                "arrivalHr": arrival,
                "flooded": flooded,
            }
        )

    max_vel = max((r["velocityFps"] for r in receptors), default=0)
    houses = [r for r in receptors if r["kind"] in ("house", "barn")]
    deep_house = any(r["depthFt"] >= 2 for r in houses)
    any_house = any(r["flooded"] for r in houses)
    wet_road = any(r["kind"] == "road" and r["depthFt"] >= 1 for r in receptors)

    if any(w.startswith("No DEM tie") for w in warnings):
        level = "Review"
        reasons = ["The crest could not be tied. Screening waits on a usable DEM crest."]
    elif deep_house:
        level = "High"
        reasons = ["A house or barn is at least 2 ft deep in this schematic."]
    elif any_house or wet_road:
        level = "Significant"
        reasons = [
            "A building is wet, but under 2 ft in this schematic."
            if any_house
            else "A road is at least 1 ft deep. No building is wet."
        ]
    else:
        level = "Low"
        reasons = ["No seeded receptor is wet above 0.1 ft."]
    reasons.append("This is a desk screen. It is not an ND Dam Safety or ACER classification.")

    return {
        "datum": {
            "rule": rule,
            "offsetFt": offset_ft,
            "crestNavdFt": crest_navd,
            "streambedNavdFt": bed["navd"],
            "spillwayNavdFt": spill["navd"],
            "poolNavdFt": pool["navd"],
            "warnings": warnings,
        },
        "breach": {
            "label": METHOD_LABEL.get(method, method),
            "widthFt": width_ft,
            "timeHr": time_hr,
            "capped": capped,
            "empiricalPeakCfs": hydro["empiricalPeakCfs"],
            "fittedPeakCfs": hydro["fittedPeakCfs"],
            "volumeAcFt": storage,
            "series": hydro["series"],
            "notes": notes,
        },
        "flood": {
            "engineLabel": decay["label"],
            "maxDepthFt": max_depth,
            "maxVelocityFps": max_vel,
            "acres": acres,
            "lengthM": length_m,
            "stations": stations,
            "receptors": receptors,
        },
        "screen": {"level": level, "reasons": reasons},
    }
