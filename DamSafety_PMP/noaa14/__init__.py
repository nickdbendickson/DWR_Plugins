"""NOAA Atlas 14 core, shared byte-identical with DamIT PMP.

atlas14.py, temporal.py, storm.py, gisgrid.py and gridstats.py in this folder are copies of
damit_pmp/<same name>.py in nickdbendickson/damit (the DamIT PMP plugin, NOAA Atlas 14 only).
The two copies must stay BYTE-IDENTICAL: a change to one of them must be made, the same way, in
both repositories, on paired branches of the same name. Code flows from DamIT to here only.

Guards:
  * SHARED.json (next to this file) lists each shared file with its sha256 and its DamIT path;
    DamSafety_PMP/tests/test_atlas14_shared.py fails when a file here no longer matches it.
  * DamIT's scripts/check_atlas14_shared.py compares the two repositories daily.

This __init__.py and SHARED.json are DWR-only. Nothing else belongs in this folder: the DWR
wrappers (algorithm_atlas14.py, atlas14_outputs.py, noaa14_http.py) live in the plugin folder.

The five modules use the standard library, numpy and GDAL, import each other relatively and take
the network as arguments (an http object with get_bytes(url, timeout) and a download(url, dest)
callable), so they have no QGIS or plugin imports.
"""
