"""Boann, the suite's flood engine and terrain conditioner: the one copy (DamSafety Core 1.7.0).

    hydro/   BoannHydro, the flood engine (was DamSafety_Runner/core/metalflood): engines, Metal
             kernels, their CPU build, the deck reader, the standalone command line (boann.py)
    grid/    BoannGrid, the terrain conditioner (was DamSafety_Runner/core/metalterrain): fill,
             flow, burn, condition, audit, culverts
    FILES.json   sha256 of every file here and BOANN_VERSION (tools/boann_files.py; CI checks it)

The Runner's core/metalflood and core/metalterrain and Screening's core/metalflood are two-line
shims onto these packages (DamSafety_Core/alias.py), so their old import paths keep working.
Boann's version is hydro/version.BOANN_VERSION. Importing this package imports nothing.
"""
