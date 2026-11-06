"""Data 1.7.0: DamSafety Data keeps its offline pack up to date (packsource.py), like the ND Data
Updater: missing layers built, layers older than the refresh interval fetched again, the rest
left alone, both formats rewritten from the same download, local DWR / HIFLD layers never
fetched by age; the updater only looks after a pack Build offline pack made; one build of a
folder at a time.

    python -m pytest -q DamSafety_Data/tests/test_pack_refresh.py   (from the repository root)
"""
import datetime
import json
import os

import pytest

pytest.importorskip("osgeo.gdal")

from DamSafety_Data import core  # noqa: E402
from DamSafety_Data import pack_build as PB  # noqa: E402
from DamSafety_Data import packsource as PS  # noqa: E402
from DamSafety_Data import sources  # noqa: E402
from DamSafety_Data.tests.test_pack_build import BBOX, _build, mocked  # noqa: E402,F401


def _source(out, days_later=0, refresh_days=30):
    later = datetime.date.today() + datetime.timedelta(days=days_later)
    src = PS.DataPackSource("pack", "Offline pack", str(out), refresh_days=refresh_days,
                            hifld_folder=getattr(PB, "_TEST_HIFLD", None),
                            now=lambda: later)
    return src


def _built(tmp_path, mocked):
    calls, nlcd = mocked
    out = tmp_path / "out"
    out.mkdir()
    r, _ = _build(out, nlcd)
    assert not r["failed"], r["failed"]
    del calls[:]
    return out, calls


def _bbox_build(monkeypatch):
    real = PB.build

    def build(out_parent, **kw):
        kw.setdefault("bbox", BBOX)
        return real(out_parent, **kw)
    monkeypatch.setattr(PB, "build", build)


def test_no_pack_no_source(tmp_path):
    cfg = {"enabled": {"pack": True}}
    assert [s for s in sources.build_sources(cfg) if s.id == "pack"] == []
    cfg["pack_out"] = str(tmp_path)                # a folder without a pack built there
    assert [s for s in sources.build_sources(cfg) if s.id == "pack"] == []
    assert sources.SOURCE_INFO["pack"][2] is True


def test_source_for_a_built_pack(tmp_path, mocked):
    out, _calls = _built(tmp_path, mocked)
    got = [s for s in sources.build_sources({"pack_out": str(out), "pack_refresh_days": 7})
           if s.id == "pack"]
    assert len(got) == 1
    s = got[0]
    assert s.in_place and s.refresh_days == 7
    assert s.folder == os.path.join(str(out), PB.PACK2)
    # Updater.source_path keeps the absolute pack folder (not under the data folder)
    assert core.Updater(str(tmp_path / "data"), [s], None).source_path(s) == s.folder
    # off in the settings: not built
    assert [x for x in sources.build_sources({"pack_out": str(out),
                                              "enabled": {"pack": False}})
            if x.id == "pack"] == []


def test_plan(tmp_path, mocked):
    out, _calls = _built(tmp_path, mocked)
    assert _source(out).plan()[:2] == ([], [])
    missing, stale, why = _source(out, days_later=40).plan()
    assert missing == []
    assert "ND_counties" in stale and "HPMS_ND" in stale and "ABI_dams" in stale
    # the user's HIFLD files and the land cover are not fetched by age
    assert not [n for n in stale if n.startswith("HIFLD_")]
    assert PB.NLCD_NAME not in stale
    assert "older than 30 days" in why
    # a layer gone from the manifest is missing; one not complete is stale
    p = os.path.join(str(out), PB.PACK2, "manifest.json")
    m = json.load(open(p))
    m["datasets"].pop("NID_dams")
    m["datasets"]["ND_counties"]["status"] = "partial"
    json.dump(m, open(p, "w"))
    missing, stale, _why = _source(out).plan()
    assert missing == ["NID_dams"] and stale == ["ND_counties"]


def test_refresh_fetches_only_old_layers_both_formats(tmp_path, mocked, monkeypatch):
    out, calls = _built(tmp_path, mocked)
    _bbox_build(monkeypatch)
    p2 = os.path.join(str(out), PB.PACK2)
    m = json.load(open(os.path.join(p2, "manifest.json")))
    for k in ("ND_counties", "NID_dams"):              # these two are 40 days old
        m["datasets"][k]["fetched"] = (datetime.date.today()
                                       - datetime.timedelta(days=40)).isoformat()
    json.dump(m, open(os.path.join(p2, "manifest.json"), "w"))

    def stamp():
        mm = json.load(open(os.path.join(p2, "manifest.json")))
        return {k: os.path.getmtime(os.path.join(p2, *e["file"].split("/")))
                for k, e in mm["datasets"].items()}
    before = stamp()
    src = _source(out)
    up = core.Updater(str(tmp_path / "data"), [src], None)
    res = up.check_and_update(force=True)
    assert [(r.status, r.message.split(";")[0]) for r in res] == [
        ("updated", "{} layers, both formats".format(len(before) + 1))], res
    assert sorted(calls) == ["ND_counties", "NID_dams"]
    after = stamp()
    changed = sorted(k for k in before if after[k] != before[k])
    assert changed == ["ND_counties", "NID_dams"]     # format 2: only those files rewritten
    m2 = json.load(open(os.path.join(p2, "manifest.json")))
    m1 = json.load(open(os.path.join(str(out), PB.PACK1, "manifest.json")))
    assert m2["datasets"]["ND_counties"]["fetched"] == datetime.date.today().isoformat()
    assert m1["datasets"]["ND_counties"]["fetched"] == datetime.date.today().isoformat()
    assert PB.verify(str(out))[0]
    # the next check (forced, so the 24 h interval does not hide it) has nothing to do
    del calls[:]
    res = up.check_and_update(force=True)
    assert [r.status for r in res] == ["current"] and calls == []


def test_all_failed_is_offline_and_keeps_the_pack(tmp_path, mocked, monkeypatch):
    out, _calls = _built(tmp_path, mocked)
    _bbox_build(monkeypatch)
    from DamSafety_Data import build_nd_pack as BP

    def down(*a, **k):
        raise RuntimeError("HTTP 504")
    monkeypatch.setattr(BP, "_run_one", down)
    src = _source(out, days_later=40)
    src.plan = lambda: ([], ["ND_counties"], "1 layer")
    res = core.Updater(str(tmp_path / "data"), [src], None).check_and_update(force=True)
    assert res[0].status == "offline" and "HTTP 504" in res[0].message
    m2 = json.load(open(os.path.join(str(out), PB.PACK2, "manifest.json")))
    assert m2["datasets"]["ND_counties"]["count"] == 3             # the old copy is kept


def test_the_active_pack_is_kept_up_to_date(tmp_path, mocked, monkeypatch):
    """The window refuses to write into the pack the plugins read; the refresh is meant to."""
    out, _calls = _built(tmp_path, mocked)
    p2 = os.path.join(str(out), PB.PACK2)
    with pytest.raises(PB.PackBuildError):
        PB.check_target(str(out), [p2])
    _bbox_build(monkeypatch)
    src = _source(out)
    src.plan = lambda: ([], ["ND_counties"], "1 layer")
    status, _msg, _v = src.update(None, src.folder, core.Feedback(), True, {})
    assert status == "updated"


def test_a_pack_not_built_here_is_not_touched(tmp_path):
    out = tmp_path / "o"
    (out / PB.PACK2).mkdir(parents=True)
    (out / PB.PACK2 / "manifest.json").write_text(json.dumps({"format": "x", "datasets": {}}))
    assert PS.built_here(str(out)) == ""
    src = _source(out)
    with pytest.raises(core.UpdateError):
        src.update(None, src.folder, core.Feedback(), True, {})


def test_one_build_of_a_folder_at_a_time(tmp_path, mocked):
    out = tmp_path / "o"
    wk = out / PB.WORK
    wk.mkdir(parents=True)
    lock = wk / PB.LOCK_NAME
    lock.write_text("{} now".format(os.getpid()))       # a live build in this process
    with pytest.raises(PB.PackBuildError, match="Another build"):
        _build(out, None, only=["ND_counties"])
    lock.write_text("999999999 then")                   # its process is gone: cleared
    r, lines = _build(out, None, only=["ND_counties"])
    assert any("Cleared the left-over lock" in x for x in lines)
    assert not lock.exists()                            # and removed after the build


def test_write_format2_rewrites_only_the_named_layers(tmp_path, mocked):
    out, _calls = _built(tmp_path, mocked)
    p1, p2 = os.path.join(str(out), PB.PACK1), os.path.join(str(out), PB.PACK2)
    m2 = json.load(open(os.path.join(p2, "manifest.json")))
    m1 = json.load(open(os.path.join(p1, "manifest.json")))
    before = {k: os.path.getmtime(os.path.join(p2, *e["file"].split("/")))
              for k, e in m2["datasets"].items()}
    PB.write_format2(os.path.join(p1, "nd_offline_pack.sqlite"), p2, m1, {}, {},
                     {"NID_dams"}, lambda *a: None, lambda: False)
    m2b = json.load(open(os.path.join(p2, "manifest.json")))
    after = {k: os.path.getmtime(os.path.join(p2, *e["file"].split("/")))
             for k, e in m2b["datasets"].items()}
    assert sorted(k for k in before if before[k] != after[k]) == ["NID_dams"]
