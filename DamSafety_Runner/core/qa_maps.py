"""QA maps written after every run into the dam's output folder:

  qa_inflow_map.png  hillshade + contours of the DEM near the dam, the crest line, the breach
                     (dam) point, the breach inflow point, the inflow search circle and the
                     downstream trace; the title gives the dam ID and the inflow elevation
                     against the top of dam.
  qa_depth_map.png   the maximum depth (ft) over a hillshade, with the trace and the dam.

QA_MAPS setting: "off" | "inflow" | "inflow+depth" (default). matplotlib (in the QGIS python)
draws them; without it a plain Qt image is drawn (hillshade and symbols, no contours). A map
never fails the run: write_qa_maps() catches everything and returns the problem as text."""
import math
import os
import time

import numpy as np

QA_OFF, QA_INFLOW, QA_BOTH = "off", "inflow", "inflow+depth"
QA_MODES = (QA_OFF, QA_INFLOW, QA_BOTH)
QA_LABELS = ("Off", "Inflow map only", "Inflow map and depth map")
DEFAULT_QA_MAPS = QA_BOTH
INFLOW_PNG = "qa_inflow_map.png"
DEPTH_PNG = "qa_depth_map.png"
FT = 0.3048


def normalize(mode):
    """A QA_MAPS value (name, label, index, bool or None) as one of QA_MODES."""
    if mode is None or mode == "":
        return DEFAULT_QA_MAPS
    if mode is True:
        return QA_BOTH
    if mode is False:
        return QA_OFF
    if isinstance(mode, (int, np.integer)) and not isinstance(mode, bool):
        if 0 <= int(mode) < len(QA_MODES):
            return QA_MODES[int(mode)]
        raise ValueError("QA_MAPS index {} is not one of 0-{}".format(mode, len(QA_MODES) - 1))
    m = str(mode).strip().lower().replace(" ", "")
    for name, label in zip(QA_MODES, QA_LABELS):
        if m in (name.replace(" ", ""), label.lower().replace(" ", "")):
            return name
    if m in ("none", "no", "false", "0"):
        return QA_OFF
    if m in ("both", "all", "depth", "yes", "true", "1"):
        return QA_BOTH
    raise ValueError("QA_MAPS must be one of {} (got {!r})".format(", ".join(QA_MODES), mode))


def _hillshade(z, cell):
    z = np.where(np.isfinite(z), z, np.nanmean(z) if np.isfinite(z).any() else 0.0)
    gy, gx = np.gradient(z, cell)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = math.radians(315.0), math.radians(45.0)
    hs = math.sin(alt) * np.cos(slope) + math.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(hs, 0, 1)


def _window(grid, centre, half):
    """(sub-array, extent) of `grid` within `half` m of `centre` (clipped to the grid)."""
    r0, c0 = grid.rc(centre[0] - half, centre[1] + half)
    r1, c1 = grid.rc(centre[0] + half, centre[1] - half)
    r0, c0 = max(r0, 0), max(c0, 0)
    r1, c1 = min(r1 + 1, grid.ny), min(c1 + 1, grid.nx)
    if r1 - r0 < 2 or c1 - c0 < 2:
        r0, c0, r1, c1 = 0, 0, grid.ny, grid.nx
    a = grid.masked()[r0:r1, c0:c1]
    x0 = grid.x0 + c0 * grid.dx
    y0 = grid.y0 - r0 * grid.dx
    return a, (x0, x0 + (c1 - c0) * grid.dx, y0 - (r1 - r0) * grid.dx, y0)


def _plt():
    import matplotlib
    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    return plt


def inflow_title(dam_id, inflow_z, top_m):
    if inflow_z is None or top_m is None or not np.isfinite(inflow_z):
        return "{}: breach inflow (elevation not known)".format(dam_id)
    return "{}: inflow at {:.1f} ft, {:.1f} ft below the top of dam ({:.1f} ft)".format(
        dam_id, inflow_z / FT, (top_m - inflow_z) / FT, top_m / FT)


def draw_inflow_map(path, grid, dam_id, dam_xy, inflow_xy, top_m, radius_m=None, trace=None,
                    crest_line=None, breach_xy=None):
    """qa_inflow_map.png. `grid` is a core.grid.Grid of terrain (m) around the dam."""
    half = max(3.0 * (radius_m or 90.0), 300.0)
    a, ext = _window(grid, dam_xy, half)
    r, c = grid.rc(*inflow_xy) if inflow_xy else (-1, -1)
    z = grid.masked()
    inflow_z = float(z[r, c]) if grid.inside(r, c) else None
    title = inflow_title(dam_id, inflow_z, top_m)
    try:
        plt = _plt()
    except Exception:                        # noqa: BLE001 - no matplotlib: plain Qt image
        return _qt_map(path, a, ext, grid.dx, title, dam_xy, inflow_xy, trace, None)
    fig, ax = plt.subplots(figsize=(8, 8))
    try:
        ax.imshow(_hillshade(a, grid.dx), cmap="gray", extent=ext)
        im = ax.imshow(a, cmap="terrain", alpha=0.5, extent=ext)
        if np.isfinite(a).sum() > 4 and np.nanmax(a) > np.nanmin(a):
            lo, hi = np.nanmin(a), np.nanmax(a)
            step = max(0.5, round((hi - lo) / 12.0, 1))
            X = np.linspace(ext[0], ext[1], a.shape[1])
            Y = np.linspace(ext[3], ext[2], a.shape[0])
            cs = ax.contour(X, Y, a, levels=np.arange(math.floor(lo), hi + step, step),
                            colors="k", linewidths=0.4)
            ax.clabel(cs, fontsize=6, fmt="%.0f")
        if crest_line:
            xy = np.asarray(crest_line, dtype=float)
            ax.plot(xy[:, 0], xy[:, 1], "-", color="saddlebrown", lw=3, label="crest line")
        if trace is not None and len(trace) > 1:
            t = np.asarray(trace, dtype=float)
            ax.plot(t[:, 0], t[:, 1], "b-", lw=1.5, label="downstream trace")
        if radius_m:
            ax.add_patch(plt.Circle(dam_xy, radius_m, fill=False, ls=":", color="k",
                                    label="inflow search ({:.0f} m)".format(radius_m)))
        if breach_xy is not None and tuple(breach_xy) != tuple(dam_xy):
            ax.plot(*breach_xy, "kx", ms=10, label="dam point given")
        ax.plot(*dam_xy, "k*", ms=14, mec="w", label="breach (dam) point")
        if inflow_xy:
            ax.plot(*inflow_xy, "ro", ms=11, mec="w", label="breach inflow point")
        ax.set_xlim(ext[0], ext[1])
        ax.set_ylim(ext[2], ext[3])
        ax.ticklabel_format(useOffset=False, style="plain")
        ax.set_title(title, fontsize=10)
        ax.legend(fontsize=7, loc="lower left", framealpha=0.85)
        fig.colorbar(im, ax=ax, shrink=0.6, label="elevation (m)")
        fig.savefig(path, dpi=100, bbox_inches="tight")
    finally:
        plt.close(fig)
    return path


def draw_depth_map(path, grid, depth_m, dam_id, dam_xy, inflow_xy=None, trace=None):
    """qa_depth_map.png: the max depth (ft) of the final model grid over its hillshade."""
    d = np.asarray(depth_m, dtype=float) / FT
    wet = np.isfinite(d) & (d > 0.1)
    z = grid.masked()
    ext = (grid.x0, grid.x0 + grid.nx * grid.dx, grid.y0 - grid.ny * grid.dx, grid.y0)
    acres = wet.sum() * grid.dx * grid.dx / 4046.86
    title = "{}: max depth (ft), {:,.0f} ac over 0.1 ft, max {:.2f} ft".format(
        dam_id, acres, float(np.nanmax(d)) if wet.any() else 0.0)
    try:
        plt = _plt()
    except Exception:                        # noqa: BLE001
        return _qt_map(path, z, ext, grid.dx, title, dam_xy, inflow_xy, trace, np.where(wet, d, np.nan))
    fig, ax = plt.subplots(figsize=(9, 8))
    try:
        ax.imshow(_hillshade(z, grid.dx), cmap="gray", extent=ext)
        im = ax.imshow(np.where(wet, d, np.nan), cmap="Blues", extent=ext, vmin=0,
                       vmax=max(1.0, float(np.nanpercentile(d[wet], 98))) if wet.any() else 1.0)
        if trace is not None and len(trace) > 1:
            t = np.asarray(trace, dtype=float)
            ax.plot(t[:, 0], t[:, 1], "r-", lw=0.8, label="downstream trace")
        ax.plot(*dam_xy, "k*", ms=12, mec="w", label="dam")
        if wet.any():
            rr, cc = np.where(wet)
            pad = 10 * grid.dx
            ax.set_xlim(max(ext[0], grid.x0 + cc.min() * grid.dx - pad),
                        min(ext[1], grid.x0 + (cc.max() + 1) * grid.dx + pad))
            ax.set_ylim(max(ext[2], grid.y0 - (rr.max() + 1) * grid.dx - pad),
                        min(ext[3], grid.y0 - rr.min() * grid.dx + pad))
        ax.ticklabel_format(useOffset=False, style="plain")
        ax.set_title(title, fontsize=10)
        ax.legend(fontsize=7, loc="lower left")
        fig.colorbar(im, ax=ax, shrink=0.6, label="max depth (ft)")
        fig.savefig(path, dpi=90, bbox_inches="tight")
    finally:
        plt.close(fig)
    return path


def _qt_map(path, a, ext, cell, title, dam_xy, inflow_xy, trace, depth_ft):
    """Fallback without matplotlib: a Qt image (hillshade, depth in blue, symbols)."""
    from qgis.PyQt.QtGui import QImage, QPainter, QColor, QPen
    from qgis.PyQt.QtCore import QPointF
    hs = (_hillshade(a, cell) * 255).astype(np.uint8)
    h, w = hs.shape
    img = QImage(w, h + 20, QImage.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    for r in range(h):
        for c in range(w):
            v = int(hs[r, c])
            if depth_ft is not None and np.isfinite(depth_ft[r, c]):
                img.setPixel(c, r + 20, QColor(max(0, v - 120), max(0, v - 60), 255).rgb())
            else:
                img.setPixel(c, r + 20, QColor(v, v, v).rgb())
    p = QPainter(img)
    try:
        def px(x, y):
            return QPointF((x - ext[0]) / (ext[1] - ext[0]) * w, 20 + (ext[3] - y) / (ext[3] - ext[2]) * h)
        if trace is not None and len(trace) > 1:
            p.setPen(QPen(QColor(0, 0, 255), 2))
            pts = [px(*t) for t in trace]
            for i in range(len(pts) - 1):
                p.drawLine(pts[i], pts[i + 1])
        p.setPen(QPen(QColor(0, 0, 0), 2))
        p.drawEllipse(px(*dam_xy), 5, 5)
        if inflow_xy:
            p.setPen(QPen(QColor(255, 0, 0), 3))
            p.drawEllipse(px(*inflow_xy), 5, 5)
        p.setPen(QPen(QColor(0, 0, 0)))
        p.drawText(4, 14, title)
    finally:
        p.end()
    if not img.save(path, "PNG"):
        raise OSError("could not save {}".format(path))
    return path


def write_qa_maps(mode, out_folder, dam_id, terrain_grid, dam_xy, inflow_xy, top_m, radius_m=None,
                  trace=None, crest_line=None, breach_xy=None, model_grid=None, depth_m=None,
                  log=None):
    """Write the QA maps `mode` asks for into `out_folder`. Never raises.
    Returns ({"inflow_map": path, "depth_map": path, "seconds": s, "mode": mode}, [problems])."""
    out, problems = {}, []
    t0 = time.time()
    try:
        mode = normalize(mode)
    except ValueError as e:
        return {"mode": str(mode)}, [str(e)]
    out["mode"] = mode
    if mode == QA_OFF:
        return out, problems
    try:
        os.makedirs(out_folder, exist_ok=True)
    except OSError as e:
        return out, ["QA maps not written: {}".format(e)]
    jobs = [("inflow_map", INFLOW_PNG, lambda p: draw_inflow_map(
        p, terrain_grid, dam_id, dam_xy, inflow_xy, top_m, radius_m, trace, crest_line, breach_xy),
        terrain_grid is not None)]
    if mode == QA_BOTH:
        jobs.append(("depth_map", DEPTH_PNG, lambda p: draw_depth_map(
            p, model_grid, depth_m, dam_id, dam_xy, inflow_xy, trace),
            model_grid is not None and depth_m is not None))
    for key, name, fn, ok in jobs:
        if not ok:
            problems.append("QA {} not written: no data for it".format(key.replace("_", " ")))
            continue
        try:
            out[key] = fn(os.path.join(out_folder, name))
        except Exception as e:  # noqa: BLE001 - a QA map never fails the run
            problems.append("QA {} not written: {}: {}".format(key.replace("_", " "), type(e).__name__, e))
    out["seconds"] = round(time.time() - t0, 2)
    if log:
        for p in problems:
            log(p)
    return out, problems
