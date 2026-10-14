"""Test-only alias: the tests import the plugin as 'nd_breach_runner' (its name when installed);
in this repository the folder is DamSafety_Runner. This makes 'nd_breach_runner.x' resolve to DamSafety_Runner/x
so the tests run from a checkout (python3 -m nd_breach_runner.tests..., or a test script directly).
Not part of any plugin zip."""
import os as _os

__path__ = [_os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "DamSafety_Runner")]
__file__ = _os.path.join(__path__[0], "__init__.py")
with open(__file__) as _fh:
    exec(compile(_fh.read(), __file__, "exec"))
