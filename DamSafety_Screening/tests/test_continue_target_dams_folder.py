"""L10: continue_target resolves the chosen Dams folder (settings["dams_folder"]) through
dams_folder(), not a hardcoded <work>/Dams, so Continue works when the engineer points the
"Dams folder (7b, dam.json)" line edit at a folder outside the work directory. No QGIS/scipy."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.core import workflow as wf          # noqa: E402
from nd_dam_screening.damsuite import dam_record as DR     # noqa: E402


def _make_record(folder):
    os.makedirs(folder)
    rec = DR.DamRecord(folder)
    rec.status = [{"step": "screened", "when": "2026-11-09T00:00:00"}]
    rec.save()
    assert os.path.isfile(os.path.join(folder, DR.FILE_NAME))


def main():
    tmp = tempfile.mkdtemp(prefix="continue_target_")
    work = os.path.join(tmp, "work")
    os.makedirs(work)

    # a chosen dams folder OUTSIDE the work directory, with a record in it
    custom = os.path.join(tmp, "custom_dams")
    _make_record(os.path.join(custom, "DWR-5289"))

    # with the chosen folder in settings, the record is found (next_action runs; the button is
    # not left grey with "no dam record yet")
    _plugin, _label, why_c = wf.continue_target(work, {"dams_folder": custom}, "DWR-5289")
    assert "no dam record yet" not in why_c, ("custom dams folder not resolved: " + why_c)

    # with default (empty) settings, dams_folder is <work>/Dams, which has no record here
    _p, _l, why_d = wf.continue_target(work, {}, "DWR-5289")
    assert "no dam record yet" in why_d, ("default should miss the custom-folder record: " + why_d)

    # and a record under the default <work>/Dams is found with default settings (no regression)
    _make_record(os.path.join(wf.dams_folder(work, {}), "DWR-1"))
    _p2, _l2, why2 = wf.continue_target(work, {}, "DWR-1")
    assert "no dam record yet" not in why2, ("default <work>/Dams record not found: " + why2)

    print("ok  continue_target resolves settings['dams_folder'] (L10)")


if __name__ == "__main__":
    main()
