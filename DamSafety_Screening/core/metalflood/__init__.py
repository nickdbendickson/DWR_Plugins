"""Boann's one copy lives in DamSafety Core (DamSafety_Core/boann/hydro, since Core 1.7.0 / Boann 6.3.1).
This shim makes `<this plugin>.core.metalflood` that very package (DamSafety_Core/alias.py), so
`from .metalflood import x` here returns the core's module objects. Nothing else may live in this
folder (tools/ci_checks.py checks)."""
from DamSafety_Core import alias as _alias
_alias.install(__name__, "DamSafety_Core.boann.hydro")
