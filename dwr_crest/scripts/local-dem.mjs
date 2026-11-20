/**
 * A local DEM (any GDAL raster, e.g. the statewide 1 m mosaic ND_1m_EPSG26914.vrt from nd-3dep-1m) as a drop-in for
 * Crest's 3DEP fetch (outline.ts fetchDem): the same request ({ bbox lon/lat, width, height, cellM }) answered with the
 * same grid (row-major from the north-west corner, metres, NODATA -9999), cut by gdalwarp. For the batch command line
 * only; a browser tab cannot read a local mosaic.
 *
 * Resampling: "average" when the request's cells are coarser than the source (1 m -> 10 m flood grid: the mean ground
 * in each cell, closer to what 3DEP's 10 m product holds than a single bilinear sample), "bilinear" otherwise.
 * Mosaic gaps stay NODATA, as in the Runner.
 */
import { execFile } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);
const NODATA = -9999;

/** gdalwarp on PATH, else the copy inside QGIS on macOS. */
export function findGdalwarp(explicit) {
  if (explicit) return explicit;
  for (const p of ["/Applications/QGIS.app/Contents/MacOS/bin/gdalwarp", "/Applications/QGIS-LTR.app/Contents/MacOS/bin/gdalwarp"])
    if (existsSync(p)) return p;
  return "gdalwarp";
}

/** The gdalwarp arguments for one request (exported for the test). */
export function warpArgs(src, req, out, { sourceCellM = 1 } = {}) {
  const [w, s, e, n] = req.bbox;
  return [
    "-q",
    "-overwrite",
    "-t_srs",
    "EPSG:4326",
    "-te",
    String(w),
    String(s),
    String(e),
    String(n),
    "-ts",
    String(req.width),
    String(req.height),
    "-r",
    req.cellM > sourceCellM * 1.5 ? "average" : "bilinear",
    "-ot",
    "Float32",
    "-dstnodata",
    String(NODATA),
    "-of",
    "ENVI",
    src,
    out,
  ];
}

/**
 * fetchDem stand-in over a local raster. `zFactor` converts the source's vertical unit to metres (0.3048 for feet).
 * Requests run one gdalwarp each, in a temp folder that is removed afterwards.
 */
export function localDemFetcher(src, { gdalwarp, sourceCellM = 1, zFactor = 1 } = {}) {
  if (!existsSync(src)) throw new Error(`DEM not found: ${src}`);
  const bin = findGdalwarp(gdalwarp);
  return async (req) => {
    const dir = mkdtempSync(join(tmpdir(), "crest-dem-"));
    try {
      const out = join(dir, "dem.bin");
      await run(bin, warpArgs(src, req, out, { sourceCellM }), { maxBuffer: 1 << 24 });
      const buf = readFileSync(out);
      const hdr = readFileSync(join(dir, "dem.hdr"), "utf8");
      const bigEndian = /byte order\s*=\s*1/.test(hdr);
      const n = req.width * req.height;
      if (buf.length < n * 4) throw new Error(`gdalwarp wrote ${buf.length} bytes, expected ${n * 4}`);
      const z = new Float32Array(n);
      const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
      for (let i = 0; i < n; i++) {
        const v = dv.getFloat32(i * 4, !bigEndian);
        z[i] = Number.isFinite(v) && v > NODATA + 1 ? v * zFactor : NODATA;
      }
      return { ...req, z };
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  };
}
