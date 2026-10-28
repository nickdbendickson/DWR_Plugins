"""One signed, hash-bound Classification Record.

A hazard classification is only as defensible as the evidence that it came from these inputs,
this engine and these signed charts. The suite already has the pieces separately: `chain.py`
ties the classified rasters to the breach-run manifest, the chart file carries a signer, and the
fatality-rate table carries a signer. This module binds them into ONE record under a single
SHA-256 so a reviewer can confirm, with one `verify`, that nothing was altered and that the
result rests on a validated engine and signed data.

    rec = build_record(classification, dam_folder=<dam>, charts=<chart info>, chart_path=<file>,
                       loss_rates=<signed table or None>, engine=<label or manifest software>)
    write_record(folder, rec)                 # -> <folder>/classification_record.json
    v = verify_record(rec, dam_folder=<dam>)  # {"ok": bool, "problems": [...]}

`verify_record` returns ok=False (NOT VERIFIED) when the record was edited, an input/raster or
the engine binary changed since the run, the run manifest is missing or self-inconsistent, the
charts or (when loss of life is reported) the rate table are not signed off, or the flood was
produced by the built-in screening engine (a numpy approximation, never a basis for a final
classification). Nothing here needs QGIS or numpy; it reuses chain.py's digests so the hashes
compare directly with the runner's manifest.
"""
import hashlib
import json
import os

from . import chain

RECORD_NAME = "classification_record.json"
KIND = "nd_dam_classification_record"
VERSION = 1
BUILTIN_MARK = "builtin_lisflood.py"      # a run whose engine "binary" is this is the built-in


def _digest(body):
    """SHA-256 of the canonical record body (everything but record_sha256)."""
    b = {k: v for k, v in body.items() if k != "record_sha256"}
    return hashlib.sha256(chain._canonical(b).encode()).hexdigest()


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _governing(classification):
    out = []
    for f in (_get(classification, "governing", []) or []):
        out.append({"reason": str(_get(f, "reason", "")), "section": str(_get(f, "section", ""))})
    return out


def _charts_block(charts, chart_path):
    """Signer + sha256 of the ACER TM-11 charts. `charts` is a ChartSet.info()-style dict."""
    charts = charts or {}
    signer = (str(charts.get("signed_off_by") or charts.get("signer") or "").strip()
              or str(charts.get("digitized_by") or "").strip())
    sha = ""
    if chart_path and os.path.isfile(chart_path):
        sha = chain.file_digest(chart_path, use_disk_cache=False).get("sha256", "")
    return {"signer": signer, "signed_off_on": str(charts.get("signed_off_on") or "").strip(),
            "digitized_by": str(charts.get("digitized_by") or "").strip(),
            "reference": str(charts.get("reference") or charts.get("source") or "").strip(),
            "sha256": sha}


def _loss_block(loss_rates):
    """Signer of the fatality-rate table, or an unsigned/absent marker."""
    if not isinstance(loss_rates, dict):
        return {"present": False, "signed_off": False, "signed_off_by": "", "source": ""}
    by = str(loss_rates.get("signed_off_by") or "").strip()
    return {"present": True, "signed_off": bool(by), "signed_off_by": by,
            "source": str(loss_rates.get("source") or "").strip()}


def _engine_block(engine, manifest):
    """{model, built_in, sha256, path}. `engine` may be a label string; otherwise the engine is
    read from the manifest's software.lisflood_exe."""
    sw = ((manifest or {}).get("software") or {}).get("lisflood_exe") or {}
    path = str(sw.get("path") or "")
    sha = str(sw.get("sha256") or "")
    label = engine if isinstance(engine, str) and engine else (
        "built-in engine (numpy; NOT LISFLOOD-FP)"
        if path.endswith(BUILTIN_MARK) else ("LISFLOOD-FP" if path else "unknown"))
    built_in = path.endswith(BUILTIN_MARK) or ("built-in" in label.lower())
    return {"model": label, "built_in": bool(built_in), "sha256": sha, "path": path}


def build_record(classification, dam_folder=None, charts=None, chart_path=None,
                 loss_rates=None, engine=None, created="", manifest=None):
    """Assemble the bound record. `classification` is a Classification or its to_dict(). The run
    manifest is read from `dam_folder` (chain.find_runner_manifest) unless one is passed."""
    man_path = None
    if manifest is None and dam_folder:
        man_path = chain.find_runner_manifest(dam_folder)
        if man_path:
            try:
                manifest = chain.read_runner_manifest(man_path)
            except (OSError, ValueError):
                manifest = None
    man_ok, man_text = chain.manifest_ok(manifest) if manifest else (False, "no run manifest found")
    inputs = {k: (v or {}).get("sha256", "") for k, v in ((manifest or {}).get("inputs") or {}).items()}
    body = {
        "kind": KIND,
        "version": VERSION,
        "created": str(created or ""),
        "hazard_class": str(_get(classification, "hazard_class", "")),
        "governing": _governing(classification),
        "policy": str(_get(classification, "policy", "")),
        "module_version": str(_get(classification, "module_version", "")),
        "charts": _charts_block(charts if charts is not None else _get(classification, "charts"),
                                chart_path),
        "loss": _loss_block(loss_rates),
        "engine": _engine_block(engine, manifest),
        "run_manifest": {
            "found": bool(manifest),
            "sha256": str((manifest or {}).get("manifest_sha256") or ""),
            "self_consistent": bool(man_ok),
            "tool": str((manifest or {}).get("tool") or ""),
            "note": man_text,
        },
        "inputs": inputs,
    }
    body["record_sha256"] = _digest(body)
    return body


def verify_record(record, dam_folder=None):
    """Recompute everything a reviewer would. Returns {"ok", "problems"}; ok=False means the
    record must NOT be relied on without explanation."""
    problems = []
    if not isinstance(record, dict) or record.get("kind") != KIND:
        return {"ok": False, "problems": ["not a classification record"]}
    if _digest(record) != record.get("record_sha256"):
        problems.append("the record was edited after it was written (digest mismatch)")
    charts = record.get("charts") or {}
    if not charts.get("signer"):
        problems.append("the ACER TM-11 charts are not signed off (no signer)")
    loss = record.get("loss") or {}
    if loss.get("present") and not loss.get("signed_off"):
        problems.append("a fatality-rate table is present but not signed off")
    engine = record.get("engine") or {}
    if engine.get("built_in"):
        problems.append("the flood came from the built-in screening engine (numpy approximation, "
                        "NOT LISFLOOD-FP): not a basis for a final classification without a "
                        "validated-engine run")
    rm = record.get("run_manifest") or {}
    if not rm.get("found"):
        problems.append("no breach-run manifest is bound (provenance of the flood is unproven)")
    elif not rm.get("self_consistent"):
        problems.append("the bound run manifest is not self-consistent (" + str(rm.get("note")) + ")")
    elif dam_folder:
        # re-find the manifest on disk and confirm its digest still matches what we recorded
        p = chain.find_runner_manifest(dam_folder)
        if not p:
            problems.append("the run manifest recorded here is no longer in the dam folder")
        else:
            try:
                live = chain.read_runner_manifest(p)
            except (OSError, ValueError) as e:
                live = None
                problems.append("the run manifest could not be re-read: {}".format(e))
            if live is not None:
                ok, _ = chain.manifest_ok(live)
                if not ok:
                    problems.append("the run manifest on disk is no longer self-consistent")
                elif live.get("manifest_sha256") != rm.get("sha256"):
                    problems.append("the run manifest on disk changed since this record was made")
    return {"ok": not problems, "problems": problems}


def write_record(folder, record):
    path = folder if folder.endswith(".json") else os.path.join(folder, RECORD_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(record, indent=1, sort_keys=True) + "\n")
    return path


def read_record(folder):
    path = folder if folder.endswith(".json") else os.path.join(folder, RECORD_NAME)
    with open(path, encoding="utf-8") as f:
        return json.load(f)
