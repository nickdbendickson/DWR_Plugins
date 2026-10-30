"""Call the installed QGIS plugins. Never start the chain after a preflight FAIL."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from crest.handoff import HandoffError, _dem_point, write_dam_folder, write_named_files
from crest.layers import download_aoi, find_suite_folder, suite_dem, use_offline_pack, write_settings

# qgis_process accepts the enum text. The index would silently change if the plugin reorders methods.
METHOD_LABEL = {
    ("froehlich", "piping"): "Piping failure, Froehlich (2008) parameters, routed",
    ("froehlich", "overtopping"): "Overtopping failure, Froehlich (2008) parameters, routed",
    ("macdonald", "piping"): "MacDonald & Langridge-Monopolis (1984) earthfill parameters, routed",
    ("macdonald", "overtopping"): "MacDonald & Langridge-Monopolis (1984) earthfill parameters, routed",
    ("tr60", "piping"): "NRCS TR-60 peak breach discharge, triangular hydrograph",
    ("tr60", "overtopping"): "NRCS TR-60 peak breach discharge, triangular hydrograph",
}
CLI_METHOD = {
    ("froehlich", "piping"): "piping",
    ("froehlich", "overtopping"): "overtopping",
    ("macdonald", "piping"): "mlm1984",
    ("macdonald", "overtopping"): "mlm1984",
    ("tr60", "piping"): "tr60",
    ("tr60", "overtopping"): "tr60",
}

# qgis_process enum text, then the CLI model.tier value.
MODELS = {
    "lisflood": ("LISFLOOD-FP only", "lisflood", False),
    "anuga": (
        "LISFLOOD-FP for the extent, then ANUGA (final results from ANUGA)",
        "lisflood+anuga",
        False,
    ),
    "hecras": (
        "LISFLOOD-FP then HEC-RAS 2D (final results from HEC-RAS; needs HEC-RAS 6.6, see help)",
        "lisflood+hecras",
        False,
    ),
    "builtin": ("LISFLOOD-FP only", "lisflood", True),
}


def model_choice(name: str | None) -> tuple[str, str, bool]:
    return MODELS.get(str(name or "lisflood"), MODELS["lisflood"])


def find_qgis_process() -> str | None:
    chosen = os.environ.get("QGIS_PROCESS")
    if chosen and Path(chosen).is_file():
        return chosen
    found = shutil.which("qgis_process")
    if found:
        return found
    for candidate in (
        "/Applications/QGIS.app/Contents/MacOS/bin/qgis_process",
        "/Applications/QGIS-LTR.app/Contents/MacOS/bin/qgis_process",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def find_ndbreach_cli() -> str | None:
    chosen = os.environ.get("CREST_NDBREACH_CLI")
    if chosen and Path(chosen).is_file():
        return chosen
    patterns = (
        "Library/Application Support/QGIS/QGIS3/profiles/*/python/plugins/*/scripts/ndbreach_cli.py",
        ".local/share/QGIS/QGIS3/profiles/*/python/plugins/*/scripts/ndbreach_cli.py",
    )
    home = Path.home()
    for pattern in patterns:
        found = sorted(home.glob(pattern))
        if found:
            return str(found[-1])
    return None


def work_dir(requested: str | None) -> Path:
    raw = (requested or os.environ.get("CREST_WORK") or "").strip()
    path = Path(raw).expanduser() if raw else Path.home() / "CrestDams"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def dem_path(work: Path, dam: dict, source: str = "pack") -> str | None:
    for raw in (dam.get("demPath"), os.environ.get("CREST_DEM"), work / "dem.tif", work / "dem.vrt"):
        if not raw:
            continue
        path = Path(str(raw)).expanduser()
        if path.is_file():
            return str(path.resolve())
    if source == "pack":
        found = suite_dem(find_suite_folder())
        if found:
            return found
    return None


def _ready(text: str) -> bool | None:
    match = re.search(r"READY:\s*(true|false)\b", text, re.I)
    if match:
        return match.group(1).lower() == "true"
    fails = re.search(r"FAILS:\s*(\d+)", text, re.I)
    if fails:
        return int(fails.group(1)) == 0
    # The report's first line is "Preflight: ready", "Preflight: ready, N warning(s)",
    # or "Preflight: not ready: ...". Warnings still pass.
    report = re.search(r"Preflight:\s*(not ready|ready)\b", text, re.I)
    if report:
        return report.group(1).lower() == "ready"
    return None


def _run(cmd: list[str], timeout: float | None) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        return 124, out + "\nStopped: the command ran too long.\n"
    except OSError as exc:
        return 127, f"Could not start {cmd[0]}: {exc}\n"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _method_key(dam: dict) -> tuple[str, str]:
    method = str(dam.get("method") or "froehlich").lower()
    mode = str(dam.get("failureMode") or "piping").lower()
    if method not in ("froehlich", "macdonald", "tr60"):
        method = "froehlich"
    if mode not in ("piping", "overtopping"):
        mode = "piping"
    return method, mode


def _opt_num(cmd: list[str], flag: str, value) -> None:
    if value in (None, ""):
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if number != number:  # NaN
        return
    cmd.append(f"--{flag}={number}")


def _prepare(dam: dict, analysis: dict, work: str | None, log, files=None):
    root = work_dir(work)
    try:
        folder, notes = write_dam_folder(root, dam, analysis or {})
    except HandoffError as exc:
        log(str(exc))
        return None
    for note in notes:
        log(note)
    extra = write_named_files(folder, files)
    if extra:
        log("Also wrote: " + ", ".join(extra))
    log(f"Wrote {folder / 'dam.json'}")
    log("Geometries: dam line, breach point, reservoir point, downstream area, when they were drawn.")
    log("Folder layout: <work>/dams/<id>/ with dam.json, the drawn geometries, hydrograph.csv, and inputs/.")
    return root, folder


def _settings_arg(root: Path, log) -> list[str]:
    suite = find_suite_folder()
    dem = suite_dem(suite)
    if not suite and not (root / "dem.tif").is_file():
        return []
    path = write_settings(root, suite, dem or (str(root / "dem.tif") if (root / "dem.tif").is_file() else None))
    if suite:
        log(f"Screening settings use the ND offline pack: {suite}")
    return [f"--SETTINGS={path}"]


def run_folder(dam: dict, analysis: dict, work: str | None, log, files=None) -> dict:
    prepared = _prepare(dam or {}, analysis or {}, work, log, files)
    if prepared is None:
        return {"ok": False, "stopped": True, "folder": "", "work": ""}
    root, folder = prepared
    log("The dam folder is written. No model was started.")
    return {"ok": True, "stopped": False, "folder": str(folder), "work": str(root)}


def run_layers(dam: dict, work: str | None, source: str, log) -> dict:
    root = work_dir(work)
    if source == "download":
        result = download_aoi(root, dam or {}, log)
    else:
        result = use_offline_pack(root, log)
    result["work"] = str(root)
    result.setdefault("folder", "")
    return result


def run_screening(dam: dict, analysis: dict, work: str | None, log, files=None) -> dict:
    prepared = _prepare(dam or {}, analysis or {}, work, log, files)
    if prepared is None:
        return {"ok": False, "stopped": True, "folder": "", "work": ""}
    root, folder = prepared
    dam_id = str((dam or {}).get("nid") or (dam or {}).get("id") or "dam")
    exe = find_qgis_process()
    if not exe:
        log("qgis_process was not found. The chain was not started.")
        log("Install QGIS, or set QGIS_PROCESS to the qgis_process program.")
        return {"ok": False, "stopped": True, "folder": str(folder), "work": str(root)}
    report = root / "preflight.txt"
    settings = _settings_arg(root, log)
    log(f"Tool 17: {exe} run nddam:preflight")
    code, text = _run(
        [exe, "run", "nddam:preflight", f"--WORK={root}", f"--REPORT={report}", *settings],
        timeout=180,
    )
    report_text = ""
    if report.is_file():
        report_text = report.read_text(encoding="utf-8", errors="replace")
    log(text.rstrip())
    if report_text.strip() and report_text.strip() not in text:
        log(report_text.rstrip())
    ready = _ready(text + "\n" + report_text)
    if code != 0 or ready is not True:
        if ready is None and code == 0:
            log("Could not read READY from the preflight output.")
        log("Preflight did not pass. nddam:runchain was not started.")
        log("A new CrestDams folder fails here until the screening work folder has its DEM and layers. That stop is intended.")
        return {"ok": False, "stopped": True, "folder": str(folder), "work": str(root)}
    chain_log = root / "chain_log.txt"
    log(f"Preflight passed. Starting nddam:runchain for {dam_id}.")
    code, text = _run(
        [
            exe, "run", "nddam:runchain",
            f"--WORK={root}",
            f"--DAM_ID={dam_id}",
            "--PREFLIGHT=true",
            "--FAIL_ON_ERROR=true",
            f"--LOG={chain_log}",
            *settings,
        ],
        timeout=None,
    )
    log(text.rstrip())
    if chain_log.is_file():
        written = chain_log.read_text(encoding="utf-8", errors="replace").rstrip()
        if written and written not in text:
            log(written)
    if code != 0:
        log("The screening chain failed. The flood model was not started.")
        return {"ok": False, "stopped": True, "folder": str(folder), "work": str(root)}
    log("Screening chain finished.")
    return {"ok": True, "stopped": False, "folder": str(folder), "work": str(root)}


def run_model(dam: dict, analysis: dict, work: str | None, log, model: str = "lisflood", files=None, source: str = "pack") -> dict:
    dam = dam or {}
    prepared = _prepare(dam, analysis or {}, work, log, files)
    if prepared is None:
        return {"ok": False, "stopped": True, "folder": "", "work": ""}
    root, folder = prepared
    dams = root / "dams"
    dam_id = str(dam.get("nid") or dam.get("id") or "dam")
    label, cli_tier, builtin = model_choice(model)
    dem = dem_path(root, dam, source)
    if not dem:
        log("No DEM, so the flood model was not started. The dam folder above was still written.")
        log("Type a DEM path, choose Get layers (download for this area, or the ND offline pack), or set CREST_DEM.")
        log("The built-in engine is still a raster model. It is not the desk's schematic flood, and it also needs a DEM.")
        return {"ok": False, "stopped": True, "folder": str(folder), "work": str(root)}
    point = _dem_point(dam)
    datum = (analysis or {}).get("datum") if isinstance((analysis or {}).get("datum"), dict) else {}
    key = _method_key(dam)
    log(f"Flood model: {label}" + (" with the built-in screening engine (not LISFLOOD-FP)." if builtin else "."))
    cli = find_ndbreach_cli()
    if cli and point:
        config = {
            "dem": {"path": dem, "z_units": "auto"},
            "dam": {
                "dam_id": dam_id,
                "name": dam.get("name"),
                "dam_height_ft": dam.get("heightFt"),
                "storage_top_acft": dam.get("storageAcFt"),
                "top_of_dam_ft": datum.get("crestNavdFt"),
                "point": point,
            },
            "breach": {"method": CLI_METHOD[key]},
            "model": {"tier": cli_tier, **({"lisflood_exe": "builtin"} if builtin else {})},
            "output": {"folder": str(dams)},
        }
        path = folder / "runner_config.json"
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        log("ndbreach:batch was not started. It needs a point layer of many dams.")
        log(f"One dam, no QGIS window: {cli} run")
        log("The point is the DEM-coordinate pair from the export fields, not longitude and latitude.")
        code, text = _run([_python_for(cli), cli, "run", str(path)], timeout=None)
        log(text.rstrip())
        log("Runner finished." if code == 0 else "The Runner failed.")
        return {"ok": code == 0, "stopped": code != 0, "folder": str(folder), "work": str(root)}
    if cli and not point:
        log("ndbreach_cli.py needs the dam point in the DEM's coordinates. Longitude and latitude were not sent as that point.")
    exe = find_qgis_process()
    if not exe:
        log("qgis_process was not found. The flood model was not started.")
        if not point:
            log("Enter both Point X and Point Y (DEM CRS) on the export panel to use ndbreach_cli.py instead.")
        return {"ok": False, "stopped": True, "folder": str(folder), "work": str(root)}
    cmd = [
        exe, "run", "ndbreach:rundam",
        f"--DAM_ID={dam_id}",
        f"--DAMS_FOLDER={dams}",
        f"--DEM={dem}",
        f"--METHOD={METHOD_LABEL[key]}",
        f"--TIER={label}",
    ]
    if builtin:
        cmd.append("--LISFLOOD_EXE=builtin")
    _opt_num(cmd, "HEIGHT_FT", dam.get("heightFt"))
    _opt_num(cmd, "STORAGE_ACFT", dam.get("storageAcFt"))
    _opt_num(cmd, "TOP_FT", datum.get("crestNavdFt"))
    _opt_num(cmd, "AREA_AC", dam.get("surfaceAreaAc"))
    log("ndbreach:batch was not started. It needs a point layer of many dams. This button runs ndbreach:rundam for one dam.")
    log("The drawn dam line was not sent as DAM_LINES. That argument is in the DEM's coordinates, and the line is still longitude and latitude.")
    code, text = _run(cmd, timeout=None)
    log(text.rstrip())
    log("Runner finished." if code == 0 else "The Runner failed.")
    return {"ok": code == 0, "stopped": code != 0, "folder": str(folder), "work": str(root)}


def _python_for(cli: str) -> str:
    chosen = os.environ.get("CREST_PYTHON")
    if chosen:
        return chosen
    for candidate in (
        "/Applications/QGIS.app/Contents/MacOS/bin/python3",
        "/Applications/QGIS-LTR.app/Contents/MacOS/bin/python3",
    ):
        if Path(candidate).is_file():
            return candidate
    return "python3"
