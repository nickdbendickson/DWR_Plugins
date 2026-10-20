"""Where the ND Dam Breach Runner is for the Screening <-> Runner parity checks (audit
2026-10-19 F-02: the checks looked only for <repo>/nd_breach_runner/core/*.py, but in this
repository that folder is the test-only alias (just __init__.py) for DamSafety_Runner, so
every parity check was skipped, also in CI).

runner_folder(*files) -> (import_root, folder) or (None, None):
  import_root  the folder to put on sys.path / PYTHONPATH so 'import nd_breach_runner' works
  folder       the Runner plugin folder itself (where core/breach.py etc. are)
Looked for, in order: $ND_BREACH_RUNNER_DIR (a Runner plugin folder named nd_breach_runner),
the repository (DamSafety_Runner through the nd_breach_runner alias, or a real
nd_breach_runner folder), /home/claude/suite.

missing(what): the Runner was not found. In CI (CI / GITHUB_ACTIONS set) or with
REQUIRE_RUNNER=1 that is a FAILURE (exit 1), never a silent skip; elsewhere a SKIP line."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def _candidates():
    env = os.environ.get("ND_BREACH_RUNNER_DIR", "").strip()
    if env:
        yield os.path.dirname(os.path.abspath(env)), os.path.abspath(env)
    alias = os.path.join(REPO, "nd_breach_runner")
    if os.path.isfile(os.path.join(alias, "__init__.py")):
        real = os.path.join(REPO, "DamSafety_Runner")
        yield REPO, (alias if os.path.isfile(os.path.join(alias, "core", "breach.py")) else real)
    yield "/home/claude/suite", "/home/claude/suite/nd_breach_runner"


def runner_folder(*files):
    for root, folder in _candidates():
        if all(os.path.isfile(os.path.join(folder, f)) for f in files or ("__init__.py",)):
            return root, folder
    return None, None


def required():
    return bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")
                or os.environ.get("REQUIRE_RUNNER") == "1")


def missing(what):
    """Report a Runner that is not here: FAIL (exit 1) in CI, else a SKIP line."""
    if required():
        print("FAIL: the ND Dam Breach Runner was not found (looked in $ND_BREACH_RUNNER_DIR, "
              "{}, /home/claude/suite); {} not checked".format(REPO, what))
        sys.stdout.flush()
        sys.exit(1)
    print("SKIP: the ND Dam Breach Runner is not on this machine; {} not checked".format(what))


def import_core(name):
    """nd_breach_runner.core.<name> (the Runner's module), or None (missing() reported)."""
    root, _folder = runner_folder(os.path.join("core", name + ".py"))
    if root is None:
        missing("equality with its core/{}.py".format(name))
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    import importlib
    try:
        return importlib.import_module("nd_breach_runner.core." + name)
    except Exception as e:  # noqa: BLE001
        print("FAIL: the Runner was found at {} but core/{}.py does not import: {}".format(
            root, name, e))
        sys.stdout.flush()
        sys.exit(1)
