"""Readers and writers of the LISFLOOD-FP input deck and result grids for the GPU engine: a copy of
the ones in core/builtin_lisflood.py (Runner 3.20.15), so this package stands alone and the same
files can be used by the Screening plugin and DamIT, which have no builtin_lisflood.py.
tests/test_metalflood.py checks they still read and write as the originals do."""
import os

import numpy as np

G = 9.80665
ALPHA = 0.7
DRY_M = 1e-3
MAX_HFLOW = 10.0       # LISFLOOD-FP's MaxHflow
VEL_MIN_DEPTH_M = 0.01
NODATA = -9999.0


class EngineError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


# -- input files ---------------------------------------------------------------------------

# what a cut-short or damaged input file raises while it is parsed
_INPUT_FAULTS = (ValueError, IndexError, KeyError, TypeError, OverflowError, MemoryError)


def _load(what, reader, path, *args):
    """reader(path, *args), with a damaged or truncated file reported as an EngineError that
    names the file (a raw IndexError or ValueError used to reach the user)."""
    try:
        return reader(path, *args)
    except EngineError:
        raise
    except _INPUT_FAULTS as e:
        detail = str(e) or type(e).__name__
        if isinstance(e, KeyError):
            detail = "no {} line".format(e)
        raise EngineError("the {} {} is truncated or damaged ({}); write the model files "
                          "again".format(what, os.path.basename(path), detail[:200])) from None


def _read_asc(path, shape=None):
    hdr = {}
    with open(path) as f:
        for _ in range(6):
            pos = f.tell()
            parts = f.readline().split()
            if len(parts) != 2 or parts[0][0].isdigit() or parts[0][0] == "-":
                f.seek(pos)
                break
            hdr[parts[0].lower()] = float(parts[1])
        data = np.loadtxt(f, ndmin=2)
    ny, nx = int(hdr["nrows"]), int(hdr["ncols"])
    dx = hdr["cellsize"]
    if ny <= 0 or nx <= 0 or not (np.isfinite(dx) and dx > 0):
        raise ValueError("the header gives {} rows, {} columns and cell size {}".format(
            ny, nx, dx))
    if data.size != ny * nx:
        raise ValueError("it holds {:,} values but its header says {:,} x {:,}".format(
            data.size, ny, nx))
    if shape is not None and (ny, nx) != tuple(shape):
        raise ValueError("it is {} x {} cells but the DEM is {} x {}".format(
            ny, nx, shape[0], shape[1]))
    xll = hdr.get("xllcorner", hdr.get("xllcenter", 0.0) - 0.5 * dx)
    yll = hdr.get("yllcorner", hdr.get("yllcenter", 0.0) - 0.5 * dx)
    return data.reshape(ny, nx), xll, yll, dx, hdr.get("nodata_value", NODATA)


def _write_asc(path, a, xll, yll, dx, fmt="%.3f"):
    header = ("ncols {}\nnrows {}\nxllcorner {:.6f}\nyllcorner {:.6f}\ncellsize {:.6f}\n"
              "NODATA_value {:g}".format(a.shape[1], a.shape[0], xll, yll, dx, NODATA))
    np.savetxt(path, a, fmt=fmt, header=header, comments="")


def read_par(path):
    """{keyword: value or True} from a LISFLOOD-FP .par file."""
    par = {}
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            p = line.split(None, 1)
            par[p[0].lower()] = p[1].strip() if len(p) > 1 else True
    return par


def read_bdy(path):
    """{name: (t_s, value)} from a .bdy file (values per unit width for QVAR)."""
    out = {}
    with open(path) as f:
        lines = [ln.strip() for ln in f]
    i = 1                                               # line 1 is a comment
    while i < len(lines):
        name = lines[i]
        i += 1
        if not name or i >= len(lines):
            continue
        p = lines[i].split()
        i += 1
        n, unit = int(float(p[0])), (p[1].lower() if len(p) > 1 else "seconds")
        scale = {"hours": 3600.0, "days": 86400.0}.get(unit, 1.0)
        vals, ts = [], []
        for _ in range(n):
            v, t = lines[i].split()[:2]
            vals.append(float(v))
            ts.append(float(t) * scale)
            i += 1
        if not vals:
            raise ValueError("series '{}' has no values".format(name))
        out[name] = (np.asarray(ts), np.asarray(vals))
    return out


def read_rain(path):
    """(t_s, rate m/s) from a LISFLOOD-FP .rain file (the "rainfall" keyword): line 1 a comment,
    then "<count> <units>" (seconds / minutes / hours / days), then <count> lines "<mm/hr> <time>",
    as LISFLOOD-FP 8.3's LoadRain / LoadTimeSeries. One rate over the whole grid, linear between
    the times. A negative rate (LISFLOOD-FP's old evaporation trick) is refused."""
    with open(path) as f:
        lines = [ln.strip() for ln in f]
    i = 1                                               # line 1 is a comment
    while i < len(lines) and not lines[i]:
        i += 1
    p = lines[i].split()
    n, unit = int(float(p[0])), (p[1].lower() if len(p) > 1 else "seconds")
    scale = {"minutes": 60.0, "hours": 3600.0, "days": 86400.0}.get(unit, 1.0)
    vals, ts = [], []
    for ln in lines[i + 1:]:
        if len(vals) == n:
            break
        if not ln:
            continue
        v, t = ln.split()[:2]
        vals.append(float(v))
        ts.append(float(t) * scale)
    if len(vals) < n or not vals:
        raise ValueError("the rainfall file has {} of the {} values it announces".format(len(vals), n))
    rate = np.asarray(vals) / (1000.0 * 3600.0)        # mm/hr -> m/s
    if (rate < 0).any():
        raise ValueError("negative rainfall rates (evaporation) are not supported")
    return np.asarray(ts), rate


def read_bci(path):
    """(point sources [(x, y, name)], free edges {'N','S','E','W'})."""
    pts, free = [], set()
    with open(path) as f:
        for line in f:
            p = line.split()
            if not p:
                continue
            k = p[0].upper()
            if k == "P" and len(p) >= 5 and p[3].upper() == "QVAR":
                pts.append((float(p[1]), float(p[2]), p[4]))
            elif k in ("N", "S", "E", "W") and len(p) >= 4 and p[3].upper() == "FREE":
                free.add(k)
    return pts, free


def read_gauges(path):
    with open(path) as f:
        n = int(f.readline().split()[0])
        out = []
        for _ in range(n):
            p = f.readline().split()
            out.append((float(p[0]), float(p[1]), p[2].upper(), float(p[3])))
    return out


# -- the model -----------------------------------------------------------------------------
