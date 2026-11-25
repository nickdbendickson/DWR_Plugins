"""The Runner's core. Boann (the flood engine and the terrain conditioner) lives once in DamSafety
Core (DamSafety_Core/boann; core/metalflood and core/metalterrain here are shims onto it). The two
things Boann used to reach up into this folder for are lent to it here, imported only when used:
the sub-grid channel builder (core/sgc on core/grid) and the depression-fill oracle
(core/terrain.priority_flood). See DamSafety_Core/boann/hydro/host.py."""


def _channel_builder(z, xll, yll, dx, nodata, inflow_rc, channel_n):
    from . import grid as _grid
    from . import sgc as _sgc
    ch = _sgc.build(_grid.Grid(z, xll, yll, dx, nodata), inflow_rc=inflow_rc, channel_n=channel_n)
    return ch.width, ch.depth, ch.start_depth, ch.channel_n


def _priority_flood(dem):
    from .terrain import priority_flood
    return priority_flood(dem)


def _lend_to_boann():
    try:
        from DamSafety_Core.boann.hydro import host
    except ImportError:            # DamSafety Core missing: the shims say so when Boann is used
        return
    host.register("channel_builder", _channel_builder)
    host.register("priority_flood", _priority_flood)


_lend_to_boann()
