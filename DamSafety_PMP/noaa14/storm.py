"""One storm builder for the Atlas 14 hyetograph and rain-on-grid tools: temporal pattern, then losses (pure Python).

Order: gross depth (Atlas 14, alternating block or a temporal curve) -> optional rescale to an Atlas 14 grid (raingrid) ->
initial loss -> constant loss -> excess rain. Losses are applied per time step on the incremental depth:

* initial loss (in or mm): the first ``initial`` of rain is absorbed before any runoff (the common "initial and constant"
  loss method; initial abstraction);
* constant loss rate (in/hr or mm/hr): once the initial loss is met, at most ``rate * step`` is lost in every step;
  a step with less rain than that loses all of it.
Both default to 0, which leaves the gross storm unchanged (the output is then identical to a run without losses).
"""
from . import atlas14 as A
from . import temporal as T

UNITS = ("in", "mm")
MM_PER_IN = 25.4


class LossError(A.Atlas14Error):
    pass


def apply_losses(rows, initial=0.0, constant_rate=0.0, step_min=None):
    """rows [(t_end, incremental, cumulative)] (any depth unit) -> [(t_end, gross, loss, excess)] in the same unit.
    ``initial`` is a depth, ``constant_rate`` a depth per hour in that unit."""
    if initial < 0 or constant_rate < 0:
        raise LossError("losses cannot be negative")
    out, ia_left, t_prev = [], float(initial), 0.0
    for t_end, gross, _ in rows:
        dt_h = ((t_end - t_prev) if step_min is None else step_min) / 60.0
        t_prev = t_end
        rain = float(gross)
        ia = min(ia_left, rain)
        ia_left -= ia
        rain -= ia
        cl = min(constant_rate * dt_h, rain) if ia_left <= 1e-12 else 0.0
        loss = ia + cl
        out.append((t_end, round(gross, 4), round(loss, 4), round(gross - loss, 4)))
    return out


def loss_summary(table, unit):
    g = sum(r[1] for r in table)
    lo = sum(r[2] for r in table)
    return {"gross_" + unit: round(g, 4), "loss_" + unit: round(lo, 4), "excess_" + unit: round(g - lo, 4),
            "runoff_fraction": round((g - lo) / g, 4) if g else None}


def to_unit(value, from_unit, to_unit):
    if from_unit == to_unit:
        return value
    return value * MM_PER_IN if to_unit == "mm" else value / MM_PER_IN


def build(pf, years, storm_min, step_min, pattern="alternating", label=None, curve=None):
    """Gross storm rows [(t_end, inc, cum)] (the Atlas 14 unit of ``pf``). ``curve`` = (hours, fraction) for the
    non-alternating patterns (see temporal.py). Validates the storm / step and the pattern's duration."""
    A.check_storm(storm_min, step_min, label)
    T.check_pattern(pattern, storm_min, label)
    if pattern == "alternating":
        return A.alternating_block(pf, years, storm_min, step_min)
    if curve is None:
        raise T.TemporalError("pattern {} needs its curve".format(pattern))
    total = A.depth_at(pf, storm_min, years)
    return T.rows_from_curve(curve[0], curve[1], total, storm_min, step_min)
