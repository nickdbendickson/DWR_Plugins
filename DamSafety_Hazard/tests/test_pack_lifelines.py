# -*- coding: utf-8 -*-
"""Hazard 2.20.3: the HIFLD lifeline keys read the offline packs' HIFLD_* layers.

The packs (Data's pack builder, build_nd_pack --hifld-folder) hold HIFLD_fire_ems_stations,
HIFLD_local_eoc, HIFLD_local_law_enforcement and HIFLD_wastewater_treatment_plants, while the
lifeline keys hifld_fire / ems / eoc / police / wastewater were looked up as lifeline_<key> only
(and have no download address), so a run never had them. Tiny synthetic format-1 and format-2
packs in a temp folder; checks:

  * constants.lifeline_sets on open_pack(p).has of BOTH formats: every HIFLD key resolves
    (fire and EMS share one layer, read once, by hifld_fire); the old lifeline_<key> name wins
    when a pack has both; lifeline_fetches(None) is the old download list, with the pack's has
    it adds the HIFLD keys under their pack names;
  * download.pack_has: None / False without a pack, the reader's has with one;
    fetch_arcgis_layer("", extent, folder, "HIFLD_local_eoc") offline only is served from the pack;
  * reproduce._lifeline_rank ranks the HIFLD_* downloads as their lifeline key;
  * suiteexport, pipeline and datafetch use these helpers.

Nothing outside the temp folder is read or written; the user's pack setting is not read
(offline_config is patched).
    python DamSafety_Hazard/tests/test_pack_lifelines.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
for k in ("NDSUITE_SETTINGS", "NDSUITE_PACK", "NDSUITE_SOURCE",
          "ND_DAM_HAZARD_OFFLINE_PACK", "ND_DAM_HAZARD_OFFLINE_ONLY"):
    os.environ.pop(k, None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import constants as C               # noqa: E402
from DamSafety_Hazard import download as DL               # noqa: E402
from DamSafety_Hazard import offline_pack as HOP          # noqa: E402
from DamSafety_Hazard import reproduce as RP              # noqa: E402
from DamSafety_Hazard.damsuite import gpkgpack as GP     # noqa: E402

FAILS = []
BBOX = (-104.1, 45.9, -96.5, 49.1)
HIFLD = {"hifld_fire": "HIFLD_fire_ems_stations", "hifld_eoc": "HIFLD_local_eoc",
         "hifld_police": "HIFLD_local_law_enforcement",
         "hifld_wastewater": "HIFLD_wastewater_treatment_plants"}


def expect(cond, what):
    print("{} {}".format("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def _pt(i, x, y):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": {"id": i, "NAME": "F%d" % i}}


def make_packs(root):
    p1 = os.path.join(root, "nd_offline_pack")
    os.makedirs(p1)
    db = os.path.join(p1, HOP.DB_NAME)
    w = HOP.PackWriter(db, BBOX)
    for i, name in enumerate(sorted(HIFLD.values()) + ["lifeline_nd_hospitals"]):
        w.begin(name, name, "https://example.invalid/" + name)
        w.add([_pt(i, -100.0 + 0.01 * i, 47.0)], lambda f: f["properties"]["id"])
        w.finish()
    import json
    man = w.manifest()
    w.close(finalize=True)
    with open(os.path.join(p1, HOP.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(man, fh)
    p2 = os.path.join(root, "ND_Dam_Suite_Pack")
    GP.write_manifest(p2, GP.convert_v1(db, p2))
    return p1, p2


def main():
    work = tempfile.mkdtemp(prefix="hz_lifelines_")
    saved = DL.offline_config
    try:
        p1, p2 = make_packs(work)
        for label, pk in (("format 1", p1), ("format 2", p2)):
            rd = HOP.open_pack(pk)
            got = {k: ds for k, _l, _u, _c, ds in C.lifeline_sets(rd.has)}
            expect(got == dict(HIFLD, nd_hospitals="lifeline_nd_hospitals"),
                   "{}: lifeline_sets finds every HIFLD key ({})".format(label, got))
            fx = {k: ds for k, _l, _u, _c, ds in C.lifeline_fetches(rd.has)}
            expect(all(fx.get(k) == v for k, v in HIFLD.items()) and "hifld_ems" not in fx,
                   "{}: lifeline_fetches adds the HIFLD keys, EMS read once with fire".format(label))
        old = {"lifeline_hifld_eoc", "HIFLD_local_eoc", "lifeline_hifld_ems",
               "HIFLD_fire_ems_stations"}
        got = {k: ds for k, _l, _u, _c, ds in C.lifeline_sets(old.__contains__)}
        expect(got == {"hifld_fire": "HIFLD_fire_ems_stations", "hifld_ems": "lifeline_hifld_ems",
                       "hifld_eoc": "lifeline_hifld_eoc"},
               "the old lifeline_<key> name wins ({})".format(got))
        base = [(k, "lifeline_" + k) for k, _l, u, _c in C.LIFELINE_SOURCES if u]
        expect([(k, ds) for k, _l, _u, _c, ds in C.lifeline_fetches(None)] == base and
               [t[0] for t in C.lifeline_downloads()] == [b[0] for b in base],
               "lifeline_fetches(None) is the download list")
        expect(C.lifeline_sets(lambda n: 1 / 0) == [], "a has() that raises: nothing")

        DL.offline_config = lambda: ("", False)
        expect(DL.pack_has() is None and DL.pack_has("HIFLD_local_eoc") is False,
               "pack_has: None / False without a pack")
        for label, pk in (("format 1", p1), ("format 2", p2)):
            DL.offline_config = lambda pk=pk: (pk, True)
            has = DL.pack_has()
            expect(callable(has) and has("HIFLD_local_eoc") and DL.pack_has("HIFLD_local_eoc")
                   and not DL.pack_has("lifeline_hifld_eoc"),
                   "{}: pack_has is the reader's has".format(label))
            out = os.path.join(work, "run_" + label[-1])
            os.makedirs(out)
            lyr, n, _path = DL.fetch_arcgis_layer("", (-101.0, 46.5, -99.0, 47.5), out,
                                                  "HIFLD_local_eoc")
            expect(lyr is not None and n == 1,
                   "{}: HIFLD_local_eoc served from the pack offline only ({})".format(label, n))
        DL.offline_config = lambda: (os.path.join(work, "missing"), False)
        expect(DL.pack_has() is None, "pack_has: None for a pack that cannot be opened")

        order = [k for k, _l, _u, _c in C.LIFELINE_SOURCES]
        for key, name in HIFLD.items():
            r = RP._lifeline_rank(os.path.join(out, "{}_abc123.geojson".format(name)))
            expect(r == order.index(key) + 1, "_lifeline_rank({}) = {}".format(name, r))
        expect(RP._lifeline_rank("/x/lifeline_hifld_eoc_abc.geojson") == order.index("hifld_eoc") + 1,
               "_lifeline_rank: the old name still ranks")

        src = {}
        for m in ("suiteexport", "pipeline", "datafetch"):
            with open(os.path.join(ROOT, "DamSafety_Hazard", m + ".py"), encoding="utf-8") as fh:
                src[m] = fh.read()
        expect("C.lifeline_sets(source.has)" in src["suiteexport"], "suiteexport: lifeline_sets")
        expect("lifeline_fetches(pack_has())" in src["pipeline"] and
               "lifeline_fetches(pack_has())" in src["datafetch"],
               "pipeline and datafetch: lifeline_fetches(pack_has())")
    finally:
        DL.offline_config = saved
        shutil.rmtree(work, ignore_errors=True)
    if FAILS:
        print("FAILED: {}".format(len(FAILS)))
        sys.exit(1)
    print("test_pack_lifelines: all passed")


if __name__ == "__main__":
    main()
