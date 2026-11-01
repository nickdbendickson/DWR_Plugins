"""3.13.6: what the breach results dock shows beside the run's own summary (no Qt, no QGIS).

Docs & QA dock test on main db9c665 against DWR-2801 r4 (Nick: "Yes", 2026-11-01):

  * hazard_summary(dam_dir)  reads DamSafety Hazard's Hazard_Classification_Summary.txt (or a
    Hazard_Classification_Summary.json beside it, when a Hazard version writes one) in the
    output folder dam.json's classified status names (default "outputs"): Hazard version, ND
    class, TM 11 class, provisional flag, the 5.2.2 flooded habitable structure count.
  * record_status(dam_dir)   reads dam.json: the newest non-preview "classified" status (class,
    tool version) and dam_record.next_action.
  * class_of_record(record)  HOOK. Where an engineer's class of record is stored is NOT decided
    (Nick decides). CLASS_OF_RECORD_KEYS is empty on purpose, so this returns None and the dock
    says "class of record: not recorded". Once Nick picks a place, read it here only.
  * staleness(...)           warnings when the summary is older than the installed Hazard
    version or than the run outputs (run_report.json, the final rasters).
  * layer_paths(...)         depth / velocity / arrival rasters, receptors_flooded.csv and
    Hazard's Habitable_Structures.gpkg, found in this dam folder first (a copied folder still
    works), then at the path run_report.json recorded.

The dock never classifies; every value here is read from a file already written.
"""
import json
import os
import re

SUMMARY_TXT = "Hazard_Classification_Summary.txt"
SUMMARY_JSON = "Hazard_Classification_Summary.json"
HAZARD_STRUCTURES = os.path.join("Classification", "Habitable_Structures.gpkg")
LAYER_KEYS = ("depth", "velocity", "arrival", "receptors", "hazard_structures")
LAYER_LABELS = {"depth": "Depth", "velocity": "Velocity", "arrival": "Arrival",
                "receptors": "Flooded receptors", "hazard_structures": "Hazard structures"}
FINAL_NAMES = {"depth": "Depth (Max).tif", "velocity": "Velocity (Max).tif",
               "arrival": "Arrival Time.tif"}

# HOOK - class of record. Nick decides where an engineer's class of record lives; until then
# nothing is read. Add the key path here (e.g. ("fields", "<name>") or ("status", "<step>"))
# when it is decided; class_of_record() then reads it. Do not guess one.
CLASS_OF_RECORD_KEYS = ()


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_version(text):
    out = []
    for part in str(text or "").split("."):
        m = re.match(r"\D*(\d+)", part)
        if not m:
            break
        out.append(int(m.group(1)))
    return tuple(out) or (0,)


def _older(a, b):
    a, b = parse_version(a), parse_version(b)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) < b + (0,) * (n - len(b))


def parse_summary_text(text):
    """Values of a Hazard_Classification_Summary.txt (missing ones are None)."""
    def first(pattern):
        m = re.search(pattern, text, re.M)
        return m.group(1).strip() if m else None
    nd = first(r"^ND HAZARD CLASSIFICATION[^:]*:\s*([A-Za-z]+)")
    tm = first(r"^ACER TM 11 CLASSIFICATION:\s*([A-Za-z]+)")
    flooded = first(r"(\d+) habitable structure\(s\) inundated")
    result = first(r"^\s*Result\s+ND hazard classification.*?\s([A-Z]+(?: \(provisional\))?)\s*$")
    prov = bool(re.search(r"^\s*-\s*PROVISIONAL\b", text, re.M)) or \
        bool(result and "provisional" in result)
    return {"hazard_version": first(r"DamSafety Hazard v(\d[\w.]*)"),
            "generated": first(r"Generated:\s*(\d{4}-\d\d-\d\d[ T]\d\d:\d\d)"),
            "dam_name": first(r"^Dam:\s*(.+?)\s{2,}Generated:"),
            "nd_class": nd.title() if nd else None,
            "tm11_class": tm.title() if tm else None,
            "provisional": prov,
            "flooded_structures": int(flooded) if flooded else None}


def _summary_dir(dam_dir, record_data=None):
    folder = "outputs"
    for s in reversed((record_data or {}).get("status") or []):
        res = s.get("result") if isinstance(s, dict) else None
        if s.get("step") == "classified" and isinstance(res, dict) and res.get("output_folder"):
            folder = str(res["output_folder"])
            break
    return folder if os.path.isabs(folder) else os.path.join(dam_dir, folder)


def hazard_summary(dam_dir, record_data=None):
    """{} when there is no Hazard summary; else parse_summary_text values + path, mtime."""
    if record_data is None:
        record_data = _load_json(os.path.join(dam_dir, "dam.json"))
    out_dir = _summary_dir(dam_dir, record_data)
    jpath = os.path.join(out_dir, SUMMARY_JSON)
    tpath = os.path.join(out_dir, SUMMARY_TXT)
    info = {}
    if os.path.isfile(tpath):
        with open(tpath, encoding="utf-8", errors="replace") as f:
            info = parse_summary_text(f.read())
        info["path"] = tpath
    if os.path.isfile(jpath):              # a JSON summary, when one exists, wins key by key
        data = _load_json(jpath)
        for k in ("hazard_version", "nd_class", "tm11_class", "provisional",
                  "flooded_structures", "generated"):
            if data.get(k) is not None:
                info[k] = data[k]
        info.setdefault("path", jpath)
    if info:
        info["out_dir"] = out_dir
        info["mtime"] = os.path.getmtime(info["path"])
    return info


def class_of_record(record_data):
    """HOOK: the engineer's class of record, or None. Storage not decided (Nick decides);
    reads only the keys listed in CLASS_OF_RECORD_KEYS, which is empty."""
    for keys in CLASS_OF_RECORD_KEYS:
        node = record_data
        for k in keys:
            node = node.get(k) if isinstance(node, dict) else None
        if node:
            return str(node)
    return None


def record_status(dam_dir, facts=None):
    """dam.json facts: latest classified (not preview) class, tool version, next action."""
    path = os.path.join(dam_dir, "dam.json")
    out = {"has_record": os.path.isfile(path)}
    if not out["has_record"]:
        return out
    data = _load_json(path)
    out["class_of_record"] = class_of_record(data)
    try:
        from ..damsuite import dam_record as DR
        rec = DR.load(dam_dir)
        cla = rec.latest("classified", include_previews=False)
        out["next_action"] = DR.next_action(rec, facts)
    except Exception as e:                  # never let a bad record break the dock
        out["error"] = str(e)
        cla = None
        for s in data.get("status") or []:
            r = s.get("result") if isinstance(s, dict) else None
            if s.get("step") == "classified" and not (isinstance(r, dict) and r.get("preview")):
                cla = s
    if cla:
        res = cla.get("result") if isinstance(cla.get("result"), dict) else {}
        out.update({"status": "classified", "classified_by": cla.get("tool"),
                    "classified_version": cla.get("version"), "classified_when": cla.get("when"),
                    "nd_class": res.get("nd_class"), "tm11_class": res.get("tm11_class")})
    else:
        latest = [s for s in data.get("status") or [] if isinstance(s, dict)]
        out["status"] = latest[-1].get("step") if latest else None
    return out


def run_output_mtime(dam_dir, model_dir):
    times = []
    for p in [os.path.join(model_dir, "run_report.json")] + \
            [os.path.join(dam_dir, n) for n in FINAL_NAMES.values()]:
        if os.path.isfile(p):
            times.append(os.path.getmtime(p))
    return max(times) if times else None


def staleness(summary, installed_hazard=None, run_mtime=None):
    """Warning texts (empty list: current, or nothing to compare)."""
    warns = []
    if not summary:
        return warns
    v = summary.get("hazard_version")
    if v and installed_hazard and _older(v, installed_hazard):
        warns.append("Hazard summary written by Hazard {}; Hazard {} is installed - "
                     "classify again for the current rules.".format(v, installed_hazard))
    if run_mtime is not None and summary.get("mtime") is not None and \
            summary["mtime"] < run_mtime - 1.0:
        warns.append("Hazard summary is older than the run outputs - classify again.")
    return warns


def class_line(summary, record):
    """One display line: printed ND / TM 11 class, provisional, and the class of record."""
    nd = (summary or {}).get("nd_class") or (record or {}).get("nd_class")
    tm = (summary or {}).get("tm11_class") or (record or {}).get("tm11_class")
    if not nd and not tm:
        return "Hazard: not classified (no Hazard summary)"
    bits = ["ND {}".format(nd or "?")]
    if (summary or {}).get("provisional"):
        bits[0] += " (provisional)"
    bits.append("TM 11 {}".format(tm or "?"))
    v = (summary or {}).get("hazard_version") or (record or {}).get("classified_version")
    text = "Hazard {}: {}".format(v or "?", ", ".join(bits))
    cor = (record or {}).get("class_of_record")
    if cor and nd and cor.strip().lower() != str(nd).strip().lower():
        text += " · class of record {} (differs from printed {})".format(cor, nd)
    elif cor:
        text += " · class of record {}".format(cor)
    else:
        text += " · class of record: not recorded"
    return text


def structure_count_text(n_runner, summary):
    """'37 Runner receptors (structures)' and, when known, Hazard's 5.2.2 count beside it."""
    text = "{} Runner receptor structure(s)".format(n_runner) if n_runner is not None else ""
    n_haz = (summary or {}).get("flooded_structures")
    if n_haz is not None:
        text += ("; " if text else "") + "Hazard: {} flooded habitable".format(n_haz)
    return text


def layer_paths(dam_dir, model_dir, report, summary=None):
    """{key: path} of the files that exist (keys from LAYER_KEYS)."""
    final = ((report or {}).get("outputs") or {}).get("final") or {}
    out = {}
    for key, name in FINAL_NAMES.items():
        for p in (os.path.join(dam_dir, name), final.get(key)):
            if p and os.path.isfile(p):
                out[key] = p
                break
    p = os.path.join(model_dir, "receptors_flooded.csv")
    if os.path.isfile(p):
        out["receptors"] = p
    out_dir = (summary or {}).get("out_dir") or os.path.join(dam_dir, "outputs")
    p = os.path.join(out_dir, HAZARD_STRUCTURES)
    if os.path.isfile(p):
        out["hazard_structures"] = p
    return out


def receptor_csv_uri(path, crs):
    """QGIS delimitedtext URI of receptors_flooded.csv (x, y in the run's work CRS)."""
    from urllib.parse import quote
    uri = "file://" + quote(os.path.abspath(path)) + "?delimiter=,&xField=x&yField=y"
    if crs:
        uri += "&crs=" + str(crs)
    return uri


def warning_texts(report):
    return [str(w) for w in ((report or {}).get("warnings") or [])]
