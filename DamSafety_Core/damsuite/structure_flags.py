"""Flagged structures: inventory records that are not a dam to breach (damsuite 1.17.2).

A short, version-controlled list shipped with the suite. Each entry is keyed by the DWR id
(DWR-<structure id>) and the NID id, and says what the structure is, which hazard class
stands and why. The three plugins read it (pure Python, no QGIS, no Qt):

  * Screening (tools 1, 5, 6, 7, 7b, 8, 10, 12) lists the dam with the note below instead of
    computing a breach flood or a breach hazard: hazard = the entry's class, from the record.
  * The Runner refuses a single-dam breach run of a flagged structure (with the note) unless
    the run is explicitly allowed: the tool's ALLOW_FLAGGED_STRUCTURE parameter, the CLI's
    --allow-flagged-structure, or the environment variable ALLOW_FLAGGED_STRUCTURE=1. Batch
    runs skip it (Status "flagged" and the reason in Batch_Breach_Runs.csv).
  * Hazard refuses to classify it unless allowed the same way, and an allowed run says in its
    log and its DATA SOURCES that the hazard of record is the entry's class.
  * The Dam suite home shows the flag and the class of record for the dam folder.

The list is data, not a setting: nothing here is ever written by a plugin, and the suite's
settings file (damsuite_settings.csv) and the offline data pack are not involved. To add an
entry: append a dict to FLAGS with every key of REQUIRED (see docs/Flagged_structures.md),
copy this file byte-identical into the three plugins' damsuite/ folders and re-stamp their
FILES.json. `problems()` must return [] (a test checks it).
"""
import os
import re

CLASS_ROAD_FILL_BACKWATER = "road_fill_backwater"
CLASS_LABELS = {CLASS_ROAD_FILL_BACKWATER: "road fill over backwater"}
ENV_ALLOW = "ALLOW_FLAGGED_STRUCTURE"
HAZARDS = ("Low", "Significant", "High")
REQUIRED = ("dwr_id", "nid_id", "name", "structure_class", "hazard", "hazard_source",
            "breach_analysis", "reason", "evidence", "date", "decided_by")

FLAGS = (
    {
        "dwr_id": "DWR-3161",
        "nid_id": "ND00597",
        "name": "Sanish Bay WPA Dam 1",
        "structure_class": CLASS_ROAD_FILL_BACKWATER,
        "hazard": "Low",
        "hazard_source": "DWR record",
        "breach_analysis": "not applicable",
        "reason": ("The 'dam' is the 1995 ND-23 highway fill: the pool is the creek valley "
                   "south of the road and Lake Sakakawea (Sanish Bay) is the downstream side to "
                   "the north. A breach would empty the pool into the lake (about 0.01 ft rise "
                   "for the full 2,861 ac-ft), so there is no breach flood to classify; the loss "
                   "in a failure is the highway. The HIGH of the 3.10.16 rebaseline was a model "
                   "artifact (ND-23 'overtopped' is the dam's own crest) and is not used."),
        "evidence": ("NBI structure 0023048401 (ND 23 over a creek, built 1995, twin 10 x 9 ft "
                     "box culvert, 29 m from the dam point) matches the DWR record (built 1995, "
                     "2 culverts 120 x 108 in with stoplogs, 40 ft top width). Pool side: south "
                     "of ND-23; downstream side: north, the outfall to Lake Sakakawea (NHD / 3DHP "
                     "waterbody about 100 m from the dam point). DWR max pool 1868.6 ft / 2,861 "
                     "ac-ft / 335 ac fits the terrain within about 2 ft, so it is the road top. "
                     "The Runner's 1844.44 ft top of dam is the 1966 ground from before the fill; "
                     "Screening's storage curve was one flat point at the lake surface and it "
                     "picked south as downstream (backwards). Record check of 2026-10-19: "
                     "docs/Flagged_structures.md and docs/evidence/DWR-3161/."),
        "date": "2026-10-19",
        "decided_by": "engineer (user decision, 2026-10-19)",
        "followup": ("Questions for DWR: does the record mean the ND-23 road fill; the datum of "
                     "the elevations (NGVD29 vs NAVD88, +1.51 ft here); the crest elevation of "
                     "the road; the normal pool storage (62.5 ac-ft / 22.1 ac fits about 1844.4 "
                     "ft, not 1848 ft); whether a road-embankment check is wanted instead of a "
                     "breach analysis."),
    },
)

_DWR_RX = re.compile(r"^(?:DWR)?[-_ ]?0*(\d+)$")
_NID_RX = re.compile(r"^(?:NID[-_ ]?)?([A-Z]{2}\d{5})$")


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip().upper()
    return "" if s in ("NULL", "NONE") else s


def _keys(dam_id=None, nid_id=None, structure_id=None):
    """The set of normalized keys ('DWR:3161', 'NID:ND00597') the given ids stand for."""
    out = set()
    for v in (dam_id, nid_id, structure_id):
        s = _clean(v)
        if not s:
            continue
        m = _DWR_RX.match(s)
        if m:
            out.add("DWR:" + m.group(1).lstrip("0"))
            continue
        m = _NID_RX.match(s)
        if m:
            out.add("NID:" + m.group(1))
    return out


def entry_keys(entry):
    return _keys(entry.get("dwr_id"), entry.get("nid_id"), entry.get("structure_id"))


def lookup(dam_id=None, nid_id=None, structure_id=None, flags=None):
    """The flag entry (a copy of the dict) for a dam given by any of its ids, else None.

    dam_id may be 'DWR-3161', '3161' (a DWR structure id, as folder names and Screening's
    'Only these dams' use it), 'ND00597' or 'NID-ND00597'; nid_id an NID id; structure_id a
    DWR structure id. Never raises."""
    try:
        want = _keys(dam_id, nid_id, structure_id)
        if not want:
            return None
        for e in (FLAGS if flags is None else flags):
            if entry_keys(e) & want:
                return dict(e)
    except Exception:                                  # noqa  (a flag check never fails a run)
        return None
    return None


def lookup_folder(folder, record=None, flags=None):
    """The flag for a dam folder: its name, and the dam.json record's dam_id / nid_id when a
    record (damsuite.dam_record.DamRecord or a dict) is given."""
    ids = [os.path.basename(os.path.normpath(folder or ""))]
    if record is not None:
        for k in ("dam_id", "nid_id", "structure_id"):
            try:
                ids.append(record.get(k))
            except Exception:                          # noqa
                pass
    for i in ids:
        e = lookup(i, flags=flags)
        if e:
            return e
    return None


def label(entry):
    return CLASS_LABELS.get(entry.get("structure_class"), str(entry.get("structure_class") or
                                                              "flagged structure"))


def note(entry):
    """'road fill over backwater; breach analysis not applicable; hazard Low (DWR record)'."""
    return "{}; breach analysis {}; hazard {} ({})".format(
        label(entry), entry.get("breach_analysis") or "not applicable", entry.get("hazard"),
        entry.get("hazard_source") or "record")


def short_ref(entry):
    return "{} / {}".format(entry.get("dwr_id"), entry.get("nid_id"))


def allowed(option=None, env=None):
    """True when a flagged structure may be run anyway: the option, or ALLOW_FLAGGED_STRUCTURE
    = 1 / true / yes / on in the environment."""
    if option:
        return True
    v = str((os.environ if env is None else env).get(ENV_ALLOW, "")).strip().lower()
    return v in ("1", "true", "yes", "on")


def refusal(entry, what="breach run", dam_id=None, how=None):
    """The message a tool stops with for a flagged structure."""
    how = how or "the ALLOW_FLAGGED_STRUCTURE parameter or the environment variable {}=1".format(
        ENV_ALLOW)
    return ("{} ({}, {}) is a flagged structure: {}. {}: no {}. Reason: {} To run it anyway "
            "(for information only; the hazard of record stays {}), set {}. See "
            "docs/Flagged_structures.md.".format(
                dam_id or entry.get("dwr_id"), entry.get("name"), short_ref(entry), note(entry),
                "Not modelled" if "run" in what else "Not classified", what,
                entry.get("reason"), entry.get("hazard"), how))


def overridden_text(entry, dam_id=None):
    return ("FLAGGED STRUCTURE (run allowed by ALLOW_FLAGGED_STRUCTURE): {} ({}) is a {}. The "
            "hazard of record is {} ({}); the results of this run are for information only."
            .format(dam_id or entry.get("dwr_id"), short_ref(entry), note(entry),
                    entry.get("hazard"), entry.get("hazard_source")))


def problems(flags=None):
    """[] when every entry is complete and no two entries share an id."""
    out, seen = [], {}
    for i, e in enumerate(FLAGS if flags is None else flags):
        for k in REQUIRED:
            if not str(e.get(k) or "").strip():
                out.append("entry {}: {} is empty".format(i, k))
        if e.get("structure_class") not in CLASS_LABELS:
            out.append("entry {}: unknown structure_class {!r}".format(i, e.get("structure_class")))
        if e.get("hazard") not in HAZARDS:
            out.append("entry {}: hazard {!r} is not one of {}".format(i, e.get("hazard"), HAZARDS))
        ks = entry_keys(e)
        if not any(k.startswith("DWR:") for k in ks) and not any(k.startswith("NID:") for k in ks):
            out.append("entry {}: no usable DWR or NID id".format(i))
        for k in ks:
            if k in seen:
                out.append("entry {}: {} also in entry {}".format(i, k, seen[k]))
            seen[k] = i
    return out
