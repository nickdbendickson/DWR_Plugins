"""Runner 3.10.89, review round 3 (Nick, Absaraka DWR-2801): every tool can read the DEM's unit,
or says how to.

1. The HEC-RAS terrain (Import HEC-RAS results, ndbreach_cli hecras-reexport): one that records
   no unit stopped, naming a 'DEM elevation units' those tools do not have (round 2). It is read
   in the Runner's own record of its terrain, else in its CRS's linear unit as before 3.10.88,
   with a warning naming the unit and the tools' own 'Terrain elevation units' / --terrain-z.
2. An explicit unit stops on a band with a scale / offset (it ran on the raw values) and on a
   warped VRT whose vertical shift converted the values, as Detect does.
3. An explicit unit the record contradicts: the DEM median decides - North Dakota ground only in
   the chosen unit runs with a warning; only in the recorded unit stops; unclear stops and names
   the appended 'Feet (the raster's unit tag is wrong)' choices, which run with a warning.
4. Tests the round-2 mutation run left open: the CLI's explicit check, -novshift in the carve and
   the refill, the z_check flag through the terrain wrappers and run_dam, the decimated median
   fallback, the metadata-only gft* refusal, the facts file-type rule, the QGIS '|' source, the
   exact guidance of a chosen unit, hecras_model._z_factor for an old run report.
5. Run outputs are written in the horizontal part of a compound CRS: a later warp without
   -novshift (DamSafety Hazard's reproject_raster) does not rescale them.
Needs GDAL (the QGIS Python); prints SKIP otherwise."""
import json
import os
import shutil
import tempfile
import types

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_dem_z_detect import _dem, _gis
from nd_breach_runner.tests.test_dem_z_download_record import _old_download
from nd_breach_runner.tests.test_dem_z_once import (BOX, CX, CY, US_FT, _algs, _crs_dem, _med,
                                                    _nd_download, _read, _stops)

try:
    from osgeo import gdal, osr
except ImportError:                                      # pragma: no cover - plain python3
    gdal = None

FT = 0.3048
SP = 1968.5                                              # 600 m in feet (ND State Plane)


def _srs(text):
    s = osr.SpatialReference()
    s.SetFromUserInput(text)
    return s


def _utm_grid(n=20):
    from nd_breach_runner.core.grid import Grid
    return Grid(np.zeros((n, n)), BOX[0], BOX[3], 10.0, crs_wkt=_srs("EPSG:26914").ExportToWkt())


def _skip(name):
    if gdal is None:
        print("SKIP {} (no GDAL)".format(name))
        return True
    return False


@test
def an_untagged_hecras_terrain_is_read_in_its_crs_unit_with_a_warning():
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import hecras_engine as he
    d = tempfile.mkdtemp(prefix="demz_r3_t_")
    try:
        grid = _utm_grid()
        sp = _crs_dem(d, "sp_ft.tif", "EPSG:2265", SP)          # a RAS Mapper terrain, ft
        utm = _crs_dem(d, "utm_m.tif", "EPSG:26914", 600.0)     # and one in UTM metres
        for p, how in ((sp, "feet (assumed from the CRS's linear unit)"),
                       (utm, "metres (assumed: the CRS is not in feet)")):
            notes = []
            t = he.terrain_on_grid(p, grid, notes=notes)        # a83ad04 read both as 600 m
            assert abs(_med(t) - SP) < 0.05, (p, _med(t))
            assert notes[0] == "HEC-RAS terrain elevations: {}.".format(how), notes
            w = notes[1]
            assert w.startswith("HEC-RAS terrain {}: no elevation unit is recorded".format(
                os.path.basename(p))), w
            assert "as before Runner 3.10.88" in w and \
                "'Terrain elevation units' (CLI: hecras-reexport --terrain-z)" in w, w
            assert "DEM elevation units" not in " ".join(notes), notes
        # the tools' own choice: as given, no warning when the raster records nothing
        notes = []
        t = he.terrain_on_grid(sp, grid, z="meters", notes=notes)
        assert abs(_med(t) - SP / FT) < 0.1 and notes == ["HEC-RAS terrain elevations: metres "
                                                          "(chosen)."], notes
        # ... and with a warning when it records the other unit
        tagged = _crs_dem(d, "t_m.tif", "EPSG:26914", 600.0, unit="m")
        notes = []
        he.terrain_on_grid(tagged, grid, z="feet", notes=notes)
        assert "t_m.tif records metres (raster unit 'm')" in notes[1] and \
            "read as feet, as chosen" in notes[1], notes
        notes = []
        assert abs(_med(he.terrain_on_grid(tagged, grid, notes=notes)) - SP) < 0.05
        assert len(notes) == 1 and "detected: raster unit 'm'" in notes[0], notes
        # the Runner's own terrain (hecras_stage.json names it and the model CRS): its record
        # before the CRS guess - 300 in a feet CRS would be guessed metres (300 m fits ND)
        own = _crs_dem(d, "own.tif", "EPSG:2265", 300.0)
        stage = {"terrain_tif": own, "model_wkt": _srs("EPSG:2265").ExportToWkt()}
        notes = []
        assert abs(_med(he.terrain_on_grid(own, grid, stage=stage, notes=notes)) - 300.0) < 0.05
        assert notes == ["HEC-RAS terrain elevations: feet (the Runner's own HEC-RAS terrain, "
                         "written in the model's project units)."], notes
        notes = []
        assert abs(_med(he.terrain_on_grid(own, grid, notes=notes)) - 300.0 / FT) < 0.1
        assert "metres (assumed: the CRS is in feet but a median of 300" in notes[0], notes
        other = dict(stage, terrain_tif=sp)                     # another file: not the record
        assert he.own_terrain_unit(own, other) == "" and he.own_terrain_unit(own, {}) == ""
        assert he.own_terrain_unit(own, dict(stage, model_wkt=_srs("EPSG:26914").ExportToWkt())) \
            == "meters"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _import_tool_run(h, fq, params, read_values):
    """HecRasImportAlgorithm with the synthetic 6 x 6 mesh of test_hecras (h5py stand-in) and
    the real unit rules; read_like gives each terrain file's constant value."""
    from nd_breach_runner.core import hecras
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.tests.synthetic import npy_writer
    from nd_breach_runner.tests.test_hecras import WKT, _FakeH5, _Patch, _mesh_layout
    layout, root = _mesh_layout()
    fake = _FakeH5(layout, root)
    like = Grid(np.zeros((6, 6)), 1000.0, 2060.0, 10.0, crs_wkt=WKT)
    seen = []
    real_td = hecras.terrain_depth

    def terrain_depth(*a, **k):
        seen.append(None if a[6] is None else np.array(a[6]))
        return real_td(*a, **k)
    with _Patch((hecras, "_open_hdf", lambda p: (fake, lambda: None)),
                (hecras, "terrain_depth", terrain_depth),
                (h.gdal_io, "GeoTiffWriter", lambda crs: npy_writer),
                (h.gdal_io, "read_grid", lambda p: like),
                (h.gdal_io, "read_like",
                 lambda p, g, alg="bilinear": np.full((g.ny, g.nx), read_values[p])),
                (h._ToolBase, "_need_gdal", lambda self: None)):
        a = h.HecRasImportAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        a.processAlgorithm(dict({"HDF": "run.p01.hdf", "DEPTH_BASIS": 1}, **params),
                           fq.QgsProcessingContext(), fb)
    return seen[-1], fb


@test
def the_hecras_import_tool_reads_an_untagged_terrain_and_says_how():
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.tests.test_hecras import _algs as hecras_algs
    h, fq = hecras_algs()
    d = tempfile.mkdtemp(prefix="demz_r3_imp_")
    try:
        sp = _crs_dem(d, "sp_ft.tif", "EPSG:2265", SP)
        utm = _crs_dem(d, "utm_m.tif", "EPSG:26914", 600.0)
        vals = {sp: 100.0, utm: 100.0 * FT}            # the same 100 ft ground, as stored
        for p in (sp, utm):
            out = os.path.join(d, "out_" + os.path.basename(p)[:-4])
            t_ft, fb = _import_tool_run(h, fq, {"TERRAIN": types.SimpleNamespace(
                source=lambda p=p: p), "OUT_FOLDER": out}, vals)
            assert np.allclose(t_ft, 100.0), (p, t_ft)        # round 2: QgsProcessingException
            checks = [e for e in fb.errors if "HEC-RAS terrain" in e]
            assert len(checks) == 1 and "'Terrain elevation units'" in checks[0], fb.errors
            assert not any("DEM elevation units" in e for e in fb.errors), fb.errors
            dep = np.load(os.path.join(out, "HECRAS Depth (Max).npy"))
            assert np.nanmax(dep) <= 5.0 + 1e-6 and np.nanmax(dep) > 4.9, np.nanmax(dep)
        # 'Terrain elevation units' Meters on the feet file: as chosen, no warning
        t_ft, fb = _import_tool_run(h, fq, {"TERRAIN": types.SimpleNamespace(source=lambda: sp),
                                            "TERRAIN_Z": 3, "OUT_FOLDER": os.path.join(d, "o3")},
                                    vals)
        assert np.allclose(t_ft, 100.0 / FT) and not any("HEC-RAS terrain" in e
                                                         for e in fb.errors), fb.errors
        assert any("metres (chosen)" in m for m in fb.info), fb.info
        # the dam folder's own HEC-RAS stage names this terrain: the Runner's record, no warning
        dam = os.path.join(d, "dam")
        os.makedirs(os.path.join(dam, "_hecras"))
        open(os.path.join(dam, "Depth (Max).tif"), "w").write("x")
        with open(os.path.join(dam, "_hecras", "hecras_stage.json"), "w") as fh:
            json.dump({"terrain_tif": sp, "model_wkt": _srs("EPSG:2265").ExportToWkt()}, fh)
        t_ft, fb = _import_tool_run(h, fq, {"TERRAIN": types.SimpleNamespace(source=lambda: sp),
                                            "DAM_FOLDER": dam}, vals)
        assert np.allclose(t_ft, 100.0) and not any("HEC-RAS terrain" in e for e in fb.errors)
        assert any("the Runner's own HEC-RAS terrain" in m for m in fb.info), fb.info
        a = h.HecRasImportAlgorithm()
        a.initAlgorithm()
        assert a.parameterDefinition("TERRAIN_Z") is not None
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def hecras_reexport_cli_reads_an_untagged_terrain_and_says_how():
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import gdal_io, hecras, hecras_engine
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.scripts import ndbreach_cli as cli
    from nd_breach_runner.tests.synthetic import npy_writer
    from nd_breach_runner.tests.test_hecras import _FakeH5, _Patch
    from nd_breach_runner.tests.test_hecras_terrain_depth import (CELL, DITCH_A, DX, NP, WKT,
                                                                  WSE_C, _layout, _terrain)
    d = tempfile.mkdtemp(prefix="demz_r3_cli_")
    try:
        run = os.path.join(d, "DAM-1")
        proj = os.path.join(d, "proj")
        hdf = os.path.join(proj, "run", "DAM_1.p01.hdf")
        for sub in (os.path.join(run, "_hecras"), os.path.join(proj, "Terrain"),
                    os.path.dirname(hdf)):
            os.makedirs(sub)
        open(hdf, "w").write("stand-in")
        open(os.path.join(run, "Depth (Max).tif"), "w").write("the run's raster")
        with open(os.path.join(run, "_hecras", "hecras_stage.json"), "w") as fh:
            json.dump({"engine": "hecras", "project": proj, "results_hdf": hdf, "t0_hr": 0.0,
                       "model_wkt": WKT}, fh)
        sp = _crs_dem(os.path.join(proj, "Terrain"), "DAM_1 Terrain.tif", "EPSG:2265", SP)
        utm = _crs_dem(d, "utm_m.tif", "EPSG:26914", 600.0)
        like = Grid(np.zeros((3 * NP, 3 * NP)), 0.0, 3 * CELL, DX, crs_wkt=WKT)
        vals = {sp: _terrain(), utm: _terrain() * FT}
        layout, root = _layout()
        with _Patch((hecras, "_open_hdf", lambda p: (_FakeH5(layout, root), lambda: None)),
                    (gdal_io, "read_grid", lambda p: like),
                    (gdal_io, "read_like", lambda p, g, alg="bilinear": vals[p].copy()),
                    (gdal_io, "GeoTiffWriter", lambda crs: npy_writer)):
            for i, (args, terrain, want) in enumerate((
                    ([], sp, "feet (assumed from the CRS's linear unit)"),   # auto-found
                    (["--terrain", utm], utm, "metres (assumed: the CRS is not in feet)"),
                    (["--terrain", sp, "--terrain-z", "feet"], sp, "feet (chosen)"))):
                out = os.path.join(d, "out{}".format(i))
                ns = cli.build_parser().parse_args(["hecras-reexport", run, "--out", out,
                                                    "--depth-basis", "terrain"] + args)
                assert ns.func(ns) == 0                  # round 2: a ValueError traceback
                rep = json.load(open(os.path.join(out, hecras_engine.REEXPORT_JSON)))
                assert rep["terrain_tif"] == terrain
                assert rep["terrain_z"] == "HEC-RAS terrain elevations: {}.".format(want), rep
                if "chosen" in want:
                    assert rep["warnings"] == [], rep["warnings"]
                else:
                    assert len(rep["warnings"]) == 1 and \
                        "--terrain-z" in rep["warnings"][0], rep["warnings"]
                dep = np.load(os.path.join(out, "Depth (Max).npy"))
                assert np.allclose(dep[_terrain() == DITCH_A], WSE_C - DITCH_A, atol=1e-4)
            # the Runner's own terrain (the stage names it and the model CRS): its record, no
            # warning (model_wkt given: the stage's real CRS is not the stand-in mesh's)
            with open(os.path.join(run, "_hecras", "hecras_stage.json"), "w") as fh:
                json.dump({"engine": "hecras", "project": proj, "results_hdf": hdf, "t0_hr": 0.0,
                           "terrain_tif": sp, "model_wkt": _srs("EPSG:2265").ExportToWkt()}, fh)
            rep = hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(d, "own"),
                                                        model_wkt=WKT, depth_basis="terrain")
            assert rep["terrain_z"] == ("HEC-RAS terrain elevations: feet (the Runner's own "
                                        "HEC-RAS terrain, written in the model's project "
                                        "units)."), rep["terrain_z"]
            assert rep["warnings"] == [], rep["warnings"]
            # the CLI turns any ValueError of the re-export into its own error, not a traceback
            real = hecras_engine.reexport_hecras_results

            def boom(*a, **k):
                raise ValueError("a unit problem")
            hecras_engine.reexport_hecras_results = boom
            try:
                ns = cli.build_parser().parse_args(["hecras-reexport", run])
                try:
                    ns.func(ns)
                    raise AssertionError("must stop")
                except cli.CliError as e:
                    assert "a unit problem" in str(e), str(e)
            finally:
                hecras_engine.reexport_hecras_results = real
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def an_explicit_unit_stops_on_a_scaled_band_or_a_vertical_shift_vrt():
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import gdal_io as G
    algs, fq = _algs()
    d = tempfile.mkdtemp(prefix="demz_r3_s_")
    try:
        # raw feet with scale 0.3048 and unit 'm' (306 m true): Meters read 1004 m and ran
        p = _dem(d, "sc.tif", "m", 1004.0)
        ds = gdal.Open(p, gdal.GA_Update)
        ds.GetRasterBand(1).SetScale(0.3048)
        ds = None
        # offset 100, unit 'm' (true 906 m): Feet read 246 m and ran
        q = _dem(d, "of.tif", "m", 806.0)
        ds = gdal.Open(q, gdal.GA_Update)
        ds.GetRasterBand(1).SetOffset(100.0)
        ds = None
        for path in (p, q):
            for z in (1, 2, 3, 4, 6):
                _stops(lambda: _gis(algs, path, z), "has a scale", "-unscale",
                       "No unit choice reads this file right")
            msg = _stops(lambda: _gis(algs, path, 0), "has a scale", "Translate (Convert Format)")
            assert "Meters or Feet" not in msg, msg       # Detect does not send to a choice
        # 'L': a warped VRT whose vertical shift turned feet into metres under 'US survey foot'
        src = _crs_dem(d, "c.tif", "EPSG:2265+6360", 1300.0)
        vrt = os.path.join(d, "w.vrt")
        gdal.Warp(vrt, src, format="VRT", dstSRS="EPSG:26914").FlushCache()
        for z in ("auto", "feet", "us_feet", "meters", "us_feet_tag_wrong"):
            msg = _stops(lambda: G.resolve_dem_z(vrt, z), "vertical shift", "-novshift",
                         "Warp (Reproject)", "No unit choice reads this file right")
            assert "Meters or Feet" not in msg, msg
        # the same warp written as a GeoTIFF: Detect stops on the ground check, and Meters - the
        # choice it names - runs, the values fitting North Dakota only as metres
        tif = os.path.join(d, "w.tif")
        gdal.Warp(tif, src, dstSRS="EPSG:26914").FlushCache()
        msg = _stops(lambda: _gis(algs, tif, 0), "to Meters", "the run goes on as Meters with a "
                     "warning in its report, or correct the record (gdal_edit -units m)")
        src_m = _gis(algs, tif, 3)[0]
        assert src_m.z_to_m == 1.0 and "records US survey feet" in src_m.z_check, src_m.z_check
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def an_explicit_unit_against_the_record_runs_when_only_it_fits_the_ground():
    """Review round 3: an in-QGIS way through for a raster whose unit tag is wrong (probe2 listed
    8 files that ran right with the explicit choice before round 2 and then stopped)."""
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import gdal_io as G, workflow
    algs, fq = _algs()
    d = tempfile.mkdtemp(prefix="demz_r3_u_")
    try:
        # a feet DEM tagged 'm' (1640 ft = 500 m; 1640 m is no North Dakota ground)
        ftm = _dem(d, "ft_tagged_m.tif", "m", 1640.0)
        src = _gis(algs, ftm, 1)[0]
        assert abs(src.z_to_m - FT) < 1e-12 and src.z_note == "feet (chosen)", src.z_note
        assert "ft_tagged_m.tif records metres (raster unit 'm'), but its median 1640.0 is North " \
            "Dakota ground only as feet (500 m as feet, 1640 m as metres): read as feet, as " \
            "chosen" in src.z_check, src.z_check
        assert "gdal_edit -units ft" in src.z_check
        w = workflow.units_warning(src)
        assert w.startswith("DEM elevation units: feet (chosen) - ft_tagged_m.tif records"), w
        msg = _stops(lambda: _gis(algs, ftm, 0), "to Feet", "the run goes on as Feet")
        # ... and the other way: a metre DEM tagged 'ft' (300 m; 91 m as feet)
        mft = _dem(d, "m_tagged_ft.tif", "ft", 300.0)
        src = _gis(algs, mft, 3)[0]
        assert src.z_to_m == 1.0 and "records feet" in src.z_check and \
            "only as metres (300 m as metres, 91 m as feet)" in src.z_check, src.z_check
        _stops(lambda: _gis(algs, mft, 0), "to Meters", "the run goes on as Meters")
        # the record right by the values: stop, no way round offered
        right = _dem(d, "right_m.tif", "m", 400.0)
        msg = _stops(lambda: _gis(algs, right, 1), "records metres", "only as metres",
                     "to Meters")
        assert "tag is wrong" not in msg, msg
        for z in (4, 5):                                 # 'the tag is wrong' does not override it
            _stops(lambda: _gis(algs, right, z), "only as metres", "to Meters")
        # unclear (700 m: 213 m as feet fits too): stop, naming the choice that goes through
        west = _dem(d, "west_m.tif", "m", 700.0)
        _stops(lambda: _gis(algs, west, 1), "cannot tell which is right",
               "choose 'Feet (the raster's unit tag is wrong)'")
        _stops(lambda: _gis(algs, west, 3 - 1), "choose 'US survey feet (the raster's unit tag "
               "is wrong)'")
        for z, f in ((4, FT), (5, US_FT)):
            src = _gis(algs, west, z)[0]
            assert abs(src.z_to_m - f) < 1e-12, (z, src.z_to_m)
            assert "'DEM elevation units' says the tag is wrong" in src.z_check, src.z_check
            assert workflow.units_warning(src)
        ftw = _dem(d, "ft_west.tif", "ft", 2300.0)       # 701 m as feet, 2300 m: no
        _stops(lambda: _gis(algs, ftw, 6), "only as feet")
        unclear_ft = _dem(d, "ft_unclear.tif", "ft", 900.0)  # 274 m as feet, 900 m as metres
        src = _gis(algs, unclear_ft, 6)[0]
        assert src.z_to_m == 1.0 and "says the tag is wrong" in src.z_check
        # a raster that records nothing: the tag-wrong choice is its plain unit, no warning
        plain = _dem(d, "plain.tif", "", 700.0)
        assert G.resolve_dem_z(plain, "feet_tag_wrong") == (G.Z_FEET, "chosen")
        src = _gis(algs, plain, 4)[0]
        assert abs(src.z_to_m - FT) < 1e-12 and src.z_check == "", src.z_check
        # no valid cell to decide on: unclear
        empty = os.path.join(d, "empty.tif")
        ds = gdal.GetDriverByName("GTiff").Create(empty, 30, 30, 1, gdal.GDT_Float32)
        ds.SetProjection(_srs("EPSG:26914").ExportToWkt())
        ds.SetGeoTransform((CX - 15.0, 1.0, 0.0, CY + 15.0, 0.0, -1.0))
        ds.GetRasterBand(1).Fill(-9999.0)
        ds.GetRasterBand(1).SetNoDataValue(-9999.0)
        ds.GetRasterBand(1).SetUnitType("m")
        ds = None
        _stops(lambda: G.resolve_dem_z(empty, "feet"), "no valid elevation was found",
               "Feet (the raster's unit tag is wrong)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def every_dem_z_enum_keeps_its_indices_and_offers_the_tag_wrong_choices():
    from nd_breach_runner.core import gdal_io as G, culvert_precheck as cp
    algs, fq = _algs()
    import importlib
    import nd_breach_runner.algorithms_tools as tools
    importlib.reload(tools)
    import nd_breach_runner.algorithms_animation as anim
    importlib.reload(anim)
    from nd_breach_runner import wizard_logic as wl
    ids = [z[0] for z in algs.ZUNITS]
    assert ids[:4] == ["auto", "feet", "us_feet", "meters"], ids       # saved indices unchanged
    assert tuple(ids) == G.Z_CHOICES == wl.Z_IDS == cp.ZUNITS, ids
    assert [z[1] for z in algs.ZUNITS] == [G.Z_CHOICE_LABELS[z] for z in ids]
    assert wl.Z_LABELS == G.Z_CHOICE_LABELS
    assert algs.ZUNITS[4][1] == "Feet (the raster's unit tag is wrong)"
    for cls in (algs.RunDamAlgorithm, tools.ConditionDemAlgorithm, anim.AnimationAlgorithm):
        a = cls()
        a.initAlgorithm()
        opts = a.parameterDefinition("DEM_Z").args[0]
        assert list(opts) == [z[1] for z in algs.ZUNITS], (cls.__name__, opts)
    a = wl.default_answers()
    a["z_units"] = "feet_tag_wrong"
    assert wl.build_rundam_params(a)["DEM_Z"] == 4
    d = tempfile.mkdtemp(prefix="demz_r3_e_")
    try:
        with open(os.path.join(d, "rundam.json"), "w") as fh:
            json.dump({"inputs": {"DEM_Z": 4}}, fh)
        assert cp.find_dam_inputs(d)["z_choice"] == "feet_tag_wrong"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert G.z_to_m_factor("unused.tif", "us_feet_tag_wrong") == (US_FT, "US survey feet (chosen)")
    assert G.median_unit_problem(320.0, "feet_tag_wrong")            # the base unit is checked


@test
def the_cli_checks_an_explicit_unit_against_the_record():
    """EX_cli_no_resolve: the CLI has no ND ground check of its own, so this is its only guard."""
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.scripts import ndbreach_cli as cli

    class Cfg(object):
        def section(self, name):
            return {}

        def get(self, sec, key, default=None):
            return default

        def path_of(self, p):
            return p

    d = tempfile.mkdtemp(prefix="demz_r3_cli2_")
    try:
        def open_(path, z):
            return cli._open_gdal(Cfg(), {"z_units": z, "work_crs": "EPSG:26914"}, path,
                                  (600100.0, 5199900.0), None)
        low = _dem(d, "m300.tif", "m", 300.0)            # 91 m as feet: the record is right
        _stops(lambda: open_(low, "feet"), "is Feet", "records metres", "only as metres",
               "in the CLI config: dem.z_units")
        assert open_(low, "meters").terrain.z_to_m == 1.0
        west = _dem(d, "m700.tif", "m", 700.0)
        _stops(lambda: open_(west, "feet"), "cannot tell", "feet_tag_wrong")
        st = open_(west, "feet_tag_wrong")
        assert abs(st.terrain.z_to_m - FT) < 1e-12 and "says the tag is wrong" in st.terrain.z_check
        ftm = _dem(d, "ft1640.tif", "m", 1640.0)
        st = open_(ftm, "feet")
        assert abs(st.terrain.z_to_m - FT) < 1e-12 and "only as feet" in st.terrain.z_check
        assert cli.AscTerrain.__init__ is not None
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def the_carve_and_the_refill_keep_the_values_in_a_compound_crs():
    """NV_carve / NV_refill: without -novshift the 2 m carve depth went into a feet compound
    terrain x 3.28 and a metre refill tile into a feet compound work grid x 3.28."""
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import reservoir_carve as rc, dem_refill as rf
    from nd_breach_runner.core.grid import Grid
    d = tempfile.mkdtemp(prefix="demz_r3_c_")
    try:
        tif = _crs_dem(d, "terrain.tif", "EPSG:26914+6360", 1300.0)
        dep = _crs_dem(d, "depth.tif", "EPSG:26914", 2.0)
        got = rc.carve_tif(tif, dep, z_scale=US_FT)
        assert abs(_med(_read(tif)) - (1300.0 - 2.0 / US_FT)) < 1e-3, _med(_read(tif))
        assert abs(got["max_m"] - 2.0) < 1e-6, got
        g = Grid(np.full((20, 20), np.nan), BOX[0], BOX[3], 10.0,
                 crs_wkt=_srs("EPSG:26914+6360").ExportToWkt())
        tile = _crs_dem(d, "tile.tif", "EPSG:26914", 396.24)
        out = rf.fill_grid(g, tile)
        assert abs(float(np.nanmedian(out.data)) - 396.24) < 1e-3, float(np.nanmedian(out.data))
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _Stop(Exception):
    pass


@test
def the_units_warning_reaches_the_run_report_through_every_terrain_wrapper():
    """FLAG_run_dam_drops_it / FLAG_conditioning_drops_it / FLAG_carve_drops_it."""
    from nd_breach_runner.core import conditioning as cond, reservoir_carve as rc, workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    from nd_breach_runner.core import breach as br

    class Flagged(SyntheticTerrain):
        z_note = "feet (chosen)"
        z_check = "why the unit needs a look"
    base = Flagged()
    for wrapped in (cond.ConditionedTerrain(base, cond.Edits()), rc.CarvedTerrain(base, None),
                    rc.CarvedTerrain(cond.ConditionedTerrain(base, cond.Edits()), None)):
        assert wrapped.z_check == base.z_check, type(wrapped).__name__
        assert workflow.units_warning(wrapped) == workflow.units_warning(base) != ""
    seen = []

    def flood(dam, st, warnings):
        seen.extend(warnings)
        raise _Stop()
    real = workflow._flood_hydrographs
    workflow._flood_hydrographs = flood
    d = tempfile.mkdtemp(prefix="demz_r3_f_")
    try:
        st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, max_iterations=1,
                                  scenario=workflow.SCENARIO_FLOOD)
        dam = br.DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                         top_of_dam_ft=1850.0, surface_area_top_ac=60.0)
        try:
            workflow.run_dam(dam, (0.0, 0.0), st, base, npy_writer, os.path.join(d, "T1"))
            raise AssertionError("the stand-in hydrograph step was not reached")
        except _Stop:
            pass
        assert workflow.units_warning(base) in seen, seen
    finally:
        workflow._flood_hydrographs = real
        shutil.rmtree(d, ignore_errors=True)


@test
def the_ground_check_falls_back_to_a_decimated_read_of_a_sparse_dem():
    """MED_no_decimated_fallback: valid cells only between the 7 x 7 sample windows."""
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import gdal_io as G
    algs, fq = _algs()
    d = tempfile.mkdtemp(prefix="demz_r3_m_")
    try:
        p = os.path.join(d, "sparse.tif")
        n = 1000
        a = np.full((n, n), -9999.0, dtype="float32")
        a[60:140, 60:140] = 320.0                    # windows at 0-24, 163-187, ... each way
        ds = gdal.GetDriverByName("GTiff").Create(p, n, n, 1, gdal.GDT_Float32,
                                                  ["COMPRESS=DEFLATE", "TILED=YES"])
        ds.SetProjection(_srs("EPSG:26914").ExportToWkt())
        ds.SetGeoTransform((CX - 100.0, 1.0, 0.0, CY + 100.0, 0.0, -1.0))
        ds.GetRasterBand(1).WriteArray(a)
        ds.GetRasterBand(1).SetNoDataValue(-9999.0)
        ds = None
        ds = gdal.Open(p)
        med, cells = G.sample_median(ds)
        ds = None
        assert med == 320.0 and cells > 10, (med, cells)
        _stops(lambda: _gis(algs, p, 1), "DEM median 320.0 with units 'Feet'")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def an_old_nd_dwr_download_in_a_gft_folder_is_refused_from_its_metadata_alone():
    """GFT_md_ignored: the facts file deleted, the in-file record still names ND DWR LiDAR."""
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="demz_r3_g_")
    try:
        bad = _nd_download(os.path.join(d, "gftaylor"), "D7_10m_5070.tif", metadata=True)
        os.remove(os.path.splitext(bad)[0] + "_dem.txt")
        for z in ("auto", "meters", "feet", "meters_tag_wrong"):
            _stops(lambda: G.resolve_dem_z(bad, z), "before 0.17.50", "'gftaylor'")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def a_same_stem_vrt_copy_is_not_described_by_the_facts_file():
    """r1_F1g: a gdal.Translate VRT keeps the in-file record; its facts file says GeoTIFF."""
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="demz_r3_v_")
    try:
        tif = _old_download(d, "S1_10m_5070.tif", value=1030.0 * FT)
        vrt = os.path.join(d, "S1_10m_5070.vrt")
        gdal.Translate(vrt, tif, format="VRT", scaleParams=[[0, 1, 0, 1 / FT]]).FlushCache()
        ds = gdal.Open(vrt)
        assert ds.GetMetadata().get("SOURCE", "").startswith("USGS 3DEP")
        ds = None
        _stops(lambda: G.resolve_dem_z(vrt, "auto"), "does not describe this raster",
               "file type", "the raster is VRT")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def a_qgis_source_with_options_is_read_by_its_file():
    """PIPE_*: 'x.tif|option:...' - the record, the explicit check, the ground check, the read."""
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import gdal_io as G
    algs, fq = _algs()
    d = tempfile.mkdtemp(prefix="demz_r3_p_")
    try:
        ft = _dem(d, "ft.tif", "ft", 1300.0) + "|option:NUM_THREADS=1"
        assert G.recorded_z_unit(ft)[0] == G.Z_FEET
        _stops(lambda: G.resolve_dem_z(ft, "meters"), "ft.tif records feet")
        plain = _dem(d, "plain.tif", "", 320.0) + "|option:NUM_THREADS=1"
        _stops(lambda: _gis(algs, plain, 1), "DEM median 320.0 with units 'Feet'")
        src = _gis(algs, plain, 3)[0]
        assert src.dem_path.endswith("plain.tif") and abs(_med(src.read(
            (600050.0, 5199850.0, 600150.0, 5199950.0), 10.0)) - 320.0) < 1e-6
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def a_chosen_unit_that_fails_the_ground_check_gets_the_chosen_guidance():
    """X_chosen_how_kept: not '(chosen)', not 'the unit was read from the raster'."""
    if _skip("dem_z_round3"):
        return
    algs, fq = _algs()
    d = tempfile.mkdtemp(prefix="demz_r3_x_")
    try:
        msg = _stops(lambda: _gis(algs, _dem(d, "plain.tif", "", 320.0), 1), "DEM median")
        assert msg == ("DEM median 320.0 with units 'Feet' is 98 m, outside the North Dakota "
                       "ground range (about 230-1070 m). Read as metres it is 320 m, which fits: "
                       "set 'DEM elevation units' to Meters (USGS 3DEP downloads are metres)."), msg
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def the_hecras_export_reads_an_old_run_reports_dem_unit_or_says_what_it_assumed():
    """HM_zfactor_guess: a run report older than 3.10.16 (no dem_z_to_m, no units note)."""
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import hecras_model as hm
    d = tempfile.mkdtemp(prefix="demz_r3_h_")
    try:
        assert hm._z_factor({"dem_z_to_m": 0.3048}, "x.tif")[0] == 0.3048
        assert hm._z_factor({"dem_elevation_units": "US survey feet (chosen)"}, "x.tif")[0] == US_FT
        tagged = _dem(d, "ft.tif", "ft", 1300.0)
        warn = []
        f, note = hm._z_factor({}, tagged, warn=warn)
        assert f == FT and note == "feet (detected: raster unit 'ft')" and warn == [], (f, note)
        sp = _crs_dem(d, "sp.tif", "EPSG:2265", SP)
        f, note = hm._z_factor({}, sp, warn=warn)        # round 2: ValueError, 'DEM elevation units'
        assert f == FT and note == "feet (assumed from the CRS's linear unit)", note
        assert len(warn) == 1 and "a run before Runner 3.10.16" in warn[0] and \
            "gdal_edit -units" in warn[0] and "DEM elevation units" not in warn[0], warn
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def outputs_in_a_compound_crs_survive_a_later_warp_without_novshift():
    """GeoTiffWriter writes the horizontal part of a compound DEM CRS: DamSafety Hazard's
    reproject_raster (no -novshift) read a 2 m depth written into 'ND North (ft) + NAVD88 height
    (ftUS)' as 0.61 m."""
    if _skip("dem_z_round3"):
        return
    from nd_breach_runner.core import gdal_io as G
    from nd_breach_runner.core.grid import Grid
    d = tempfile.mkdtemp(prefix="demz_r3_w_")
    try:
        g = Grid(np.full((20, 20), 2.0), BOX[0], BOX[3], 10.0,
                 crs_wkt=_srs("EPSG:26914").ExportToWkt())
        target = _srs("EPSG:2265+6360").ExportToWkt()
        path = G.GeoTiffWriter("EPSG:26914", target, 10.0 / FT)(g, os.path.join(d, "depth.tif"))
        ds = gdal.Open(path)
        s = _srs(ds.GetProjection())
        ds = None
        assert not s.IsCompound() and s.GetAuthorityCode(None) == "2265", s.GetName()
        assert abs(_med(_read(path)) - 2.0) < 1e-6
        # Hazard: srcSRS = the layer's CRS (a warp INTO a compound feet CRS still shifts a 2D
        # source; only a raster from an older Runner, written in a compound CRS, is one)
        for dst in ("EPSG:5070", "EPSG:26914", "EPSG:2265"):
            again = os.path.join(d, "again.tif")
            ds = gdal.Open(path)
            gdal.Warp(again, ds, options=gdal.WarpOptions(
                srcSRS=ds.GetProjection(), dstSRS=dst, resampleAlg="near",
                dstNodata=-9999.0)).FlushCache()
            ds = None
            assert abs(_med(_read(again)) - 2.0) < 1e-6, (dst, _med(_read(again)))
        # a compound work CRS (a DEM in UTM + NAVD88 metres is its own model CRS) too
        path = G.GeoTiffWriter(_srs("EPSG:26914+5703").ExportToWkt())(g, os.path.join(d, "w.tif"))
        ds = gdal.Open(path)
        assert not _srs(ds.GetProjection()).IsCompound()
        ds = None
        assert G.horizontal_wkt("EPSG:26914") == "EPSG:26914"
        assert G.horizontal_wkt("not a crs") == "not a crs"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def the_animation_hillshade_reads_the_recorded_unit_else_says_what_it_assumed():
    if _skip("dem_z_round3"):
        return
    import importlib
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_tools as tools
    importlib.reload(tools)
    import nd_breach_runner.algorithms_animation as anim
    importlib.reload(anim)
    d = tempfile.mkdtemp(prefix="demz_r3_a_")
    try:
        grid = _utm_grid()
        a = anim.AnimationAlgorithm()
        a.initAlgorithm()
        for path, want, warned in ((_crs_dem(d, "sp.tif", "EPSG:2265", SP), 600.0, True),
                                   (_crs_dem(d, "ftag.tif", "EPSG:26914", SP, unit="ft"), 600.0,
                                    False)):
            fb = fq.QgsProcessingFeedback()
            z = a._dem({"DEM": types.SimpleNamespace(source=lambda p=path: p), "DEM_Z": 0},
                       fq.QgsProcessingContext(), grid, d, fb)
            assert abs(_med(z) - want) < 0.05, (path, _med(z))    # round 2: 1968.5 'assumed' m
            got = [e for e in fb.errors if "hillshade DEM" in e]
            assert bool(got) == warned, fb.errors
            if warned:
                assert "feet (assumed from the CRS's linear unit)" in got[0] and \
                    "'DEM elevation units'" in got[0], got
    finally:
        shutil.rmtree(d, ignore_errors=True)
