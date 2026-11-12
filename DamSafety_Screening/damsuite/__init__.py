"""damsuite: the shared package of the DamSafety plugins. Since DamSafety Core 1.0.0 the one
copy lives in the DamSafety_Core plugin; this shim makes `<this plugin>.damsuite` that very
package (DamSafety_Core/alias.py), so `from .damsuite import x` here returns the core's module
objects. Nothing else may live in this folder (tools/ci_checks.py checks). DamSafety Core must be
installed (metadata.txt plugin_dependencies)."""
from DamSafety_Core import alias as _alias
_alias.install(__name__)
