"""Write the dam folder the ND suite reads. Does not start QGIS."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

PHRASE = "proposed — embankment inserted from the drawn line"
FT = 0.3048
FORMAT = "nd_dam_suite/dam_record"
TOOL = "crest"
VERSION = "0.1"


class HandoffError(ValueError):
    """The folder was not written. The message says why."""


def safe_name(value) -> str:
    text = re.sub(r"[^\w\- ]+", "_", str(value or "")).strip()
    return text or "dam"


def _num(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _point(feature):
    geom = (feature or {}).get("geometry") or {}
    if geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates") or []
    if len(coords) < 2:
        return None
    lon, lat = _num(coords[0]), _num(coords[1])
    if lon is None or lat is None:
        return None
    return lon, lat


def _line(feature):
    geom = (feature or {}).get("geometry") or {}
    if geom.get("type") != "LineString":
        return None
    out = []
    for point in geom.get("coordinates") or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        lon, lat = _num(point[0]), _num(point[1])
        if lon is None or lat is None:
            return None
        out.append([lon, lat])
    return out if len(out) >= 2 else None


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _feature(kind: str, feature) -> dict | None:
    if not isinstance(feature, dict):
        return None
    geom = feature.get("geometry")
    if not isinstance(geom, dict) or not geom.get("type"):
        return None
    props = dict(feature.get("properties") or {})
    props["crest_role"] = kind
    return {"type": "Feature", "geometry": geom, "properties": props}


def _dem_point(dam: dict):
    """A point the user typed in the DEM's own coordinates, or None.

    Longitude and latitude are not this point. A UTM guess is not this point.
    """
    box = dam.get("lisflood") if isinstance(dam.get("lisflood"), dict) else {}
    x, y = _num(box.get("pointX")), _num(box.get("pointY"))
    if x is None or y is None:
        x, y = _num(dam.get("pointX")), _num(dam.get("pointY"))
    if x is None or y is None:
        return None
    return [x, y]


def write_dam_folder(work: Path, dam: dict, analysis: dict) -> tuple[Path, list[str]]:
    """<work>/dams/<id>/dam.json plus the drawn geometries and the hydrograph.

    An existing dam.json keeps values another tool already set. Crest fills only
    empty fields and appends a screened status. A file that is not a suite
    record is left untouched.
    """
    dam = dam or {}
    analysis = analysis or {}
    datum_block = analysis.get("datum") if isinstance(analysis.get("datum"), dict) else {}
    breach_block = analysis.get("breach") if isinstance(analysis.get("breach"), dict) else {}
    screen = analysis.get("screen") if isinstance(analysis.get("screen"), dict) else {}
    dam_id = str(dam.get("nid") or dam.get("id") or "dam")
    folder = Path(work) / "dams" / safe_name(dam_id)
    folder.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    geom = dam.get("geometry") if isinstance(dam.get("geometry"), dict) else {}
    breach = _point(geom.get("breachPoint"))
    reservoir = _point(geom.get("reservoirPoint"))
    line = _line(geom.get("damLine"))
    crest = _num(datum_block.get("crestNavdFt"))
    datum = dam.get("statedDatum") or ""
    datum_text = {"Local": "local", "NGVD29": "NGVD29 ft", "NAVD88": "NAVD88 ft"}.get(datum)
    nid = str(dam.get("nidId") or "")
    if not nid and re.fullmatch(r"[A-Za-z]{2}\d+", str(dam.get("nid") or "")):
        nid = str(dam.get("nid"))
    incoming = {
        "dam_id": dam_id,
        "name": dam.get("name") or None,
        "nid_id": nid or None,
        "county": dam.get("county") or None,
        "height_ft": _num(dam.get("heightFt")),
        "storage_acft": _num(dam.get("storageAcFt")),
        "surface_area_ac": _num(dam.get("surfaceAreaAc")),
        "dam_length_ft": _num(dam.get("lengthFt")),
        "top_of_dam_ft": crest,
        "normal_pool_ft": _num(datum_block.get("poolNavdFt")),
        "top_of_dam_datum": datum_text if crest is not None else None,
    }
    if breach:
        incoming["lon"], incoming["lat"] = breach
    elif reservoir:
        incoming["lon"], incoming["lat"] = reservoir
    elif _num(dam.get("lon")) is not None and _num(dam.get("lat")) is not None:
        incoming["lon"], incoming["lat"] = float(dam["lon"]), float(dam["lat"])
    # crs is the CRS of projected x/y. Lon/lat does not set it.

    path = folder / "dam.json"
    if path.is_file():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HandoffError(f"{path} is not valid JSON. It was not overwritten.") from exc
        if not isinstance(record, dict) or record.get("format") != FORMAT:
            raise HandoffError(f"{path} is not an nd_dam_suite dam record. It was not overwritten.")
    else:
        record = {
            "format": FORMAT,
            "format_version": 1,
            "fields": {},
            "sources": {},
            "status": [],
            "conflicts": [],
        }
    fields = record.setdefault("fields", {})
    sources = record.setdefault("sources", {})
    if not isinstance(fields, dict) or not isinstance(sources, dict):
        raise HandoffError(f"{path} has a damaged fields or sources block. It was not overwritten.")
    when = datetime.now().isoformat(timespec="seconds")
    for key, value in incoming.items():
        if value in (None, ""):
            continue
        if fields.get(key) in (None, ""):
            fields[key] = value
            sources[key] = {"tool": TOOL, "version": VERSION, "when": when, "note": "Crest desk"}
    existing = record.get("proposed_embankment")
    if isinstance(existing, dict) and existing.get("line_xy"):
        notes.append(
            "Kept the proposed embankment already in dam.json. It already has a crest line in map coordinates."
        )
    elif line and crest is not None:
        record["proposed_embankment"] = {
            "phrase": PHRASE,
            "dam_id": dam_id,
            "line_lonlat": [[round(x, 7), round(y, 7)] for x, y in line],
            "crest_m": round(float(crest) * FT, 3),
            "crest_len_m": round(float(_num(dam.get("lengthFt")) or 0) * FT, 2),
            "height_m": round(float(_num(dam.get("heightFt")) or 0) * FT, 3),
            "crest_note": (
                "line_xy is not set. Crest stored longitude and latitude. "
                "The Runner inserts the embankment only after the line is projected to the DEM CRS."
            ),
        }
        notes.append(
            "line_xy is not set. The drawn line is longitude and latitude, so the Runner will not insert the embankment."
        )
    reasons = screen.get("reasons") if isinstance(screen.get("reasons"), list) else []
    record.setdefault("status", []).append({
        "step": "screened",
        "tool": TOOL,
        "version": VERSION,
        "when": when,
        "result": {
            "level": screen.get("level"),
            "level_label": screen.get("label"),
            "method": dam.get("method"),
            "failure_mode": dam.get("failureMode"),
            "fitted_peak_cfs": _num(breach_block.get("peakCfs")),
            "width_ft": _num(breach_block.get("widthFt")),
            "notes": reasons,
            "preview": True,
        },
    })
    _dump(path, record)

    pieces = []
    for kind, feature in (
        ("reservoir", geom.get("reservoirPoint")),
        ("breach", geom.get("breachPoint")),
        ("dam_line", geom.get("damLine")),
        ("downstream_area", geom.get("aoi")),
    ):
        feat = _feature(kind, feature)
        name = {
            "reservoir": "reservoir_point.geojson",
            "breach": "breach_point.geojson",
            "dam_line": "dam_line.geojson",
            "downstream_area": "downstream_area.geojson",
        }[kind]
        if feat:
            _dump(folder / name, feat)
            pieces.append(feat)
    _dump(folder / "layout.geojson", {"type": "FeatureCollection", "features": pieces})

    series = breach_block.get("series") if isinstance(breach_block.get("series"), list) else []
    lines = ["time_hours,discharge_cms"]
    for point in series:
        if not isinstance(point, dict):
            continue
        t_hr, q_cfs = _num(point.get("tHr")), _num(point.get("qCfs"))
        if t_hr is None or q_cfs is None:
            continue
        lines.append(f"{t_hr:.4f},{q_cfs / 35.3146667:.4f}")
    (folder / "hydrograph.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cfs_lines = ["time_hr,discharge_cfs"]
    for point in series:
        if not isinstance(point, dict):
            continue
        t_hr, q_cfs = _num(point.get("tHr")), _num(point.get("qCfs"))
        if t_hr is None or q_cfs is None:
            continue
        cfs_lines.append(f"{t_hr:.4f},{q_cfs:.4f}")
    inputs = folder / "inputs"
    inputs.mkdir(exist_ok=True)
    runner_csv = inputs / "breach_hydrograph.csv"
    if runner_csv.is_file():
        notes.append("Left inputs/breach_hydrograph.csv already in the dam folder.")
    else:
        runner_csv.write_text("\n".join(cfs_lines) + "\n", encoding="utf-8")
        notes.append(
            "Wrote inputs/breach_hydrograph.csv (time_hr, discharge_cfs). "
            "The Runner reads it only for the own-hydrograph method."
        )
    return folder, notes


_FILE_OK = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.(?:par|bci|bdy|geojson|kml|csv)|inputs/[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.csv)$"
)


def write_named_files(folder: Path, files) -> list[str]:
    """Extra desk exports (LISFLOOD package, flood strip, PLSS). Names are a fixed pattern."""
    written = []
    if not isinstance(files, dict):
        return written
    for name, text in files.items():
        if not isinstance(name, str) or not isinstance(text, str) or not _FILE_OK.match(name):
            continue
        if len(text) > 5_000_000:
            continue
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(name)
    return written
