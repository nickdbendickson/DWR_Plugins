// Results import and comparison, on tiny synthetic grids made here (no real model output is in the repo).
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { zipSync, strToU8 } from "fflate";

import { analyze } from "./engine.ts";
import { exportDamJson } from "./export.ts";
import { classifyFile, M_TO_FT, parseAsciiGrid, projector } from "./raster.ts";
import { createEngine, wantFile, type RunSummary } from "./raster-engine.ts";
import { analysisOfRecord, deskRank, disagreement, rankSample, receptorPoints, recordChecks, resultsContext } from "./results.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord, ReceptorSample, ResultsOfRecord } from "./types.ts";

const dam = (): DamRecord => structuredClone(SAMPLE_DAMS.find((d) => d.id === "nd00199")!);

const buf = (s: string | Uint8Array) => {
  const u = typeof s === "string" ? strToU8(s) : s;
  return u.buffer.slice(u.byteOffset, u.byteOffset + u.byteLength) as ArrayBuffer;
};

/** 100 × 60 cells of 10 m in UTM 14N; wet 2 m deep in a 200 m band from 200 m upstream of the breach to the east edge. */
function synthRun(breach: { lon: number; lat: number }, opts: { georef?: boolean; crs?: string } = {}) {
  const p = projector(opts.crs ?? "EPSG:32614");
  const [bx, by] = p.fwd(breach.lon, breach.lat);
  const W = 100, H = 60, dx = 10;
  const x0 = Math.round(bx) - 200, y0 = Math.round(by) + 300;
  const grid = (f: (x: number, y: number) => number) => {
    const rows: string[] = [];
    for (let r = 0; r < H; r++) {
      const row: string[] = [];
      for (let c = 0; c < W; c++) {
        const x = x0 + (c + 0.5) * dx, y = y0 - (r + 0.5) * dx;
        row.push(Math.abs(y - by) < 100 ? String(f(x, y)) : "-9999");
      }
      rows.push(row.join(" "));
    }
    const g = opts.georef === false ? { x: 0, y: 0 } : { x: x0, y: y0 - H * dx };
    return `ncols ${W}\nnrows ${H}\nxllcorner ${g.x}\nyllcorner ${g.y}\ncellsize ${dx}\nNODATA_value -9999\n${rows.join("\n")}\n`;
  };
  return {
    bx, by, x0,
    max: grid(() => 2),
    vmax: grid(() => 1),
    inittm: grid((x) => Math.max(0, (x - bx) / 1000)),
  };
}

const ctxOf = (d: DamRecord) => {
  const rc = resultsContext(d, analyze(d));
  assert.ok(rc.ctx);
  return rc.ctx!;
};

describe("classifyFile", () => {
  it("knows LISFLOOD ASCII, Runner GeoTIFFs and DSS-WISE grids, and skips the rest", () => {
    assert.deepEqual(
      [classifyFile("run/res.max"), classifyFile("res.Vmax"), classifyFile("res.inittm.asc")].map((r) => r.kind === "grid" && [r.variable, r.srcUnit]),
      [["depth", "m"], ["velocity", "m/s"], ["arrival", "h"]],
    );
    const r = classifyFile("_breach_model/lisflood_hmax_ft.tif");
    assert.ok(r.kind === "grid" && r.variable === "depth" && r.factor === 1);
    const h = classifyFile("117417_Raster_Files/117417_Hmax_ft_upto_final.tif");
    assert.ok(h.kind === "grid" && h.variable === "depth");
    assert.equal(classifyFile("117417_DVmax_ft2ps.tif").kind, "skip");
    assert.equal(classifyFile("117417_H_ft_at_final.tif").kind, "skip");
    assert.equal(classifyFile("117417_DEM_ft.tif").kind, "skip");
    assert.equal(classifyFile("._117417_Hmax_ft_upto_final.tif").kind, "skip");
    assert.equal(classifyFile("__MACOSX/x/res.max").kind, "skip");
    assert.equal(classifyFile("res-0012.wd").kind, "skip");
    const fin = classifyFile("Depth (Max).tif");
    assert.ok(fin.kind === "grid" && fin.final);
    assert.ok(wantFile("a/dam.json") && wantFile("x.zip") && wantFile("r.max") && !wantFile("notes.txt"));
  });
});

describe("parseAsciiGrid", () => {
  it("reads corners and centres, nodata, and flags an ungeoreferenced header", () => {
    const g = parseAsciiGrid("NCOLS 2\nNROWS 2\nXLLCENTER 105\nYLLCENTER 205\nCELLSIZE 10\nNODATA_VALUE -9999\n1 2\n-9999 4\n");
    assert.equal(g.x0, 100);
    assert.equal(g.y0, 220);
    assert.ok(g.georeferenced);
    assert.equal(g.data[3], 4);
    assert.ok(!(g.data[2] > -1));
    const u = parseAsciiGrid("ncols 1\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 30\n5\n");
    assert.equal(u.georeferenced, false);
  });
});

describe("results engine import", () => {
  it("imports a zipped LISFLOOD ASCII run, converts m to ft, and scans it against the record", async () => {
    const d = dam();
    const ctx = ctxOf(d);
    const s = synthRun(ctx.breach);
    const zip = zipSync({
      "run/res.max": strToU8(s.max),
      "run/res.Vmax": strToU8(s.vmax),
      "run/res.inittm": strToU8(s.inittm),
      "run/res-0001.wd": strToU8(s.max),
      "__MACOSX/run/._res.max": strToU8("junk"),
    });
    const eng = createEngine();
    const sum = (await eng.handle({ type: "import", key: "t:lisflood", source: "lisflood", files: [{ name: "run.zip", buf: buf(zip) }], crs: "auto", ctx })) as RunSummary;
    assert.equal(sum.crs, "EPSG:32614");
    assert.deepEqual([...sum.vars].sort(), ["arrival", "depth", "velocity"]);
    const st = sum.stats!;
    assert.ok(Math.abs(st.peakDepthFt - 2 * M_TO_FT) < 1e-3);
    assert.ok(Math.abs((st.maxVelocityFps ?? 0) - M_TO_FT) < 1e-3);
    // Downstream of the dam line only. The direction is the record's, not due east, so allow a band.
    assert.ok(st.wetAcAll > st.wetAc && st.wetAc > 20, `wet ${st.wetAc} of ${st.wetAcAll}`);
    assert.ok(Math.abs(st.wetAcAll - (1000 * 200) / 4046.8564224) < 1);
    assert.ok(st.edgeWetCells > 0);
    assert.ok(st.nearestWetM < 10);
    assert.ok(st.farthestKm > 0.3 && st.farthestKm <= 0.81);
    assert.ok(sum.checks.some((c) => /EPSG:32614/.test(c.text)));
    assert.ok(sum.files.some((f) => /snapshot/.test(f.use)));
    const rec = recordChecks(d, analyze(d), st, sum.runDam);
    assert.ok(rec.some((c) => c.level === "warn" && /Coverage stops/.test(c.text)), "short grid is flagged");
    assert.ok(rec.some((c) => c.level === "warn" && /raster edge/.test(c.text)));

    const pts = [
      { id: "in", lon: ctx.breach.lon, lat: ctx.breach.lat },
      { id: "out", lon: ctx.breach.lon + 0.1, lat: ctx.breach.lat },
    ];
    const samples = (await eng.handle({ type: "sample", key: "t:lisflood", points: pts })) as ReceptorSample[];
    assert.ok(samples[0].covered && Math.abs(samples[0].depthFt - 2 * M_TO_FT) < 1e-3);
    assert.equal(samples[1].covered, false);

    const strip = (await eng.handle({
      type: "strip",
      key: "t:lisflood",
      ctx,
      common: { disclaimer: "x", dam_id: "ND00199", dam_name: "t", crest_navd88_ft: 1, peak_cfs: 1, engine_version: "t" },
      direction: "",
    })) as { features: unknown[] } | null;
    assert.ok(strip && strip.features.length > 0);

    const r = (await eng.handle({ type: "render", mode: "depth", keys: ["t:lisflood"], maxPx: 128 })) as { width: number; images: Uint8ClampedArray[] };
    assert.ok(r.width <= 128 && r.images[0].some((v) => v > 0));
    assert.equal(await eng.handle({ type: "drop", key: "t:lisflood" }), true);
  });

  it("refuses an ungeoreferenced ASCII depth grid with nothing to borrow a georeference from", async () => {
    const d = dam();
    const s = synthRun(ctxOf(d).breach, { georef: false });
    await assert.rejects(
      createEngine().handle({ type: "import", key: "u", source: "lisflood", files: [{ name: "res.max", buf: buf(s.max) }], crs: "auto", ctx: ctxOf(d) }),
      /georeferenc/i,
    );
  });

  it("flags a run whose grid is far from the record's dam (wrong CRS picked)", async () => {
    const d = dam();
    const ctx = ctxOf(d);
    const s = synthRun(ctx.breach, { crs: "EPSG:32614" });
    const sum = (await createEngine().handle({ type: "import", key: "w", source: "lisflood", files: [{ name: "res.max", buf: buf(s.max) }], crs: "EPSG:32613", ctx })) as RunSummary;
    const rec = recordChecks(d, analyze(d), sum.stats!, sum.runDam);
    assert.ok(rec.some((c) => c.level === "error"), JSON.stringify(rec));
  });

  it("reads a georeferenced GeoTIFF (DSS-WISE style, ft, nodata 0)", async () => {
    const { writeArrayBuffer } = await import("geotiff");
    const d = dam();
    const ctx = ctxOf(d);
    const p = projector("EPSG:26914");
    const [bx, by] = p.fwd(ctx.breach.lon, ctx.breach.lat);
    const W = 40, H = 20, dx = 5;
    const x0 = Math.round(bx) - 50, y0 = Math.round(by) + 50;
    const v = new Float32Array(W * H);
    for (let r = 0; r < H; r++) for (let c = 0; c < W; c++) v[r * W + c] = c >= 5 ? 3 + c / 10 : 0;
    const tif = writeArrayBuffer(v, {
      width: W,
      height: H,
      ModelPixelScale: [dx, dx, 0],
      ModelTiepoint: [0, 0, 0, x0, y0, 0],
      GTModelTypeGeoKey: 1,
      GTRasterTypeGeoKey: 1,
      ProjectedCSTypeGeoKey: 26914,
      GDAL_NODATA: "0",
      SampleFormat: [3],
      BitsPerSample: [32],
    } as Record<string, unknown>) as ArrayBuffer;
    const sum = (await createEngine().handle({ type: "import", key: "g", source: "dsswise", files: [{ name: "999_Hmax_ft_upto_final.tif", buf: tif }], crs: "auto", ctx })) as RunSummary;
    assert.equal(sum.crs, "EPSG:26914");
    assert.equal(sum.width, W);
    assert.ok(Math.abs(sum.stats!.peakDepthAllFt - 6.9) < 1e-4);
    assert.equal(sum.stats!.maxVelocityFps, null);
    assert.ok(sum.checks.some((c) => /velocity/i.test(c.text)));
  });
});

describe("compare", () => {
  it("renders A−B on a common grid and counts overlap acres", async () => {
    const d = dam();
    const ctx = ctxOf(d);
    const s = synthRun(ctx.breach);
    const deeper = s.max.replace(/(^|\s)2(?=\s|$)/gm, "$13");
    const eng = createEngine();
    await eng.handle({ type: "import", key: "a", source: "lisflood", files: [{ name: "a.max", buf: buf(deeper) }], crs: "EPSG:32614", ctx });
    await eng.handle({ type: "import", key: "b", source: "lisflood", files: [{ name: "b.max", buf: buf(s.max) }], crs: "EPSG:32614", ctx });
    const r = (await eng.handle({ type: "render", mode: "compare", keys: ["a", "b"], colors: ["#ff0000", "#00ff00"], maxPx: 200 })) as {
      images: Uint8ClampedArray[];
      compare: { bothAc: number; aOnlyAc: number; bOnlyAc: number; meanAbsFt: number };
    };
    assert.equal(r.images.length, 2);
    assert.ok(r.compare.bothAc > 40 && r.compare.aOnlyAc < 1 && r.compare.bOnlyAc < 1, JSON.stringify(r.compare));
    assert.ok(Math.abs(r.compare.meanAbsFt - M_TO_FT) < 0.01);
  });
});

describe("source of record", () => {
  const ref = (d: DamRecord, depth: number): ResultsOfRecord => {
    const pts = receptorPoints(d, analyze(d));
    return {
      source: "lisflood",
      label: "LISFLOOD-FP (test)",
      files: ["res.max"],
      importedAt: "2026-10-30T00:00:00.000Z",
      crs: "EPSG:32614",
      stats: { peakDepthFt: 9, peakDepthAllFt: 12, wetAc: 100, wetAcAll: 120, farthestKm: 5, maxVelocityFps: 4, arrivalMaxHr: 10, nearestWetM: 5, edgeWetCells: 0, coverKm: 20, cellM: 30, cells: 1, breachLon: 0, breachLat: 0 },
      samples: pts.map((p) => ({ id: p.id, covered: true, depthFt: depth, velocityFps: 1, arrivalHr: 1, lon: p.lon, lat: p.lat })),
      warnings: [],
    };
  };

  it("leaves the desk analysis alone without a source of record", () => {
    const d = dam();
    const a = analyze(d);
    assert.equal(analysisOfRecord(d, a).result, a);
    assert.ok(!exportDamJson(d, a).includes("source_of_record"));
  });

  it("re-ranks the receptors on the run's samples and names it in the screen and dam.json", () => {
    const d = dam();
    d.resultsOfRecord = ref(d, 0);
    const a = analyze(d);
    const dry = analysisOfRecord(d, a);
    assert.ok(dry.result.flood.receptors.every((r) => !r.flooded));
    assert.equal(dry.result.flood.maxDepthFt, 9);
    assert.ok(dry.result.screen.reasons.some((l) => /Source of record: LISFLOOD-FP \(test\)/.test(l)));
    d.resultsOfRecord = ref(d, 8);
    const wet = analysisOfRecord(d, analyze(d));
    assert.ok(wet.result.flood.receptors.some((r) => r.flooded && r.zone));
    const j = JSON.parse(exportDamJson(d, wet.result));
    assert.equal(j.source_of_record.label, "LISFLOOD-FP (test)");
    assert.equal(j.source_of_record.peak_depth_ft, 9);
  });

  it("marks receptors outside the run as unresolved and flags disagreements", () => {
    const d = dam();
    const a = analyze(d);
    const i = 0;
    const out = rankSample(d, i, { id: d.receptors[i].id, covered: false, depthFt: 0, velocityFps: null, arrivalHr: null, lon: 0, lat: 0 }, "X");
    assert.equal(out.covered, false);
    const wet = rankSample(d, i, { id: d.receptors[i].id, covered: true, depthFt: 6, velocityFps: 5, arrivalHr: 1, lon: 0, lat: 0 }, "X");
    const dry = rankSample(d, i, { id: d.receptors[i].id, covered: true, depthFt: 0.05, velocityFps: 0, arrivalHr: null, lon: 0, lat: 0 }, "Y");
    const flags = disagreement({ desk: deskRank(a.flood.receptors[i]), lisflood: wet, dsswise: dry });
    assert.ok(flags.some((f) => /dry in/.test(f)));
    assert.deepEqual(disagreement({ lisflood: wet, dsswise: out }), []);
  });
});
