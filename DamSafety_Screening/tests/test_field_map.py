"""0.17.43: tool 1 (PrepareDams) persists the DWR/NID field map to <work>/1_fields.json so a
later chain run (workflow step 1) reads the field names instead of re-detecting them.

Guards the regression where _write_field_map was defined but never called: the reader
(core/workflow.py, step 1) looked for 1_fields.json that nothing wrote.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_field_map.py
"""
import inspect, json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
from qgis.core import QgsProcessingContext  # noqa: E402
from nd_dam_screening.algorithms.prepare_dams import PrepareDams  # noqa: E402

work = tempfile.mkdtemp(prefix="fmap_")
try:
    # ---- 1. processAlgorithm actually calls the writer (it used to never be called)
    src = inspect.getsource(PrepareDams.processAlgorithm)
    assert "self._write_field_map(" in src, "processAlgorithm no longer wires _write_field_map"
    print("OK (wired into processAlgorithm)")

    # ---- 2. the writer records the chosen fields next to the standardized-dams output
    alg = PrepareDams()
    alg.initAlgorithm()
    ctx = QgsProcessingContext()
    dest = os.path.join(work, "1_standardized_dams.gpkg")
    # DWR_ID set by the user; the rest fall back to their defaults
    alg._write_field_map({"DWR_ID": "my_structure_id"}, ctx, dest)
    fmap = os.path.join(work, "1_fields.json")
    assert os.path.isfile(fmap), "1_fields.json was not written next to the output"
    with open(fmap, encoding="utf-8") as fh:
        got = json.load(fh)
    assert got.get("DWR_ID") == "my_structure_id", got
    assert got.get("DWR_NAME") == "dam_name", got          # a defaulted field is kept too
    assert got.get("NID_ID") == "NIDID", got
    print("OK (field map written):", sorted(got)[:4], "...")

    # ---- 3. no usable output path -> no crash, no file
    alg._write_field_map({"DWR_ID": "x"}, ctx, "memory:")   # dirname("memory:") == ""
    alg._write_field_map({"DWR_ID": "x"}, ctx, "")
    print("OK (no crash without a real output folder)")
finally:
    shutil.rmtree(work, ignore_errors=True)
print("ALL OK")
