"""What a host plugin lends Boann, by name (DamSafety Core 1.7.0 / Boann 6.3.1).

Until Boann moved into DamSafety Core, two places reached up into the Runner by relative import:
engine_sgc built channels from the DEM with the Runner's core/sgc + core/grid, and BoannGrid's
`verify` command checked its fill against the Runner's core/terrain.priority_flood. From Core
those imports point nowhere, so the Runner now hands the two functions in here instead
(DamSafety_Runner/core/__init__.py registers them, lazily). Without a host they are absent and
Boann behaves as before outside the Runner: the SGC engine builds no channels from the DEM (plain
ACC where there are no channel rasters) and `verify` skips the outside oracle.

    channel_builder(z, xll, yll, dx, nodata, inflow_rc, channel_n)
        -> (width, depth, start_depth, channel_n), arrays shaped like z
    priority_flood(dem) -> filled dem (NaN = outlet)

Standard library only, like the rest of this folder."""

_HOOKS = {}
NAMES = ("channel_builder", "priority_flood")


def register(name, fn):
    """Lend Boann `fn` under `name` (one of NAMES); a later call replaces it."""
    if name not in NAMES:
        raise ValueError("unknown Boann host hook {!r} (known: {})".format(name, ", ".join(NAMES)))
    _HOOKS[name] = fn


def get(name):
    """The function registered under `name`, or None."""
    return _HOOKS.get(name)
