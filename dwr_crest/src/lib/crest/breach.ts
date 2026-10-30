/** Sunny-day breach outflow routed through a growing trapezoidal weir, ported from the
 * DamSafety_Runner core/breach.py (Reservoir, BreachOpening, routed_hydrograph).
 *
 * All SI inside. The outflow is driven by the opening width and the head on it, and the pool
 * draws down as it empties (level-pool routing, no inflow, no tailwater).
 */

export const FT = 0.3048;
export const ACFT = 1233.48183754752; // m3 per acre-foot
export const ACRE = 4046.8564224; // m2
export const CFS = 0.028316846592; // m3/s per cfs
export const G = 9.80665;

const WEIR_C_BOTTOM_SI = 3.1 * Math.sqrt(FT); // ~1.711
const WEIR_C_SIDE_SI = 2.45 * Math.sqrt(FT); // ~1.353

export const FROEHLICH_2008 = {
  piping: { ko: 1.0, z: 0.7 },
  overtopping: { ko: 1.3, z: 1.0 },
} as const;

/** Froehlich (2008) average width (m), formation time (s), side slope (H:1V). */
export function froehlich2008(vwM3: number, hbM: number, mode: "piping" | "overtopping") {
  const c = FROEHLICH_2008[mode];
  const bAvg = 0.27 * c.ko * vwM3 ** 0.32 * hbM ** 0.04;
  const tf = 63.2 * Math.sqrt(vwM3 / (G * hbM * hbM));
  return { bAvg, tf, z: c.z };
}

/** Froehlich (1995) peak outflow regression (m3/s), on the head hw = pool at failure - breach bottom. */
export function froehlich1995Peak(vwM3: number, hwM: number) {
  return 0.607 * vwM3 ** 0.295 * hwM ** 1.24;
}

/** V(y) = Vt (y/Ht)^m with m = SA Ht / Vt (limited to 1-6), or m = 2 without a surface area. */
export class Reservoir {
  readonly bottom: number;
  readonly ht: number;
  readonly vt: number;
  readonly m: number;
  readonly notes: string[] = [];

  constructor(bottomM: number, topM: number, vTopM3: number, saTopM2?: number | null) {
    this.bottom = bottomM;
    this.ht = topM - bottomM;
    this.vt = vTopM3;
    if (saTopM2 && saTopM2 > 0) {
      const m = (saTopM2 * this.ht) / this.vt;
      if (m < 1 || m > 6) {
        this.notes.push(
          `Storage-area exponent ${m.toFixed(2)} from storage, surface area and height is outside 1–6; limited to that range. Check the storage and area values.`,
        );
      }
      this.m = Math.min(6, Math.max(1, m));
    } else {
      this.m = 2;
      this.notes.push("No surface area given: storage assumed to vary with depth squared (m = 2).");
    }
  }

  volume(wse: number) {
    const y = Math.max(0, wse - this.bottom);
    return this.vt * (y / this.ht) ** this.m;
  }

  wse(volume: number) {
    const v = Math.max(0, volume);
    return this.bottom + this.ht * (v / this.vt) ** (1 / this.m);
  }
}

function weir(b: number, z: number, h: number) {
  if (h <= 0) return 0;
  return WEIR_C_BOTTOM_SI * b * h ** 1.5 + WEIR_C_SIDE_SI * z * h ** 2.5;
}

export type RoutedInput = {
  topM: number;
  /** Reservoir bottom = top of dam - dam height. */
  resBottomM: number;
  breachBottomM: number;
  wse0M: number;
  storageTopM3: number;
  surfaceAreaTopM2?: number | null;
  /** Average breach width (m) and formation time (s); side slope z (H:1V). */
  bAvgM: number;
  tfS: number;
  z: number;
  maxHours?: number;
};

export type Routed = {
  t: number[];
  q: number[];
  peakCms: number;
  timeToPeakS: number;
  bottomWidthM: number;
  tfS: number;
  releasedM3: number;
  storageAtFailureM3: number;
  storageExponent: number;
  notes: string[];
};

/** Level-pool routing through the opening (midpoint step, as the Runner). */
export function routedHydrograph(inp: RoutedInput): Routed {
  const res = new Reservoir(inp.resBottomM, inp.topM, inp.storageTopM3, inp.surfaceAreaTopM2);
  const notes = [...res.notes];
  const bottom = Math.max(inp.breachBottomM, inp.resBottomM);
  const hb = inp.topM - bottom;
  let bBottom = inp.bAvgM - inp.z * hb;
  if (bBottom < 0) {
    notes.push(
      "The average breach width is narrower than a triangle of this height and side slope; bottom width set to 0.",
    );
    bBottom = 0;
  }
  const tf = Math.max(inp.tfS, 60);
  const zc0 = Math.min(inp.wse0M, inp.topM);
  const vs = res.volume(inp.wse0M);
  const vDead = res.volume(bottom);
  const vRelease = vs - vDead;

  const qAt = (t: number, v: number) => {
    const frac = Math.min(1, t / tf);
    const zb = zc0 - frac * (zc0 - bottom);
    return weir(bBottom * frac, inp.z, res.wse(v) - zb);
  };

  let dt = Math.min(10, tf / 300);
  let t = 0;
  let v = vs;
  const ts = [0];
  const qs = [qAt(0, vs)];
  const tMax = (inp.maxHours ?? 240) * 3600;
  let qpeak = 0;
  let tpeak = 0;
  while (t < tMax) {
    const q1 = qAt(t, v);
    const vHalf = Math.max(0, v - 0.5 * dt * q1);
    const q2 = qAt(t + 0.5 * dt, vHalf);
    v -= Math.min(v, dt * q2);
    t += dt;
    const q = qAt(t, v);
    ts.push(t);
    qs.push(q);
    if (q > qpeak) {
      qpeak = q;
      tpeak = t;
    }
    if (t > tf && (v - vDead < 0.002 * vRelease || (qpeak > 0 && q < 0.002 * qpeak))) break;
    if (t > 2 * tf) dt = Math.min(60, dt * 1.02);
  }
  if (qs[qs.length - 1] > 0) {
    ts.push(ts[ts.length - 1] + dt);
    qs.push(0);
  }
  const left = v - vDead;
  if (left > 0.01 * vRelease) {
    notes.push(
      `The breach outflow was stopped after ${(t / 3600).toFixed(0)} h with ${(left / ACFT).toFixed(0)} ac-ft (${((100 * left) / vRelease).toFixed(1)}%) of the releasable storage still in the reservoir.`,
    );
  }
  return {
    t: ts,
    q: qs,
    peakCms: qpeak,
    timeToPeakS: tpeak,
    bottomWidthM: bBottom,
    tfS: tf,
    releasedM3: vs - v,
    storageAtFailureM3: vs,
    storageExponent: res.m,
    notes,
  };
}

/** Thin a long series to at most `max` points, keeping the first, the peak, and the last. */
export function thin(t: number[], q: number[], max = 600): { t: number[]; q: number[] } {
  if (t.length <= max) return { t, q };
  const stride = Math.ceil(t.length / max);
  let ip = 0;
  for (let i = 1; i < q.length; i++) if (q[i] > q[ip]) ip = i;
  const keep: number[] = [];
  for (let i = 0; i < t.length; i += stride) keep.push(i);
  if (!keep.includes(ip)) keep.push(ip);
  if (keep[keep.length - 1] !== t.length - 1) keep.push(t.length - 1);
  keep.sort((a, b) => a - b);
  return { t: keep.map((i) => t[i]), q: keep.map((i) => q[i]) };
}
