// Hydraulic tables, normal depth and Muskingum-Cunge routing on synthetic prismatic channels.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { DEFAULT_ROUGHNESS, normalDepth, reachSlopes, routeMuskingumCunge, type Section, sectionTable, wetAt } from "./routing.ts";

const near = (a: number, b: number, rel: number, what: string) => assert.ok(Math.abs(a - b) <= rel * Math.abs(b) + 1e-9, `${what}: ${a} vs ${b}`);

/** Rectangular channel of width b and depth d in a floodplain fw wide each side, side walls vertical-ish (1 cm). */
function rectSection(x: number, b: number, d: number, bed: number, fw = 0): Section {
  const o: number[] = [];
  const z: number[] = [];
  const add = (off: number, el: number) => {
    o.push(off);
    z.push(el);
  };
  if (fw > 0) add(-b / 2 - fw, bed + d + 5);
  add(-b / 2 - fw, bed + d);
  add(-b / 2 - 0.01, bed + d);
  add(-b / 2, bed);
  add(b / 2, bed);
  add(b / 2 + 0.01, bed + d);
  add(b / 2 + fw, bed + d);
  if (fw > 0) add(b / 2 + fw, bed + d + 5);
  return { x, offsets: o, z };
}

/** Trapezoid: bottom b, side slope z (H:1V), depth d. */
function trapSection(x: number, b: number, zz: number, d: number, bed: number): Section {
  return { x, offsets: [-b / 2 - zz * d, -b / 2, b / 2, b / 2 + zz * d], z: [bed + d, bed, bed, bed + d] };
}

const single = { nChannel: 0.03, nFloodplain: 0.03, bankfullM: 1000 };

describe("sectionTable and wetAt", () => {
  it("matches the rectangle's area, perimeter, top width and conveyance", () => {
    const t = sectionTable(rectSection(0, 20, 5, 100), single, 30, 0.5);
    const r = t.depth.length - 1;
    near(t.maxDepthM, 5, 1e-9, "max depth");
    near(t.area[r], 100, 0.01, "area");
    near(t.topWidth[r], 20, 0.01, "top width");
    near(t.perimeter[r], 30, 0.01, "perimeter");
    near(t.conveyance[r], (100 * Math.pow(100 / 30, 2 / 3)) / 0.03, 0.01, "conveyance");
  });

  it("splits channel and floodplain roughness at bankfull, matches a trapezoid, and ignores a pocket behind a levee", () => {
    // wetAt matches the exact trapezoid formulas under one roughness.
    const tr = trapSection(0, 10, 2, 8, 0);
    const w = wetAt(tr, 4, 1000, single);
    const A = (10 + 2 * 4) * 4;
    near(w.area, A, 1e-6, "trap area");
    near(w.topWidth, 10 + 2 * 2 * 4, 1e-6, "trap top width");
    near(w.perimeter, 10 + 2 * 4 * Math.hypot(1, 2), 1e-6, "trap perimeter");
    near(w.conveyance, (A * Math.pow(A / (10 + 2 * 4 * Math.hypot(1, 2)), 2 / 3)) / 0.03, 1e-6, "trap conveyance");
    // A rougher floodplain lowers conveyance versus a uniform low n at the same stage.
    const sec = rectSection(0, 10, 2, 50, 100);
    const lowN = wetAt(sec, 53, 51, { nChannel: 0.03, nFloodplain: 0.03, bankfullM: 1 });
    const hiFp = wetAt(sec, 53, 51, { nChannel: 0.03, nFloodplain: 0.1, bankfullM: 1 });
    near(hiFp.area, lowN.area, 1e-9, "same area");
    assert.ok(hiFp.conveyance < lowN.conveyance, `floodplain n lowers conveyance: ${hiFp.conveyance} vs ${lowN.conveyance}`);
    // A 20 m pond beyond a 1 m levee on the right is not counted (not connected to the thalweg at this stage).
    const pocket: Section = { x: 0, offsets: [-10, -5, 5, 10, 11, 31, 32], z: [60, 50, 50, 53, 50, 50, 60] };
    const pw = wetAt(pocket, 52.5, 51, single);
    assert.ok(pw.topWidth < 25, `pocket excluded from top width: ${pw.topWidth}`);
    near(sectionTable(pocket, single).maxDepthM, 3, 1e-9, "table stops at the lower bank");
  });
});

describe("normalDepth", () => {
  it("reproduces the wide-rectangle formula and an independent trapezoid solution, and flags a capped section", () => {
    const n = 0.03;
    const S = 0.001;
    const t = sectionTable(rectSection(0, 1000, 10, 0), single, 30, 0.02);
    const q = 2; // m²/s per metre of width
    const y = Math.pow((n * q) / Math.sqrt(S), 3 / 5);
    const nd = normalDepth(t, q * 1000, S);
    near(nd.depthM, y, 0.02, "wide rectangle depth");
    near(nd.velocityMps, q / y, 0.03, "velocity");
    // Trapezoid b = 10, z = 2, Q = 50, by bisection on the analytic A and P.
    const b = 10, zz = 2, Q = 50;
    let lo = 0, hi = 10;
    for (let i = 0; i < 80; i++) {
      const m = (lo + hi) / 2;
      const A = (b + zz * m) * m;
      const P = b + 2 * m * Math.hypot(1, zz);
      if ((A * Math.pow(A / P, 2 / 3) * Math.sqrt(S)) / n < Q) lo = m;
      else hi = m;
    }
    const tt = sectionTable(trapSection(0, b, zz, 10, 0), single, 30, 0.02);
    near(normalDepth(tt, Q, S).depthM, lo, 0.01, "trapezoid depth");
    const shallow = sectionTable(rectSection(0, 10, 1, 0), single);
    const c = normalDepth(shallow, 500, S);
    assert.equal(c.capped, true);
    near(c.depthM, 1, 1e-9, "capped at the table top");
    assert.equal(normalDepth(shallow, 0, S).depthM, 0);
  });
});

/** A prismatic channel: sections every `dx` m over `L` m at slope S, rectangle b wide, 8 m deep, 300 m floodplain. */
function channel(L: number, dx: number, S: number, b = 30) {
  const secs: Section[] = [];
  for (let x = 0; x <= L + 1e-6; x += dx) secs.push(rectSection(x, b, 8, 100 - S * x, 300));
  return secs.map((s) => sectionTable(s, DEFAULT_ROUGHNESS, 30, 0.05));
}

/** Triangular hydrograph: 0 → peak at tp → 0 at 3 tp, then a tail of zeros to tEnd. */
const triangle = (peak: number, tp: number, tEnd: number) => {
  const tS: number[] = [];
  const qCms: number[] = [];
  for (let t = 0; t <= tEnd; t += 60) {
    tS.push(t);
    qCms.push(t <= tp ? (peak * t) / tp : t <= 3 * tp ? peak * (1 - (t - tp) / (2 * tp)) : 0);
  }
  return { tS, qCms };
};

describe("routeMuskingumCunge", () => {
  it("carries the inflow peak itself at the first section, even when the peak falls between default steps", () => {
    // Peak at 1,117 s: with the default step (tp/30 ≈ 37.2 s, then rounded) the clock would straddle it.
    const tS = [0, 1117, 4000];
    const qCms = [0, 830.25, 0];
    const r = routeMuskingumCunge({ tS, qCms }, channel(5_000, 500, 0.001), { maxHours: 12 });
    near(r.sections[0].peakCms, 830.25, 1e-9, "first-section peak");
    near(r.sections[0].tPeakS, 1117, 1e-9, "first-section peak time");
  });

  it("passes a steady flow unchanged and conserves volume", () => {
    const tables = channel(10_000, 500, 0.001);
    const steady = { tS: [0, 3600 * 6], qCms: [100, 100] };
    const r = routeMuskingumCunge(steady, tables, { maxHours: 6 });
    for (const s of r.sections) near(s.peakCms, 100, 0.002, `steady peak at ${s.x}`);
    const tri = triangle(400, 1800, 6 * 3600);
    // Run until the reach has all but drained, so volume out ≈ volume in (the small gap is the tail below the cut-off).
    const r2 = routeMuskingumCunge(tri, tables, { maxHours: 72, recessionFraction: 0.0002 });
    assert.ok(r2.volumeOutM3 <= r2.volumeInM3 * 1.001, `no volume created: ${r2.volumeOutM3} vs ${r2.volumeInM3}`);
    near(r2.volumeOutM3, r2.volumeInM3, 0.03, "volume");
    assert.ok(r2.subReaches.every((k) => k >= 1));
    assert.ok(r2.sections.every((s) => Number.isFinite(s.peakCms) && s.peakCms >= 0 && !s.capped));
  });

  it("attenuates and delays the peak downstream, more on a flat reach than on a steep one", () => {
    const tri = triangle(400, 1800, 6 * 3600);
    const steep = routeMuskingumCunge(tri, channel(20_000, 500, 0.002), { maxHours: 24, keepPoints: 50 });
    const flat = routeMuskingumCunge(tri, channel(20_000, 500, 0.0002), { maxHours: 48 });
    for (const r of [steep, flat])
      for (let j = 1; j < r.sections.length; j++) {
        assert.ok(r.sections[j].peakCms <= r.sections[j - 1].peakCms + 1e-6, `peak falls at ${r.sections[j].x}`);
        assert.ok(r.sections[j].tPeakS >= r.sections[j - 1].tPeakS, `peak later at ${r.sections[j].x}`);
        assert.ok((r.sections[j].tArrivalS ?? 0) >= (r.sections[j - 1].tArrivalS ?? 0), `arrival later at ${r.sections[j].x}`);
      }
    const lossSteep = 1 - steep.sections.at(-1)!.peakCms / 400;
    const lossFlat = 1 - flat.sections.at(-1)!.peakCms / 400;
    assert.ok(lossSteep > 0.01 && lossFlat > lossSteep, `losses steep ${lossSteep} flat ${lossFlat}`);
    assert.ok(steep.sections[0].t!.length <= 50 && steep.sections[0].t!.length > 10);
    assert.ok(steep.sections.every((s) => s.wseM > s.bedM && s.velocityMps > 0 && s.topWidthM >= 30));
  });

  it("moves the peak at about the kinematic celerity", () => {
    const S = 0.001;
    const tables = channel(20_000, 500, S);
    const r = routeMuskingumCunge(triangle(400, 3600, 10 * 3600), tables, { maxHours: 24 });
    const first = r.sections[0];
    const last = r.sections.at(-1)!;
    // Kinematic celerity dQ/dA of the compound section at a discharge between the two peaks (what the scheme uses).
    const qm = 0.5 * (first.peakCms + last.peakCms);
    const a1 = normalDepth(tables[0], qm * 0.9, S).areaM2;
    const a2 = normalDepth(tables[0], qm * 1.1, S).areaM2;
    const c = (qm * 0.2) / (a2 - a1);
    const travel = last.tPeakS - first.tPeakS;
    near(travel, 20_000 / c, 0.35, "peak travel time");
  });

  it("floors flat and adverse reaches and reports them, and flags sections that cannot carry the peak", () => {
    const tables = channel(5_000, 500, 0.001);
    tables[3] = { ...tables[3], bedM: tables[2].bedM + 1 }; // an adverse step
    const { slopes, floored } = reachSlopes(tables, 0.0001);
    assert.equal(floored, 1);
    assert.ok(slopes.every((s) => s >= 0.0001));
    const small = [rectSection(0, 5, 0.5, 100), rectSection(1000, 5, 0.5, 99)].map((s) => sectionTable(s, single));
    const r = routeMuskingumCunge({ tS: [0, 7200], qCms: [200, 200] }, small, { maxHours: 2 });
    assert.ok(r.sections.every((s) => s.capped));
    assert.match(r.notes.join(" "), /cannot carry the routed peak/);
  });
});

/** A channel whose upstream half is steep and downstream half is flat, to exercise the per-reach terrain trigger. */
function mixedChannel(steepS: number, flatS: number, b = 30) {
  const secs: Section[] = [];
  let bed = 100;
  let x = 0;
  for (let i = 0; i < 10; i++) {
    secs.push(rectSection(x, b, 8, bed, 300));
    const s = i < 5 ? steepS : flatS; // reach i connects section i to i+1
    bed -= s * 500;
    x += 500;
  }
  secs.push(rectSection(x, b, 8, bed, 300));
  return secs.map((s) => sectionTable(s, DEFAULT_ROUGHNESS, 30, 0.05));
}

describe("routeMuskingumCunge: MCT and the terrain trigger", () => {
  const tri = triangle(400, 1800, 6 * 3600);

  it("conserves mass with the MCT (variable-parameter Todini) router", () => {
    // A steady flow passes unchanged: the varying-parameter coefficients still sum to 1 every step, so with no toe to
    // clamp the balance is exact (the Todini mass-conservative property, the whole reason to prefer it over naive VPMC).
    const tables = channel(15_000, 500, 0.001);
    const steady = routeMuskingumCunge({ tS: [0, 3600 * 6], qCms: [100, 100] }, tables, { router: "mct", maxHours: 6 });
    for (const s of steady.sections) near(s.peakCms, 100, 0.002, `MCT steady peak at ${s.x}`);
    // On a wave, MCT never manufactures volume (unlike naive VPMC's ~20 %): volume out stays at or below volume in,
    // the only leak being the shared non-negativity clamp on the recession toe, well under a percent.
    const r = routeMuskingumCunge(tri, tables, { router: "mct", maxHours: 72, recessionFraction: 0.0002 });
    assert.ok(r.volumeOutM3 <= r.volumeInM3 * 1.01, `MCT manufactures no volume: ${r.volumeOutM3} vs ${r.volumeInM3}`);
    assert.ok(r.sections.every((s) => Number.isFinite(s.peakCms) && s.peakCms >= 0), "MCT peaks finite and non-negative");
    for (let j = 1; j < r.sections.length; j++)
      assert.ok(r.sections[j].peakCms <= r.sections[j - 1].peakCms + 1e-6, `MCT peak falls at ${r.sections[j].x}`);
  });

  it("'auto' with a high trigger is identical to plain MC, and 'auto' on a confined channel turns MCT on", () => {
    const tables = channel(10_000, 500, 0.002);
    const mc = routeMuskingumCunge(tri, tables, { router: "mc", maxHours: 48, keepPoints: 20 });
    // A trigger above any real slope leaves every reach on frozen MC: the peaks must match MC to machine precision.
    const autoHigh = routeMuskingumCunge(tri, tables, { router: "auto", confineSlopeTrigger: 1, maxHours: 48, keepPoints: 20 });
    for (let j = 0; j < tables.length; j++) near(autoHigh.sections[j].peakCms, mc.sections[j].peakCms, 1e-12, `auto(high)=MC at ${tables[j].x}`);
    assert.ok(/0 of \d+ reaches confined/.test(autoHigh.notes.join(" ")), "auto(high) uses no MCT");
    // On this confined channel a low trigger puts every reach on MCT.
    const autoLow = routeMuskingumCunge(tri, tables, { router: "auto", confineSlopeTrigger: 0.0005, maxHours: 48 });
    const m = autoLow.notes.join(" ").match(/(\d+) of (\d+) reaches confined/);
    assert.ok(m && Number(m[1]) === Number(m[2]) && Number(m[1]) > 0, `auto(low) uses MCT on every reach: ${autoLow.notes.join(" ")}`);
  });

  it("picks MCT on the steep half and MC on the flat half of a mixed channel", () => {
    const tables = mixedChannel(0.003, 0.0003);
    const r = routeMuskingumCunge(tri, tables, { router: "auto", confineSlopeTrigger: 0.0015, maxHours: 48 });
    const m = r.notes.join(" ").match(/(\d+) of (\d+) reaches confined/);
    assert.ok(m, "auto reports its split");
    const mct = Number(m![1]);
    const total = Number(m![2]);
    // The five steep reaches qualify, the five flat ones do not — a genuine per-reach split, not all-or-nothing.
    assert.ok(mct > 0 && mct < total, `mixed channel splits the router: ${mct} of ${total}`);
    assert.equal(mct, 5);
    // The result is still physical: peaks fall downstream and mass is not created beyond the shared toe clamp.
    assert.ok(r.volumeOutM3 <= r.volumeInM3 * 1.01, `mixed-router manufactures no volume: ${r.volumeOutM3} vs ${r.volumeInM3}`);
    for (let j = 1; j < r.sections.length; j++) assert.ok(r.sections[j].peakCms <= r.sections[j - 1].peakCms + 1e-6, `peak falls at ${r.sections[j].x}`);
  });
});
