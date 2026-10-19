"""3.10.23: flagged structures (damsuite/structure_flags.py, docs/Flagged_structures.md).

DWR-3161 / ND00597 (Sanish Bay WPA Dam 1) is the ND-23 road fill over backwater: breach analysis
not applicable, hazard Low from the DWR record. The Runner refuses a single-dam breach run of it
unless ALLOW_FLAGGED_STRUCTURE (parameter / environment / CLI flag); the batch tools skip it with
Status 'flagged' and the reason in Batch_Breach_Runs.csv; the Dam suite home shows the flag."""
import csv
import os
import shutil
import sys

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.preflight_stub import without_dem_and_smoke_checks

NOTE = "road fill over backwater; breach analysis not applicable; hazard Low (DWR record)"
OLD_BATCH_FIELDS = ("Dam", "Folder", "Status", "Scenario", "Final_Model", "Peak_Breach_cfs",
                    "Storage_At_Failure_acft", "Flooded_ac", "Max_Depth_ft", "Max_Velocity_fps",
                    "Latest_Arrival_hr", "Receptors_Flooded", "Runs", "Domain_mi", "Minutes",
                    "Warnings", "Message")


class _Env(object):
    """ALLOW_FLAGGED_STRUCTURE set / unset for the block, restored after."""
    def __init__(self, value=None):
        self.value, self.saved = value, None

    def __enter__(self):
        self.saved = os.environ.get("ALLOW_FLAGGED_STRUCTURE")
        if self.value is None:
            os.environ.pop("ALLOW_FLAGGED_STRUCTURE", None)
        else:
            os.environ["ALLOW_FLAGGED_STRUCTURE"] = self.value
        return self

    def __exit__(self, *a):
        if self.saved is None:
            os.environ.pop("ALLOW_FLAGGED_STRUCTURE", None)
        else:
            os.environ["ALLOW_FLAGGED_STRUCTURE"] = self.saved


@test
def flagged_structures_list_loads_and_finds_dwr3161_by_every_id():
    from nd_breach_runner.damsuite import structure_flags as sf
    import nd_breach_runner.damsuite as ds
    assert sf.problems() == [], sf.problems()
    assert ds.selfcheck() == [], ds.selfcheck()
    e = sf.lookup("DWR-3161")
    assert e["nid_id"] == "ND00597" and e["name"] == "Sanish Bay WPA Dam 1"
    assert e["structure_class"] == "road_fill_backwater" and e["hazard"] == "Low"
    assert e["hazard_source"] == "DWR record" and e["date"] == "2026-10-19"
    assert "0023048401" in e["evidence"] and "Lake Sakakawea" in e["evidence"]
    assert "3.10.16" in e["reason"] and "artifact" in e["reason"]
    assert sf.note(e) == NOTE
    for args, kw in ((("dwr-3161",), {}), (("3161",), {}), (("DWR_03161",), {}),
                     (("ND00597",), {}), (("NID-ND00597",), {}), ((), {"nid_id": "nd00597"}),
                     ((), {"structure_id": 3161}), ((), {"structure_id": 3161.0}),
                     (("other",), {"nid_id": "ND00597"})):
        assert sf.lookup(*args, **kw) is not None, (args, kw)
    for v in ("DWR-3162", "31610", "ND00598", "", None, "NULL", "DWR-"):
        assert sf.lookup(v) is None, v
    e2 = sf.lookup("DWR-3161")
    e2["hazard"] = "High"                                   # a copy: the list is not changed
    assert sf.lookup("DWR-3161")["hazard"] == "Low"
    # problems() catches an incomplete or duplicated entry
    bad = [dict(sf.FLAGS[0]), dict(sf.FLAGS[0], reason="", hazard="Medium")]
    p = sf.problems(bad)
    assert any("reason is empty" in x for x in p) and any("hazard 'Medium'" in x for x in p)
    assert any("also in entry 0" in x for x in p), p
    # the environment switch
    assert not sf.allowed(env={}) and sf.allowed(env={"ALLOW_FLAGGED_STRUCTURE": "1"})
    assert sf.allowed(True, env={}) and not sf.allowed(env={"ALLOW_FLAGGED_STRUCTURE": "0"})


@test
def flagged_structures_module_is_identical_in_the_three_plugins():
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    root = os.path.dirname(here)
    copies = [os.path.join(root, p, "damsuite", "structure_flags.py")
              for p in ("DamSafety_Runner", "DamSafety_Hazard", "DamSafety_Screening")]
    have = [c for c in copies if os.path.isfile(c)]
    if len(have) < 3:
        return                                             # an installed single plugin
    data = [open(c, "rb").read() for c in have]
    assert data[0] == data[1] == data[2]


def _algs():
    from nd_breach_runner.tests.test_v320 import _algs as a
    return a()


@test
def rundam_refuses_a_flagged_structure_unless_allowed():
    from nd_breach_runner.tests.test_v320 import _tmp
    algs, fq = _algs()
    d = _tmp()
    try:
        a = algs.RunDamAlgorithm()
        a.initAlgorithm()
        assert a.parameterDefinition("ALLOW_FLAGGED_STRUCTURE") is not None
        ctx = fq.QgsProcessingContext()
        assert a.parameterAsBool({}, "ALLOW_FLAGGED_STRUCTURE", ctx) is False     # off by default
        base = {"DAMS_FOLDER": d, "HEIGHT_FT": 28.1, "STORAGE_ACFT": 2861.0}
        with _Env(None):
            for extra in ({"DAM_ID": "DWR-3161"}, {"DAM_ID": "3161"},
                          {"DAM_ID": "SANISH", "NID_ID": "ND00597"},
                          {"DAM_ID": "SANISH", "STRUCTURE_ID": "3161"}):
                try:
                    a.dam_from_params(dict(base, **extra), ctx)
                    raise AssertionError("expected a refusal for {}".format(extra))
                except fq.QgsProcessingException as e:
                    msg = str(e)
                    assert "flagged structure" in msg and NOTE in msg, msg
                    assert "ALLOW_FLAGGED_STRUCTURE" in msg and "Not modelled" in msg, msg
                    assert "ND00597" in msg and "Sanish Bay WPA Dam 1" in msg, msg
            # another dam is not touched
            dam = a.dam_from_params(dict(base, DAM_ID="DWR-5289"), ctx)
            assert dam.dam_id == "DWR-5289"
            assert not any("FLAGGED" in n for n in a._rec_info["read"])
            # allowed by the parameter: the run goes on, the note is in the log / run report
            dam = a.dam_from_params(dict(base, DAM_ID="DWR-3161", ALLOW_FLAGGED_STRUCTURE=True), ctx)
            assert dam.dam_id == "DWR-3161" and dam.dam_height_ft == 28.1
            first = a._rec_info["read"][0]
            assert first.startswith("FLAGGED STRUCTURE (run allowed by ALLOW_FLAGGED_STRUCTURE)"), first
            assert "hazard of record is Low (DWR record)" in first and "information only" in first
        with _Env("1"):                                    # allowed by the environment
            dam = a.dam_from_params(dict(base, DAM_ID="DWR-3161"), ctx)
            assert a._rec_info["read"][0].startswith("FLAGGED STRUCTURE")
        # a dam.json NID id flags a folder named otherwise
        from nd_breach_runner.tests.test_v320 import _record
        _record(os.path.join(d, "SANISH2"), nid_id="ND00597", height_ft=28.1, storage_acft=2861.0)
        with _Env(None):
            try:
                a.dam_from_params({"DAM_ID": "SANISH2", "DAMS_FOLDER": d}, ctx)
                raise AssertionError("expected a refusal (dam.json NID id)")
            except fq.QgsProcessingException as e:
                assert "flagged structure" in str(e)
            # the time estimate is not a breach run: noted, not refused
            est = algs.EstimateAlgorithm()
            est.initAlgorithm()
            est.dam_from_params(dict(base, DAM_ID="DWR-3161"), ctx)
            assert est._rec_info["read"][0] == "Flagged structure: " + NOTE + " (DWR-3161 / ND00597)"
            # the sensitivity tool is a breach run: refused
            sens = algs.SensitivityAlgorithm()
            sens.initAlgorithm()
            try:
                sens.dam_from_params(dict(base, DAM_ID="DWR-3161"), ctx)
                raise AssertionError("expected a refusal (sensitivity)")
            except fq.QgsProcessingException:
                pass
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
@without_dem_and_smoke_checks
def batch_skips_a_flagged_structure_and_records_the_reason():
    from nd_breach_runner.tests.test_v320 import _tmp
    algs, fq = _algs()
    assert algs.BATCH_FIELDS[:len(OLD_BATCH_FIELDS)] == OLD_BATCH_FIELDS     # names kept
    assert algs.BATCH_FIELDS[len(OLD_BATCH_FIELDS):] == ("Structure_Flag", "Hazard_Of_Record")
    d = _tmp()
    try:
        os.makedirs(os.path.join(d, "DWR-3161"))
        open(os.path.join(d, "DWR-3161", "Depth (Max).tif"), "w").close()   # an old result

        class _R:
            def crs(self):
                return fq.QgsCoordinateReferenceSystem("EPSG:26914")

            def source(self):
                return "synthetic"
        pt = lambda: fq.FakeGeometry("point", [[(0.0, 0.0)]])
        feats = [fq.FakeFeature(pt(), ID="DWR-3161", H=28.1, S=2861.0),
                 fq.FakeFeature(pt(), ID="SANISH", NIDID="ND00597", H=28.1, S=2861.0),
                 fq.FakeFeature(pt(), ID="ND00001", H=10.0, S=500.0)]
        a = algs.BatchAlgorithm()
        a.initAlgorithm()
        params = {"DAMS": fq.FakeSource(feats), "F_ID": "ID", "F_HEIGHT": "H", "F_STORAGE": "S",
                  "DEM": _R(), "DAMS_FOLDER": d, "LISFLOOD_EXE": sys.executable}
        fb = fq.QgsProcessingFeedback()
        with _Env(None):
            r = a.processAlgorithm(params, fq.QgsProcessingContext(), fb)
        with open(r["SUMMARY"], newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            assert tuple(rd.fieldnames) == algs.BATCH_FIELDS
            rows = {row["Dam"]: row for row in rd}
        for dam in ("DWR-3161", "SANISH"):
            row = rows[dam]
            assert row["Status"] == "flagged", row          # not "skipped: already has Depth"
            assert row["Structure_Flag"] == NOTE and row["Hazard_Of_Record"] == "Low (DWR record)"
            assert "flagged structure" in row["Message"] and "Not modelled" in row["Message"]
        assert rows["ND00001"]["Status"] != "flagged" and rows["ND00001"]["Structure_Flag"] == ""
        assert "2 flagged structure(s) not run" in r["COUNTS"], r["COUNTS"]
        assert any("DWR-3161: skipped, flagged structure" in m for m in fb.info)
        # ALLOW_FLAGGED_STRUCTURE=1: it is run (here it fails later: no GDAL DEM), the flag noted
        with _Env("1"):
            r = a.processAlgorithm(dict(params, SKIP_DONE=False), fq.QgsProcessingContext(),
                                   fq.QgsProcessingFeedback())
        with open(r["SUMMARY"], newline="", encoding="utf-8") as fh:
            rows = {row["Dam"]: row for row in csv.DictReader(fh)}
        assert rows["DWR-3161"]["Status"] != "flagged"
        assert rows["DWR-3161"]["Structure_Flag"] == NOTE + " (run allowed)"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def cli_refuses_and_batch_skips_a_flagged_structure():
    from nd_breach_runner.tests.test_cli import cli, run_cli, Tmp, base_config, dump, _FakeResult
    c = cli()
    assert c.BATCH_FIELDS[:len(OLD_BATCH_FIELDS)] == OLD_BATCH_FIELDS
    assert c.BATCH_FIELDS[len(OLD_BATCH_FIELDS):] == ("Structure_Flag", "Hazard_Of_Record")
    with Tmp() as d, _Env(None):
        p, cfg = base_config(d)
        cfg["dam"] = dict(cfg.get("dam") or {}, dam_id="DWR-3161")
        dump(p, cfg)
        code, out, err = run_cli("run", p)
        assert code == 2 and "flagged structure" in err and "--allow-flagged-structure" in err, (
            code, out, err)
        cfg["dam"] = {"point": None}
        cfg["batch"] = {"crs": None}
        cfg["scenario"] = {"type": "sunny"}
        dump(p, cfg)
        table = os.path.join(d, "dams.csv")
        with open(table, "w") as f:
            f.write("NIDID,Dam Name,Dam Height (ft),Max Storage (acre-feet),Surface Area (acres),"
                    "Top of Dam (ft),x,y\n")
            f.write("ND00597,Sanish,28.1,2861,335,1868.6,0,0\n")
            f.write("A1,Alpha,25,20,4,1850,0,0\n")
        fake_exe = os.path.join(d, "lisflood")
        with open(fake_exe, "w") as f:
            f.write("x")
        calls = []

        def fake_run_dam(dam, dam_xy, st, terrain, writer, folder, inflow_xy, progress, log,
                         cancel):
            calls.append(dam.dam_id)
            return _FakeResult(dam.dam_id)
        keep = c.workflow.run_dam
        c.workflow.run_dam = fake_run_dam
        try:
            code, out, err = run_cli("batch", table, p, "--lisflood", fake_exe)
        finally:
            c.workflow.run_dam = keep
        with open(os.path.join(d, "out", "Batch_Breach_Runs.csv")) as fh:
            rows = {r["Dam"]: r for r in csv.DictReader(fh)}
        assert calls == ["A1"], (calls, out, err)
        assert rows["ND00597"]["Status"] == "flagged" and rows["ND00597"]["Structure_Flag"] == NOTE
        assert rows["ND00597"]["Hazard_Of_Record"] == "Low (DWR record)"
        assert rows["A1"]["Status"] == "done"
        assert "1 flagged structure(s) not run" in out, out


@test
def suite_home_shows_the_flag_and_low_for_a_flagged_dam_folder():
    from nd_breach_runner.damsuite import suite_home as sh, dam_record as dr
    from nd_breach_runner.tests.test_v320 import _tmp
    d = _tmp()
    try:
        f = os.path.join(d, "DWR-3161")
        rec = dr.load(f, create=True)
        rec.set_many({"name": "Sanish Bay WPA Dam 1", "nid_id": "ND00597"}, "ndhazard", "2.18.30")
        rec.add_status("classified", "ndhazard", "2.18.30", result={"nd_class": "High"})
        rec.save()
        g = os.path.join(d, "DWR-5289")
        dr.load(g, create=True).save()
        rows = {r["dam_id"]: r for r in sh.scan(d)}
        r = rows["DWR-3161"]
        assert r["flag"] == NOTE and r["class"] == "Low" and r["next"] == "none", r
        assert "Flagged structure (DWR-3161 / ND00597)" in r["next_text"]
        assert "The recorded class High is not used." in r["next_text"], r["next_text"]
        assert rows["DWR-5289"]["flag"] == "" and rows["DWR-5289"]["class"] == ""
        assert rows["DWR-5289"]["next"] != "none", rows["DWR-5289"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
