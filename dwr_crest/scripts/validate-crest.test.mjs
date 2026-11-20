// The validation CLI end to end on synthetic LISFLOOD ASCII grids (no real model output is in the repo).
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";
import proj4 from "proj4";

import { bearingVector, layerFrame, parseArgs, parseCsv, readPoints } from "./validate-crest.mjs";
import { tempDir } from "./test-tmp.mjs";

const UTM14 = "+proj=utm +zone=14 +datum=WGS84 +units=m +no_defs";
const BREACH = { lon: -97.3252, lat: 46.9474 };

/** 100 × 60 cells of 10 m: 2 m deep (1 m/s) in a 200 m band from 200 m west of the breach to the east edge. */
function writeRun(dir) {
  const p = proj4("+proj=longlat +datum=WGS84 +no_defs", UTM14);
  const [bx0, by0] = p.forward([BREACH.lon, BREACH.lat]);
  const bx = Math.round(bx0);
  const by = Math.round(by0);
  const W = 100, H = 60, dx = 10;
  const x0 = bx - 200, y0 = by + 300;
  const grid = (f) => {
    const rows = [];
    for (let r = 0; r < H; r++) {
      const row = [];
      for (let c = 0; c < W; c++) {
        const x = x0 + (c + 0.5) * dx, y = y0 - (r + 0.5) * dx;
        row.push(Math.abs(y - by) < 100 ? String(f(x, y)) : "-9999");
      }
      rows.push(row.join(" "));
    }
    return `ncols ${W}\nnrows ${H}\nxllcorner ${x0}\nyllcorner ${y0 - H * dx}\ncellsize ${dx}\nNODATA_value -9999\n${rows.join("\n")}\n`;
  };
  writeFileSync(join(dir, "res.max"), grid(() => 2));
  writeFileSync(join(dir, "res.Vmax"), grid(() => 1));
  writeFileSync(join(dir, "res.inittm"), grid((x) => Math.max(0, (x - bx) / 1000)));
  const inv = (x, y) => p.inverse([x, y]);
  const rect = (xa, xb, ya, yb) => [[xa, ya], [xb, ya], [xb, yb], [xa, yb], [xa, ya]].map(([x, y]) => inv(x, y));
  return { bx, by, x0, W, dx, inv, rect };
}

describe("validate-crest CLI", () => {
  it("parses its options and the layer frame", () => {
    const o = parseArgs(["--runner", "r", "--crest", "a.geojson", "--crest", "b.geojson", "--breach", "-97.3,46.9", "--bearing", "130", "--json", "o.json"]);
    assert.deepEqual(o.crest, ["a.geojson", "b.geojson"]);
    assert.deepEqual(o.breach, { lon: -97.3, lat: 46.9 });
    assert.equal(o.bearing, 130);
    assert.throws(() => parseArgs(["--crest", "a"]), /--runner/);
    assert.throws(() => parseArgs(["--runner", "r"]), /--crest/);
    const fr = layerFrame({ properties: { breach_lon: -97.1, breach_lat: 46.5, direction: "Downstream direction: across the dam (bearing 130°). x" } });
    assert.deepEqual(fr, { breach: { lon: -97.1, lat: 46.5 }, bearing: 130, label: "flood strip" });
    const [e, n] = bearingVector(90);
    assert.ok(Math.abs(e - 1) < 1e-9 && Math.abs(n) < 1e-9);
  });

  it("reads receptor points from CSV and GeoJSON", () => {
    const dir = tempDir("validate-crest-");
    writeFileSync(join(dir, "p.csv"), 'order,address,lon,lat\n1,"3210 150TH AVE SE, WHEATLAND",-97.32,46.94\n2,"x",bad,1\n');
    assert.deepEqual(readPoints(join(dir, "p.csv")), [{ id: "1", lon: -97.32, lat: 46.94 }]);
    writeFileSync(join(dir, "p.geojson"), JSON.stringify({ type: "FeatureCollection", features: [{ type: "Feature", properties: { id: "h1" }, geometry: { type: "Point", coordinates: [-97.3, 46.9] } }] }));
    assert.deepEqual(readPoints(join(dir, "p.geojson")), [{ id: "h1", lon: -97.3, lat: 46.9 }]);
    assert.deepEqual(parseCsv('a,"b ""c""",d\r\n1,2,3\n'), [["a", 'b "c"', "d"], ["1", "2", "3"]]);
  });

  it("scores a half-width strip against the synthetic run and writes the JSON report", () => {
    const dir = tempDir("validate-crest-");
    const g = writeRun(dir);
    const strip = {
      type: "FeatureCollection",
      properties: { breach_lon: BREACH.lon, breach_lat: BREACH.lat, direction: "Downstream direction: east (bearing 90°)." },
      features: [
        { type: "Feature", properties: { depth_max_ft: 3, depth_min_ft: 3 }, geometry: { type: "Polygon", coordinates: [g.rect(g.x0, g.x0 + g.W * g.dx, g.by - 50, g.by + 50)] } },
      ],
    };
    writeFileSync(join(dir, "strip.geojson"), JSON.stringify(strip));
    const [plon, plat] = g.inv(g.bx + 300, g.by);
    const [qlon, qlat] = g.inv(g.bx + 300, g.by - 80);
    writeFileSync(join(dir, "pts.csv"), `id,lon,lat\nin,${plon},${plat}\nedge,${qlon},${qlat}\n`);
    const out = join(dir, "report.json");
    const r = spawnSync(
      process.execPath,
      ["--experimental-strip-types", "--no-warnings", "scripts/validate-crest.mjs", "--runner", dir, "--crest", join(dir, "strip.geojson"), "--receptors", join(dir, "pts.csv"), "--crs", "EPSG:32614", "--json", out],
      { encoding: "utf8" },
    );
    assert.equal(r.status, 0, r.stderr + r.stdout);
    assert.match(r.stdout, /Model: LISFLOOD-FP/);
    assert.match(r.stdout, /flood strip vs LISFLOOD-FP \(downstream of the breach only\)/);
    assert.match(r.stdout, /recall 5\d %/);
    assert.match(r.stdout, /receptors 2\/2 inside the model grid: both wet 1, both dry 0, missed 1/);
    const j = JSON.parse(readFileSync(out, "utf8"));
    assert.equal(j.bearing, 90);
    assert.equal(j.reports.length, 1);
    const e = j.reports[0].extent;
    assert.ok(Math.abs(e.precision - 1) < 0.03 && Math.abs(e.areaRatio - 0.5) < 0.03, JSON.stringify(e));
    // Model depth is 2 m = 6.56 ft; the strip said 3 ft.
    assert.ok(Math.abs(e.depthBiasFt + 3.56) < 0.02, String(e.depthBiasFt));
    const rows = j.reports[0].receptors.rows;
    assert.equal(rows[0].verdict, "both wet");
    assert.equal(rows[0].ref.velocityFps.toFixed(2), "3.28");
    assert.equal(rows[0].ref.zone, "high");
    assert.equal(rows[1].verdict, "missed");
  });
});
