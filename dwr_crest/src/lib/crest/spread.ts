/**
 * Quasi-dynamic 2-D volume spread (the successor to the routed stage fill, Nick, 2026-11-03).
 *
 * The routed stage fill (fill.ts) seeds the channel with a 1-D Manning stage and carries that surface across the
 * terrain. The 2026-11-03 fill review showed why that can't work: the carried surface runs down the valley with no
 * brake (neutral stability), so it lays down 40–5,000× the released water, and with that leak removed a 1-D stage
 * spread sideways under-covers a 2-D sheet. This module drops the stage altogether and pours VOLUME.
 *
 * The volume comes from the Muskingum-Cunge routing itself (routing.ts). For every stretch between two cut sections the
 * router integrates continuity, S(t) = ∫ (Q_in − Q_out) dt, and keeps the largest water volume the stretch ever holds
 * (storeMaxM3) and when. That is a distance-time slice of the flood: the water in that stretch at its own peak. Each
 * slice is poured as a level pool onto the terrain of its own stretch, so:
 *
 *   - it is volume-conserving slice by slice (the pool holds exactly the slice's volume),
 *   - the slices are wet at different times, so their sum is the flood's max-depth envelope volume, not one
 *     simultaneous pond of the release: Σ max_t S_j(t) → ∫ max_t A(x,t) dx as the stretches shrink. The envelope-to-
 *     release ratio (the LISFLOOD envelope holds ~3–5× the release on the baseline dams) comes out of the routing; it
 *     is not a tuned multiplier,
 *   - a slice can only occupy its own stretch's ground, so no water seeded near the dam can run down the valley over
 *     ground far below it (the leak that sank the stage fill).
 *
 * The stretch's ground is every cell whose nearest channel cell (4-neighbour distance from the traced centreline) lies
 * in that stretch, measured as height above that channel cell (HAND-like). The pool fills from the channel outward in
 * spill order (a priority flood on that height), so it backs into side lows only once it tops the saddle into them.
 *
 * Known under-prediction, stated rather than tuned away: a level pool reaches only ground below its level. A real
 * breach front carries momentum and surges over low divides it would not statically top, so the spread can miss
 * ground the front overruns (the opposite of the stage fill's leak; compare the Red River bathtub, which over-predicts).
 * It also depends on the traced channel being in the flood's valley; where the trace misses the flood (DWR-4031) the
 * pour lands in the wrong place.
 *
 * Velocity is Manning's on each wet cell, scaled per stretch so the stretch passes its routed peak discharge
 * (continuity) and capped at Froude 1; arrival is the routing's arrival at the stretch.
 *
 * Pure module (no DOM, no fetch). Metres, seconds, m³ inside.
 */

import { MaxHeap } from "./fill.ts";
import type { DemGrid } from "./outline.ts";
import { DEFAULT_ROUGHNESS, type RoughnessOptions, type RoutedSection } from "./routing.ts";
import type { Centerline } from "./sections.ts";

const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;
const kxOf = (lat: number) => M_PER_DEG_LON_EQ * Math.cos((lat * Math.PI) / 180);
const NODATA = -9999;
const ACRE_M2 = 4046.8564224;
/** Tie-break slope (m/m) for the breach path on flat ground: far below any real bed slope (routing floor 1e-4). */
const FLAT_TIE = 1e-6;

export type SpreadOptions = {
  /** A cell counts as wet at this depth (m). Default 0.03 (~0.1 ft). */
  minDepthM?: number;
  /** How far from the channel (m) a cell can belong to a stretch. Default 3000. */
  maxLateralM?: number;
  /** Manning's n split for the velocity (the routing's default). */
  rough?: RoughnessOptions;
  /** The breach's released volume (m³), for the envelope-to-release ratio. */
  releasedM3?: number;
  /**
   * Treat each closed basin the traced channel enters as storage the wave must fill before it can pass (default true;
   * needs releasedM3). A basin that holds all the water still travelling ends the flood there as one level pool; a
   * smaller one fills to its spill level and the rest carries on. See spreadVolume.
   */
  basins?: boolean;
  /** A basin counts only when it is at least this deep below its spill level (m). Default BASIN_MIN_DEPTH_M. */
  basinMinDepthM?: number;
};

/** Default for SpreadOptions.basinMinDepthM. */
export const BASIN_MIN_DEPTH_M = 1.0;

export type SpreadResult = {
  width: number;
  height: number;
  /** Depth (m) per cell, 0 where dry. */
  depthM: Float32Array;
  /** Water surface (m) per cell, NaN where dry. */
  wseM: Float32Array;
  /** Velocity (m/s) per cell, NaN where dry. */
  velocityMps: Float32Array;
  /** Arrival (s from the breach start) per cell, NaN where dry. */
  arrivalS: Float32Array;
  wetCells: number;
  floodedAc: number;
  /** Water laid down (m³), counting only cells at or above minDepthM. */
  volumeM3: number;
  /** Σ of the slices poured (m³): the routing's envelope volume over the traced reach. */
  slicesM3: number;
  /** volumeM3 / releasedM3, when the release was given. */
  volumeRatio: number | null;
  /** Water left behind in depressions along the way (m³), taken out of the volume still travelling. */
  retainedM3: number;
  /** The volume still travelling past the last stretch poured (m³); 0 when the flood was all left behind. */
  travellingM3: number;
  /** Stretches whose ground ran out before the slice was poured (the pool stands deeper than the terrain shows). */
  exhaustedStretches: number;
  /** Closed basins on the channel the wave filled to their spill level and passed. */
  basinsFilled: number;
  /**
   * Where the flood ended in a closed basin that holds all the water still travelling (the DEM has no way through it:
   * a road or rail fill with no culvert in the DEM, or a real basin); null when it never did.
   */
  stoppedIn: { atM: number; spillM: number; capacityM3: number; heldM3: number; areaAc: number } | null;
  stretches: number;
  reachedEdge: boolean;
};

/**
 * Pour each stretch's peak storage as a level pool on its own ground. `sections` must come from routeMuskingumCunge
 * (they carry storeMaxM3, peakCms, tArrivalS, slopeStage).
 */
export function spreadVolume(g: DemGrid, line: Centerline, sections: RoutedSection[], opts: SpreadOptions = {}): SpreadResult {
  const W = g.width;
  const H = g.height;
  const N = W * H;
  const minDepth = opts.minDepthM ?? 0.03;
  const rough = opts.rough ?? DEFAULT_ROUGHNESS;
  const kx = kxOf(line.loc.lat);
  const dlon = (g.bbox[2] - g.bbox[0]) / W;
  const dlat = (g.bbox[3] - g.bbox[1]) / H;
  const cellMx = dlon * kx;
  const cellMy = dlat * M_PER_DEG_LAT;
  const cellM = Math.min(cellMx, cellMy);
  const cellM2 = cellMx * cellMy;
  const maxSteps = Math.ceil((opts.maxLateralM ?? 3000) / cellM);
  const valid = (k: number) => {
    const z = g.z[k];
    return Number.isFinite(z) && z > NODATA + 1 && z < 9000;
  };
  const cellAt = (e: number, n: number) => {
    const lon = line.loc.lon + e / kx;
    const lat = line.loc.lat + n / M_PER_DEG_LAT;
    const i = Math.floor((lon - g.bbox[0]) / dlon);
    const j = Math.floor((g.bbox[3] - lat) / dlat);
    return i < 0 || j < 0 || i >= W || j >= H ? -1 : j * W + i;
  };
  const nS = sections.length;
  const stretchOf = (x: number) => {
    if (nS < 2 || x < sections[0].x || x >= sections[nS - 1].x) return -1;
    let lo = 0;
    let hi = nS - 1;
    while (hi - lo > 1) {
      const m = (lo + hi) >> 1;
      if (sections[m].x <= x) lo = m;
      else hi = m;
    }
    return lo;
  };

  // Downstream direction at the breach, so the reservoir side (behind the dam) is not handed to the first stretch.
  const p0 = line.pts[0];
  let pa = line.pts[line.pts.length - 1];
  for (const p of line.pts)
    if (p.x - p0.x >= 100) {
      pa = p;
      break;
    }
  const ul = Math.hypot(pa.e - p0.e, pa.n - p0.n) || 1;
  const u0: [number, number] = [(pa.e - p0.e) / ul, (pa.n - p0.n) / ul];
  const behindDam = (k: number) => {
    const i = k % W;
    const j = (k - i) / W;
    const e = (g.bbox[0] + (i + 0.5) * dlon - line.loc.lon) * kx - p0.e;
    const n = (g.bbox[3] - (j + 0.5) * dlat - line.loc.lat) * M_PER_DEG_LAT - p0.n;
    return e * u0[0] + n * u0[1] < 0;
  };

  // 1. Channel cells, each with its stretch.
  const label = new Int32Array(N).fill(-1);
  const bed = new Float64Array(N).fill(NaN);
  const dist = new Int32Array(N).fill(-1);
  const queue = new Int32Array(N);
  let qh = 0;
  let qt = 0;
  const channel: number[] = [];
  for (let p = 0; p + 1 < line.pts.length; p++) {
    const a = line.pts[p];
    const b = line.pts[p + 1];
    const segM = Math.hypot(b.e - a.e, b.n - a.n);
    const steps = Math.max(1, Math.ceil(segM / (cellM * 0.5)));
    for (let s = 0; s <= steps; s++) {
      const f = s / steps;
      const k = cellAt(a.e + f * (b.e - a.e), a.n + f * (b.n - a.n));
      if (k < 0 || !valid(k) || dist[k] >= 0) continue;
      const j = stretchOf(a.x + f * (b.x - a.x));
      dist[k] = 0;
      label[k] = j;
      bed[k] = g.z[k];
      queue[qt++] = k;
      channel.push(k);
    }
  }

  // 2. Nearest channel cell (4-neighbour BFS): each cell joins the stretch of the channel cell nearest it and is
  // measured against that cell's bed. Cells nearest the part of the trace past the last section stay unlabelled.
  const D4I = [1, -1, 0, 0];
  const D4J = [0, 0, 1, -1];
  while (qh < qt) {
    const c = queue[qh++];
    if (dist[c] >= maxSteps) continue;
    const ci = c % W;
    const cj = (c - ci) / W;
    for (let t = 0; t < 4; t++) {
      const i = ci + D4I[t];
      const j = cj + D4J[t];
      if (i < 0 || j < 0 || i >= W || j >= H) continue;
      const m = j * W + i;
      if (dist[m] >= 0 || !valid(m)) continue;
      dist[m] = dist[c] + 1;
      label[m] = label[c];
      bed[m] = bed[c];
      queue[qt++] = m;
    }
  }
  for (let k = 0; k < N; k++) if (label[k] === 0 && dist[k] > 0 && behindDam(k)) label[k] = -1;

  // 3. Spill height: a priority flood from the channel on height above the reference bed, staying inside each
  // stretch, so a cell only becomes reachable once the pool tops every saddle between it and the channel.
  const spill = new Float64Array(N).fill(Infinity);
  const negKey = new Float64Array(N);
  const heap = new MaxHeap(negKey, Math.max(1024, channel.length * 4));
  for (const k of channel) {
    if (label[k] < 0) continue;
    spill[k] = 0;
    negKey[k] = 0;
    heap.push(k);
  }
  const done = new Uint8Array(N);
  const DI = [1, -1, 0, 0, 1, 1, -1, -1];
  const DJ = [0, 0, 1, -1, 1, -1, 1, -1];
  while (heap.size > 0) {
    const c = heap.pop();
    if (done[c]) continue;
    done[c] = 1;
    const ci = c % W;
    const cj = (c - ci) / W;
    for (let t = 0; t < 8; t++) {
      const i = ci + DI[t];
      const j = cj + DJ[t];
      if (i < 0 || j < 0 || i >= W || j >= H) continue;
      const m = j * W + i;
      if (done[m] || label[m] !== label[c]) continue;
      const s = Math.max(g.z[m] - bed[m], spill[c]);
      if (s < spill[m]) {
        spill[m] = s;
        negKey[m] = -s;
        heap.push(m);
      }
    }
  }

  // 3b. Depression storage: how deep each cell sits below the level it would have to rise to before draining off the
  // DEM (a priority flood from the DEM's edge). Water the pour leaves in a depression stays there after the wave has
  // passed, so it is taken out of the volume still travelling down the valley (below). The routing alone has no such
  // dead storage, which is why it carries a small flood the whole length of the reach.
  const pitDepth = new Float32Array(N);
  const lvl = new Float64Array(N).fill(NaN);
  if (opts.releasedM3 != null && opts.releasedM3 > 0) {
    const neg = new Float64Array(N);
    const h2 = new MaxHeap(neg, Math.max(1024, 2 * (W + H)));
    for (let k = 0; k < N; k++) {
      const i = k % W;
      const j = (k - i) / W;
      if (!valid(k)) continue;
      if (i === 0 || j === 0 || i === W - 1 || j === H - 1) {
        lvl[k] = g.z[k];
        neg[k] = -g.z[k];
        h2.push(k);
      }
    }
    while (h2.size > 0) {
      const c = h2.pop();
      const ci = c % W;
      const cj = (c - ci) / W;
      for (let t = 0; t < 8; t++) {
        const i = ci + DI[t];
        const j = cj + DJ[t];
        if (i < 0 || j < 0 || i >= W || j >= H) continue;
        const m = j * W + i;
        if (!Number.isNaN(lvl[m]) || !valid(m)) continue;
        lvl[m] = Math.max(g.z[m], lvl[c]);
        neg[m] = -lvl[m];
        h2.push(m);
      }
    }
    for (let k = 0; k < N; k++) if (Number.isFinite(lvl[k])) pitDepth[k] = Math.max(0, lvl[k] - g.z[k]);
  }

  // 3c. Closed basins on the channel. A basin is a connected set of cells below one spill level (they share the
  // priority flood's level exactly). The routing has no idea a basin is there: on DWR-3034 a 22 ft road fill with no
  // culvert in the DEM closes a 233 ac-ft basin 1.7 km down, the LISFLOOD-FP run ponds the whole 71 ac-ft release
  // behind it, and the Muskingum-Cunge wave (which only sees the cut sections' thalwegs) runs on through it at
  // 4,000 cfs. Before the wave can pass a basin it has to fill it, so (below) each basin the channel enters takes its
  // capacity out of the water still travelling, and a basin that can hold all of that water ends the flood.
  const basinOf = new Int32Array(N).fill(-1);
  const basinCap: number[] = [];
  const basinStart: number[] = [];
  const basinCells: number[] = [];
  const basinAtStretch = new Map<number, number[]>();
  if ((opts.basins ?? true) && opts.releasedM3 != null && opts.releasedM3 > 0) {
    const PIT_EPS = 1e-3;
    const minBasin = opts.basinMinDepthM ?? BASIN_MIN_DEPTH_M;
    const stack: number[] = [];
    for (const k0 of channel) {
      if (label[k0] < 0 || basinOf[k0] !== -1 || !(pitDepth[k0] > PIT_EPS)) continue;
      const id = basinCap.length;
      basinStart.push(basinCells.length);
      let cap = 0;
      let deepest = 0;
      const from = basinCells.length;
      basinOf[k0] = id;
      stack.push(k0);
      while (stack.length) {
        const c = stack.pop()!;
        basinCells.push(c);
        cap += pitDepth[c] * cellM2;
        deepest = Math.max(deepest, pitDepth[c]);
        const ci = c % W;
        const cj = (c - ci) / W;
        for (let t = 0; t < 8; t++) {
          const i = ci + DI[t];
          const j = cj + DJ[t];
          if (i < 0 || j < 0 || i >= W || j >= H) continue;
          const m = j * W + i;
          if (basinOf[m] !== -1 || !(pitDepth[m] > PIT_EPS) || lvl[m] !== lvl[k0]) continue;
          basinOf[m] = id;
          stack.push(m);
        }
      }
      if (deepest < minBasin) {
        // Too shallow to matter as a barrier: its water is counted as before (dead storage under the slices).
        for (let r = from; r < basinCells.length; r++) basinOf[basinCells[r]] = -2;
        basinCells.length = from;
        basinStart.pop();
        continue;
      }
      basinCap.push(cap);
      // The basin acts at the first stretch whose channel enters it (channel cells are in trace order).
      const at = basinAtStretch.get(label[k0]) ?? [];
      at.push(id);
      basinAtStretch.set(label[k0], at);
    }
    basinStart.push(basinCells.length);
  }
  const basinHeld = new Float64Array(basinCap.length);

  // 4. Pour each stretch's slice: the level h (above the reference bed) at which the pool over the stretch's reachable
  // cells, taken in spill order, holds exactly the slice's volume.
  const counts = new Int32Array(nS + 1);
  for (let k = 0; k < N; k++) if (label[k] >= 0 && Number.isFinite(spill[k])) counts[label[k] + 1]++;
  for (let j = 0; j < nS; j++) counts[j + 1] += counts[j];
  const order = new Int32Array(counts[nS]);
  const fillPos = counts.slice();
  for (let k = 0; k < N; k++) if (label[k] >= 0 && Number.isFinite(spill[k])) order[fillPos[label[k]]++] = k;

  const depthM = new Float32Array(N);
  const wseOut = new Float32Array(N).fill(NaN);
  const velOut = new Float32Array(N).fill(NaN);
  const arrOut = new Float32Array(N).fill(NaN);
  let slicesM3 = 0;
  let exhausted = 0;
  let stretches = 0;
  let retainedM3 = 0;
  const v0 = opts.releasedM3 != null && opts.releasedM3 > 0 ? opts.releasedM3 : 0;
  let travelling = v0;
  let basinsFilled = 0;
  let stoppedIn: SpreadResult["stoppedIn"] = null;
  const sqrtOf = (s: number) => Math.sqrt(Math.max(s, 1e-6));
  // Lay a level pool holding `vol` (m³) over a basin's cells, lowest ground first (still water: velocity 0).
  const poolBasin = (id: number, vol: number, tArr: number) => {
    const cells = basinCells.slice(basinStart[id], basinStart[id + 1]).sort((p, q) => g.z[p] - g.z[q]);
    const target = vol / cellM2;
    let n = 0;
    let sumZ = 0;
    let h = lvl[cells[0]];
    for (let r = 0; r < cells.length; r++) {
      n++;
      sumZ += g.z[cells[r]];
      const next = r + 1 < cells.length ? g.z[cells[r + 1]] : Infinity;
      const hh = (target + sumZ) / n;
      if (hh <= next) {
        h = Math.min(hh, lvl[cells[0]]);
        break;
      }
    }
    let area = 0;
    for (let r = 0; r < n; r++) {
      const k = cells[r];
      const d = h - g.z[k];
      if (d < minDepth) continue;
      area += cellM2;
      if (d > depthM[k]) {
        depthM[k] = d;
        wseOut[k] = h;
        velOut[k] = 0;
        arrOut[k] = Number.isFinite(arrOut[k]) ? Math.min(arrOut[k], tArr) : tArr;
      }
    }
    return { h, area };
  };
  for (let j = 0; j + 1 < nS; j++) {
    // Closed basins the channel enters in this stretch: the wave fills each before it can go on.
    let stop = false;
    for (const id of basinAtStretch.get(j) ?? []) {
      const room = basinCap[id] - basinHeld[id];
      if (!(room > 0)) continue;
      const tArr = sections[j].tArrivalS ?? sections[j].tPeakS;
      if (travelling <= room) {
        const held = basinHeld[id] + travelling;
        const { area } = poolBasin(id, held, tArr);
        retainedM3 += travelling;
        stoppedIn = { atM: sections[j].x, spillM: lvl[basinCells[basinStart[id]]], capacityM3: basinCap[id], heldM3: held, areaAc: area / ACRE_M2 };
        travelling = 0;
        stop = true;
        break;
      }
      poolBasin(id, basinCap[id], tArr);
      basinHeld[id] = basinCap[id];
      basinsFilled++;
      retainedM3 += room;
      travelling -= room;
    }
    if (stop) break;
    // The slice shrinks with the water already left behind in depressions upstream (the wave carries less).
    const v = sections[j].storeMaxM3 * (v0 > 0 ? travelling / v0 : 1);
    const a = counts[j];
    const b = counts[j + 1];
    if (!(v > 0) || b <= a) continue;
    stretches++;
    slicesM3 += v;
    const cells = Array.from(order.subarray(a, b)).sort((p, q) => spill[p] - spill[q]);
    const target = v / cellM2; // m of depth summed over cells
    // Pour the slice as one or more level pools. Cells join a pool in spill (connection) order; a pool can only rise
    // to a saddle it has the volume to top. If adding the next cell would drop the computed level below that cell's
    // own saddle, there isn't enough water to carry the pool over it: the near side caps at the saddle and the
    // remainder perches as a fresh pool beyond it. Without this (a single running average over all the cells) a slice
    // too small to top a saddle but big enough to reach it would be smeared across the saddle, pulling the near-side
    // surface below the saddle it supposedly crossed and laying a sheet of water in the low behind it — a dry gap in
    // the channel and ~an extra slice of water in the wrong place. (M13)
    const levelOf = new Float64Array(cells.length).fill(NaN);
    let budget = target;
    let start = 0;
    while (start < cells.length && budget > 1e-9) {
      let cnt = 0;
      let sumG = 0;
      let level = NaN;
      let end = cells.length - 1;
      let capped = false;
      for (let r = start; r < cells.length; r++) {
        const k = cells[r];
        cnt++;
        sumG += g.z[k] - bed[k];
        const hh = (budget + sumG) / cnt;
        if (r > start && hh < spill[k]) {
          // Too little water to carry the pool over this cell's saddle: cap the near side at the saddle, perch the rest.
          level = spill[k];
          end = r - 1;
          capped = true;
          break;
        }
        const next = r + 1 < cells.length ? spill[cells[r + 1]] : Infinity;
        if (hh <= next) {
          level = hh;
          end = r;
          break;
        }
      }
      if (!Number.isFinite(level)) break;
      let consumed = 0;
      for (let r = start; r <= end; r++) {
        levelOf[r] = level;
        consumed += level - (g.z[cells[r]] - bed[cells[r]]);
      }
      budget -= consumed;
      if (capped) {
        start = end + 1; // the overflow perches starting at the cell beyond the saddle
      } else {
        if (end === cells.length - 1) exhausted++; // the pool stood over every cell of the stretch
        break; // a contained pool took the whole remaining budget
      }
    }
    // Velocity: Manning on each wet cell, then one factor so the stretch passes its routed peak (Σ v·d·w = Qpeak,
    // with each cell's width across the flow its area over the stretch length).
    const dx = Math.max(cellM, sections[j + 1].x - sections[j].x);
    const sS = sqrtOf(sections[j].slopeStage);
    let qMan = 0;
    for (let r = 0; r < cells.length; r++) {
      const level = levelOf[r];
      if (!Number.isFinite(level)) continue;
      const k = cells[r];
      const d = bed[k] + level - g.z[k];
      if (d < minDepth) continue;
      const nn = d <= rough.bankfullM ? rough.nChannel : rough.nFloodplain;
      qMan += (Math.pow(d, 5 / 3) * sS * cellM2) / (nn * dx);
    }
    const scale = qMan > 0 ? sections[j].peakCms / qMan : 0;
    const tArr = sections[j].tArrivalS ?? sections[j].tPeakS;
    let dead = 0;
    for (let r = 0; r < cells.length; r++) {
      const level = levelOf[r];
      if (!Number.isFinite(level)) continue;
      const k = cells[r];
      const d = bed[k] + level - g.z[k];
      if (d < minDepth) continue;
      // A basin the wave already filled keeps its (deeper) standing pool.
      if (basinOf[k] >= 0 && depthM[k] > d) continue;
      depthM[k] = d;
      const kept = Math.min(d, pitDepth[k]) * cellM2;
      const bid = basinOf[k];
      if (bid >= 0) {
        // Water already standing in a basin counts against its room, once.
        const add = Math.min(kept, Math.max(0, basinCap[bid] - basinHeld[bid]));
        basinHeld[bid] += add;
        dead += add;
      } else dead += kept;
      wseOut[k] = bed[k] + level;
      const nn = d <= rough.bankfullM ? rough.nChannel : rough.nFloodplain;
      // Froude ≤ 1: a pool too thin for the stretch's peak would otherwise need impossible speeds to pass it.
      velOut[k] = Math.min((scale * Math.pow(d, 2 / 3) * sS) / nn, Math.sqrt(9.80665 * d));
      arrOut[k] = tArr;
    }
    retainedM3 += dead;
    if (v0 > 0) {
      travelling = Math.max(0, travelling - dead);
      if (travelling <= 0) break;
    }
  }

  let wetCells = 0;
  let volumeM3 = 0;
  let reachedEdge = false;
  for (let k = 0; k < N; k++) {
    if (depthM[k] <= 0) continue;
    wetCells++;
    volumeM3 += depthM[k] * cellM2;
    const i = k % W;
    const j = (k - i) / W;
    if (i === 0 || j === 0 || i === W - 1 || j === H - 1) reachedEdge = true;
  }
  const rel = opts.releasedM3;
  return {
    width: W,
    height: H,
    depthM,
    wseM: wseOut,
    velocityMps: velOut,
    arrivalS: arrOut,
    wetCells,
    floodedAc: (wetCells * cellM2) / ACRE_M2,
    volumeM3,
    slicesM3,
    volumeRatio: rel && rel > 0 ? volumeM3 / rel : null,
    retainedM3,
    travellingM3: travelling,
    exhaustedStretches: exhausted,
    basinsFilled,
    stoppedIn,
    stretches,
    reachedEdge,
  };
}

/**
 * The breach's own flow path, for the spread: where water let go just below the breach actually goes on this DEM.
 * A lowest-first exploration from the toe (each step takes the lowest unvisited ground next to the water so far), so
 * the path runs down the valley floor, fills any closed basin on the way up to its spill point and carries on over the
 * saddle, until it reaches the DEM's border. Cells behind the dam (upstream of it along the desk bearing `u`) are
 * walls, so the path can't run back through the breach into the reservoir.
 *
 * Unlike the outline's drainage trace (which follows the drainage TO the nearest domain edge, and so can leave by a
 * side of the domain close to the dam, or the wrong way out of a prairie pothole), this follows the water FROM the
 * breach. Returns null when it never leaves the start cell.
 */
export function breachPath(
  g: DemGrid,
  loc: { lon: number; lat: number },
  u: [number, number],
  start: { e: number; n: number },
  opts: { maxCells?: number; startSearchM?: number; farEndFraction?: number; simplifyM?: number } = {},
): Centerline | null {
  const W = g.width;
  const H = g.height;
  const N = W * H;
  const kx = kxOf(loc.lat);
  const dlon = (g.bbox[2] - g.bbox[0]) / W;
  const dlat = (g.bbox[3] - g.bbox[1]) / H;
  const eOf = (i: number) => (g.bbox[0] + (i + 0.5) * dlon - loc.lon) * kx;
  const nOf = (j: number) => (g.bbox[3] - (j + 0.5) * dlat - loc.lat) * M_PER_DEG_LAT;
  const valid = (k: number) => {
    const z = g.z[k];
    return Number.isFinite(z) && z > NODATA + 1 && z < 9000;
  };
  const i0 = Math.floor((loc.lon + start.e / kx - g.bbox[0]) / dlon);
  const j0 = Math.floor((g.bbox[3] - (loc.lat + start.n / M_PER_DEG_LAT)) / dlat);
  if (i0 < 0 || j0 < 0 || i0 >= W || j0 >= H) return null;
  let k0 = j0 * W + i0;
  // Start where the Runner puts its inflow: the lowest ground within startSearchM of the dam on the downstream side
  // (the toe of the breach), not wherever the bearing happens to land 60 m out, which can sit on the embankment's
  // flank and send the path off sideways.
  const rS = opts.startSearchM ?? 105;
  if (rS > 0) {
    let best = valid(k0) ? g.z[k0] : Infinity;
    const ri = Math.ceil(rS / (dlon * kx)) + 1;
    const rj = Math.ceil(rS / (dlat * M_PER_DEG_LAT)) + 1;
    const ic = Math.floor((loc.lon - g.bbox[0]) / dlon);
    const jc = Math.floor((g.bbox[3] - loc.lat) / dlat);
    for (let j = Math.max(0, jc - rj); j <= Math.min(H - 1, jc + rj); j++)
      for (let i = Math.max(0, ic - ri); i <= Math.min(W - 1, ic + ri); i++) {
        const k = j * W + i;
        const e = eOf(i);
        const n = nOf(j);
        if (!valid(k) || Math.hypot(e, n) > rS || e * u[0] + n * u[1] < 0) continue;
        if (g.z[k] < best) {
          best = g.z[k];
          k0 = k;
        }
      }
  }
  if (!valid(k0)) return null;
  let maxAlong = 0;
  for (let j = 0; j < H; j += 4) for (let i = 0; i < W; i += 4) maxAlong = Math.max(maxAlong, eOf(i) * u[0] + nOf(j) * u[1]);
  const farEnd = (opts.farEndFraction ?? 0.6) * maxAlong;
  const parent = new Int32Array(N).fill(-1);
  const pathM = new Float64Array(N);
  const cellD = Math.min(dlon * kx, dlat * M_PER_DEG_LAT);
  const seen = new Uint8Array(N);
  const neg = new Float64Array(N);
  const heap = new MaxHeap(neg, 4096);
  seen[k0] = 1;
  neg[k0] = -g.z[k0];
  heap.push(k0);
  const DI = [1, -1, 0, 0, 1, 1, -1, -1];
  const DJ = [0, 0, 1, -1, 1, -1, 1, -1];
  const maxCells = opts.maxCells ?? N;
  let popped = 0;
  let end = -1;
  // If the far end is never reached (a closed basin holds everything), end at the farthest cell the water got to.
  let lastFar = -1;
  let farA = -Infinity;
  while (heap.size > 0 && popped++ < maxCells) {
    const c = heap.pop();
    {
      const a = eOf(c % W) * u[0] + nOf((c - (c % W)) / W) * u[1];
      if (a > farA) {
        farA = a;
        lastFar = c;
      }
    }
    const ci = c % W;
    const cj = (c - ci) / W;
    // The DEM is a corridor along the bearing: its far end is the way out. Its sides are only where the corridor was
    // cut (a pond the box clips would otherwise "drain" sideways at its own level), so they don't end the path.
    if ((ci === 0 || cj === 0 || ci === W - 1 || cj === H - 1) && eOf(ci) * u[0] + nOf(cj) * u[1] >= farEnd) {
      end = c;
      break;
    }
    for (let t = 0; t < 8; t++) {
      const i = ci + DI[t];
      const j = cj + DJ[t];
      if (i < 0 || j < 0 || i >= W || j >= H) continue;
      const m = j * W + i;
      if (seen[m] || !valid(m)) continue;
      seen[m] = 1;
      if (eOf(i) * u[0] + nOf(j) * u[1] < 0) continue; // behind the dam
      parent[m] = c;
      // Ties on flat ground (a hydro-flattened pond, a level field) go to the cell nearer the start along the path, so
      // the path crosses a flat straight instead of zig-zagging through it in arbitrary heap order.
      pathM[m] = pathM[c] + (DI[t] !== 0 && DJ[t] !== 0 ? cellD * Math.SQRT2 : cellD);
      neg[m] = -(g.z[m] + FLAT_TIE * pathM[m]);
      heap.push(m);
    }
  }
  if (end < 0) end = lastFar;
  if (end < 0) return null;
  const chain: number[] = [];
  for (let k = end; k >= 0; k = parent[k]) chain.push(k);
  chain.reverse();
  if (chain.length < 2) return null;
  // Simplify (Douglas-Peucker): through potholes and flats the lowest-first walk detours to each low and back, which
  // would lengthen the reach (and so the routed storage) without moving the water any further down the valley. The
  // simplified line keeps every turn larger than simplifyM; sections are cut along it and resampled evenly anyway.
  // Off by default: on the baseline it shortened the reaches without bringing the envelope ratio into band.
  const raw = chain.map((k) => {
    const i = k % W;
    return { e: eOf(i), n: nOf((k - i) / W) };
  });
  const keep = douglasPeucker(raw, opts.simplifyM ?? 0);
  const pts: { e: number; n: number; x: number }[] = [];
  let x = 0;
  for (let p = 0; p < keep.length; p++) {
    const { e, n } = keep[p];
    if (p > 0) x += Math.hypot(e - pts[p - 1].e, n - pts[p - 1].n);
    pts.push({ e, n, x });
  }
  return { loc: { lon: loc.lon, lat: loc.lat }, pts };
}

/** Douglas-Peucker simplification of a polyline (iterative), keeping the end points. */
function douglasPeucker(p: { e: number; n: number }[], tol: number): { e: number; n: number }[] {
  if (p.length < 3 || !(tol > 0)) return p.slice();
  const keep = new Uint8Array(p.length);
  keep[0] = keep[p.length - 1] = 1;
  const stack: [number, number][] = [[0, p.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop()!;
    const ax = p[a].e;
    const ay = p[a].n;
    const dx = p[b].e - ax;
    const dy = p[b].n - ay;
    const L = Math.hypot(dx, dy);
    let best = -1;
    let bd = tol;
    for (let i = a + 1; i < b; i++) {
      const d = L > 0 ? Math.abs((p[i].e - ax) * dy - (p[i].n - ay) * dx) / L : Math.hypot(p[i].e - ax, p[i].n - ay);
      if (d > bd) {
        bd = d;
        best = i;
      }
    }
    if (best >= 0) {
      keep[best] = 1;
      stack.push([a, best], [best, b]);
    }
  }
  return p.filter((_, i) => keep[i]);
}
