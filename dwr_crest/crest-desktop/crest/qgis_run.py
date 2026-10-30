"""Call the installed QGIS plugins. Never start the chain after a preflight FAIL."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
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


STOPPED_TIMEOUT = 124
STOPPED_CANCEL = 130


def screening_timeout(value=None) -> float | None:
    """Seconds nddam:runchain may run: the request's value, else CREST_SCREENING_TIMEOUT_S, else no limit.

    A one-dam chain on a statewide pack has run past 45 minutes (2026-10-30), so there is no default limit;
    0, blank or a bad value means no limit."""
    for raw in (value, os.environ.get("CREST_SCREENING_TIMEOUT_S")):
        if raw in (None, ""):
            continue
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            continue
        if seconds == seconds and seconds > 0:
            return seconds
        return None
    return None


def _stop(proc: subprocess.Popen) -> None:
    """Stop qgis_process and what it started (its own process group on POSIX)."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass


def _run(cmd: list[str], timeout: float | None, cancel: threading.Event | None = None) -> tuple[int, str]:
    """Run cmd and return (exit code, output). timeout (seconds) gives 124, cancel.set() gives 130;
    both stop the process (and its children) and keep the output so far."""
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", **kwargs)
    except OSError as exc:
        return 127, f"Could not start {cmd[0]}: {exc}\n"
    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read() if proc.stdout else ""), daemon=True)
    reader.start()
    start = time.monotonic()
    stopped = None
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            stopped = STOPPED_CANCEL
        elif timeout is not None and time.monotonic() - start > timeout:
            stopped = STOPPED_TIMEOUT
        if stopped:
            _stop(proc)
            break
        time.sleep(0.2)
    reader.join(timeout=10)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if proc.stdout is not None and not reader.is_alive():
        proc.stdout.close()
    out = "".join(chunks)
    if stopped == STOPPED_TIMEOUT:
        return stopped, out + f"\nStopped: the command ran longer than {timeout:g} s.\n"
    if stopped == STOPPED_CANCEL:
        return stopped, out + "\nStopped: cancelled in Crest.\n"
    return proc.returncode, out


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


def _settings_arg(root: Path, log, only_ids: str | None = None) -> list[str]:
    suite = find_suite_folder()
    dem = suite_dem(suite)
    if not suite and not (root / "dem.tif").is_file() and not only_ids:
        return []
    path = write_settings(root, suite, dem or (str(root / "dem.tif") if (root / "dem.tif").is_file() else None),
                          only_ids)
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


def chain_names(root: Path, dam_id: str) -> list[str]:
    """The dam_id(s) tool 1 gave this dam in <work>/1_dams.gpkg, matched on its dam_id or nid_id.

    Crest sends the NID id (ND00199); tool 1 names a dam that is in the DWR layer by its DWR id
    (DWR-2801), and every later output (3_ to 10_) uses that name. Empty before the first run."""
    path = Path(root) / "1_dams.gpkg"
    want = str(dam_id or "").strip().upper()
    if not want or not path.is_file():
        return []
    found: list[str] = []
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        tables = [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents WHERE data_type = 'features'")]
        for table in tables:
            quoted = '"' + str(table).replace('"', '""') + '"'
            cols = {str(r[1]).lower() for r in con.execute(f"PRAGMA table_info({quoted})")}
            if "dam_id" not in cols:
                continue
            where = "UPPER(TRIM(dam_id)) = ?" + (" OR UPPER(TRIM(nid_id)) = ?" if "nid_id" in cols else "")
            args = (want, want) if "nid_id" in cols else (want,)
            for (name,) in con.execute(f"SELECT dam_id FROM {quoted} WHERE {where}", args):
                if name and str(name) not in found:
                    found.append(str(name))
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return found


def chain_results(root: Path, names: list[str]) -> list[dict]:
    """This dam's rows of <work>/10_reports/summary.csv (tool 10), by the chain's dam_id."""
    path = Path(root) / "10_reports" / "summary.csv"
    want = {str(n).strip().upper() for n in names if str(n).strip()}
    if not want or not path.is_file():
        return []
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            return [row for row in csv.DictReader(fh) if str(row.get("dam_id") or "").strip().upper() in want]
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def _result_line(row: dict) -> str:
    cls = row.get("hz_class") or "no class"
    existing = row.get("haz_existing") or "none on record"
    text = f"Screening result for {row.get('dam_id')}: {cls} (class of record: {existing}"
    if row.get("hz_vs_existing"):
        text += f"; {row['hz_vs_existing']}"
    if row.get("hz_conf"):
        text += f"; confidence {row['hz_conf']}"
    text += ")."
    if row.get("review"):
        text += f" Review: {row['review']}."
    return text + " A screening result, not an official class. The Crest record is not changed."


def run_screening(
    dam: dict,
    analysis: dict,
    work: str | None,
    log,
    files=None,
    timeout: float | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Preflight (tool 17), then nddam:runchain for this dam only if it passes.

    timeout: seconds for the chain (else CREST_SCREENING_TIMEOUT_S, else no limit). cancel: an Event;
    setting it stops qgis_process. After the chain the dam's name in the chain outputs and its
    10_reports/summary.csv row are read back into the log and the result."""
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
    has_id = bool((dam or {}).get("nid") or (dam or {}).get("id"))
    # A work folder that already ran the chain knows the chain's name for this dam (DWR-2801 for
    # ND00199): both go in only_ids, so a Screening that filters later steps by dam_id alone keeps it.
    known = [n for n in chain_names(root, dam_id) if n.upper() != dam_id.upper()] if has_id else []
    only = ",".join([dam_id, *known]) if has_id else None
    settings = _settings_arg(root, log, only)
    limit = screening_timeout(timeout)
    log(f"Tool 17: {exe} run nddam:preflight")
    code, text = _run(
        [exe, "run", "nddam:preflight", f"--WORK={root}", f"--REPORT={report}", *settings],
        timeout=180,
        cancel=cancel,
    )
    if code == STOPPED_CANCEL:
        log(text.rstrip())
        log("Cancelled. nddam:runchain was not started.")
        return {"ok": False, "stopped": True, "cancelled": True, "folder": str(folder), "work": str(root)}
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
    log(f"Preflight passed. Starting nddam:runchain for {only or dam_id}.")
    if limit:
        log(f"The chain stops after {limit:g} s (CREST_SCREENING_TIMEOUT_S or the request's timeout).")
    code, text = _run(
        [
            exe, "run", "nddam:runchain",
            f"--WORK={root}",
            f"--DAM_ID={only or dam_id}",
            "--PREFLIGHT=true",
            "--FAIL_ON_ERROR=true",
            f"--LOG={chain_log}",
            *settings,
        ],
        timeout=limit,
        cancel=cancel,
    )
    log(text.rstrip())
    if chain_log.is_file():
        written = chain_log.read_text(encoding="utf-8", errors="replace").rstrip()
        if written and written not in text:
            log(written)
    names = chain_names(root, dam_id) if has_id else []
    if names and [n.upper() for n in names] != [dam_id.upper()]:
        log(f"The screening outputs name this dam {', '.join(names)} (Crest: {dam_id}).")
    out = {"folder": str(folder), "work": str(root), "chainIds": names}
    if code in (STOPPED_TIMEOUT, STOPPED_CANCEL):
        log("The screening chain was stopped. Its outputs so far stay in the work folder; chain_log.txt has the steps that ran.")
        return {**out, "ok": False, "stopped": True, "cancelled": code == STOPPED_CANCEL, "timedOut": code == STOPPED_TIMEOUT}
    if code != 0:
        log("The screening chain failed. The flood model was not started.")
        return {**out, "ok": False, "stopped": True}
    rows = chain_results(root, names or [dam_id])
    for row in rows:
        log(_result_line(row))
    if not rows:
        log("No row for this dam in 10_reports/summary.csv (the chain plan may not include tool 10).")
    log("Screening chain finished.")
    return {**out, "ok": True, "stopped": False, "chainResult": rows}


def run_model(
    dam: dict,
    analysis: dict,
    work: str | None,
    log,
    model: str = "lisflood",
    files=None,
    source: str = "pack",
    cancel: threading.Event | None = None,
) -> dict:
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
        code, text = _run([_python_for(cli), cli, "run", str(path)], timeout=None, cancel=cancel)
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
    code, text = _run(cmd, timeout=None, cancel=cancel)
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
