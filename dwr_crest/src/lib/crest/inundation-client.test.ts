// The inundation worker client (inundation-client.ts) against a stand-in Worker that runs the worker's own handler
// in-process: the message protocol, the main-thread terrain hand-over (transferred), cancel, and the pool.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { runBatch, type Structure } from "./batch.ts";
import { analyze } from "./engine.ts";
import type { FloodStrip } from "./floodstrip.ts";
import { CANCELLED, InundationPool, InundationWorker, poolSize } from "./inundation-client.ts";
import { nd911Bbox } from "./nd911.ts";
import { lightResult, type InundationIn, type InundationOut } from "./inundation-protocol.ts";
import { buildInundationReach } from "./inundation.ts";
import type { DemGrid, DemRequest } from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";

const M_PER_DEG_LAT = 110574;
const kx = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);

/** A synthetic valley (batch.test.ts's): south-running, 1 % slope, 3 % banks. */
const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const RESULT = analyze(ND);
const toeM = RESULT.datum.crestNavdFt * 0.3048 - ND.heightFt * 0.3048;
const LOC = { lon: ND.lon!, lat: ND.lat! };
const valley = async (req: DemRequest): Promise<DemGrid> => {
  const z = new Float32Array(req.width * req.height);
  for (let j = 0; j < req.height; j++)
    for (let i = 0; i < req.width; i++) {
      const lon = req.bbox[0] + ((i + 0.5) / req.width) * (req.bbox[2] - req.bbox[0]);
      const lat = req.bbox[3] - ((j + 0.5) / req.height) * (req.bbox[3] - req.bbox[1]);
      const e = (lon - LOC.lon) * kx(LOC.lat);
      const n = (lat - LOC.lat) * M_PER_DEG_LAT;
      z[j * req.width + i] = toeM - 0.001 * -n + 0.03 * Math.abs(e);
    }
  return { ...req, z };
};

/**
 * The worker's handler (inundation.worker.ts scope.onmessage) run in-process behind the Worker interface the client
 * expects. Messages are delivered on a microtask, as a real worker's would arrive asynchronously. `transfers` records
 * what the page offered to transfer.
 */
class FakeWorker {
  onmessage: ((e: { data: InundationOut }) => void) | null = null;
  onerror: ((e: { message: string }) => void) | null = null;
  terminated = false;
  transfers: ArrayBuffer[][] = [];
  private waiting = new Map<number, { resolve: (g: DemGrid) => void; reject: (e: Error) => void }>();
  private seq = 0;
  static made = 0;
  constructor() {
    FakeWorker.made++;
  }
  private post(m: InundationOut) {
    queueMicrotask(() => {
      if (!this.terminated) this.onmessage?.({ data: m });
    });
  }
  postMessage(msg: InundationIn, transfer: ArrayBuffer[] = []) {
    if (transfer.length) this.transfers.push(transfer);
    if ("dem" in msg) {
      const w = this.waiting.get(msg.dem.seq);
      if (!w) return;
      this.waiting.delete(msg.dem.seq);
      if (msg.dem.grid) w.resolve(msg.dem.grid);
      else w.reject(new Error(msg.dem.error ?? "x"));
      return;
    }
    const { id, req } = msg;
    void (async () => {
      try {
        const fetchOnMain = (r: DemRequest) =>
          new Promise<DemGrid>((resolve, reject) => {
            const s = ++this.seq;
            this.waiting.set(s, { resolve, reject });
            this.post({ id, fetch: r, seq: s });
          });
        const out = await buildInundationReach(req.dam, req.result, {
          maxRounds: req.maxRounds,
          onStatus: (text) => this.post({ id, progress: text }),
          ...(req.fetchOnMain ? { fetch: fetchOnMain } : {}),
        });
        this.post({ id, ok: true, out: lightResult(out) });
      } catch (err) {
        this.post({ id, ok: false, error: (err as Error).message });
      }
    })();
  }
  terminate() {
    this.terminated = true;
  }
}
const Ctor = FakeWorker as unknown as new () => Worker;

describe("InundationWorker", () => {
  it("builds through the worker protocol: progress lines arrive, the terrain is handed over transferred, the cache comes back light", { timeout: 300_000 }, async () => {
    const w = new InundationWorker(Ctor, valley);
    const status: string[] = [];
    const r = await w.build(ND, RESULT, { onStatus: (t) => status.push(t), fetch: valley, maxRounds: 0 });
    assert.ok(r.ok, r.ok ? "" : r.reason);
    if (!r.ok) return;
    assert.equal(r.cache.layer, "routed");
    assert.equal(r.routed, undefined, "the per-cell grids stay in the worker");
    assert.ok(r.cache.outline.features.length > 0);
    assert.ok(status.some((t) => /Fetching the 3DEP terrain/.test(t)));
    assert.ok(status.some((t) => /Building the inundation layer/.test(t)));
    // Against the in-thread build: the same cache (bar the build time).
    const direct = await buildInundationReach(ND, RESULT, { fetch: valley, maxRounds: 0 });
    assert.ok(direct.ok);
    if (!direct.ok) return;
    assert.deepEqual({ ...r.cache, builtAt: "" }, { ...direct.cache, builtAt: "" });
    assert.ok(!w.busy);
  });

  it("transfers the DEM buffer to the worker rather than copying it", { timeout: 300_000 }, async () => {
    FakeWorker.made = 0;
    let made: FakeWorker | null = null;
    const Spy = class extends FakeWorker {
      constructor() {
        super();
        // eslint-disable-next-line @typescript-eslint/no-this-alias -- the spy records the instance it made
        made = this;
      }
    } as unknown as new () => Worker;
    const w = new InundationWorker(Spy, valley);
    await w.build(ND, RESULT, { fetch: valley, maxRounds: 0 });
    assert.ok(made);
    const fw = made as unknown as FakeWorker;
    assert.ok(fw.transfers.length >= 1, "at least one grid handed over");
    for (const t of fw.transfers) assert.ok(t[0] instanceof ArrayBuffer);
  });

  it("cancel ends the build with CANCELLED and the next build starts a fresh worker", { timeout: 300_000 }, async () => {
    FakeWorker.made = 0;
    const w = new InundationWorker(Ctor, valley);
    const ctl = new AbortController();
    const p = w.build(ND, RESULT, { fetch: valley, maxRounds: 0, signal: ctl.signal, onStatus: () => ctl.abort() });
    await assert.rejects(p, (e: Error) => e.message === CANCELLED);
    assert.ok(!w.busy);
    assert.equal(FakeWorker.made, 1);
    const r = await w.build(ND, RESULT, { fetch: valley, maxRounds: 0 });
    assert.ok(r.ok);
    assert.equal(FakeWorker.made, 2, "a fresh worker after a cancel");
    await assert.rejects(w.build(ND, RESULT, { fetch: valley, signal: AbortSignal.abort() }), /cancelled/);
  });

  it("a build that fails for its own reason is reported, not swallowed", { timeout: 300_000 }, async () => {
    const w = new InundationWorker(Ctor);
    const down = async () => {
      throw new Error("The 3DEP terrain service answered 503.");
    };
    await assert.rejects(w.build(ND, RESULT, { fetch: down, maxRounds: 0 }), /answered 503/);
    assert.ok(!w.busy);
    assert.ok(!InundationWorker.broke(new Error("x")));
  });
});

describe("InundationPool and poolSize", () => {
  it("sizes to the spare cores, 1..4", () => {
    // An explicit undefined takes the default, the runtime's navigator.hardwareConcurrency (Node 21+ has one too).
    const auto = poolSize(undefined);
    assert.ok(auto >= 1 && auto <= 4, `${auto}`);
    assert.equal(poolSize(2), 1);
    assert.equal(poolSize(4), 3);
    assert.equal(poolSize(16), 4);
    assert.equal(poolSize(1), 1);
  });

  it("hands out at most `size` workers and queues the rest until one is released", async () => {
    const pool = new InundationPool(Ctor, 2);
    const a = await pool.acquire();
    const b = await pool.acquire();
    assert.notEqual(a, b);
    let got: InundationWorker | null = null;
    const third = pool.acquire().then((w) => (got = w));
    await Promise.resolve();
    assert.equal(got, null, "waits while both are out");
    pool.release(a);
    await third;
    assert.equal(got, a, "the released worker goes to the waiter");
    pool.release(b);
    assert.equal(await pool.acquire(), b);
    pool.terminateAll();
  });
});

describe("runBatch on a worker pool", () => {
  const S = (id: string): Structure => ({
    id,
    kind: "dam",
    label: id,
    nid: null,
    status: "Intact",
    county: "Cass",
    lat: LOC.lat,
    lon: LOC.lon,
    heightFt: 20,
    storageAcFt: 120,
    areaAc: 15,
    lengthFt: 400,
    year: 1980,
    hazard: "Low",
    purpose: "Livestock",
    huc8: "Maple (09020205)",
  });
  const noNd911 = (async (area: FloodStrip) => ({ bbox: nd911Bbox(area), fetchedAt: "2026-11-05T00:00:00Z", source: "test", attribution: "test", fields: [], points: [] })) as never;

  it("gives the same rows as the in-thread batch, builds through the pool, and stops on abort", { timeout: 600_000 }, async () => {
    FakeWorker.made = 0;
    const list = [S("A"), S("B"), S("C")];
    const pool = new InundationPool(Ctor, 2, valley);
    const pooled = await runBatch(list, { fetchDem: valley, fetchNd911: noNd911, pool });
    pool.terminateAll();
    assert.ok(FakeWorker.made >= 1 && FakeWorker.made <= 2, `${FakeWorker.made} workers made`);
    const direct = await runBatch(list, { fetchDem: valley, fetchNd911: noNd911 });
    const strip = (rows: typeof pooled) => rows.map((r) => ({ ...r, seconds: 0 })).sort((a, b) => a.id.localeCompare(b.id));
    assert.deepEqual(strip(pooled), strip(direct));
    assert.ok(pooled.every((r) => r.status === "done"), pooled.map((r) => r.reason).join("; "));
    const ctl = new AbortController();
    ctl.abort();
    const pool2 = new InundationPool(Ctor, 2, valley);
    assert.equal((await runBatch(list, { fetchDem: valley, pool: pool2, signal: ctl.signal })).length, 0);
    pool2.terminateAll();
  });
});
