"""3.40.1: duplicate layers in the Runner window (audit 3-pack, lower priority).

The DEM list showed the same raster file twice when it was loaded twice (two entries, one DEM) and
could not tell two files with the same layer name apart; "Add results to project" added every
result layer again on a second click. popup_logic.dem_choices / results_to_add (pure) and the
window driven through the Qt doubles of test_popup."""
import os

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_popup import Tmp, make_dam, qt_fakes, touch


@test
def popup_layer_key_and_dem_choices_list_one_file_once():
    from nd_breach_runner import popup_logic as pl
    with Tmp() as d:
        a = touch(d, "A", "dem.tif")
        b = touch(d, "B", "dem.tif")
        assert pl.layer_key("") == "" and pl.layer_key(None) == ""
        assert pl.layer_key(a) == pl.layer_key(os.path.join(d, "A", ".", "dem.tif"))
        assert pl.layer_key(a + "|layername=dem") == pl.layer_key(a)
        assert pl.layer_key(a) != pl.layer_key(b)
        link = os.path.join(d, "link.tif")
        try:
            os.symlink(a, link)
            assert pl.layer_key(link) == pl.layer_key(a)          # a link to the same file is the same file
        except (OSError, NotImplementedError):
            pass
        rows, rep = pl.dem_choices([("LiDAR", "id2", a), ("LiDAR copy", "id3", a), ("Other", "id4", b)])
        assert rows == [("LiDAR", "id2"), ("Other", "id4")]
        assert rep == [("LiDAR copy", "id3", "LiDAR", "id2")]
        # two files with one name: both listed, told apart by folder / file
        rows, rep = pl.dem_choices([("dem", "x", b), ("dem", "y", a)])
        assert rows == [("dem - A/dem.tif", "y"), ("dem - B/dem.tif", "x")] and rep == []
        # the same name and the same file: one entry, the plain name
        rows, rep = pl.dem_choices([("dem", "y", a), ("dem", "x", a)])
        assert rows == [("dem", "x")] and rep == [("dem", "y", "dem", "x")]
        # a layer with no file source (a web layer) is told apart by its id, never dropped
        rows, rep = pl.dem_choices([("wms", "w1", ""), ("wms", "w2", "")])
        assert [r[1] for r in rows] == ["w1", "w2"] and rep == []
        assert pl.dem_choices([]) == ([], [])


@test
def popup_results_to_add_skips_layers_already_loaded():
    from nd_breach_runner import popup_logic as pl
    with Tmp() as d:
        f = make_dam(os.path.join(d, "Dams"), "DWR-1")
        touch(f, "Flood Extent.geojson")
        files = pl.result_files(f)
        assert len(files) == 4
        add, have = pl.results_to_add(files, [])
        assert add == files and have == []
        depth = os.path.join(f, "Depth (Max).tif")
        ext = os.path.join(f, "Flood Extent.geojson")
        add, have = pl.results_to_add(files, [depth, ext + "|layername=Flood Extent", "", None])
        assert [os.path.basename(p) for _k, p in have] == ["Depth (Max).tif", "Flood Extent.geojson"]
        assert [os.path.basename(p) for _k, p in add] == ["Velocity (Max).tif", "Arrival Time.tif"]


@test
def popup_window_lists_a_dem_once_and_adds_results_once():
    with Tmp() as d, qt_fakes() as q:
        dem = touch(d, "terrain", "lidar.tif")
        other = touch(d, "other", "lidar.tif")
        q.project.layers.clear()
        for lyr in (q.Raster(dem, "LiDAR"), q.Raster(other, "LiDAR")):
            q.project.layers[lyr.id()] = lyr

        class Same(q.Raster):                     # a second layer of the same file, its own id
            def id(self):
                return "dup-id"
        q.project.layers["dup-id"] = Same(dem, "LiDAR again")
        dlg = q.dialog.NDBreachRunnerDialog()
        cb = dlg.dem_combo
        items = [(cb.itemText(i), cb.itemData(i)) for i in range(cb.count())]
        assert items == [("(choose in the tool)", ""), ("LiDAR - other/lidar.tif", other),
                         ("LiDAR - terrain/lidar.tif", dem)], items
        assert any("same raster file loaded more than once" in m and "'LiDAR again'" in m
                   for m in dlg.log_edit.lines)
        n_logged = len([m for m in dlg.log_edit.lines if "more than once" in m])
        dlg._fill_dem()                                                 # the same note is not logged again
        assert len([m for m in dlg.log_edit.lines if "more than once" in m]) == n_logged
        # a saved choice of the left-out layer becomes the entry kept for that file
        dlg._pending_dem = "dup-id"
        dlg._fill_dem()
        assert cb.currentData() == dem
        # results: the second click adds nothing again
        dams = os.path.join(d, "Dams")
        f = make_dam(dams, "DWR-1")
        touch(f, "Flood Extent.geojson")
        dlg.dams_edit.setText(dams)
        dlg.dam_edit.setText("DWR-1")
        dlg._changed()
        assert dlg._add_results() == 4
        before = len(q.project.added)
        assert dlg._add_results() == 0 and len(q.project.added) == before
        assert any("4 result layer(s) of" in m and "already in the project, not added again" in m
                   for m in dlg.log_edit.lines)
        grp = q.project.root.groups["Breach: DWR-1"]
        assert len(grp.layers) == 4
        dlg.close()
