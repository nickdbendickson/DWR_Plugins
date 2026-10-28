"""Runner 3.12.2: the parameters of the last 'Run breach model for one dam' in a dam folder
(<dam folder>/_breach_model/run_params.json), so a later session - the window, or
qgis_process with ndbreach:finishanuga - can run the same dam again with Resume on and the
same settings (the same checkpoint key) after QGIS died during the ANUGA step: LISFLOOD-FP is
reused, the detached ANUGA worker is waited for or its finished result is used
(core/anuga_detach.py, anuga_model.adoption) and the products are built as usual.

No QGIS imports: layer values are turned into their data sources by the caller's resolver."""
import json
import os
import time

NAME = "run_params.json"
MODEL_FOLDER = "_breach_model"          # = workflow.MODEL_FOLDER


def path(dam_folder):
    return os.path.join(dam_folder, MODEL_FOLDER, NAME)


def jsonable(v, resolve=None):
    """A JSON value for a Processing parameter value; `resolve(text)` turns a project layer id
    / name into its data source (None when it is not a layer). Layers -> their source,
    feature-source definitions -> their layer's source, QgsProperty -> its static value."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        r = resolve(v) if resolve else None
        return r if r else v
    if isinstance(v, (list, tuple)):
        return [jsonable(x, resolve) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x, resolve) for k, x in v.items()}
    src = getattr(v, "source", None)
    if src is not None and not callable(src):          # QgsProcessingFeatureSourceDefinition
        return jsonable(src, resolve)
    if callable(src):                                  # a QgsMapLayer
        try:
            return str(src())
        except Exception:                              # noqa: BLE001
            return None
    sv = getattr(v, "staticValue", None)               # QgsProperty
    if callable(sv):
        return jsonable(sv(), resolve)
    for attr in ("toString", "asWkt"):
        f = getattr(v, attr, None)
        if callable(f):
            try:
                return str(f())
            except Exception:                          # noqa: BLE001
                pass
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def save(dam_folder, alg_id, parameters, resolve=None, layer_params=(), skip=("RESUME",)):
    """Write run_params.json (best effort; returns the path or None). `resolve` is used for
    the values of `layer_params` only (a plain text value is never taken for a layer)."""
    try:
        os.makedirs(os.path.join(dam_folder, MODEL_FOLDER), exist_ok=True)
        params = {k: jsonable(v, resolve if k in layer_params else None)
                  for k, v in (parameters or {}).items() if k not in skip}
        rec = {"algorithm": alg_id, "parameters": params,
               "saved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        p = path(dam_folder)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, default=str)
        os.replace(tmp, p)
        return p
    except Exception:                                  # noqa: BLE001 - never stop a run
        return None


def load(dam_folder):
    """(algorithm id, parameters) of the saved run, or (None, None)."""
    try:
        with open(path(dam_folder), encoding="utf-8") as f:
            rec = json.load(f)
        return rec.get("algorithm"), dict(rec.get("parameters") or {})
    except (OSError, ValueError, AttributeError):
        return None, None


def pending_workers(dam_folder):
    """[(sub, worker_state dict)] of the dam's ANUGA workers that are running, or finished
    and not yet built into products (anuga_detach.pending)."""
    from . import anuga_detach as detach
    out = []
    for sub in ("anuga", "anuga_base"):
        wd = os.path.join(dam_folder, MODEL_FOLDER, sub)
        if os.path.isfile(detach.status_path(wd)):
            w = detach.pending(wd)
            if w:
                out.append((sub, w))
    return out
