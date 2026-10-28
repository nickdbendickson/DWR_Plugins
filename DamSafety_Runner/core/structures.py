"""Culverts, bridges and other openings through embankments.

Bare-earth LiDAR leaves a road embankment as a solid wall across the channel
(a culvert cannot be seen) and removes bridge decks (the opening is already in
the ground). Each structure is a line from its upstream end to its downstream
end:

* LISFLOOD-FP: its inertial solver has no weir or culvert links, so every
  structure is cut through the embankment in the DEM (an open cut: no roof, so
  no pressure flow and a capacity that a real culvert would not have).
* ANUGA: a bridge or notch is cut into the DEM the same way; a culvert stays
  in the DEM and is modelled with ANUGA's culvert operators (Boyd box or
  pipe: inlet control, outlet control, losses), which do limit the flow.

The report says which representation each structure got.
"""
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

from . import conditioning as cond

KIND_CULVERT = "culvert"
KIND_BRIDGE = "bridge"
KIND_NOTCH = "notch"
KINDS = (KIND_CULVERT, KIND_BRIDGE, KIND_NOTCH)


@dataclass
class Structure:
    kind: str
    line: np.ndarray                    # upstream end first, work CRS (m)
    width_m: float = 1.0                # box span or pipe diameter (one barrel)
    height_m: float = 1.0               # rise (equals width for a pipe)
    barrels: int = 1
    shape: str = "box"                  # "box" or "pipe"
    invert_up_m: Optional[float] = None
    invert_dn_m: Optional[float] = None
    manning: float = 0.013
    losses: float = 1.0
    name: str = ""
    invert_note: str = ""               # 3.5: what became of inventory inverts (reconcile_inverts)
    invert_inventory: Optional[tuple] = None   # (up, dn) metres as the inventory gave them
    invert_channel_bed: Optional[tuple] = None  # 3.10.96: (up, dn) channel bed from the LiDAR
                                        # (reconcile_inverts); the ANUGA bed floor "channel".
                                        # Not in to_dict: the run key stays as before
    source: str = "inventory"           # "inventory" or "guess"
    surveyed: bool = True               # a guess is not a measured culvert
    min_cells: int = 0                  # >0: the cut line spans at least this many model cells
                                        # (inventory-matched guesses: a short line inside one
                                        # coarse cell has no lower ground at its ends to cut to)
    carries: str = ""                   # 3.10.90: a bridge's facility, "road" / "rail" / ""
                                        # (unknown; core/bridge_snap.facility_kind). Not in
                                        # to_dict: the run key stays as before
    snap_from: Optional[dict] = None    # PR #198 review: set when core/bridge_snap moved this
                                        # bridge (from / to / distance / why). Sub-runs share
                                        # the moved object (shallow copies): they report the
                                        # move from it and never judge the bridge again. Not
                                        # in to_dict

    def cut_line(self, cell):
        """The line the DEM cut follows at this cell size: as given, or lengthened
        symmetrically along its own direction to min_cells x cell."""
        line = np.asarray(self.line, dtype=float)
        if not self.min_cells or len(line) < 2:
            return line
        a, b = line[0], line[-1]
        v = b - a
        L = float(np.hypot(v[0], v[1]))
        need = float(self.min_cells) * float(cell)
        if L <= 0 or L >= need:
            return line
        u = v / L
        ext = 0.5 * (need - L)
        return np.vstack([a - u * ext, line[1:-1], b + u * ext]) if len(line) > 2 else \
            np.array([a - u * ext, b + u * ext])

    def corridor_width(self, cell):
        return max(cell, self.width_m * max(1, self.barrels) * 1.1)

    def notch(self, cell):
        return cond.Notch(self.cut_line(cell), self.corridor_width(cell), self.invert_up_m,
                          self.invert_dn_m, name=self.name or self.kind)

    def to_dict(self):
        d = {k: getattr(self, k) for k in ("kind", "width_m", "height_m", "barrels", "shape",
                                           "invert_up_m", "invert_dn_m", "manning", "losses",
                                           "name", "source", "surveyed")}
        d["line"] = np.asarray(self.line, dtype=float).tolist()
        return d


def structure_edits(structures, cell, models):
    """Notches to cut for `models` ("lisflood" or "anuga")."""
    out = []
    for s in structures:
        if s.kind in (KIND_BRIDGE, KIND_NOTCH) or models == "lisflood":
            out.append(s.notch(cell))
    return out


def anuga_culverts(structures, dem, mesh_polygon=None, bed_floor="off"):
    """ANUGA culvert specs for the worker (dicts with end points and inverts
    read from the DEM), and notes for those that cannot be used. bed_floor (3.10.96,
    NDBREACH_CULVERT_BED_FLOOR): "off" (the specs are as before), "enquiry" or "channel" -
    the worker raises an invert below that bed to it (anuga_worker.apply_bed_floor)."""
    specs, notes = [], []
    for s in structures:
        if s.kind != KIND_CULVERT:
            continue
        line = np.asarray(s.line, dtype=float)
        if len(line) < 2:
            notes.append("Culvert {}: needs a line with two ends.".format(s.name or "?"))
            continue
        ends = np.array([line[0], line[-1]])
        if polyline_len(line) < 2.0 * max(s.width_m, 1.0):
            notes.append("Culvert {}: shorter than twice its width; the ANUGA operator needs "
                         "more distance between the ends.".format(s.name or "?"))
            continue
        if mesh_polygon is not None and not cond.points_in_polygon(
                ends[:, 0], ends[:, 1], mesh_polygon).all():
            notes.append("Culvert {}: an end lies outside the ANUGA mesh (the flooded area); "
                         "left out of the ANUGA run.".format(s.name or "?"))
            continue
        z = dem.sample_bilinear(ends[:, 0], ends[:, 1])
        inv = [s.invert_up_m if s.invert_up_m is not None else float(z[0]),
               s.invert_dn_m if s.invert_dn_m is not None else float(z[1])]
        if not np.all(np.isfinite(inv)):
            notes.append("Culvert {}: no DEM at an end and no invert given.".format(s.name or "?"))
            continue
        # 3.10.71: where each invert came from ("given": the structure's own, after
        # reconcile_inverts; "model_dem": read here). fine_ground_inverts updates both.
        src = [INV_GIVEN if v is not None else INV_MODEL_DEM for v in (s.invert_up_m, s.invert_dn_m)]
        specs.append({"end_points": ends.tolist(), "invert_elevations": inv,
                      "width": s.width_m, "height": s.height_m, "barrels": int(s.barrels),
                      "shape": s.shape, "manning": s.manning, "losses": s.losses,
                      "apron": max(2.0, 1.0 * s.width_m), "name": s.name,
                      "invert_source": src})
        if bed_floor and bed_floor != "off":
            specs[-1]["bed_floor"] = bed_floor
            cb = getattr(s, "invert_channel_bed", None)
            if bed_floor == "channel" and cb is not None:
                specs[-1]["channel_bed_m"] = [None if v is None else round(float(v), 3)
                                              for v in cb]
    return specs, notes


LOSSES_FIELDS = ("losses", "loss_coef", "k_loss")    # 3.10.96, case-insensitive
LOSSES_RANGE = (0.0, 10.0)


def losses_field(names):
    """3.10.96: the STRUCTURES layer's loss-coefficient field (LOSSES_FIELDS), or ''."""
    low = {str(n).lower(): n for n in names or ()}
    return next((low[f] for f in LOSSES_FIELDS if f in low), "")


def losses_value(v):
    """3.10.96: (losses, ok) from a STRUCTURES attribute. Empty -> (None, True): the default
    1.0 stays; a number within LOSSES_RANGE -> (value, True); anything else -> (None, False)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, True
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None, False
    if f != f:                     # NULL read as NaN
        return None, True
    if not (LOSSES_RANGE[0] <= f <= LOSSES_RANGE[1]):
        return None, False
    return f, True


BED_FLOOR_ENV = "NDBREACH_CULVERT_BED_FLOOR"
BED_FLOOR_MODES = ("off", "enquiry", "channel")


def bed_floor_setting(environ=None):
    """3.10.96: the ANUGA culvert bed floor from NDBREACH_CULVERT_BED_FLOOR (off | enquiry |
    channel; unset = off). Opt-in until the floor rule is agreed (DWR-3034 REVIEW 9.8)."""
    v = str((os.environ if environ is None else environ).get(BED_FLOOR_ENV) or "off")
    v = v.strip().lower()
    if v in ("", "0", "no", "false", "none"):
        v = "off"
    if v not in BED_FLOOR_MODES:
        raise ValueError("{} must be one of {}, not {!r}".format(
            BED_FLOOR_ENV, ", ".join(BED_FLOOR_MODES), v))
    return v


# -------------------------------------------------- 3.10.71: inverts on ANUGA's bed --
INV_GIVEN = "given"                   # the structure's own invert (inventory / user layer)
INV_GIVEN_CAPPED = "given_capped"     # given, above the fine ground: lowered to ground + tol
INV_MODEL_DEM = "model_dem"           # bilinear on the model DEM (no fine ground at that end)
INV_FINE = "fine_grid"                # lowest fine ground within the search radius
INVERT_SEARCH_MIN_M = 5.0             # search radius = max(apron, this)
INVERT_CAP_TOL_M = 0.3                # a given invert may sit this far above the fine ground


def invert_search_radius(spec):
    return max(float(spec.get("apron") or 0.0), INVERT_SEARCH_MIN_M)


def fine_ground_inverts(specs, lowest_ground, tol_m=INVERT_CAP_TOL_M):
    """3.10.71: put the ANUGA culvert inverts on the bed ANUGA actually uses.

    With ANUGA_ELEVATION=fine the mesh bed is the fine terrain (1-2 m) at the triangle
    vertices, while anuga_culverts read missing inverts bilinearly from the coarse model DEM
    (30 m), which smears the road embankment into the cells at the culvert ends: DWR-2801's
    34 culverts had their inverts a median 0.69 m (up to 2.3 m) above the fine ground, so a
    culvert passed little or nothing until the water was that deep.

    `lowest_ground(xy, radius)` -> the lowest fine ground (m) within `radius` of xy, or None
    where the fine terrain has no data. For each end (radius = max(apron, 5 m)):
      * no invert given -> the lowest fine ground ("fine_grid"); none there -> the model DEM
        value stays ("model_dem", noted);
      * a given invert at most `tol_m` above that ground -> kept as given;
      * a given invert higher than that -> lowered to ground + tol_m ("given_capped") with a
        WARNING note naming both values (never silently).
    Updates the specs in place (invert_elevations, invert_source, plus invert_ground_m,
    invert_search_m and invert_model_dem_m for the report) and returns the notes."""
    notes = []
    for c in specs:
        r = invert_search_radius(c)
        inv = [float(v) for v in c["invert_elevations"]]
        src = list(c.get("invert_source") or [INV_MODEL_DEM, INV_MODEL_DEM])
        before, src_before = list(inv), list(src)
        ground = []
        name = c.get("name") or "culvert"
        for i, e in enumerate(c["end_points"]):
            g = lowest_ground(e, r)
            g = float(g) if g is not None and np.isfinite(g) else None
            ground.append(None if g is None else round(g, 3))
            end = ("upstream", "downstream")[i]
            if g is None:
                if src[i] == INV_MODEL_DEM:
                    notes.append("Culvert {}: no fine ground within {:.0f} m of the {} end; its "
                                 "invert is the model DEM's ({:.2f} m).".format(name, r, end, inv[i]))
                continue
            if src[i] == INV_GIVEN:
                if inv[i] > g + tol_m:
                    notes.append("WARNING: Culvert {}: the given {} invert {:.2f} m is {:.2f} m above "
                                 "the lowest ANUGA ground within {:.0f} m ({:.2f} m); lowered to "
                                 "{:.2f} m (ground + {:.1f} m). Check the inventory elevation."
                                 .format(name, end, inv[i], inv[i] - g, r, g, g + tol_m, tol_m))
                    inv[i] = g + tol_m
                    src[i] = INV_GIVEN_CAPPED
                continue
            if src[i] == INV_MODEL_DEM:
                inv[i] = g
                src[i] = INV_FINE
        c["invert_elevations"] = inv
        c["invert_source"] = src
        c["invert_ground_m"] = ground
        c["invert_search_m"] = round(r, 2)
        c["invert_model_dem_m"] = [round(b, 3) if s0 == INV_MODEL_DEM else None
                                   for b, s0 in zip(before, src_before)]
    return notes


def invert_record(specs):
    """Per culvert: the inverts the ANUGA operator gets and where they came from (run report)."""
    return [{"name": c.get("name"), "invert_elevations_m": [round(float(v), 3) for v in
                                                            c["invert_elevations"]],
             "invert_source": c.get("invert_source"), "ground_m": c.get("invert_ground_m"),
             "model_dem_m": c.get("invert_model_dem_m"), "search_m": c.get("invert_search_m"),
             "enquiry_gap_m": c.get("enquiry_gap"),
             **({"channel_bed_m": c["channel_bed_m"]} if c.get("channel_bed_m") else {}),
             **({"bed_floor": c["bed_floor"]} if c.get("bed_floor") else {})} for c in specs]


def polyline_len(line):
    return cond.polyline_length(line)


# ----------------------------------------------------------- inventory inverts --
FT = 0.3048


def lidar_bed(grid, xy, radius):
    """The lowest finite ground within `radius` of xy on a Grid (metres), or None."""
    try:
        X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
        z = grid.masked()                                        # no-data as NaN
        near = (np.hypot(X - xy[0], Y - xy[1]) <= radius) & np.isfinite(z)
        if not near.any():
            return None
        return float(np.nanmin(z[near]))
    except Exception:                                            # noqa
        return None


CHANNEL_REACH_M = 30.0     # 3.10.96: how far past each end the channel bed is looked for
CHANNEL_CELL_M = 1.0       # ... on ground read this fine (a 3 m cell averages a narrow
                           # channel with its banks)
CHANNEL_NEAR_M = 10.0      # 3.11.1: the bed at the end is read over the first 10 m ...
CHANNEL_BED_PCT = 10.0     # ... as this low percentile of the slope-corrected thalweg
CHANNEL_MIN_BINS = 4       # thalweg points needed to fit the channel's slope


def thalweg_profile(grid, line, end, half, reach=CHANNEL_REACH_M):
    """3.11.1: (along, bed) arrays of the channel beyond one end of a culvert line: the
    lowest finite ground in each 1 m (or one-cell) bin of a strip from the end outward
    along the line's own direction (0..reach m), up to `half` (at least a cell) to either
    side. None without ground there."""
    line = np.asarray(line, dtype=float)
    p, q = (line[0], line[1]) if end == 0 else (line[-1], line[-2])
    u = p - q
    n_ = float(np.hypot(*u))
    if n_ <= 0:
        return None
    u = u / n_
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    z = grid.masked()
    dx, dy = X - p[0], Y - p[1]
    along = dx * u[0] + dy * u[1]
    across = np.abs(-dx * u[1] + dy * u[0])
    w = max(float(half), float(abs(grid.dx)))
    near = (along >= 0) & (along <= reach) & (across <= w) & np.isfinite(z)
    if not near.any():
        return None
    a, b = along[near], z[near]
    step = max(1.0, float(abs(grid.dx)))
    k = np.floor(a / step).astype(int)
    bins = np.unique(k)
    return (np.array([(i + 0.5) * step for i in bins]),
            np.array([float(b[k == i].min()) for i in bins]))


def channel_slope(along, bed):
    """3.11.1: the bed's fall per metre along the profile (Theil-Sen: the median of the
    pairwise slopes, so a few toe-slope or pool points do not tilt it); 0 with too few
    points."""
    if along is None or len(along) < CHANNEL_MIN_BINS:
        return 0.0
    i, j = np.triu_indices(len(along), 1)
    d = along[j] - along[i]
    ok = d > 0
    if not ok.any():
        return 0.0
    return float(np.median((bed[j] - bed[i])[ok] / d[ok]))


def channel_bed(grid, line, end, half, reach=CHANNEL_REACH_M, near=CHANNEL_NEAR_M,
                pct=CHANNEL_BED_PCT):
    """3.10.96: the channel bed just beyond one end of a culvert line, from the strip
    of thalweg_profile. None without ground there.

    3.11.1 (smoke test of 7feee1f): the minimum over 0..30 m read a sloping channel's bed
    up to slope x 30 m low at the downstream end (2 % -> 0.6 m = 2 ft), which rejected
    correct inventory inverts. Now the channel's slope is fitted over the whole reach
    (channel_slope), each thalweg point is carried back to the end along it, and the bed
    is the `pct` percentile of those values over the first `near` m (a low percentile,
    not the minimum, so one pit does not decide it).

    The disc around the end (lidar_bed) sits on the embankment's toe slope: at DWR-3034's
    Boyd culvert it read 528.26 / 526.68 m at a 3 m cell where the channel 4-28 m out is
    527.90-528.07 / 526.56-527.02 m on the 1 m LiDAR, which made the inventory inverts
    (527.30 / 526.39 m) look like a datum shift (REVIEW 9.8 item 3)."""
    try:
        prof = thalweg_profile(grid, line, end, half, reach)
        if prof is None:
            return None
        along, bed = prof
        slope = channel_slope(along, bed)
        at_end = bed - slope * along
        sel = along <= max(float(near), 1.0)
        if not sel.any():
            sel = np.ones_like(along, dtype=bool)
        return float(np.percentile(at_end[sel], pct))
    except Exception:                                            # noqa
        return None


CHANNEL_MIN_DROP_M = 0.15


def end_bed(disc, channel):
    """The LiDAR bed at one end: the channel beyond the end when it reads at least
    CHANNEL_MIN_DROP_M (~0.5 ft) below the disc at the end (the disc is on the embankment's
    toe slope), else the disc. 3.11.1: channel_bed is carried back to the end along the
    channel's slope, so a smaller drop is noise, not the channel's own fall."""
    if disc is None:
        return channel
    if channel is None or disc - channel < CHANNEL_MIN_DROP_M:
        return disc
    return channel


def reconcile_inverts(structures, read_dem, tol_m, cell):
    """3.5: inventory inlet / outlet elevations (NDDOT pipes, a user's layer) against the LiDAR.

    The models only ever LOWER ground to an invert, so an inventory elevation that is wrong
    against the DEM trenches the stream (too low) or leaves the embankment uncut (too high).
    For each structure whose inverts were given:
      * both ends within `tol_m` of the LiDAR bed -> the inventory inverts are used as given
        (slope and any perch survive);
      * both ends off by about the same amount (within 1 ft of each other) -> a datum shift:
        both moved by the mean offset;
      * otherwise -> the LiDAR bed at both ends (the inventory values kept for the report).
    A cover check notes a rise that does not fit under the road crown in the LiDAR.
    3.10.96: the LiDAR bed at an end is the ground near the end (lidar_bed) unless the channel
    beyond it (channel_bed, CHANNEL_REACH_M out on ground read at CHANNEL_CELL_M) reads
    CHANNEL_MIN_DROP_M lower (end_bed): near the end the disc can read the embankment's toe
    slope, not the channel (DWR-3034).
    s.invert_channel_bed keeps the bed used.
    `read_dem(bounds, cell)` returns a Grid. Returns the notes; the structures are updated in
    place (invert_inventory keeps the given values)."""
    notes = []
    for s in structures:
        if s.invert_up_m is None and s.invert_dn_m is None:
            continue
        line = np.asarray(s.line, dtype=float)
        if len(line) < 2:
            continue
        s.invert_inventory = (s.invert_up_m, s.invert_dn_m)
        half = 0.5 * s.corridor_width(cell)
        r = 2.0 * half + cell
        pad = r + 2.0 * cell
        bounds = (min(line[:, 0]) - pad, min(line[:, 1]) - pad, max(line[:, 0]) + pad, max(line[:, 1]) + pad)
        try:
            g = read_dem(bounds, cell)
        except Exception as e:                                   # noqa
            s.invert_note = "inventory inverts kept unchecked (no DEM here: {})".format(e)
            notes.append("{}: {}".format(s.name or s.kind, s.invert_note))
            continue
        bed_up, bed_dn = lidar_bed(g, line[0], r), lidar_bed(g, line[-1], r)
        ch_up = ch_dn = None
        try:                                    # 3.10.96: the channel beyond each end
            fc = min(float(cell), CHANNEL_CELL_M)
            cp = CHANNEL_REACH_M + half + 2.0 * fc
            gf = read_dem((min(line[:, 0]) - cp, min(line[:, 1]) - cp,
                           max(line[:, 0]) + cp, max(line[:, 1]) + cp), fc)
            ch_up, ch_dn = channel_bed(gf, line, 0, half), channel_bed(gf, line, -1, half)
        except Exception:                                        # noqa - the disc only
            pass
        bed_up, bed_dn = end_bed(bed_up, ch_up), end_bed(bed_dn, ch_dn)
        s.invert_channel_bed = (bed_up, bed_dn)
        inv_up, inv_dn = s.invert_up_m, s.invert_dn_m
        off_up = (inv_up - bed_up) if (inv_up is not None and bed_up is not None) else None
        off_dn = (inv_dn - bed_dn) if (inv_dn is not None and bed_dn is not None) else None
        offs = [o for o in (off_up, off_dn) if o is not None]
        name = s.name or s.kind

        def ft(v):
            return "{:+.1f} ft".format(v / FT)
        if not offs:
            s.invert_note = "inventory inverts kept unchecked (no DEM at the ends)"
        elif all(abs(o) <= tol_m for o in offs):
            s.invert_note = "inventory inverts used ({} / {} from the LiDAR channel bed, within {:.1f} ft)".format(
                ft(off_up) if off_up is not None else "n/a", ft(off_dn) if off_dn is not None else "n/a", tol_m / FT)
            if inv_up is None and bed_up is not None:
                s.invert_up_m = bed_up + (off_dn or 0.0)
            if inv_dn is None and bed_dn is not None:
                s.invert_dn_m = bed_dn + (off_up or 0.0)
        elif len(offs) == 2 and abs(off_up - off_dn) <= 1.0 * FT:
            shift = 0.5 * (off_up + off_dn)
            s.invert_up_m, s.invert_dn_m = inv_up - shift, inv_dn - shift
            s.invert_note = ("inventory elevations {} from the LiDAR at both ends: taken as a datum shift, "
                             "moved to the LiDAR (slope kept)").format(ft(shift))
        else:
            s.invert_up_m, s.invert_dn_m = None, None
            s.invert_note = ("inventory elevations disagree with the LiDAR ({} / {}): LiDAR inverts used").format(
                ft(off_up) if off_up is not None else "n/a", ft(off_dn) if off_dn is not None else "n/a")
        # cover (culverts): does the rise fit under the road crown in the LiDAR?
        crown = None
        if s.kind == KIND_CULVERT:
            try:
                X, Y = np.meshgrid(g.x_centers(), g.y_centers())
                z = g.masked()
                d, _sv = cond.polyline_distance(X, Y, line)
                corr = (d <= half) & np.isfinite(z)
                crown = float(np.nanmax(z[corr])) if corr.any() else None
            except Exception:                                    # noqa
                crown = None
        used = [v for v in (s.invert_up_m, s.invert_dn_m) if v is not None]
        if used and crown is not None:
            top = max(used) + s.height_m
            if top > crown + 0.3:
                s.invert_note += "; the {:.1f} ft rise does not fit under the road crown in the LiDAR " \
                                 "(cover {:+.1f} ft): check the size or the elevations".format(
                                     s.height_m / FT, (crown - top) / FT)
        notes.append("{}: {}".format(name, s.invert_note))
    return notes


def describe(structures):
    """One line per structure for the run report."""
    rows = []
    for s in structures:
        tail = ""
        if getattr(s, "surveyed", True) is False:
            tail = "; not surveyed (a guess, not a measured culvert)"
        if s.kind == KIND_CULVERT:
            rows.append("{} (culvert, {} x {:.2f} x {:.2f} m {}): LISFLOOD-FP open cut; ANUGA "
                        "culvert operator{}{}".format(s.name or "?", s.barrels, s.width_m,
                                                    s.height_m, s.shape,
                                                    "; " + s.invert_note if s.invert_note else "",
                                                    tail))
        else:
            rows.append("{} ({}, {:.1f} m span): cut through the embankment in both models{}{}".format(
                s.name or "?", s.kind, s.width_m,
                "; " + s.invert_note if s.invert_note else "", tail))
    return rows
