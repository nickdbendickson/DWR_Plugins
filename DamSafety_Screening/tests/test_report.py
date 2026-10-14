"""Report builder: PNG encoder, per-dam page, statewide summary and review queue."""
import os, sys, zlib, struct, tempfile, csv
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import report as rp, gridutil as gu  # noqa: E402

out = os.environ.get("REPORT_OUT") or tempfile.mkdtemp()
os.makedirs(os.path.join(out, "dams"), exist_ok=True)
yy, xx = np.mgrid[0:300, 0:400]
dem = 100 - 0.02 * yy + 0.0005 * (xx - 200) ** 2
depth = np.clip(3.5 - 0.0005 * (xx - 200) ** 2 - 0.004 * yy, 0, None) * (yy > 30)
img = rp.flood_map(depth, gu.hillshade(dem, 5.0), [(120, 205, "lethal"), (150, 150, "structure"),
                                                  (200, 200, "road")], breach_rc=(30, 200))
png = rp.png_bytes(img)
assert png[:8] == b"\x89PNG\r\n\x1a\n"
w, h = struct.unpack(">II", png[16:24]); assert (w, h) == (400, 300)
t = np.linspace(0, 7200, 300); q = 800 * (t / 2400) * np.exp(1 - t / 2400)
rec = {"dam_id": "DWR-5289", "name": "Test Dam", "hz_class": "High", "hz_reason": "1 habitable structure(s) with lethal conditions",
       "haz_existing": "Low", "hz_vs_existing": "higher than existing", "hz_conf": "medium", "height_m": 8.0,
       "store_af": 2700.0, "qp_m3s": 812.0, "tf_min": 77.0, "engine": "builtin", "cell_m": 10.0, "sim_h": 3.9,
       "max_depth_m": 3.5, "area_ha": 210.0, "n_bldg_wet": 2, "n_bldg_lethal": 1, "n_road_wet": 1, "n_rail_wet": 0,
       "class_stable": "yes", "hz_notes": "ACC velocities near the breach may be unreliable"}
page = rp.dam_page(rec, png, (t, q), [{"kind": "structure", "depth_m": 1.4, "vel_ms": 1.1, "dv_m2s": 1.5,
                                        "lethal": "YES", "level": "High"}], os.path.join(out, "dams", "DWR-5289.html"))
assert "Needs review" in page and "HIGHER" in page and "data:image/png;base64," in page
recs = [rec, {"dam_id": "DWR-2", "name": "Quiet Dam", "hz_class": "Low", "haz_existing": "Low",
              "hz_vs_existing": "agree", "hz_conf": "medium", "status_sim": "triage"},
        {"dam_id": "DWR-3", "name": "Broken", "hz_class": "NoData", "status_sim": "FAILED"}]
idx, csvp = rp.summary(recs, out)
rows = list(csv.DictReader(open(csvp)))
assert len(rows) == 3 and rows[1]["review"] == "" and "simulation failed" in rows[2]["review"]
assert rp.review_reasons({"edge_flag": "YES", "dir_conf": "low", "class_stable": "no"}) == [
    "flood truncated at terrain edge", "breach direction uncertain", "class changes between sensitivity runs"]
print("report written to", out); print("OK")
