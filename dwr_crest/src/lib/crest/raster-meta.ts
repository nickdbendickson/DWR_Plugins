/**
 * The light half of the results import: file roles, units, CRS list and legend classes. No proj4 or geotiff here, so
 * the desk's main bundle can name files and show the CRS picker without loading the raster code (raster.ts).
 */
export const M_TO_FT = 3.280839895;

/** CRSs the import knows. NAD 83 is taken as WGS 84 (about 1 m in North Dakota), which the map cannot show. */
export const CRS_DEFS: Record<string, { label: string; def: string }> = {
  "EPSG:32614": { label: "WGS 84 / UTM 14N (EPSG:32614)", def: "+proj=utm +zone=14 +datum=WGS84 +units=m +no_defs" },
  "EPSG:26914": { label: "NAD83 / UTM 14N (EPSG:26914)", def: "+proj=utm +zone=14 +datum=NAD83 +units=m +no_defs" },
  "EPSG:32613": { label: "WGS 84 / UTM 13N (EPSG:32613)", def: "+proj=utm +zone=13 +datum=WGS84 +units=m +no_defs" },
  "EPSG:26913": { label: "NAD83 / UTM 13N (EPSG:26913)", def: "+proj=utm +zone=13 +datum=NAD83 +units=m +no_defs" },
  "EPSG:5070": {
    label: "NAD83 / Conus Albers (EPSG:5070, Breach Runner)",
    def: "+proj=aea +lat_0=23 +lon_0=-96 +lat_1=29.5 +lat_2=45.5 +x_0=0 +y_0=0 +datum=NAD83 +units=m +no_defs",
  },
};
export const DEFAULT_ASCII_CRS = "EPSG:32614";

// ---------------------------------------------------------------------------------------------------------------
// File roles

export type VarId = "depth" | "velocity" | "arrival" | "wse" | "tmax";
export const VAR_LABEL: Record<VarId, string> = {
  depth: "max depth",
  velocity: "max velocity",
  arrival: "arrival time",
  wse: "max water surface",
  tmax: "time of max depth",
};

export type FileRole =
  | {
      kind: "grid";
      format: "ascii" | "geotiff";
      variable: VarId;
      /** Multiply raw values by this to get ft, ft/s or h. */
      factor: number;
      srcUnit: string;
      /** Lower is preferred when two files give the same variable. */
      rank: number;
      /** The unit is a guess (no unit in the name); flagged in the import checks. */
      guessed?: boolean;
      /** Runner final rasters ("Depth (Max).tif"): from the run's final model, which may be ANUGA. */
      final?: boolean;
    }
  | { kind: "meta"; what: "dam.json" | "run.par" }
  | { kind: "skip"; reason: string };

export function classifyFile(path: string): FileRole {
  const name = path.split(/[\\/]/).pop() ?? path;
  const n = name.toLowerCase();
  if (n.startsWith("._") || /(^|[\\/])__macosx[\\/]/i.test(path)) return { kind: "skip", reason: "macOS resource-fork file" };
  if (n === "dam.json") return { kind: "meta", what: "dam.json" };
  if (n.endsWith(".par")) return { kind: "meta", what: "run.par" };
  if (/\.(aux\.xml|ovr|tfw|prj|xml|qml|txt|csv|log|json|html|pdf|kml|kmz|geojson|zip|png|jpg)$/.test(n))
    return { kind: "skip", reason: "not a result raster" };
  const g = (variable: VarId, factor: number, srcUnit: string, rank: number, extra: Partial<FileRole> = {}): FileRole =>
    ({ kind: "grid", format: /\.tiff?$/.test(n) ? "geotiff" : "ascii", variable, factor, srcUnit, rank, ...extra }) as FileRole;
  if (/\.tiff?$/.test(n)) {
    if (/^anuga_|diff_depth|_minus_/.test(n)) return { kind: "skip", reason: "ANUGA or difference raster, not LISFLOOD/DSS-WISE" };
    // Breach Runner LISFLOOD GeoTIFFs (EPSG:5070, ft / ft/s / h in the name)
    if (/^lisflood_hmax_ft/.test(n)) return g("depth", 1, "ft", 0);
    if (/^lisflood_vmax_fps/.test(n)) return g("velocity", 1, "ft/s", 0);
    if (/^lisflood_arr_hr/.test(n)) return g("arrival", 1, "h", 0);
    if (/^lisflood_wsemax_ft/.test(n)) return g("wse", 1, "ft", 0);
    // DSS-WISE Lite rasters
    if (/dvmax/.test(n)) return { kind: "skip", reason: "depth × velocity, not used here" };
    if (/h_ft_at_final/.test(n)) return { kind: "skip", reason: "depth at the final time step, not the maximum" };
    if (/(^|_)dem(_|\.)/.test(n)) return { kind: "skip", reason: "terrain, not a result" };
    if (/hmax_ft/.test(n)) return g("depth", 1, "ft", 0);
    if (/hmax_m(\.|_)/.test(n)) return g("depth", M_TO_FT, "m", 0);
    if (/vmax_ftps|vmax_fps/.test(n)) return g("velocity", 1, "ft/s", 0);
    if (/vmax_mps|vmax_ms/.test(n)) return g("velocity", M_TO_FT, "m/s", 0);
    if (/arrival_time_hr/.test(n)) return g("arrival", 1, "h", 0);
    // Breach Runner final rasters (from the run's final model: ANUGA or LISFLOOD; dam.json says which)
    if (/^depth \(max\)/.test(n)) return g("depth", 1, "ft", 2, { final: true });
    if (/^velocity \(max\)/.test(n)) return g("velocity", 1, "ft/s", 2, { final: true });
    if (/^arrival time/.test(n)) return g("arrival", 1, "h", 2, { final: true });
    if (/hmax|max_?depth|depth_?max/.test(n)) return g("depth", 1, "ft", 3, { guessed: true });
    return { kind: "skip", reason: "GeoTIFF not recognized (expected Hmax_ft, Vmax_ftps, Arrival_Time_hr or lisflood_*)" };
  }
  // LISFLOOD-FP ASCII outputs: <resroot>.<ext>, with or without a trailing .asc
  const base = n.replace(/\.asc$/, "");
  const ext = base.includes(".") ? base.slice(base.lastIndexOf(".") + 1) : "";
  if (/-\d{4}\./.test(base) || /-\d{4}$/.test(base)) return { kind: "skip", reason: "time-step snapshot, not a maximum" };
  switch (ext) {
    case "max":
      return g("depth", M_TO_FT, "m", 1);
    case "maxvc":
    case "vmax":
      return g("velocity", M_TO_FT, "m/s", 1);
    case "inittm":
      return g("arrival", 1, "h", 1);
    case "mxe":
      return g("wse", M_TO_FT, "m", 1);
    case "maxtm":
      return g("tmax", 1, "h", 1);
    case "maxhaz":
    case "maxvcd":
    case "maxvx":
    case "maxvy":
    case "totaltm":
    case "mass":
    case "dem":
    case "wd":
    case "elev":
    case "bci":
    case "bdy":
    case "stage":
      return { kind: "skip", reason: `LISFLOOD .${ext} output, not used here` };
  }
  if (n.endsWith(".asc")) return { kind: "skip", reason: "ASCII grid not recognized (expected .max, .maxVc/.Vmax, .inittm, .mxe, .maxtm)" };
  return { kind: "skip", reason: "not a result raster" };
}

/** Depth A−B classes (ft): blue = B deeper, red = A deeper. */
export const DIFF_BINS: { min: number; max: number; color: string; label: string }[] = [
  { min: -Infinity, max: -5, color: "#08306b", label: "B deeper by 5 ft +" },
  { min: -5, max: -2, color: "#2171b5", label: "B deeper 2–5 ft" },
  { min: -2, max: -0.5, color: "#6baed6", label: "B deeper 0.5–2 ft" },
  { min: -0.5, max: 0.5, color: "#d9d9d9", label: "within 0.5 ft" },
  { min: 0.5, max: 2, color: "#fc9272", label: "A deeper 0.5–2 ft" },
  { min: 2, max: 5, color: "#de2d26", label: "A deeper 2–5 ft" },
  { min: 5, max: Infinity, color: "#67000d", label: "A deeper by 5 ft +" },
];

/** Names the client should read: result grids, dam.json, and zips (not macOS resource forks). */
export function wantFile(name: string) {
  if (/(^|[\\/])(__MACOSX[\\/]|\._)/.test(name)) return false;
  if (/\.zip$/i.test(name)) return true;
  return classifyFile(name).kind !== "skip";
}
