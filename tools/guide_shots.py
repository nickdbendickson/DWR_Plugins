#!/usr/bin/env python3
"""The pictures of docs/USER_GUIDE.md: each window as a new user meets it, with the sample dam.

    xvfb-run -a -s "-screen 0 1920x1080x24" env QT_QPA_PLATFORM=xcb <QGIS python> \\
        tools/guide_shots.py [--dams FOLDER] [--out docs/img/guide]

Writes one PNG per window into --out (default docs/img/guide) and prints what it wrote. Run it
again after a window changes, and commit the pictures with the change. The windows are opened
with the real Qt of QGIS, with a scratch home folder, so nothing is read from or written to
your own QGIS profile and nothing is run.

--dams is a Dams folder that already holds the sample dam (DWR-2801). Without it the sample is
built into a scratch folder first, which downloads its terrain from the USGS (about 29 MB).

On a real screen (no xvfb) the pictures take that system's look; the committed ones are made
on the virtual 1080p display above, the one tools/gui_lint.py uses, so that a new set differs
from the old one only where a window changed. With QT_QPA_PLATFORM=offscreen the screen is
800 x 600 and the wider windows are cut off: the tool refuses to write pictures there.

tools/test_guide_shots.py checks that every picture the guide shows is in docs/img/guide and
that every picture there is shown (no QGIS needed)."""
import argparse
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = ("DamSafety_Core", "DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening",
           "DamSafety_Data", "DamSafety_Setup", "DamSafety_HazardLite", "DamSafety_PMP")
OUT = os.path.join("docs", "img", "guide")
# file name -> what the picture shows (the guide's alt text is checked against these names)
SHOTS = (
    ("suite_home_sample.png", "Dam suite home with the sample dam in the Dams folder"),
    ("runner_sample_quick_try.png", "The breach Runner on the sample dam, Advanced open, with "
                                    "the quick first try's four values"),
    ("runner_empty.png", "The breach Runner as it first opens"),
    ("hazard_main.png", "DamSafety Hazard on the sample dam's folder"),
    ("screening_main.png", "DamSafety Screening as it first opens"),
)
MIN_SCREEN_W = 1280


def names():
    return [n for n, _t in SHOTS]


def _setup():
    """The plugins importable under their installed names, a scratch HOME, a QGIS application."""
    root = tempfile.mkdtemp(prefix="guide_shots_")
    for p in PLUGINS:
        os.symlink(os.path.join(REPO, p), os.path.join(root, p))
    sys.path.insert(0, root)
    os.environ.pop("NDSUITE_SETTINGS", None)
    os.environ["HOME"] = tempfile.mkdtemp(prefix="guide_shots_home_")
    from qgis.core import QgsApplication
    app = QgsApplication([], True)
    app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", sys.prefix), True)
    app.initQgis()
    return app, root


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dams", default="", help="a Dams folder that holds the sample dam")
    ap.add_argument("--out", default=os.path.join(REPO, OUT))
    args = ap.parse_args(argv)
    app, root = _setup()

    def pump(n=8):
        for _ in range(n):
            app.processEvents()

    screen = app.primaryScreen().availableGeometry()
    if screen.width() < MIN_SCREEN_W:
        print("guide_shots: the screen is {} x {}; the windows would be cut off. Run it under "
              "xvfb at 1920x1080 (see the top of this file). Nothing written.".format(
                  screen.width(), screen.height()))
        return 2

    from DamSafety_Core.damsuite import sample_dam as SD
    from DamSafety_Core.ui import suite_home as SH
    dams = os.path.abspath(os.path.expanduser(args.dams)) if args.dams else ""
    if dams and not SD.installed(dams):
        print("guide_shots: {} does not hold the sample dam ({}).".format(dams, SD.DEFAULT))
        return 2
    if not dams:
        dams = os.path.join(tempfile.mkdtemp(prefix="guide_shots_dams_"), "Dams")
        os.makedirs(dams)
        print("building the sample dam (downloads its terrain, about {} MB)...".format(SD.download_mb()))
        SD.install(SD.build(os.path.join(os.path.dirname(dams), "pack")), dams)
    # the pictures show the same short path on every machine: the windows are pointed at a
    # copy of the Dams folder under <temp>/DamSafety/Dams (removed again at the end)
    show = os.path.join(tempfile.gettempdir(), "DamSafety", "Dams")
    shutil.rmtree(show, ignore_errors=True)
    os.makedirs(os.path.dirname(show), exist_ok=True)
    shutil.copytree(dams, show)
    folder = SD.installed(show)
    os.makedirs(args.out, exist_ok=True)
    wrote = []

    def shot(name, win, w=980, h=680):
        win.resize(w, h)
        win.show()
        pump()
        path = os.path.join(args.out, name)
        if not win.grab().save(path):
            raise RuntimeError("could not write " + path)
        wrote.append(name)
        win.close()
        pump()

    # 1. the home, with the sample dam listed and selected
    # the three plugins loaded, as in a QGIS that has them: the home's last line lists them
    import DamSafety_Runner.dialog                               # noqa: F401
    import DamSafety_Screening.dialog                            # noqa: F401
    from DamSafety_Hazard.suite_home import SuiteHomeDialog       # as Hazard's menu opens it
    home = SuiteHomeDialog(None, None, "ndhazard")
    home._save = lambda: None
    home.dams_edit.setText(show)
    home.refresh()
    home._select_folder(folder)
    shot("suite_home_sample.png", home, 1100, 520)

    # 2. the Runner as it first opens, then on the sample with the quick first try
    from qgis.core import QgsProject
    from DamSafety_Runner import dialog as RD
    QgsProject.instance().removeEntry(RD.SCOPE, RD.ENTRY)
    run = RD.NDBreachRunnerDialog()
    shot("runner_empty.png", run)
    QgsProject.instance().removeEntry(RD.SCOPE, RD.ENTRY)
    run = RD.NDBreachRunnerDialog()
    row = {"folder": folder, "dem": os.path.join(folder, SD.dem_name()), "first_try": dict(SD.FIRST_TRY)}
    SH.point_window_at(run, "ndbreach", show, row)
    if row.get("first_try_missing"):
        raise RuntimeError("the Runner window has no field for " + ", ".join(row["first_try_missing"]))
    run.adv_section.header.setChecked(True)
    shot("runner_sample_quick_try.png", run, 980, 860)

    # 3. Hazard on the sample dam's folder
    from DamSafety_Hazard.dialog import NDDamHazardDialog
    haz = NDDamHazardDialog(parent=None)
    haz.dam_folder_edit.setText(folder)
    shot("hazard_main.png", haz, 1000, 720)

    # 4. Screening as it first opens
    from DamSafety_Screening.dialog import NDDamScreeningDialog
    shot("screening_main.png", NDDamScreeningDialog(parent=None, iface=None))

    missing = sorted(set(names()) - set(wrote))
    shutil.rmtree(show, ignore_errors=True)
    for n in wrote:
        print("wrote " + os.path.join(args.out, n))
    if missing:
        print("guide_shots: not written: " + ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        sys.stdout.flush()
    os._exit(code)                  # QGIS can crash on teardown; the result stands
