"""Boann's one copy lives in DamSafety Core (DamSafety_Core/boann/grid, since Core 1.7.0 / Boann 6.3.2).
This shim makes `<this plugin>.core.metalterrain` that very package (DamSafety_Core/alias.py), so
`from .metalterrain import x` here returns the core's module objects. Nothing else may live in this
folder (tools/ci_checks.py checks)."""
from DamSafety_Core import alias as _alias
_alias.install(__name__, "DamSafety_Core.boann.grid")
