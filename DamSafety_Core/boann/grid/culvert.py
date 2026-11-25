"""BoannGrid culvert ratings: FHWA HDS-5 inlet- and outlet-control head-discharge hydraulics, as a
suite-callable calculator. Today this math only runs inside the ANUGA Boyd operator and HEC-RAS;
the conditioner needs it to turn a reconnected road/rail crossing into a head->Q rating a flood
engine can use as an internal boundary.

Scope (honest): the governing culvert headwater is max(inlet control, outlet control).
  * Submerged inlet control - the closed HDS-5 form HW/D = c*(Q/(A*sqrt(D)))^2 + Y - 0.5*S - is
    exact for the regime that matters in a dam-breach (the culvert is overwhelmed, inlet
    submerged). Unsubmerged (low-flow) inlet control is NOT modelled here; this is the high-flow
    calculator.
  * Outlet control - full-barrel energy: H = (1 + ke + 29 n^2 L / R^1.33) * V^2/2g, with the
    tailwater or barrel full as the outlet control - is the standard HDS-5 outlet-control head.
HDS-5 is public domain; HY-8 is the external verification oracle for the coefficient calibration
(a future cross-check). The equations are evaluated in English units (HDS-5's native units) with SI
in and out, so the published coefficients apply unchanged.

Coefficient discipline (nothing silent): the PRESET coefficients below are the published HDS-5
Appendix A values, but every preset carries its printed-table `source` and ships `verified=False`.
The head/discharge math is pure physics and runs regardless (for research / PROVISIONAL use), but an
emitted rating - the thing a flood engine would consume as an internal boundary - is stamped
PROVISIONAL until a caller has checked the coefficients against the printed FHWA tables and built
the culvert with `verified=True`. After the Froehlich/Xu-Zhang breach-regression lessons, a
hazard-affecting rating never claims PRODUCTION on constants nobody confirmed against the book.

    from DamSafety_Core.boann.grid import culvert as cv
    c = cv.Culvert.preset("circular_concrete_groove", diameter_m=1.5, length_m=20, slope=0.01)
    hw_m = c.headwater(q_cms=8.0, tailwater_m=0.0)       # governing headwater depth (m) - pure math
    rated = c.emit([1, 2, 4, 8, 16], tailwater_m=0.0)     # {"status": "PROVISIONAL", "table": [...]}
    # after checking the printed HDS-5 chart for this inlet:
    cv.Culvert.preset("circular_concrete_groove", 1.5, 20, 0.01, verified=True).emit([...])  # PRODUCTION
"""
import math
from dataclasses import dataclass

FT = 0.3048                 # m per ft
CFS = 0.0283168            # m^3/s per cfs
G = 32.174                 # ft/s^2

# HDS-5 inlet-control (submerged) coefficients c, Y and entrance loss ke, by shape + edge.
# Published HDS-5 (3rd ed.) Appendix A / HY-8 values (English units); HY-8 is the verification
# oracle. `source` names the printed configuration to confirm each set against - the exact chart /
# scale number is left for that human check rather than asserted here from memory.
PRESETS = {
    # circular concrete
    "circular_concrete_groove": dict(shape="circular", c=0.0292, Y=0.74, ke=0.2, n=0.012,
        source="HDS-5 App. A, circular concrete pipe, groove end w/ headwall (inlet-control c, Y)"),
    "circular_concrete_square": dict(shape="circular", c=0.0398, Y=0.67, ke=0.5, n=0.012,
        source="HDS-5 App. A, circular concrete pipe, square edge w/ headwall"),
    # circular corrugated metal pipe
    "circular_cmp_headwall": dict(shape="circular", c=0.0379, Y=0.69, ke=0.5, n=0.024,
        source="HDS-5 App. A, circular corrugated metal pipe, headwall"),
    "circular_cmp_projecting": dict(shape="circular", c=0.0553, Y=0.54, ke=0.9, n=0.024,
        source="HDS-5 App. A, circular corrugated metal pipe, projecting (thin-wall) entrance"),
    # concrete box, 90-deg headwall
    "box_concrete_headwall": dict(shape="box", c=0.0385, Y=0.81, ke=0.5, n=0.012,
        source="HDS-5 App. A, concrete box, 90-deg headwall"),
}


@dataclass
class Culvert:
    shape: str                  # "circular" or "box"
    diameter_m: float           # circular: diameter; box: rise (height)
    length_m: float
    slope: float                # barrel slope S0 (rise/run)
    c: float                    # HDS-5 submerged inlet coefficient
    Y: float                    # HDS-5 submerged inlet coefficient
    ke: float                   # entrance loss coefficient
    n: float                    # Manning's n of the barrel
    span_m: float = None        # box only: width; None => circular
    source: str = ""            # printed-table provenance of the inlet coefficients
    verified: bool = False      # True only once a human checked the coefficients vs the HDS-5 book

    @classmethod
    def preset(cls, name, diameter_m, length_m, slope=0.0, span_m=None, n=None, verified=False):
        """Build from a named HDS-5 preset. `verified=True` asserts the caller has confirmed this
        preset's coefficients against the printed HDS-5 chart (see PRESETS[...]['source']); it is
        what lets an emitted rating reach PRODUCTION status."""
        p = PRESETS[name]
        return cls(shape=p["shape"], diameter_m=diameter_m, length_m=length_m, slope=slope,
                   c=p["c"], Y=p["Y"], ke=p["ke"], n=n if n is not None else p["n"],
                   span_m=span_m if p["shape"] == "box" else None,
                   source=p["source"], verified=bool(verified))

    # -- barrel geometry in English units (full-flow) --
    def _area_ft2(self):
        d = self.diameter_m / FT
        if self.shape == "box":
            b = (self.span_m if self.span_m else self.diameter_m) / FT
            return b * d
        return math.pi * d * d / 4.0

    def _hyd_radius_ft(self):
        d = self.diameter_m / FT
        if self.shape == "box":
            b = (self.span_m if self.span_m else self.diameter_m) / FT
            return (b * d) / (2.0 * (b + d))
        return d / 4.0

    # -- the two controls, returning headwater depth above the inlet invert (ft) --
    def _inlet_ft(self, q_cfs):
        d = self.diameter_m / FT
        a = self._area_ft2()
        hw_d = self.c * (q_cfs / (a * math.sqrt(d))) ** 2 + self.Y - 0.5 * self.slope
        return hw_d * d

    def _outlet_ft(self, q_cfs, tw_ft):
        d = self.diameter_m / FT
        a = self._area_ft2()
        r = self._hyd_radius_ft()
        v = q_cfs / a
        H = (1.0 + self.ke + 29.0 * self.n ** 2 * (self.length_m / FT) / r ** (4.0 / 3.0)) \
            * v * v / (2.0 * G)
        ho = max(tw_ft, d)                               # full-barrel / tailwater outlet control
        return H + ho - self.slope * (self.length_m / FT)

    def headwater(self, q_cms, tailwater_m=0.0):
        """Governing headwater depth (m) above the inlet invert for discharge `q_cms` (m^3/s) and
        a tailwater depth above the outlet invert. max(inlet control, outlet control)."""
        if q_cms <= 0:
            return 0.0
        q_cfs = q_cms / CFS
        tw_ft = tailwater_m / FT
        hw_ft = max(self._inlet_ft(q_cfs), self._outlet_ft(q_cfs, tw_ft))
        return hw_ft * FT

    def control(self, q_cms, tailwater_m=0.0):
        """Which control governs at `q_cms`: 'inlet' or 'outlet'."""
        q_cfs = q_cms / CFS
        return ("inlet" if self._inlet_ft(q_cfs) >= self._outlet_ft(q_cfs, tailwater_m / FT)
                else "outlet")

    def rating(self, q_list_cms, tailwater_m=0.0):
        """Head-discharge rating: [(Q m^3/s, HW m, control), ...] for each Q in `q_list_cms`. Pure
        physics (no status stamp); use `emit` for a rating a downstream consumer should trust."""
        return [(float(q), self.headwater(q, tailwater_m), self.control(q, tailwater_m))
                for q in q_list_cms]

    def status(self):
        """'PRODUCTION' once the coefficients have been confirmed against the printed HDS-5 chart
        (verified=True), else 'PROVISIONAL'. An unverified rating is never production-ready."""
        return "PRODUCTION" if self.verified else "PROVISIONAL"

    def emit(self, q_list_cms, tailwater_m=0.0):
        """A rating stamped for downstream use (e.g. a flood engine's internal boundary): the
        head->Q table plus its PROVISIONAL/PRODUCTION status and the coefficient provenance, so a
        consumer can see whether the constants were ever checked against the book."""
        return {
            "status": self.status(),
            "coefficients": {"c": self.c, "Y": self.Y, "ke": self.ke, "n": self.n,
                             "source": self.source, "verified": self.verified},
            "table": [{"Q_m3s": float(q), "HW_m": self.headwater(q, tailwater_m),
                       "control": self.control(q, tailwater_m)} for q in q_list_cms],
        }

    def discharge(self, headwater_m, tailwater_m=0.0, q_hi_cms=1.0e5):
        """Invert the rating: the discharge (m^3/s) that produces `headwater_m`, by bisection.
        The governing headwater is monotone increasing in Q, so bisection is exact."""
        if headwater_m <= self.headwater(0.0, tailwater_m):
            return 0.0
        lo, hi = 0.0, q_hi_cms
        if self.headwater(hi, tailwater_m) < headwater_m:
            return hi
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            if self.headwater(mid, tailwater_m) < headwater_m:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)
