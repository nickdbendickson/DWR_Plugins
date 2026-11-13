"""Where Vor's data lives, across the damresearch -> vor rename.

Why this exists: the rename moved ~/DamResearch to ~/Vor only "when ~/Vor does not exist".
But three things could create ~/Vor first: fetch_asdso.py and download-library.zsh (their
default output), and first-run.zsh (its log folder, made before the installer runs). Each
left the indexed library behind under the old name, wrote VOR_HOME=~/Vor, and nothing
reported it: an empty library is not an error. The rule is now "the folder that holds the
index", in vor.py and fetch_asdso.py alike, and the shell scripts ask fetch_asdso.py.
"""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
VOR = os.path.dirname(HERE)
ZSH = shutil.which("zsh")


def slurp(name):
    with open(os.path.join(VOR, name), encoding="utf-8") as f:
        return f.read()


def load(name, home, env=None):
    """Import a script with HOME pointed at a scratch folder (both read ~ at import or call)."""
    keep = {k: os.environ.get(k) for k in ("HOME", "VOR_HOME", "VOR_DB", "DAMRESEARCH_HOME")}
    os.environ["HOME"] = home
    for k in ("VOR_HOME", "VOR_DB", "DAMRESEARCH_HOME"):
        os.environ.pop(k, None)
    os.environ.update(env or {})
    try:
        spec = importlib.util.spec_from_file_location("home_test_" + name.replace(".", "_"), os.path.join(VOR, name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.HOME if name == "vor.py" else mod.vor_home()
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Scratch(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="vor-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.new = os.path.join(self.home, "Vor")
        self.old = os.path.join(self.home, "DamResearch")

    def make(self, folder, *files):
        os.makedirs(folder, exist_ok=True)
        for f in files:
            p = os.path.join(folder, f)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(f)


class HomeRuleTest(Scratch):
    def both(self, env=None):
        a, b = load("vor.py", self.home, env), load("fetch_asdso.py", self.home, env)
        self.assertEqual(a, b, "vor.py and fetch_asdso.py disagree about where the library is")
        return a

    def test_fresh_machine_uses_the_new_name(self):
        self.assertEqual(self.both(), self.new)

    def test_pre_rename_install_is_found_under_the_old_name(self):
        self.make(self.old, "research.db", "library/a.pdf")
        self.assertEqual(self.both(), self.old)

    def test_an_empty_new_folder_does_not_hide_the_indexed_old_one(self):
        # the bug: a download or a first-run log made ~/Vor before setup had moved the library
        self.make(self.old, "research.db", "library/a.pdf")
        self.make(self.new, "library/references/breach/x.pdf")
        self.assertEqual(self.both(), self.old)

    def test_after_the_move_the_new_name_wins(self):
        self.make(self.new, "vor.db")
        self.assertEqual(self.both(), self.new)

    def test_new_name_wins_when_both_hold_an_index(self):
        self.make(self.new, "vor.db")
        self.make(self.old, "research.db")
        self.assertEqual(self.both(), self.new)

    def test_moved_folder_whose_index_is_not_renamed_yet(self):
        self.make(self.new, "research.db")
        self.assertEqual(self.both(), self.new)

    def test_vor_home_overrides_everything(self):
        self.make(self.old, "research.db")
        other = os.path.join(self.home, "elsewhere")
        self.assertEqual(self.both({"VOR_HOME": other}), other)

    def test_a_library_kept_elsewhere_is_found_through_the_old_variable(self):
        # the old installers exported DAMRESEARCH_HOME; a shell that has not re-read its rc
        # file still carries it, and it is the only thing that names a library on another disk
        other = os.path.join(self.home, "disk2", "papers")
        self.make(other, "research.db")
        self.assertEqual(self.both({"DAMRESEARCH_HOME": other}), other)

    def test_a_stale_old_variable_pointing_at_nothing_is_ignored(self):
        self.make(self.new, "vor.db")
        self.assertEqual(self.both({"DAMRESEARCH_HOME": self.old}), self.new)

    def test_print_home_is_what_the_shell_scripts_get(self):
        self.make(self.old, "research.db")
        self.make(self.new, "library/x.pdf")
        env = dict(os.environ, HOME=self.home)
        env.pop("VOR_HOME", None)
        out = subprocess.run([os.sys.executable, os.path.join(VOR, "fetch_asdso.py"), "--print-home"],
                             env=env, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, self.old)

    def test_no_shell_script_hard_codes_the_new_folder_as_its_default(self):
        for name in ("download-library.zsh", "refresh-library.zsh", "refresh-library.ps1"):
            text = slurp(name)
            self.assertIn("--print-home", text, name)
            self.assertNotIn("${VOR_HOME:-$HOME/Vor}", text, name)


def block(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


class ZshPresentTest(unittest.TestCase):
    def test_ci_has_zsh(self):
        # CI sets REQUIRE_ZSH so that the class below cannot skip there without anyone noticing
        if os.environ.get("REQUIRE_ZSH"):
            self.assertTrue(ZSH, "REQUIRE_ZSH is set and zsh is not installed")


@unittest.skipUnless(ZSH, "zsh not installed")
class SetupMoveTest(Scratch):
    """The move itself, cut out of setup-vor.zsh and first-run.zsh and run for real."""
    PRELUDE = ('ok(){ print -r -- "OK $*"; }; warn(){ print -r -- "WARN $*"; }\n'
               'RHOME="${VOR_HOME:-$HOME/Vor}"; OLD_HOME="$HOME/DamResearch"\n')

    def run_block(self, script, start, end, prelude=None):
        code = (self.PRELUDE if prelude is None else prelude) + block(slurp(script), start, end)
        env = {"HOME": self.home, "PATH": os.environ["PATH"]}
        r = subprocess.run([ZSH, "-f", "-c", code + '\nprint -r -- "RHOME=${RHOME:-${DR_HOME:-}}"'],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def setup(self):
        return self.run_block("setup-vor.zsh", "has_index() {", "# ----------")

    def test_plain_rename(self):
        self.make(self.old, "research.db", "library/a.pdf")
        out = self.setup()
        self.assertIn("RHOME=" + self.new, out)
        self.assertFalse(os.path.exists(self.old))
        self.assertTrue(os.path.isfile(os.path.join(self.new, "vor.db")))
        self.assertTrue(os.path.isfile(os.path.join(self.new, "library/a.pdf")))

    def test_new_folder_made_first_by_a_download_still_ends_with_the_indexed_library(self):
        self.make(self.old, "research.db", "library/a.pdf", "library/shared.pdf")
        self.make(self.new, "library/references/breach/x.pdf", "library/shared.pdf")
        with open(os.path.join(self.new, "library/shared.pdf"), "w") as f:
            f.write("the stray copy")
        out = self.setup()
        self.assertIn("RHOME=" + self.new, out)
        self.assertFalse(os.path.exists(self.old))
        self.assertTrue(os.path.isfile(os.path.join(self.new, "vor.db")), "the index did not come across")
        self.assertTrue(os.path.isfile(os.path.join(self.new, "library/a.pdf")))
        # the stray folder's files are merged in ...
        self.assertTrue(os.path.isfile(os.path.join(self.new, "library/references/breach/x.pdf")))
        # ... without overwriting the indexed library's own file of the same name ...
        with open(os.path.join(self.new, "library/shared.pdf")) as f:
            self.assertEqual(f.read(), "library/shared.pdf")
        # ... and nothing was deleted: the stray folder is kept aside
        aside = [d for d in os.listdir(self.home) if d.startswith("Vor.before-rename-")]
        self.assertEqual(len(aside), 1, os.listdir(self.home))
        with open(os.path.join(self.home, aside[0], "library/shared.pdf")) as f:
            self.assertEqual(f.read(), "the stray copy")

    def test_rerun_after_the_move_touches_nothing(self):
        self.make(self.new, "vor.db", "library/a.pdf")
        out = self.setup()
        self.assertIn("RHOME=" + self.new, out)
        self.assertNotIn("moved", out)
        self.assertEqual(sorted(os.listdir(self.home)), ["Vor"])

    def test_two_real_libraries_are_left_alone(self):
        self.make(self.new, "vor.db")
        self.make(self.old, "research.db")
        out = self.setup()
        self.assertNotIn("moved", out)
        self.assertTrue(os.path.isfile(os.path.join(self.old, "research.db")))

    def test_vor_home_set_means_no_move(self):
        self.make(self.old, "research.db")
        other = os.path.join(self.home, "elsewhere")
        out = self.run_block("setup-vor.zsh", "has_index() {", "# ----------",
                             prelude='export VOR_HOME="%s"\n' % other + self.PRELUDE)
        self.assertTrue(os.path.isfile(os.path.join(self.old, "research.db")))
        self.assertIn("RHOME=" + other, out)

    def test_library_on_another_disk_is_kept_there_not_moved(self):
        other = os.path.join(self.home, "disk2", "papers")
        self.make(other, "research.db", "library/a.pdf")
        out = self.run_block("setup-vor.zsh", "has_index() {", "# ----------",
                             prelude='export DAMRESEARCH_HOME="%s"\n' % other + self.PRELUDE)
        self.assertIn("RHOME=" + other, out)
        self.assertTrue(os.path.isfile(os.path.join(other, "library/a.pdf")))
        self.assertTrue(os.path.isfile(os.path.join(other, "vor.db")))       # only the index is renamed
        self.assertFalse(os.path.exists(self.new))

    def test_first_run_moves_the_library_before_it_makes_its_log_folder(self):
        text = slurp("first-run.zsh")
        self.assertLess(text.index('mv "$HOME/DamResearch" "$HOME/Vor"'), text.index('mkdir -p "$OUT"'),
                        "first-run.zsh makes ~/Vor/first-run-*/ before the old library is moved")
        self.make(self.old, "research.db", "library/a.pdf")
        out = self.run_block("first-run.zsh", 'DR_HOME="${VOR_HOME:-$HOME/Vor}"', "exec > >(tee",
                             prelude='ok(){ print -r -- "OK $*"; }; warn(){ print -r -- "WARN $*"; }\n')
        self.assertIn("RHOME=" + self.new, out)
        self.assertFalse(os.path.exists(self.old))
        self.assertTrue(os.path.isfile(os.path.join(self.new, "research.db")))
        self.assertTrue(os.path.isdir(os.path.join(self.new, "library")))


@unittest.skipUnless(shutil.which("bash"), "bash not installed")
@unittest.skipUnless(os.path.isfile(os.path.join(VOR, "..", "install-local-ai-linux.sh")),
                     "this copy of damit_ai ships no Linux/Windows installer (the DamSafety one does not)")
class LinuxInstallerMoveTest(Scratch):
    """The same carry-over in install-local-ai-linux.sh (it had none: it made an empty ~/Vor
    and pinned VOR_HOME to it), cut out and run under bash."""

    def run_block(self, extra_env=None):
        text = slurp(os.path.join("..", "install-local-ai-linux.sh"))
        code = ('ok(){ echo "OK $*"; }; warn(){ echo "WARN $*"; }\n'
                'LOCAL_AI="$HOME/local-ai"; MODELS_DIR="$LOCAL_AI/models"; VOR_DIR="$LOCAL_AI/vor"\n'
                'RHOME="${VOR_HOME:-$HOME/Vor}"\n'
                + block(text, 'OLD_RHOME="${DAMRESEARCH_HOME', "ok \"$LOCAL_AI, $RHOME\"")
                + '\necho "RHOME=$RHOME"')
        env = {"HOME": self.home, "PATH": os.environ["PATH"]}
        env.update(extra_env or {})
        r = subprocess.run(["bash", "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_old_library_is_moved_before_the_skeleton_is_made(self):
        self.make(self.old, "research.db", "library/a.pdf")
        out = self.run_block()
        self.assertIn("RHOME=" + self.new, out)
        self.assertFalse(os.path.exists(self.old))
        self.assertTrue(os.path.isfile(os.path.join(self.new, "vor.db")))
        self.assertTrue(os.path.isfile(os.path.join(self.new, "library/a.pdf")))

    def test_new_folder_already_there_keeps_the_indexed_one_as_the_library(self):
        self.make(self.old, "research.db", "library/a.pdf")
        self.make(self.new, "library/x.pdf")
        out = self.run_block()
        self.assertIn("RHOME=" + self.old, out)           # VOR_HOME gets pinned to the real library
        self.assertTrue(os.path.isfile(os.path.join(self.old, "library/a.pdf")))

    def test_library_on_another_disk_is_kept_there(self):
        other = os.path.join(self.home, "disk2", "papers")
        self.make(other, "research.db")
        out = self.run_block({"DAMRESEARCH_HOME": other})
        self.assertIn("RHOME=" + other, out)
        self.assertFalse(os.path.isfile(os.path.join(self.new, "vor.db")))

    def test_fresh_machine_and_rerun_are_untouched(self):
        self.assertIn("RHOME=" + self.new, self.run_block())
        self.make(self.new, "vor.db")
        out = self.run_block()
        self.assertIn("RHOME=" + self.new, out)
        self.assertNotIn("moved", out)

    def test_the_windows_installer_carries_the_same_rule(self):
        # PowerShell cannot be run here, so this holds the structure only: the carry-over
        # exists and comes before the folders are created.
        ps = slurp(os.path.join("..", "install-local-ai-windows.ps1"))
        self.assertLess(ps.index("function VorHasIndex"), ps.index("foreach ($d in @($LocalAI, $ModelsDir, $VorDir"))
        for needle in ("DAMRESEARCH_HOME", "Move-Item -LiteralPath $OldRHome", "$RHome = $OldRHome", "'research.db'"):
            self.assertIn(needle, ps)


if __name__ == "__main__":
    unittest.main()
