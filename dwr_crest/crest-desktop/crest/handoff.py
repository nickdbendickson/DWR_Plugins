"""Write the dam folder the ND suite reads. Does not start QGIS."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# One lock per dam folder. The desktop server runs one thread per request, so two jobs on the same dam would both
# read dam.json, merge, and write — the second clobbering the first (a lost field or status entry). Serialise the
# read-merge-write per folder. Keyed by resolved path; the set stays tiny (a handful of dams per session).
_folder_locks: dict[str, threading.Lock] = {}
_folder_locks_guard = threading.Lock()


def _folder_lock(folder: Path) -> threading.Lock:
    key = str(folder)
    with _folder_locks_guard:
        lock = _folder_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _folder_locks[key] = lock
        return lock

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


def _replace(tmp: Path, path: Path, tries: int = 20, wait_s: float = 0.02) -> None:
    """os.replace, retried on PermissionError. On Windows a replace is refused ("Access is denied") while another
    thread or process has the target open even for an instant: a second hand-off job writing the same shared file,
    a virus scanner, the search indexer. The Windows CI run of the concurrent-write test hit exactly that. POSIX
    never raises it here, so there the first try is the only one."""
    for attempt in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(wait_s * (attempt + 1))


def _dump(path: Path, obj) -> None:
    """Write JSON atomically. A crash mid-write must not leave a truncated dam.json — write_dam_folder then
    refuses to overwrite it (not a valid record) and the folder is stuck until someone deletes the file by hand.
    Write to a unique sibling .part, then os.replace (the pattern write_bytes already uses for binary exports).
    The temp name carries pid+thread id so two threads writing the same file (e.g. shared geometry exports) do
    not share one .part and race each other's replace."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
        _replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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


def _tied_datum_text(dam: dict, datum_block: dict) -> str | None:
    """The datum of the desk's tied elevations (crestNavdFt, streambedNavdFt, poolNavdFt).

    The desk converts stated elevations to NAVD88 (an NGVD29 offset, or a local 0-300 ft crest tied to the DEM crest),
    so the values are NAVD88 unless the tie failed. Labelling them with the stated datum put a DEM-tied ~1,630 ft crest
    in dam.json as "local", and an NGVD29 dam's shifted value as "NGVD29 ft".
    """
    stated = dam.get("statedDatum") or ""
    warnings = datum_block.get("warnings") if isinstance(datum_block.get("warnings"), list) else []
    if any(str(w).startswith("No DEM tie") for w in warnings):
        # A local crest that could not be tied is left as stated.
        return {"Local": "local", "NGVD29": "NGVD29 ft", "NAVD88": "NAVD88 ft"}.get(stated)
    if stated == "NGVD29" and datum_block.get("offsetFt") is None:
        # No VERTCON shift or entered offset: the values are still NGVD29.
        return "NGVD29 ft"
    return "NAVD88 ft"


# dam.json field -> the desk record's provenance key, so a field's source names where the value came from.
_PROVENANCE_KEY = {
    "height_ft": "heightFt",
    "storage_acft": "storageAcFt",
    "surface_area_ac": "surfaceAreaAc",
    "dam_length_ft": "lengthFt",
    "top_of_dam_ft": "crestFt",
    "top_of_dam_datum": "statedDatum",
    "streambed_ft": "streambedFt",
    "streambed_datum": "statedDatum",
    "normal_pool_ft": "normalPoolFt",
    "county": "county",
}


def _source_note(dam: dict, key: str) -> str:
    """Where a field came from: the DWR/NID field it was read from (e.g. "DWR streambed__navd88"), else "Crest desk"."""
    prov = dam.get("provenance") if isinstance(dam.get("provenance"), dict) else {}
    entry = prov.get(_PROVENANCE_KEY.get(key, ""))
    if isinstance(entry, dict):
        src = str(entry.get("src") or "").strip()
        field = str(entry.get("field") or "").strip()
        if src == "DEM":
            return f"Crest desk, {field}" if field else "Crest desk, typed from DEM"
        if src and field:
            return f"{src} {field} via Crest desk"
    return "Crest desk"


def write_dam_folder(work: Path, dam: dict, analysis: dict, append_status: bool = True) -> tuple[Path, list[str]]:
    """<work>/dams/<id>/dam.json plus the drawn geometries and the hydrograph.

    An existing dam.json keeps values another tool already set. Crest fills only
    empty fields and, when append_status is true, appends one screened status entry.
    A plain folder export passes append_status=false so repeated exports do not keep
    growing the status list. A file that is not a suite record is left untouched.

    The read-merge-write is serialised per folder (see _folder_lock) so two concurrent
    jobs on the same dam cannot clobber each other's dam.json.
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
    # The breach bottom, tied the same way as the crest (Docs & QA N3). Written with its own datum field.
    streambed = _num(datum_block.get("streambedNavdFt"))
    datum_text = _tied_datum_text(dam, datum_block)
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
        "streambed_ft": streambed,
        "streambed_datum": datum_text if streambed is not None else None,
    }
    if breach:
        incoming["lon"], incoming["lat"] = breach
    elif reservoir:
        incoming["lon"], incoming["lat"] = reservoir
    elif _num(dam.get("lon")) is not None and _num(dam.get("lat")) is not None:
        incoming["lon"], incoming["lat"] = float(dam["lon"]), float(dam["lat"])
    # crs is the CRS of projected x/y. Lon/lat does not set it.

    path = folder / "dam.json"
    with _folder_lock(folder):
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
        when = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for key, value in incoming.items():
            if value in (None, ""):
                continue
            if fields.get(key) in (None, ""):
                fields[key] = value
                sources[key] = {"tool": TOOL, "version": VERSION, "when": when, "note": _source_note(dam, key)}
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
        if append_status:
            reasons = screen.get("reasons") if isinstance(screen.get("reasons"), list) else []
            # A record written by another tool may carry "status": null (or a non-list); setdefault would
            # then return that value and .append() would raise AttributeError instead of the graceful
            # HandoffError. Coerce to a list, as fields/sources are guarded above.
            if not isinstance(record.get("status"), list):
                record["status"] = []
            record["status"].append({
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


# \Z, not $: in Python $ also matches just before a trailing newline, so "evil.par\n" would pass. \Z anchors the
# true end of the string.
_FILE_OK = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.(?:par|bci|bdy|geojson|kml|csv)|inputs/[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.csv)\Z"
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
