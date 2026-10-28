"""Write shared/damsuite/vertcon_nd.py: NOAA's VERTCON NGVD29 -> NAVD88 grid clipped to North
Dakota, embedded as text (tools/sync_damsuite.py copies only .py files into the plugins).

Source (public domain, "Derived from work by NOAA"):
    https://cdn.proj.org/us_noaa_vertconc.tif   (PROJ-data; VERTCON 2.x, converted from NGS's
    vertconc.gtx of 2018-10-05; NGVD29 height EPSG:7968 -> NAVD88 height EPSG:5703, metres,
    0.05 deg nodes, AREA_OR_POINT=Point, lon -107.0 .. -84.0, lat 24.0 .. 50.0)
    sha256 2e56823c84d9e191e3c5157da37e31ce6489e9e750f162524173ba3a940916c9

The ND clip (nodes lon -104.60 .. -95.95, lat 45.45 .. 49.55; 174 x 83 nodes):
    gdal_translate -projwin -104.6 49.55 -95.95 45.45 -of AAIGrid us_noaa_vertconc.tif nd.asc
(-projwin takes the outer edges of the half-cell around the nodes; the .asc header gives
xllcorner -104.625, yllcorner 45.425, cellsize 0.05.)

    python3 tools/make_vertcon_nd.py nd.asc            (then tools/sync_damsuite.py)

The values are stored as int16 tenths of a millimetre (round-off <= 0.05 mm), zlib + base64."""
import base64
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "shared", "damsuite", "vertcon_nd.py")
SCALE = 10000          # stored units per metre (0.1 mm)

HEAD = '''"""NOAA VERTCON NGVD29 -> NAVD88 vertical offsets for North Dakota (damsuite 1.18.0).

Public domain: derived from NOAA / NGS work. Clipped from PROJ-data's us_noaa_vertconc.tif
(https://cdn.proj.org/us_noaa_vertconc.tif, "NGVD29 height (m) (EPSG:7968) to NAVD88 height
(EPSG:5703). Converted from vertconc.gtx (last modified at 2018/10/05)", sha256
2e56823c84d9e191e3c5157da37e31ce6489e9e750f162524173ba3a940916c9) by tools/make_vertcon_nd.py.
Not DWR data.

NAVD88 = NGVD29 + offset (metres). Nodes every STEP degrees (NAD 27 geographic in the source;
the NAD 27 / NAD 83 / WGS 84 horizontal difference, a few tens of metres in ND, is far below
the 0.05 deg (~4-5 km) node spacing): column i at lon LON0 + i * STEP, row j at
lat LAT_TOP - j * STEP (row 0 = north). DATA: zlib + base64 of NROWS * NCOLS little-endian
int16 values in 1 / SCALE metres. Generated file: edit tools/make_vertcon_nd.py, not this.
"""
SOURCE = "NOAA VERTCON (PROJ-data us_noaa_vertconc.tif, vertconc.gtx 2018-10-05), ND clip"
'''


def read_asc(path):
    hdr, rows = {}, []
    with open(path, encoding="ascii") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            if parts[0][0].isalpha():
                hdr[parts[0].lower()] = float(parts[1])
            else:
                rows.append([float(x) for x in parts])
    return hdr, rows


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    hdr, rows = read_asc(argv[1])
    ncols, nrows, step = int(hdr["ncols"]), int(hdr["nrows"]), hdr["cellsize"]
    if len(rows) != nrows or any(len(r) != ncols for r in rows):
        sys.exit("bad grid: {} rows".format(len(rows)))
    lon0 = round(hdr["xllcorner"] + step / 2.0, 6)
    lat_top = round(hdr["yllcorner"] + (nrows - 0.5) * step, 6)
    vals = [int(round(v * SCALE)) for r in rows for v in r]
    raw = struct.pack("<{}h".format(len(vals)), *vals)
    b64 = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    lines = [b64[i:i + 96] for i in range(0, len(b64), 96)]
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(HEAD)
        f.write("LON0 = {!r}\nLAT_TOP = {!r}\nSTEP = {!r}\nNCOLS = {}\nNROWS = {}\nSCALE = {}\n"
                .format(lon0, lat_top, step, ncols, nrows, SCALE))
        f.write("DATA = (\n" + "".join('    "{}"\n'.format(s) for s in lines) + ")\n")
    print("wrote {} ({} x {} nodes, {} bytes of data)".format(OUT, ncols, nrows, len(b64)))


if __name__ == "__main__":
    main(sys.argv)
