"""3.10.70: the ANUGA worker's optional time-step diagnostic and its per-yield progress fields.

Off by default (results never change, the domain is only read). Turned on by the job key
dt_diag_every (written by anuga_model.write_job for the Runner's anuga_dt_diag setting) or by
NDBREACH_ANUGA_DT_DIAG=1 in the worker's environment (NDBREACH_ANUGA_DT_DIAG_EVERY=N = every
N yield steps, default 1; NDBREACH_ANUGA_DT_DIAG=0 turns it off whatever the job says).

At a sampled yield step it writes one row of timestep_diag.csv next to the job file:
  the internal steps and their dt since the last sample (ANUGA's recorded_min/max_timestep,
  number_of_steps, domain.timestep), and the triangle that limits the time step: the argmin of
  radii / max_speed. ANUGA 4 overwrites max_speed at every internal step, so at a yield it
  holds the LAST internal step's wave speeds; the row is that step's limiting triangle (its
  centroid in the model CRS, lon / lat when a CRS is known, area, depth, speed) and the
  nearest structure operator (breach inlet, culvert, weir) within max(3 x its inradius, 50 m).
At the end a summary of the triangles that limited most often (by samples, internal steps and
wall seconds) goes to timestep_diag_summary.txt, stdout (anuga.log) and stats["dt_diag"].
A resumed checkpoint keeps the rows up to the resume time and appends.

Pure numpy (plus pyproj for lon / lat when it imports); no anuga import, so the helpers are
tested without ANUGA. Runs in the worker as a sibling module (python anuga_worker.py ...).
"""
import csv
import datetime
import math
import os
import time

import numpy as np

DT_DIAG_KEYS = ("dt_diag_every", "dt_diag_crs")
DT_DIAG_ENV = "NDBREACH_ANUGA_DT_DIAG"
DT_DIAG_EVERY_ENV = "NDBREACH_ANUGA_DT_DIAG_EVERY"
CSV_NAME = "timestep_diag.csv"
SUMMARY_NAME = "timestep_diag_summary.txt"
NEAR_MIN_M = 50.0            # an operator this close (or 3 inradii) is "near" the triangle
NEAR_RADII = 3.0
COLUMNS = ("utc", "t_s", "model_s", "wall_s", "model_s_per_wall_s", "yields", "steps",
           "steps_total", "dt_mean_s", "dt_min_s", "dt_max_s", "dt_last_s", "dt_cfl_last_s",
           "tri", "x", "y", "lon", "lat", "area_m2", "radius_m", "depth_m", "speed_mps",
           "wave_speed_mps", "dt_tri_s", "source", "near_op", "near_op_type", "near_op_dist_m")
_OFF = ("0", "off", "false", "no")
_ON = ("1", "on", "true", "yes")


def utc_now(now=None):
    """Wall-clock UTC, ISO 8601 to the second (2026-10-24T09:50:02Z)."""
    t = time.time() if now is None else now
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def dt_diag_every(job, environ=None):
    """Yield steps between samples; 0 = off. The environment wins over the job key."""
    env = os.environ if environ is None else environ
    try:
        n = int(float(job["dt_diag_every"])) if "dt_diag_every" in job else 0
    except (TypeError, ValueError):
        n = 0
    on = str(env.get(DT_DIAG_ENV, "") or "").strip().lower()
    if on in _OFF:
        return 0
    if on in _ON:
        n = max(n, 1)
    every = str(env.get(DT_DIAG_EVERY_ENV, "") or "").strip()
    if n > 0 and every:
        try:
            n = max(1, int(float(every)))
        except ValueError:
            pass                       # a bad N keeps the job's (or 1)
    return max(0, n)


def limiting_triangle(radii, max_speed=None, depth=None, speed=None, g=9.8, h_dry=1e-3):
    """(index, radius / wave speed, wave speed, source) of the triangle with the smallest
    local time step, or None. source "max_speed": ANUGA's per-triangle wave speed of the last
    internal step; "state": |v| + sqrt(g h) of the wet triangles (when max_speed is all zero,
    e.g. a dry first step). Vectorised; the inputs are never written."""
    r = np.asarray(radii, float)
    if max_speed is not None:
        ms = np.asarray(max_speed, float)
        if ms.shape == r.shape and np.any(ms > 0):
            with np.errstate(divide="ignore", invalid="ignore"):
                ldt = np.where(ms > 0, r / np.where(ms > 0, ms, 1.0), np.inf)
            k = int(np.argmin(ldt))
            return k, float(ldt[k]), float(ms[k]), "max_speed"
    if depth is not None and speed is not None:
        h = np.asarray(depth, float)
        c = np.where(h > h_dry, np.abs(np.asarray(speed, float))
                     + np.sqrt(g * np.clip(h, 0.0, None)), 0.0)
        if np.any(c > 0):
            with np.errstate(divide="ignore", invalid="ignore"):
                ldt = np.where(c > 0, r / np.where(c > 0, c, 1.0), np.inf)
            k = int(np.argmin(ldt))
            return k, float(ldt[k]), float(c[k]), "state"
    return None


def nearest_operator(x, y, regions):
    """(name, type, distance m) of the operator region (list of (name, type, (n, 2) points))
    nearest to (x, y), or None when there are none."""
    best = None
    for name, kind, pts in regions:
        p = np.asarray(pts, float).reshape(-1, 2)
        if not len(p):
            continue
        d = float(np.min(np.hypot(p[:, 0] - x, p[:, 1] - y)))
        if best is None or d < best[2]:
            best = (name, kind, d)
    return best


def near_limit(radius):
    return max(NEAR_RADII * float(radius), NEAR_MIN_M)


def operator_kind(op):
    cls = type(op).__name__
    low = cls.lower()
    if low.endswith("inlet_operator"):          # Inlet_operator (Parallel_ in ANUGA 4)
        return "breach inlet"
    if "boyd" in low or "culvert" in low:
        return "culvert"
    if "weir" in low:
        return "weir"
    if "breach" in low or "erosion" in low:
        return "breach"
    return cls


def operator_regions(domain, culvert_names=(), x_off=0.0, y_off=0.0):
    """The structure operators' triangle centroids (model CRS = domain + offset): an inlet's
    inlet.triangle_indices, a structure's inlets[i].triangle_indices, a rate operator's
    indices. The worker's Envelope operator (and any operator without a region) is skipped.
    Culverts take their names, in build order, from culvert_names."""
    cc = np.asarray(domain.centroid_coordinates, float)
    names = list(culvert_names or [])
    out, ci = [], 0
    for op in list(getattr(domain, "fractional_step_operators", None) or []):
        if type(op).__name__ == "Envelope":
            continue
        parts = []
        inl = getattr(op, "inlet", None)
        if getattr(inl, "triangle_indices", None) is not None:
            parts.append(inl.triangle_indices)
        for i in (getattr(op, "inlets", None) or []):
            if getattr(i, "triangle_indices", None) is not None:
                parts.append(i.triangle_indices)
        ind = getattr(op, "indices", None)
        if not parts and ind is not None and not np.isscalar(ind):
            parts.append(ind)
        if not parts:
            continue
        try:
            ii = np.unique(np.concatenate([np.asarray(a, int).ravel() for a in parts]))
        except (TypeError, ValueError):
            continue
        ii = ii[(ii >= 0) & (ii < len(cc))]
        if not ii.size:
            continue
        kind = operator_kind(op)
        name = getattr(op, "label", None) or type(op).__name__
        if kind == "culvert":
            if ci < len(names) and names[ci]:
                name = names[ci]
            ci += 1
        elif kind == "breach inlet":
            name = "breach inlet"
        pts = cc[ii] + np.array([x_off, y_off])
        out.append((str(name), kind, pts))
    return out


def lonlat_transformer(crs_wkt):
    """A function (x, y) -> (lon, lat) for the model CRS, or None (no CRS / no pyproj)."""
    if not crs_wkt:
        return None
    try:
        from pyproj import CRS, Transformer
        src = CRS.from_user_input(crs_wkt)
        if src.is_geographic:
            return lambda x, y: (x, y)
        tr = Transformer.from_crs(src, CRS.from_epsg(4326), always_xy=True)
        return lambda x, y: tr.transform(x, y)
    except Exception:            # noqa: BLE001 - lon / lat are optional
        return None


def _fmt(v, nd=6):
    if v is None:
        return ""
    if isinstance(v, (float, np.floating)):
        if not math.isfinite(float(v)):
            return ""
        return "{:.{}g}".format(float(v), nd)
    return str(v)


_FIXED = {"x": 2, "y": 2, "lon": 7, "lat": 7, "t_s": 3, "model_s": 3, "wall_s": 3,
          "near_op_dist_m": 1}


def format_row(row):
    """The CSV cells of a sample (coordinates in fixed decimals, the rest 6 significant)."""
    out = []
    for c in COLUMNS:
        v = row.get(c)
        if c in _FIXED and isinstance(v, (float, int, np.floating)) and math.isfinite(v):
            out.append("{:.{}f}".format(float(v), _FIXED[c]))
        else:
            out.append(_fmt(v))
    return out


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


class YieldClock:
    """Always on (3.10.70): wall-clock UTC, internal steps (since the last yield and in total
    for this worker process) and wall seconds per yield, appended to each PROGRESS line so a
    frozen host (a big wall gap with a normal step count) and a real slowdown (many small
    steps) can be told apart from anuga.log alone. Reads domain.number_of_steps only."""

    def __init__(self, t_start=0.0):
        self.wall = time.time()
        self.t = float(t_start)
        self.total = 0

    def tick(self, domain, t):
        now = time.time()
        n = int(getattr(domain, "number_of_steps", 0) or 0)
        self.total += n
        wall, model = now - self.wall, float(t) - self.t
        self.wall, self.t = now, float(t)
        rate = model / wall if wall > 0 and model > 0 else None
        return {"utc": utc_now(now), "steps": n, "steps_total": self.total, "wall_s": wall,
                "model_s": model, "rate": rate}

    @staticmethod
    def suffix(f):
        """' utc=... steps=... dsteps=... wall_s=... rate=...' (appended to PROGRESS)."""
        s = " utc={} steps={} dsteps={} wall_s={:.1f}".format(f["utc"], f["steps_total"],
                                                             f["steps"], f["wall_s"])
        if f["rate"] is not None:
            s += " rate={:.3g}".format(f["rate"])
        return s


def _truncate_after(path, t_max):
    """Keep the header and the rows with t_s <= t_max (a resumed run repeats the rest)."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except OSError:
        return False
    if not rows or rows[0] != list(COLUMNS):
        return False
    i_t = COLUMNS.index("t_s")
    keep = [rows[0]] + [r for r in rows[1:] if len(r) > i_t and _f(r[i_t]) is not None
                        and _f(r[i_t]) <= t_max + 1e-6]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(keep)
    os.replace(tmp, path)
    return True


class DtDiag:
    """The sampler. on_yield() after each PROGRESS line; finish() after evolve. Never raises
    into the run: a failure is printed once and the diagnostic stops."""

    def __init__(self, domain, workdir, every, resumed_t=None, x_off=0.0, y_off=0.0,
                 crs_wkt="", culvert_names=()):
        self.every = max(1, int(every))
        self.path = os.path.join(workdir, CSV_NAME)
        self.summary_path = os.path.join(workdir, SUMMARY_NAME)
        self.x_off, self.y_off = float(x_off), float(y_off)
        self.ok = True
        self.error = None
        cont = resumed_t is not None and os.path.exists(self.path) \
            and _truncate_after(self.path, float(resumed_t))
        self.f = open(self.path, "a" if cont else "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if not cont:
            self.w.writerow(COLUMNS)
            self.f.flush()
        self.radii = np.array(getattr(domain, "radii", []), float, copy=True)
        self.areas = np.array(getattr(domain, "areas", []), float, copy=True)
        self.cc = np.array(domain.centroid_coordinates, float, copy=True)
        self.ops = operator_regions(domain, culvert_names, self.x_off, self.y_off)
        self.lonlat = lonlat_transformer(crs_wkt)
        self.samples = 0
        self._reset(domain.get_time())

    def _reset(self, t):
        self.t_prev, self.wall_prev = float(t), time.time()
        self.yields, self.steps = 0, 0
        self.dt_min, self.dt_max = math.inf, 0.0

    def on_yield(self, domain, t, clock=None):
        if not self.ok:
            return None
        try:
            return self._on_yield(domain, t, clock)
        except Exception as e:         # noqa: BLE001 - a diagnostic never stops the run
            self.ok, self.error = False, "{}: {}".format(type(e).__name__, str(e)[:200])
            print("DT_DIAG stopped at t={:.0f}s: {}".format(float(t), self.error), flush=True)
            return None

    def _on_yield(self, domain, t, clock):
        n = int(getattr(domain, "number_of_steps", 0) or 0)
        self.yields += 1
        if n > 0:
            self.steps += n
            rmin = _f(getattr(domain, "recorded_min_timestep", None))
            rmax = _f(getattr(domain, "recorded_max_timestep", None))
            if rmin is not None:
                self.dt_min = min(self.dt_min, rmin)
            if rmax is not None:
                self.dt_max = max(self.dt_max, rmax)
        if self.steps == 0:            # the t=0 yield (no step yet): restart the interval
            self._reset(t)
            return None
        if self.yields < self.every:
            return None
        row = self._sample(domain, t, clock)
        self.w.writerow(format_row(row))
        self.f.flush()
        self.samples += 1
        self._reset(t)
        return row

    def _sample(self, domain, t, clock):
        now = time.time()
        model, wall = float(t) - self.t_prev, now - self.wall_prev
        cfl = _f(getattr(domain, "CFL", None)) or 1.0
        q = domain.quantities
        stage = q["stage"].centroid_values
        elev = q["elevation"].centroid_values
        xm, ym = q["xmomentum"].centroid_values, q["ymomentum"].centroid_values
        h_min = _f(getattr(domain, "minimum_allowed_height", None)) or 1e-5
        g = _f(getattr(domain, "g", None)) or 9.8
        lim = None
        if self.radii.size == len(stage):
            lim = limiting_triangle(self.radii, getattr(domain, "max_speed", None))
            if lim is None:            # max_speed all zero: the wet state's wave speed
                h = np.asarray(stage, float) - np.asarray(elev, float)
                with np.errstate(divide="ignore", invalid="ignore"):
                    sp = np.where(h > h_min, np.hypot(xm, ym) / np.where(h > h_min, h, 1.0),
                                  0.0)
                lim = limiting_triangle(self.radii, None, h, sp, g=g, h_dry=h_min)
        flux_ts = _f(getattr(domain, "flux_timestep", None))
        row = {c: None for c in COLUMNS}
        row.update(utc=utc_now(now), t_s=float(t), model_s=model, wall_s=wall,
                   model_s_per_wall_s=model / wall if wall > 0 else None,
                   yields=self.yields, steps=self.steps,
                   steps_total=clock.total if clock is not None else None,
                   dt_mean_s=model / self.steps if self.steps else None,
                   dt_min_s=self.dt_min if math.isfinite(self.dt_min) else None,
                   dt_max_s=self.dt_max if self.dt_max > 0 else None,
                   dt_last_s=_f(getattr(domain, "timestep", None)),
                   dt_cfl_last_s=cfl * flux_ts if flux_ts is not None and flux_ts < 1e30
                   else None)
        if lim is not None:
            k, ldt, wave, source = lim
            x, y = float(self.cc[k, 0]) + self.x_off, float(self.cc[k, 1]) + self.y_off
            h = float(stage[k]) - float(elev[k])
            spd = math.hypot(float(xm[k]), float(ym[k])) / h if h > h_min else 0.0
            row.update(tri=k, x=x, y=y, area_m2=float(self.areas[k])
                       if self.areas.size > k else None, radius_m=float(self.radii[k]),
                       depth_m=h, speed_mps=spd, wave_speed_mps=wave, dt_tri_s=cfl * ldt,
                       source=source)
            if self.lonlat is not None:
                try:
                    lon, lat = self.lonlat(x, y)
                    row.update(lon=float(lon), lat=float(lat))
                except Exception:      # noqa: BLE001
                    pass
            near = nearest_operator(x, y, self.ops)
            if near is not None and near[2] <= near_limit(self.radii[k]):
                row.update(near_op=near[0], near_op_type=near[1],
                           near_op_dist_m=round(near[2], 1))
        return row

    def finish(self, top=10):
        try:
            self.f.close()
        except Exception:              # noqa: BLE001
            pass
        info = {"csv": self.path, "every_yields": self.every, "samples_this_run": self.samples}
        if self.error:
            info["error"] = self.error
        try:
            rows = read_rows(self.path)
            summ = summarize(rows, top=top)
            info.update(samples=len(rows), top=summ[:5], summary=self.summary_path)
            text = summary_text(summ, len(rows), self.path)
            with open(self.summary_path, "w", encoding="utf-8") as f:
                f.write(text)
            for ln in text.splitlines():
                print("DT_DIAG " + ln, flush=True)
        except Exception as e:         # noqa: BLE001
            info["summary_error"] = str(e)[:200]
        return info


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def summarize(rows, top=10):
    """The triangles that limited most often: per triangle the samples, internal steps, model
    and wall seconds of those samples (time weights), the smallest dt and the last location /
    nearby operator; sorted by samples, then steps."""
    by = {}
    for r in rows:
        if not r.get("tri"):
            continue
        k = int(float(r["tri"]))
        d = by.setdefault(k, {"tri": k, "samples": 0, "steps": 0, "model_s": 0.0,
                              "wall_s": 0.0, "dt_min_s": None})
        d["samples"] += 1
        d["steps"] += int(float(r.get("steps") or 0))
        d["model_s"] += _f(r.get("model_s")) or 0.0
        d["wall_s"] += _f(r.get("wall_s")) or 0.0
        dt = _f(r.get("dt_tri_s"))
        if dt is not None and (d["dt_min_s"] is None or dt < d["dt_min_s"]):
            d["dt_min_s"] = dt
        for c in ("x", "y", "lon", "lat", "area_m2", "depth_m", "speed_mps", "near_op",
                  "near_op_type", "near_op_dist_m"):
            v = r.get(c)
            d[c] = (_f(v) if c not in ("near_op", "near_op_type") else v) or \
                (None if c not in ("near_op", "near_op_type") else "")
    out = sorted(by.values(), key=lambda d: (-d["samples"], -d["steps"], d["tri"]))
    tot = {k: sum(d[k] for d in by.values()) or 1 for k in ("samples", "steps", "wall_s")}
    for d in out:
        d["share_samples"] = round(d["samples"] / tot["samples"], 4)
        d["share_steps"] = round(d["steps"] / tot["steps"], 4)
        d["share_wall"] = round(d["wall_s"] / tot["wall_s"], 4)
        d["model_s"], d["wall_s"] = round(d["model_s"], 1), round(d["wall_s"], 1)
    return out[:top]


def summary_text(summ, n_rows, path):
    lines = ["timestep diagnostic: {} samples in {}".format(n_rows, os.path.basename(path)),
             "top limiting triangles (share of samples / internal steps / wall seconds):"]
    for d in summ:
        lines.append(
            "tri {tri}: {samples} samples ({ss:.0%} / {st:.0%} / {sw:.0%}), dt_min {dt} s, "
            "x {x} y {y}, area {a} m2, depth {h} m{near}".format(
                tri=d["tri"], samples=d["samples"], ss=d["share_samples"],
                st=d["share_steps"], sw=d["share_wall"], dt=_fmt(d["dt_min_s"], 4),
                x=_fmt(d.get("x"), 12), y=_fmt(d.get("y"), 12), a=_fmt(d.get("area_m2"), 4),
                h=_fmt(d.get("depth_m"), 3),
                near=", near {} ({}, {} m)".format(d["near_op"], d["near_op_type"],
                                                   _fmt(d.get("near_op_dist_m"), 4))
                if d.get("near_op") else ""))
    return "\n".join(lines) + "\n"
