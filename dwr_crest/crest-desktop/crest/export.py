from __future__ import annotations

import json
import re


def export_hydrograph_csv(series: list[dict]) -> str:
    lines = ["time_hours,discharge_cms"]
    for p in series:
        cms = p["qCfs"] / 35.3146667
        lines.append(f"{p['tHr']:.4f},{cms:.4f}")
    return "\n".join(lines) + "\n"


def export_dam_json(dam: dict, analysis: dict) -> str:
    obj = {
        "dam_id": dam.get("nid") or dam.get("id"),
        "name": dam.get("name"),
        "height_ft": dam.get("heightFt"),
        "storage_acft": dam.get("storageAcFt"),
        "length_ft": dam.get("lengthFt"),
        "crest_ft": analysis["datum"]["crestNavdFt"],
        "method": dam.get("method"),
        "failure_mode": dam.get("failureMode"),
        "screened": {
            "level": analysis["screen"]["level"],
            "notes": analysis["screen"]["reasons"],
        },
        "hydrograph_peak_cfs": analysis["breach"]["fittedPeakCfs"],
        "volume_acft": analysis["breach"]["volumeAcFt"],
        "geometry": dam.get("geometry"),
    }
    return json.dumps(obj, indent=2) + "\n"


def generate_lisflood_package(
    dam: dict,
    series: list[dict],
    cell_size_m: float = 10,
    point_x: float = 0,
    point_y: float = 0,
) -> dict:
    resroot = re.sub(r"[^a-z0-9]", "_", (dam.get("nid") or "dam").lower())
    end = series[-1]["tHr"] if series else 1
    par = (
        f"resroot {resroot}\n"
        "dirroot .\n"
        "solver 1\n"
        "drycheck 1\n"
        "start 0\n"
        f"end {int((end * 3600) + 0.999999)}\n"
        "mass 1\n"
        "hazard 1\n"
        "mint_hk 1\n"
        "qoutput 1\n"
    )
    bci = f"P {point_x} {point_y} QVAR upstream1\n"
    bdy_lines = ["upstream1"]
    for p in series:
        cms = p["qCfs"] / 35.3146667
        unit = cms / cell_size_m
        bdy_lines.append(f"{unit:.6f} {p['tHr'] * 3600:.1f}")
    return {
        "par": par,
        "bci": bci,
        "bdy": "\n".join(bdy_lines) + "\n",
        "geometry": json.dumps(dam.get("geometry"), indent=2) if dam.get("geometry") else None,
    }
