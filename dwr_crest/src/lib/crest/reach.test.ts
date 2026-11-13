// The demand-driven downstream reach (crest issue #62): the demand signal, the terrain growth on the same lattice,
// and the loop's guards (a contained flood is never grown; the round cap flags the layer incomplete).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ACFT } from "./breach.ts";
import { analyze } from "./engine.ts";
import { type DemGrid, type DemRequest, demRequest, OUTLINE_MAX_PX, type OutlineInputs, outlineInputs } from "./outline.ts";
import {
  buildRoutedFloodReach,
  channelEnd,
  extendInputs,
  extendRequest,
  REACH_END_TOL_M,
  REACH_FLOOR_ACFT,
  REACH_MAX_ROUNDS,
  REACH_STEP_MAX_M,
  REACH_STEP_MIN_M,
  reachDemand,
  reachNote,
  reachStepM,
} from "./reach.ts";
import { buildRoutedFlood, type RoutedFloodResult } from "./routed-flood.ts";
import { SAMPLE_DAMS } from "./samples.ts";

const M_PER_DEG_LAT = 110574;
const kxOf = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);

const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const RESULT = analyze(ND);
const IO = outlineInputs(ND, RESULT);
assert.ok(IO.ok);
const INP: OutlineInputs = IO.inputs;
const LOC = INP.loc;
const U = INP.u as [number, number];

/** A routed result with just what the reach reads: the demand signal, the release, and a straight channel along u. */
function fakeRouted(d: Partial<NonNullable<RoutedFloodResult["demand"]>> & { channelEndM?: number; releasedAcFt?: number }): RoutedFloodResult {
  const channelEndM = d.channelEndM ?? 8000;
  const pts = [];
  for (let x = 0; x < channelEndM; x += 10) pts.push({ e: U[0] * x, n: U[1] * x, x });
  pts.push({ e: U[0] * channelEndM, n: U[1] * channelEndM, x: channelEndM });
  return {
    ok: true,
    demand: {
      reachedEdge: d.reachedEdge ?? true,
      travellingM3: d.travellingM3 ?? 1000 * ACFT,
      wetEndM: d.wetEndM ?? channelEndM,
      channelEndM,
      stoppedInBasin: d.stoppedInBasin ?? false,
    },
    stats: { releasedAcFt: d.releasedAcFt ?? 1600 } as RoutedFloodResult["stats"],
    centerline: { loc: LOC, pts },
    notes: [],
  } as unknown as RoutedFloodResult;
}

describe("reachDemand: grow only while the wave still reaches the channel's end with water to carry", () => {
  it("asks to grow when the flood reaches the edge, is wet to the end, and has water above the floor", () => {
    const d = reachDemand(fakeRouted({}));
    assert.ok(d.extend);
    assert.equal(d.stop, null);
    assert.equal(d.floorAcFt, REACH_FLOOR_ACFT, "5 % of 1600 ac-ft is more than the 10 ac-ft floor");
  });
  it("a flood that never reaches the terrain's edge is contained (the DWR-4031 contract)", () => {
    const d = reachDemand(fakeRouted({ reachedEdge: false }));
    assert.ok(!d.extend);
    assert.equal(d.stop, "contained");
  });
  it("water that stops short of the traced channel's end is contained even when a lateral edge is wet", () => {
    const d = reachDemand(fakeRouted({ wetEndM: 8000 - REACH_END_TOL_M - 1 }));
    assert.equal(d.stop, "contained");
    assert.ok(reachDemand(fakeRouted({ wetEndM: 8000 - REACH_END_TOL_M + 1 })).extend);
  });
  it("a flood that ended in a closed basin is not grown", () => {
    assert.equal(reachDemand(fakeRouted({ stoppedInBasin: true })).stop, "basin");
  });
  it("too little water at the edge is the volume floor: 5 % of the release or 10 ac-ft, the lesser", () => {
    assert.equal(reachDemand(fakeRouted({ travellingM3: 9 * ACFT })).stop, "volume-floor");
    const small = reachDemand(fakeRouted({ releasedAcFt: 40, travellingM3: 3 * ACFT }));
    assert.equal(small.floorAcFt, 2);
    assert.ok(small.extend);
    assert.equal(reachDemand(fakeRouted({ releasedAcFt: 40, travellingM3: 1.5 * ACFT })).stop, "volume-floor");
  });
  it("the stage fill and a failed build carry no signal", () => {
    assert.equal(reachDemand({ ok: false, reason: "x" }).stop, "none");
    const r = fakeRouted({});
    delete (r as { demand?: unknown }).demand;
    assert.equal(reachDemand(r).stop, "none");
  });
});

describe("channelEnd and reachStepM", () => {
  it("takes the direction over the channel's last kilometre, not the bearing", () => {
    // 5 km along u, then 2 km due east.
    const pts = [];
    for (let x = 0; x <= 5000; x += 10) pts.push({ e: U[0] * x, n: U[1] * x, x });
    const last = pts[pts.length - 1];
    for (let s = 10; s <= 2000; s += 10) pts.push({ e: last.e + s, n: last.n, x: last.x + s * U[0] });
    const end = channelEnd({ loc: LOC, pts }, U);
    assert.ok(Math.abs(end.dir[0] - 1) < 1e-6 && Math.abs(end.dir[1]) < 1e-6, `dir ${end.dir}`);
    assert.equal(end.e, last.e + 2000);
  });
  it("falls back to the bearing for a trace shorter than a metre", () => {
    const end = channelEnd({ loc: LOC, pts: [{ e: 0, n: 0, x: 0 }] }, U);
    assert.deepEqual(end.dir, U);
  });
  it("steps a quarter of the channel, within 1–4 km", () => {
    assert.equal(reachStepM(8700), 2175);
    assert.equal(reachStepM(1100), REACH_STEP_MIN_M);
    assert.equal(reachStepM(40_000), REACH_STEP_MAX_M);
  });
});

describe("extendRequest: the terrain grows on the same lattice", () => {
  const prev = demRequest(INP);
  const kx = kxOf(LOC.lat);
  const cellLon = (prev.bbox[2] - prev.bbox[0]) / prev.width;
  const cellLat = (prev.bbox[3] - prev.bbox[1]) / prev.height;
  // The request's box is rounded to 1e-8° (about a millimetre), so "whole" is to a thousandth of a cell.
  const whole = (v: number) => Math.abs(v - Math.round(v)) < 1e-3;

  it("growing east/south keeps the west and north edges and the cell; the box grows by whole cells", () => {
    // A strip 3 km long due east from the box's east edge, mid-height.
    const e0 = (prev.bbox[2] - LOC.lon) * kx;
    const n0 = ((prev.bbox[1] + prev.bbox[3]) / 2 - LOC.lat) * M_PER_DEG_LAT;
    const { req, grewCells, coarsened } = extendRequest(prev, LOC, { e: e0, n: n0, dir: [1, 0], lengthM: 3000, halfM: 1000 }, null);
    assert.ok(!coarsened);
    assert.equal(req.cellM, prev.cellM);
    assert.equal(req.bbox[0], prev.bbox[0]);
    assert.equal(req.bbox[3], prev.bbox[3]);
    assert.equal(req.height, prev.height, "a strip inside the box's latitude range adds no rows");
    assert.ok(req.width > prev.width + 300, `${req.width} vs ${prev.width}: 3 km + the pad at 10 m`);
    assert.ok(grewCells > 0);
    assert.ok(whole((req.bbox[2] - prev.bbox[2]) / cellLon), "the east edge moved by whole cells");
    assert.ok(Math.abs((req.bbox[2] - req.bbox[0]) / req.width - cellLon) < 1e-10, "same cell in degrees");
  });
  it("growing west/north shifts the origin by whole cells so every old cell centre is still a cell centre", () => {
    const e0 = (prev.bbox[0] - LOC.lon) * kx;
    const n0 = (prev.bbox[3] - LOC.lat) * M_PER_DEG_LAT;
    const { req, coarsened } = extendRequest(prev, LOC, { e: e0, n: n0, dir: [-Math.SQRT1_2, Math.SQRT1_2], lengthM: 2000, halfM: 1000 }, null);
    assert.ok(!coarsened);
    assert.ok(req.bbox[0] < prev.bbox[0] && req.bbox[3] > prev.bbox[3]);
    assert.ok(whole((prev.bbox[0] - req.bbox[0]) / cellLon), "west by whole cells");
    assert.ok(whole((req.bbox[3] - prev.bbox[3]) / cellLat), "north by whole cells");
    assert.ok(Math.abs((req.bbox[3] - req.bbox[1]) / req.height - cellLat) < 1e-10);
  });
  it("a strip the drawn area does not allow leaves the box as it was (no growth)", () => {
    const e0 = (prev.bbox[2] - LOC.lon) * kx;
    const n0 = ((prev.bbox[1] + prev.bbox[3]) / 2 - LOC.lat) * M_PER_DEG_LAT;
    // A drawn area no bigger than the box already fetched.
    const parts = [
      {
        outer: [
          [(prev.bbox[0] - LOC.lon) * kx, (prev.bbox[1] - LOC.lat) * M_PER_DEG_LAT],
          [(prev.bbox[2] - LOC.lon) * kx, (prev.bbox[1] - LOC.lat) * M_PER_DEG_LAT],
          [(prev.bbox[2] - LOC.lon) * kx, (prev.bbox[3] - LOC.lat) * M_PER_DEG_LAT],
          [(prev.bbox[0] - LOC.lon) * kx, (prev.bbox[3] - LOC.lat) * M_PER_DEG_LAT],
        ] as [number, number][],
        holes: [],
      },
    ];
    const { req, grew, grewCells } = extendRequest(prev, LOC, { e: e0, n: n0, dir: [1, 0], lengthM: 3000, halfM: 1000 }, parts);
    assert.ok(!grew);
    assert.equal(grewCells, 0);
    assert.deepEqual(req, prev);
  });
  it("a box that would need more cells on a side than allowed is laid out afresh, coarser", () => {
    const e0 = (prev.bbox[2] - LOC.lon) * kx;
    const n0 = ((prev.bbox[1] + prev.bbox[3]) / 2 - LOC.lat) * M_PER_DEG_LAT;
    const { req, grew, coarsened } = extendRequest(prev, LOC, { e: e0, n: n0, dir: [1, 0], lengthM: 4000, halfM: 1000 }, null, prev.width + 100);
    assert.ok(coarsened);
    assert.ok(grew, "the box grew even though the coarser cells may be fewer");
    assert.ok(req.cellM > prev.cellM);
    assert.ok(Math.max(req.width, req.height) <= prev.width + 100);
  });
});

describe("extendInputs", () => {
  it("pushes lastX and repeats the last station there; the fingerprint and the rest stay", () => {
    const out = extendInputs(INP, 2175);
    assert.equal(out.lastX, INP.lastX + 2175);
    assert.equal(out.st.length, INP.st.length + 1);
    assert.deepEqual(out.st[out.st.length - 1], { ...INP.st[INP.st.length - 1], x: INP.lastX + 2175 });
    assert.equal(out.fingerprint, INP.fingerprint);
    assert.equal(out.u, INP.u);
  });
});

describe("buildRoutedFloodReach: the loop and its guards (stand-in fetch and build)", () => {
  const grid = (req: DemRequest): DemGrid => ({ ...req, z: new Float32Array(req.width * req.height).fill(300) });
  type Round = Partial<NonNullable<RoutedFloodResult["demand"]>> & { channelEndM?: number; fail?: boolean };
  /** A builder that answers each round from a script and records the inputs it was given. */
  const scripted = (rounds: Round[]) => {
    const calls: { lastX: number; width: number }[] = [];
    const fetched: DemRequest[] = [];
    const fetch = async (req: DemRequest) => {
      fetched.push(req);
      return grid(req);
    };
    const build: typeof buildRoutedFlood = async (_dam, _result, g, opts = {}) => {
      const r = rounds[Math.min(calls.length, rounds.length - 1)];
      const lastX = opts.inputs?.lastX ?? INP.lastX;
      calls.push({ lastX, width: g.width });
      if (r.fail) return { ok: false, reason: "No routed flood: scripted failure." };
      return fakeRouted({ ...r, channelEndM: r.channelEndM ?? lastX });
    };
    return { calls, fetched, fetch, build };
  };

  it("a contained flood gets the single build every layer always got: one fetch, one build, no growth", async () => {
    const s = scripted([{ reachedEdge: false }]);
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, fetch: s.fetch, build: s.build });
    assert.equal(rb.reach.rounds, 0);
    assert.equal(rb.reach.stoppedBecause, "contained");
    assert.ok(!rb.reach.incomplete);
    assert.equal(rb.reach.strandedAcFt, 0);
    assert.equal(s.fetched.length, 1);
    assert.equal(s.calls.length, 1);
    assert.deepEqual(rb.request, demRequest(INP));
    assert.equal(rb.inputs, INP, "the inputs are the schematic's own");
    assert.ok(rb.routed.ok && rb.routed.notes.some((n) => n.startsWith("Downstream reach: the flood stays inside")));
  });

  it("grows while the wave asks for it, then stops when it is contained; each box takes in the last", async () => {
    const s = scripted([{}, {}, { reachedEdge: false }]);
    const seen: number[] = [];
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, fetch: s.fetch, build: s.build, onRound: (r) => seen.push(r.round) });
    assert.equal(rb.reach.rounds, 2);
    assert.equal(rb.reach.stoppedBecause, "contained");
    assert.ok(!rb.reach.incomplete);
    assert.equal(s.calls.length, 3);
    assert.deepEqual(seen, [0, 0, 1, 1, 2, 2]);
    // Each round's trace cap moved on by that round's step (a quarter of the channel, within 1–4 km).
    const step1 = reachStepM(INP.lastX);
    assert.ok(Math.abs(s.calls[1].lastX - (INP.lastX + step1)) < 1e-6);
    assert.ok(Math.abs(rb.reach.extendedKm - (step1 + reachStepM(INP.lastX + step1)) / 1000) < 0.06);
    for (let i = 1; i < s.fetched.length; i++) {
      const a = s.fetched[i - 1].bbox;
      const b = s.fetched[i].bbox;
      assert.ok(b[0] <= a[0] + 1e-9 && b[1] <= a[1] + 1e-9 && b[2] >= a[2] - 1e-9 && b[3] >= a[3] - 1e-9, "the grown box takes in the previous");
      assert.equal(s.fetched[i].cellM, s.fetched[0].cellM);
    }
    assert.ok(rb.routed.ok && rb.routed.notes.some((n) => /grown 2 times downstream/.test(n)));
  });

  it("the round cap flags the layer incomplete with the water still at the edge", async () => {
    const s = scripted([{ travellingM3: 1468 * ACFT }]);
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, fetch: s.fetch, build: s.build });
    assert.equal(rb.reach.rounds, REACH_MAX_ROUNDS);
    assert.equal(rb.reach.stoppedBecause, "round-cap");
    assert.ok(rb.reach.incomplete);
    assert.equal(rb.reach.strandedAcFt, 1468);
    assert.ok(rb.routed.ok && rb.routed.notes.some((n) => /1468 ac-ft still reaches the edge .*round cap/.test(n)));
    assert.ok(rb.routed.ok && /incomplete/.test(reachNote(rb.reach)));
  });

  it("maxRounds 0 is the old single build, with the reason recorded", async () => {
    const s = scripted([{}]);
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, fetch: s.fetch, build: s.build, maxRounds: 0 });
    assert.equal(rb.reach.rounds, 0);
    assert.equal(rb.reach.stoppedBecause, "round-cap");
    assert.ok(rb.reach.incomplete);
    assert.equal(s.calls.length, 1);
  });

  it("a build that fails on the grown terrain keeps the previous round's layer and says so", async () => {
    const s = scripted([{}, { fail: true }]);
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, fetch: s.fetch, build: s.build });
    assert.ok(rb.routed.ok);
    assert.equal(rb.reach.rounds, 0);
    assert.equal(rb.reach.stoppedBecause, "trace-failed");
    assert.ok(rb.reach.incomplete);
    assert.deepEqual(rb.request, demRequest(INP));
  });

  it("a flood that would need a coarser terrain than allowed stops at the limit", async () => {
    const s = scripted([{}]);
    // With at most as many cells as the first box has on its long side, the first growth must coarsen the terrain,
    // and growing by 2 km in 4 rounds would not coarsen it past 1.5×, so shrink the allowance until it does.
    const base = demRequest(INP);
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, fetch: s.fetch, build: s.build, maxPx: Math.max(base.width, base.height) });
    assert.ok(rb.reach.rounds <= REACH_MAX_ROUNDS);
    assert.ok(["dem-limit", "round-cap"].includes(rb.reach.stoppedBecause));
    assert.ok(rb.request.cellM <= 1.5 * base.cellM + 1e-9, "never built on a terrain coarser than 1.5× the first");
  });

  it("asks for the terrain cell it is given (the scorer's --cell), and for the 10 m default otherwise", async () => {
    const s = scripted([{}, { fail: true }]);
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, fetch: s.fetch, build: s.build, cellM: 5 });
    assert.deepEqual(rb.request, demRequest(INP, 5));
    assert.equal(rb.request.cellM, 5);
    assert.equal(demRequest(INP).cellM, 10);
    assert.ok(rb.request.width > 1.9 * demRequest(INP).width, "the same box at half the cell");
  });

  it("a terrain service failure on a grown box keeps the previous round's layer", async () => {
    const s = scripted([{}, {}]);
    let n = 0;
    const fetch = async (req: DemRequest) => {
      if (++n === 2) throw new Error("The 3DEP terrain service answered 504.");
      return s.fetch(req);
    };
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, fetch, build: s.build });
    assert.ok(rb.routed.ok);
    assert.equal(rb.reach.rounds, 0);
    assert.equal(rb.reach.stoppedBecause, "fetch-failed");
    assert.ok(rb.reach.incomplete);
    assert.ok(rb.routed.ok && rb.routed.notes.some((n) => /could not be fetched/.test(n)));
  });

  it("a flood stopped in a closed basin is never grown", async () => {
    const s = scripted([{ stoppedInBasin: true }]);
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, fetch: s.fetch, build: s.build });
    assert.equal(rb.reach.rounds, 0);
    assert.equal(rb.reach.stoppedBecause, "basin");
  });
});

describe("buildRoutedFloodReach end to end on a synthetic valley (ND00199's breach)", () => {
  // A valley 25° off the bearing (so the drainage trace leaves the schematic's box through its side, as DWR-2801's
  // creek does): 0.08 % fall, 1 % side slopes, bed at the toe of the 27.5 ft dam. The release (1,600 ac-ft) is still
  // all travelling where the trace meets the edge, so the reach grows the terrain along the valley. With a 3.8 m
  // road fill across the valley 6 km down, closing a basin that holds the whole release, the flood ends there and the
  // terrain is never grown.
  const kx = kxOf(LOC.lat);
  const toeM = RESULT.datum.crestNavdFt * 0.3048 - ND.heightFt * 0.3048;
  const TURN = (25 * Math.PI) / 180;
  const V: [number, number] = [U[0] * Math.cos(TURN) - U[1] * Math.sin(TURN), U[0] * Math.sin(TURN) + U[1] * Math.cos(TURN)];
  const ground = (fill: boolean) => (e: number, n: number) => {
    const along = e * V[0] + n * V[1];
    const cross = -e * V[1] + n * V[0];
    let z = toeM - 0.0008 * along + 0.01 * Math.abs(cross);
    if (fill && along > 6000 && along < 6040) z += 3.8; // the road fill: its crest is still below the dam's toe
    return z;
  };
  const fetchOn = (bowl: boolean) => async (req: DemRequest): Promise<DemGrid> => {
    const g = ground(bowl);
    const dlon = (req.bbox[2] - req.bbox[0]) / req.width;
    const dlat = (req.bbox[3] - req.bbox[1]) / req.height;
    const z = new Float32Array(req.width * req.height);
    for (let j = 0; j < req.height; j++)
      for (let i = 0; i < req.width; i++) {
        const e = (req.bbox[0] + (i + 0.5) * dlon - LOC.lon) * kx;
        const n = (req.bbox[3] - (j + 0.5) * dlat - LOC.lat) * M_PER_DEG_LAT;
        z[j * req.width + i] = g(e, n);
      }
    return { ...req, z };
  };

  it("grows the terrain downstream and floods further than the single build", { timeout: 600_000 }, async () => {
    const fetch = fetchOn(false);
    const fetched: DemRequest[] = [];
    const rb = await buildRoutedFloodReach(ND, RESULT, {
      inputs: INP,
      method: "spread",
      maxRounds: 1,
      fetch: async (req) => {
        fetched.push(req);
        return fetch(req);
      },
    });
    assert.ok(rb.routed.ok, rb.routed.ok ? "" : rb.routed.reason);
    if (!rb.routed.ok) return;
    assert.equal(rb.reach.rounds, 1, `stopped: ${rb.reach.stoppedBecause}`);
    assert.equal(fetched.length, 2);
    const single = await buildRoutedFlood(ND, RESULT, await fetch(demRequest(INP)), { method: "spread" });
    assert.ok(single.ok);
    if (!single.ok) return;
    assert.ok(rb.routed.stats.channelKm > single.stats.channelKm, `${rb.routed.stats.channelKm} vs ${single.stats.channelKm} km`);
    assert.ok(rb.routed.stats.floodedAc > single.stats.floodedAc, `${rb.routed.stats.floodedAc} vs ${single.stats.floodedAc} ac`);
    assert.ok(rb.routed.width * rb.routed.height > single.width * single.height);
    // The grown box is on the first box's lattice: the first box's cells are the same ground, cell for cell.
    const a = fetched[0];
    const b = fetched[1];
    const di = Math.round((a.bbox[0] - b.bbox[0]) / ((b.bbox[2] - b.bbox[0]) / b.width));
    const dj = Math.round((b.bbox[3] - a.bbox[3]) / ((b.bbox[3] - b.bbox[1]) / b.height));
    const ga = await fetch(a);
    const gb = rb.grid;
    for (const [i, j] of [
      [0, 0],
      [a.width - 1, a.height - 1],
      [Math.floor(a.width / 2), Math.floor(a.height / 3)],
    ])
      assert.ok(Math.abs(ga.z[j * a.width + i] - gb.z[(j + dj) * b.width + (i + di)]) < 1e-3, `cell (${i},${j})`);
  });

  it("a flood held behind a road fill is never grown and is the single build, cell for cell", { timeout: 600_000 }, async () => {
    const fetch = fetchOn(true);
    const rb = await buildRoutedFloodReach(ND, RESULT, { inputs: INP, method: "spread", fetch });
    assert.ok(rb.routed.ok, rb.routed.ok ? "" : rb.routed.reason);
    if (!rb.routed.ok) return;
    assert.equal(rb.reach.rounds, 0, `stopped: ${rb.reach.stoppedBecause}`);
    assert.equal(rb.reach.stoppedBecause, "basin");
    assert.ok(!rb.reach.incomplete);
    const single = await buildRoutedFlood(ND, RESULT, await fetch(demRequest(INP)), { method: "spread" });
    assert.ok(single.ok);
    if (!single.ok) return;
    assert.deepEqual(rb.routed.depthFt, single.depthFt);
  });
});

describe("OUTLINE_MAX_PX", () => {
  it("is the reach's default ceiling on a side", () => assert.ok(OUTLINE_MAX_PX >= 1000));
});
