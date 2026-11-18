# PFDS fixtures

Verbatim answers of the NOAA Atlas 14 Precipitation Frequency Data Server
(`https://hdsc.nws.noaa.gov/cgi-bin/new/cgi_readH5.py`), fetched once on 2026-11-09 so the tests
never call NOAA:

| File | Query |
|---|---|
| `pfds_pds_english_fargo_nd.txt` | lat=46.8772 lon=-96.7898 type=pf data=depth units=english series=pds (Volume 8 v2, Midwestern States) |
| `pfds_ams_metric_lincoln_ne.txt` | lat=40.8136 lon=-96.7026 type=pf data=depth units=metric series=ams |
| `pfds_no_coverage_portland_or.txt` | lat=45.52 lon=-122.68 (Oregon is outside NOAA Atlas 14; PFDS answers result = 'none') |

`mw_2_24h_temporal.csv` is NOAA's own file `https://hdsc.nws.noaa.gov/pub/hdsc/data/mw/mw_2_24h_temporal.csv` (Atlas 14 Volume 8
temporal distribution, area 2, 24-hour duration, fetched 2026-11-12), unchanged; the temporal tests use it with a fake downloader.

Copied unchanged from DamIT (`damit_pmp/tests/fixtures/`) for the `test_atlas14_*.py` tests (core, grid, chain and
QGIS). Not part of the shared core: they are test data, not checked by `SHARED.json`.
