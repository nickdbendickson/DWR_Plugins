"""damsuite.display_units (Core 1.1.0), pure Python. Run: python3 DamSafety_Core/tests/test_display_units.py
(or pytest). The real-QGIS dialog check is qgis_unit_dialog_check.py beside this file."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_Core.damsuite import display_units as DU   # noqa: E402


def test_system_default_and_env(monkeypatch=None):
    old = os.environ.pop(DU.ENV, None)
    try:
        assert DU.system() == DU.US
        os.environ[DU.ENV] = "metric"
        assert DU.system() == DU.SI
        os.environ[DU.ENV] = "nonsense"
        assert DU.system() == DU.US
    finally:
        os.environ.pop(DU.ENV, None)
        if old is not None:
            os.environ[DU.ENV] = old


def test_us_changes_nothing():
    for u in ("ft", "acres", "ac-ft", "cfs", "ft/s", "miles", "in", "m", "mm"):
        assert DU.factor(u, "us") == 1.0
        assert DU.display_unit(u, "us") == u
    d = "Crest width (ft; starting value 15 is the default)"
    assert DU.relabel(d, "us") == d


def test_si_factors_round_trip():
    cases = {"ft": (15.0, 4.572, "m"), "acres": (1.0, 0.40468564224, "ha"),
             "ac-ft": (1.0, 1233.48183754752, "m³"), "cfs": (1.0, 0.028316846592, "m³/s"),
             "ft/s": (10.0, 3.048, "m/s"), "miles": (1.0, 1.609344, "km"), "in": (1.0, 25.4, "mm")}
    for u, (stored, shown, du) in cases.items():
        assert abs(DU.to_display(stored, u, "si") - shown) < 1e-9, u
        assert abs(DU.from_display(shown, u, "si") - stored) < 1e-9, u
        assert DU.display_unit(u, "si") == du
    # metric units are not converted in either system
    assert DU.factor("m", "si") == 1.0 and DU.factor("mm", "si") == 1.0


def test_unit_in_label():
    assert DU.unit_in_label("Crest width (ft): TR-60") == "ft"
    assert DU.unit_in_label("Largest pond (acres; YOUR value)") == "acres"
    assert DU.unit_in_label("Storm depth (in): PMP") == "in"
    assert DU.unit_in_label("Cell size (in the plan's CRS)") is None
    assert DU.unit_in_label("Cell size (m)") is None
    assert DU.unit_in_label("Arrival (hours)") is None
    # a value whose unit depends on another choice is never converted
    d = "Flood scenario: failure time (hr) or pool elevation (ft)"
    assert DU.unit_in_label(d) is None and DU.relabel(d, "si") == d
    assert DU.unit_in_label("Breach (Froehlich 2008) width (ft)") is None


def test_relabel_keeps_meaning_of_numbers():
    r = DU.relabel
    assert r("Pool (ft)", "si") == "Pool (m)"
    assert r("Channels: bottom width (ft; starting value 15 is the default)", "si") == \
        "Channels: bottom width (m; starting value 15 ft is the default)"
    assert r("Span (ft; 0 or 99999.9 = not recorded)", "si") == \
        "Span (m; 0 ft or 99999.9 ft = not recorded)"
    assert r("Roads (ft; as tool 8)", "si") == "Roads (m; as tool 8)"
    assert r("Invert (ft; 1 ft to start)", "si") == "Invert (m; 1 ft to start)"
    assert r("Area (acres; 0 = no limit)", "si") == "Area (ha; 0 acres = no limit)"
    assert r("Cell size (in the plan)", "si") == "Cell size (in the plan)"


def test_fmt():
    assert DU.fmt(15, "ft", "si") == "4.572 m"
    assert DU.fmt(15, "ft", "us") == "15 ft"


def test_convert_text_quantities_and_chains():
    c = lambda t: DU.convert_text(t, "si")                         # noqa: E731
    assert c("3,215 cfs") == "91 m³/s"
    assert c("Maximum depth / velocity | 17.2 ft / 9.8 ft/s") == \
        "Maximum depth / velocity | 5.24 m / 2.99 m/s"
    assert c("10.21 x 9.23 mi") == "16.43 x 14.85 km"
    # a quantity before a colon is not a key named by its unit
    assert c("8,049.2 / 8,011.4 ac (at or above 1 ft: 2,198.9 / 2,169.9 ac)") == \
        "3,257.4 / 3,242.1 ha (at or above 0.305 m: 889.9 / 878.1 ha)"
    # every number of a chain carries the unit at its end
    assert c("at or above 0.1 / 1 / 2 ft") == "at or above 0.0305 / 0.305 / 0.61 m"
    assert c("4.86 / 13.09 vs 4.84 / 13.09 ft/s") == "1.48 / 3.99 vs 1.48 / 3.99 m/s"
    assert c("- peak cfs: 28671.4") == "- peak m³/s: 811.9"
    # numbers that are not quantities, and metric ones, are left alone
    for t in ("REG_05.2026 5.4(c)", "v3.10.84 at 10 m", "at (-106230, 2666610)", "5 min",
              "96 h", "10 in the pool"):
        assert c(t) == t, t
    assert DU.convert_text("3,215 cfs", "us") == "3,215 cfs"


def test_convert_table_and_markdown():
    head, rows = DU.convert_table(["ID", "Depth ft", "Vel ft/s", "Arrival h"],
                                  [["7", "12.3", "4.5", "1.25"]], "si")
    assert head == ["ID", "Depth m", "Vel m/s", "Arrival h"]
    assert rows == [["7", "3.75", "1.37", "1.25"]]
    md = ("| Pass | Size (mi) | Edge depth N/S/E/W (ft) |\n|---|---|---|\n"
          "| 1 | 4.21 x 3.23 | 0.0/5.89/2.93/0.0 |")
    out = DU.convert_markdown(md, "si").split("\n")
    assert out[0] == "| Pass | Size (km) | Edge depth N/S/E/W (m) |"
    assert out[2] == "| 1 | 6.78 x 5.2 | 0/1.8/0.893/0 |"
    assert DU.convert_markdown(md, "us") == md


# -- SI copies of the result CSVs (Core 1.3.0) --------------------------------------------------

def _csv(path, rows):
    import csv
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return path


def _rows(path):
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_csv_column_unit_reads_the_unit_from_the_name():
    u = DU.csv_column_unit
    assert u("Max_Depth_ft") == ("ft", "Max_Depth_m")
    assert u("Max_Vel_fps") == ("ft/s", "Max_Vel_mps")
    assert u("peak_cfs") == ("cfs", "peak_cms")
    assert u("Storage_acft") == ("ac-ft", "Storage_m3")
    assert u("Area_ac") == ("acres", "Area_ha") and u("Flooded_acres") == ("acres", "Flooded_ha")
    assert u("dist_mi") == ("miles", "dist_km") and u("drain_mi2") == ("sq mi", "drain_km2")
    assert u("DV_Max_ft2s") == ("ft²/s", "DV_Max_m2s") and u("Area_ft2") == ("ft²", "Area_m2")
    assert u("HEIGHT_FT") == ("ft", "HEIGHT_M")
    assert u("Peak (cfs)") == ("cfs", "Peak (m³/s)") and u("Depth (ft)") == ("ft", "Depth (m)")
    # inches only after a word that means a depth of water
    assert u("storm_depth_in") == ("in", "storm_depth_mm")
    for name in ("built_in", "also_in", "Notes", "cell_m", "Left", "draft", "ft", "Crest ft",
                 "Struct_ID", "Arrival_hr", ""):
        assert u(name) == (None, None), name


def test_si_table_puts_an_si_column_beside_each_us_one():
    names, rows = DU.si_table(
        ["id", "Max_Depth_ft", "peak_cfs", "Notes", "width_ft", "Top_ft", "Top_m"],
        [["1", "3.25", "28671", "x", "wide", "1069.117", "325.87"],
         ["2", "", "1,200.5", "y", "", "", ""],
         ["3", "n/a", "0", "z", "", "", ""]])
    # width_ft holds no number and Top_m is already there: neither gets a new column
    assert names == ["id", "Max_Depth_ft", "Max_Depth_m", "peak_cfs", "peak_cms", "Notes",
                     "width_ft", "Top_ft", "Top_m"], names
    assert rows[0] == ["1", "3.25", "0.9906", "28671", "811.9", "x", "wide", "1069.117",
                       "325.87"], rows[0]
    assert rows[1][:5] == ["2", "", "", "1,200.5", "33.99"], rows[1]
    assert rows[2][:5] == ["3", "n/a", "", "0", "0"], rows[2]
    # the US values are exactly as they were
    assert [r[1] for r in rows] == ["3.25", "", "n/a"] and [r[3] for r in rows] == \
        ["28671", "1,200.5", "0"]
    assert DU.si_table(["id", "Notes"], [["1", "x"]]) == (None, None)


def test_write_si_csv_only_under_si_and_never_touches_the_table(tmp_path=None):
    import tempfile
    import shutil
    tmp = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp(prefix="si_csv_")
    try:
        src = _csv(os.path.join(tmp, "run", "receptors_flooded.csv"),
                   [["id", "depth_ft", "vel_fps"], ["a", "2.0", "10"], ["b", "0.5", ""]])
        before = open(src, "rb").read()
        assert DU.write_si_csv(src, "us") == ""
        assert not os.path.exists(os.path.join(tmp, "run", DU.SI_CSV_DIR))
        out = DU.write_si_csv(src, "si")
        assert out == os.path.join(tmp, "run", "SI_units", "receptors_flooded.csv"), out
        assert _rows(out) == [["id", "depth_ft", "depth_m", "vel_fps", "vel_mps"],
                              ["a", "2.0", "0.6096", "10", "3.048"],
                              ["b", "0.5", "0.1524", "", ""]], _rows(out)
        assert open(src, "rb").read() == before and DU.is_si_copy(out)
        assert DU.write_si_csv(out, "si") == ""                    # never a copy of a copy
        # a table that no longer has a unit column: the old copy goes
        _csv(src, [["id", "note"], ["a", "x"]])
        assert DU.write_si_csv(src, "si") == "" and not os.path.exists(out)
        assert DU.write_si_csv(os.path.join(tmp, "missing.csv"), "si") == ""
        assert DU.write_si_csv("", "si") == ""
    finally:
        if tmp_path is None:
            shutil.rmtree(tmp, ignore_errors=True)


def test_write_si_csvs_in_a_folder_and_of_a_tools_results(tmp_path=None):
    import tempfile
    import shutil
    tmp = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp(prefix="si_csv_")
    try:
        run = os.path.join(tmp, "run")
        a = _csv(os.path.join(run, "a.csv"), [["x_ft"], ["1"]])
        _csv(os.path.join(run, "b.csv"), [["name"], ["n"]])
        deep = _csv(os.path.join(run, "inputs", "pmf.csv"), [["q_cfs"], ["100"]])
        made = DU.write_si_csvs_in(run, "si")
        assert made == [os.path.join(run, "SI_units", "a.csv")], made
        assert not os.path.exists(os.path.join(run, "inputs", "SI_units"))
        # a second pass makes nothing new and no copy of the copies
        assert DU.write_si_csvs_in(run, "si") == made
        assert os.listdir(os.path.join(run, "SI_units")) == ["a.csv"]
        assert DU.write_si_csvs_in(run, "si", recursive=True) == [
            made[0], os.path.join(run, "inputs", "SI_units", "pmf.csv")]
        # a table that is gone takes its copy with it
        os.remove(a)
        assert DU.write_si_csvs_in(run, "si") == []
        assert not os.path.exists(os.path.join(run, "SI_units"))
        assert DU.write_si_csvs_in(run, "us") == [] and DU.write_si_csvs_in("", "si") == []

        # what a tool returned: its CSV values, and the folder of its report
        out = os.path.join(tmp, "out")
        summary = _csv(os.path.join(out, "Batch_Breach_Runs.csv"), [["Peak_cfs"], ["5"]])
        rec = _csv(os.path.join(out, "model", "receptors_flooded.csv"), [["depth_ft"], ["2"]])
        report = os.path.join(out, "model", "run_report.md")
        open(report, "w").close()
        dem_dir = os.path.join(tmp, "dem")
        _csv(os.path.join(dem_dir, "tiles.csv"), [["size_ft"], ["3"]])
        vrt = os.path.join(dem_dir, "dem.vrt")
        open(vrt, "w").close()
        res = {"SUMMARY": summary, "REPORT": report, "VRT": vrt, "FOLDER": dem_dir,
               "COUNTS": "3 run", "N": 4, "NONE": None, "DEPTH": ""}
        assert DU.write_si_csvs_of(res, "us") == []
        made = DU.write_si_csvs_of(res, "si")
        assert made == [os.path.join(out, "SI_units", "Batch_Breach_Runs.csv"),
                        os.path.join(out, "model", "SI_units", "receptors_flooded.csv")], made
        assert not os.path.exists(os.path.join(dem_dir, "SI_units"))   # not a result table
        assert os.path.isfile(rec)
        assert DU.write_si_csvs_of(None, "si") == [] and DU.write_si_csvs_of({}, "si") == []
    finally:
        if tmp_path is None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
            print("ok  ", k)
    print("%d tests ok" % n)
