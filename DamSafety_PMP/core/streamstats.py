"""USGS StreamStats client for DamSafety_PMP: basin outline, basin characteristics and the North
Dakota peak-flow regression estimates for a dam's outlet point (read-only web services).

Pure Python 3 standard library, typed, no QGIS: the plugin's dialog runs it in a QgsTask and the
tests run it on saved responses with no network.

Workflow (the USGS 'StreamStatsFlowStatisticsWorkflow.ipynb'; the services that replaced the
legacy streamstatsservices, which were switched off on 2026-01-30):
  1. ss-delineate  GET  /ss-delineate/v1/delineate/sshydro/{region}?lat&lon
  2. NSS           POST /nssservices/regressionregions/bylocation  (watershed geometry)
  3. NSS           GET  /nssservices/scenarios?regions&statisticgroups&regressionregions
  4. ss-hydro      POST /ss-hydro/v1/basin-characteristics/calculate (delineation response)
  5. NSS           POST /nssservices/scenarios/estimate?regions

USGS asks users to make no more than four simultaneous requests and says rate limits may change.
This client sends ONE request at a time, at least `min_interval_s` (2 s) apart, and checks every
answer (an HTTP 200 can still carry an unusable basin). Every exchange is cached on disk with a
provenance sidecar (url, body sha256, response sha256, UTC time, HTTP status); a cached answer
whose sha256 no longer matches is refused.

Not here: a PMF (StreamStats has none), a curve number (StreamStats has none for ND; CN comes
from the user / SSURGO), and low-flow statistics (NSS has no ND low-flow equations).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

__version__ = "1.0.0"
BASE = "https://streamstats.usgs.gov"
DELINEATE = BASE + "/ss-delineate/v1/delineate/sshydro/{region}"
BCHARS = BASE + "/ss-hydro/v1/basin-characteristics/calculate"
NSS = BASE + "/nssservices"
OPENAPI = {"ss-delineate": BASE + "/ss-delineate/openapi.json",
           "ss-hydro": BASE + "/ss-hydro/openapi.json"}
SQM_PER_SQMI = 2_589_988.110336
USER_AGENT = "DamSafety_PMP/{plugin} (QGIS plugin; North Dakota dam safety; streamstats.py {client})"

# basin characteristics asked for on top of the ones the equations need (the PMF inputs and the
# context shown to the user). The regression regions add theirs (e.g. LAT_OUT, SSURGOM).
DEFAULT_EXTRA_BCHARS = ["AG_OF_DA", "BASINPERIM", "BSLDEM10M", "COMPRAT", "CSL1085LFP", "DRNAREA",
                        "ELEV", "ELEVMAX", "ISOLAKEDA", "ISOLAKESUM", "LAKEAREA", "LAKEAREASU",
                        "LC11DEV", "LC11IMP", "LFPLENGTH", "MINBELEV", "PRECIP", "RUGGED",
                        "SLOPERAT", "SOILPERM", "STRMTOT"]

CITATIONS = {
    "nd_peak_flow": ("Williams-Sether, T., 2015, Regional regression equations to estimate "
                     "peak-flow frequency at sites in North Dakota using data through 2009: U.S. "
                     "Geological Survey Scientific Investigations Report 2015-5096, 12 p., "
                     "https://doi.org/10.3133/sir20155096"),
    "max_flood_envelope": ("Crippen, J.R., and Bue, C.D., 1977, Maximum floodflows in the "
                           "conterminous United States: U.S. Geological Survey Water-Supply Paper "
                           "1887, 52 p., https://doi.org/10.3133/wsp1887"),
    "streamstats": ("U.S. Geological Survey, StreamStats (ss-delineate, ss-hydro and NSS web "
                    "services), https://streamstats.usgs.gov/"),
}
SERVICE_NOTE = ("StreamStats moved to new open-source services (application 4.30.0, Dec 2025); the "
                "legacy streamstatsservices were switched off on 2026-01-30. USGS notes results "
                "may differ slightly from the old services.")

# peak-flow statistics in the sanity table (NSS codes), largest AEP first
PEAK_AEPS: List[Tuple[str, str]] = [
    ("PK50AEP", "50%"), ("PK20AEP", "20%"), ("PK10AEP", "10%"), ("PK4AEP", "4%"),
    ("PK2AEP", "2%"), ("PK1AEP", "1%"), ("PK0_5AEP", "0.5%"), ("PK0_2AEP", "0.2%")]
PEAK_GROUP = "Peak-Flow Statistics"
ENVELOPE_CODE = "PKMAX_CB_R"
ASEP = "Average standard error of prediction"

# fetcher(method, url, body_bytes_or_None, headers) -> (status, body_bytes)
Fetcher = Callable[[str, str, Optional[bytes], Dict[str, str]], Tuple[int, bytes]]


def http_fetch(method: str, url: str, body: Optional[bytes],
               headers: Dict[str, str]) -> Tuple[int, bytes]:
    """urllib (honours the http(s)_proxy environment variables)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:  # keep the error body for the message
        return e.code, e.read()


class StreamStatsError(RuntimeError):
    pass


class NotOnStreamError(StreamStatsError):
    """The outlet point is not on the StreamStats stream grid. str() is the plain-language
    message for the user; `detail` keeps what StreamStats said."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class OfflineCacheMiss(StreamStatsError):
    pass


class Cancelled(StreamStatsError):
    pass


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StreamStatsClient:
    cache_dir: Path
    fetcher: Fetcher = http_fetch
    offline: bool = False          # True: never touch the network (tests, reproducible reruns)
    min_interval_s: float = 2.0    # USGS politeness: serial requests, >= 2 s apart
    plugin_version: str = "?"
    should_cancel: Optional[Callable[[], bool]] = None
    progress: Optional[Callable[[str, float], None]] = None   # (message, 0..100)
    retries: int = 2               # on HTTP 429 / 5xx, waiting 10 s, then 30 s
    _last: float = field(default=0.0, repr=False)
    log: List[Dict[str, Any]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)

    # ---------------- cached HTTP ----------------
    @staticmethod
    def _key(method: str, url: str, body: Optional[bytes]) -> str:
        return hashlib.sha256(f"{method} {url}\n".encode() + (body or b"")).hexdigest()[:20]

    def _check_cancel(self) -> None:
        if self.should_cancel is not None and self.should_cancel():
            raise Cancelled("cancelled")

    def _sleep(self, s: float) -> None:
        end = time.monotonic() + s
        while True:
            self._check_cancel()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.1, left))

    def _step(self, msg: str, pct: float) -> None:
        self._check_cancel()
        if self.progress is not None:
            self.progress(msg, pct)

    def _live(self, method: str, url: str, body: Optional[bytes]) -> Tuple[int, bytes, float]:
        headers = {"User-Agent": USER_AGENT.format(plugin=self.plugin_version, client=__version__),
                   "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        waits = [10.0, 30.0][: max(0, self.retries)]
        while True:
            self._sleep(self.min_interval_s - (time.monotonic() - self._last))
            t0 = time.monotonic()
            status, raw = self.fetcher(method, url, body, headers)
            self._last = time.monotonic()
            if (status == 429 or status >= 500) and waits:
                self._sleep(waits.pop(0))
                continue
            return status, raw, self._last - t0

    def request(self, method: str, url: str, params: Optional[Dict[str, Any]] = None,
                payload: Any = None, label: str = "") -> Any:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        body = None if payload is None else json.dumps(payload, sort_keys=True,
                                                       separators=(",", ":")).encode()
        key = self._key(method, url, body)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{label + '_' if label else ''}{key}"
        resp_p, prov_p = self.cache_dir / f"{stem}.json", self.cache_dir / f"{stem}.prov.json"
        if resp_p.exists() and prov_p.exists():
            raw = resp_p.read_bytes()
            prov = json.loads(prov_p.read_text(encoding="utf-8"))
            if hashlib.sha256(raw).hexdigest() != prov.get("response_sha256"):
                raise StreamStatsError(f"cache checksum mismatch (file changed after download): "
                                       f"{resp_p}; delete it to download again")
            self.log.append(dict(prov, cached=True, cache_file=resp_p.name))
            return json.loads(raw)
        if self.offline:
            raise OfflineCacheMiss(f"{method} {url} (key {stem})")
        status, raw, elapsed = self._live(method, url, body)
        if status != 200:
            raise StreamStatsError(f"HTTP {status} from StreamStats ({method} {url.split('?')[0]}): "
                                   f"{raw[:300]!r}")
        try:
            data = json.loads(raw)  # validate before caching
        except ValueError as e:
            raise StreamStatsError(f"StreamStats answered HTTP 200 but not JSON ({url}): {e}")
        resp_p.write_bytes(raw)
        prov = {"method": method, "url": url, "label": label,
                "request_body_sha256": hashlib.sha256(body).hexdigest() if body else None,
                "response_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                "http_status": status, "elapsed_s": round(elapsed, 2), "retrieved_utc": _utc(),
                "client": f"DamSafety_PMP streamstats.py {__version__}"}
        prov_p.write_text(json.dumps(prov, indent=1), encoding="utf-8")
        if body:
            (self.cache_dir / f"{stem}.request.json").write_bytes(body)
        self.log.append(dict(prov, cached=False, cache_file=resp_p.name))
        return data

    def service_versions(self) -> Dict[str, str]:
        """{'ss-delineate': '1.2.0', 'ss-hydro': '1.4.0'} from the services' OpenAPI documents
        (not cached: they say which service answered today). Offline: 'not checked (offline)'."""
        out: Dict[str, str] = {}
        for name, url in OPENAPI.items():
            if self.offline:
                out[name] = "not checked (offline / cached run)"
                continue
            try:
                status, raw, _ = self._live("GET", url, None)
                out[name] = (str(json.loads(raw)["info"]["version"]) if status == 200
                             else f"unknown (HTTP {status})")
            except Cancelled:
                raise
            except Exception as e:                                    # noqa: BLE001
                out[name] = f"unknown ({type(e).__name__})"
        out["nssservices"] = "unversioned (NSS)"
        return out

    # ---------------- workflow steps ----------------
    def delineate(self, region: str, lat: float, lon: float, label: str = "") -> Dict[str, Any]:
        return self.request("GET", DELINEATE.format(region=region),
                            {"lat": f"{lat:.7f}", "lon": f"{lon:.7f}"}, label=f"{label}delineate")

    @staticmethod
    def watershed(delin: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        """-> (global watershed polygon feature, snapped point feature, point properties)."""
        poly = pt = None
        try:
            items = delin["bcrequest"]["wsresp"]["featurecollection"][0]
        except (KeyError, IndexError, TypeError):
            raise StreamStatsError("StreamStats returned no watershed for this point")
        for item in items:
            for f in item.get("feature", {}).get("features", []):
                if item.get("name") == "globalwatershed" and f["properties"].get("GlobalWshd") == 1:
                    poly = f
                elif item.get("name") == "globalwatershedpoint":
                    pt = f
        if poly is None or pt is None:
            raise StreamStatsError("StreamStats returned no watershed for this point")
        if pt["properties"].get("ExclusionType", 0):
            raise StreamStatsError("the point is in a StreamStats exclusion area (%s)"
                                   % pt["properties"].get("ExclusionReason", pt["properties"]))
        return poly, pt, pt["properties"]

    def regression_regions(self, geometry: Dict[str, Any], label: str = "") -> List[Dict[str, Any]]:
        return self.request("POST", NSS + "/regressionregions/bylocation", payload=geometry,
                            label=f"{label}rr")

    def scenarios(self, region: Optional[str], rr_codes: List[str], statgroup: Optional[int] = 2,
                  label: str = "") -> List[Dict[str, Any]]:
        """State regions need ``regions=ND``; national ones (Crippen-Bue, Bieger) answer only
        without it."""
        params: Dict[str, Any] = {"regressionregions": ",".join(rr_codes)}
        if region:
            params["regions"] = region
        if statgroup is not None:
            params["statisticgroups"] = statgroup
        return self.request("GET", NSS + "/scenarios", params, label=f"{label}scenarios")

    def basin_characteristics(self, delin: Dict[str, Any], codes: List[str],
                              label: str = "") -> List[Dict[str, Any]]:
        body = copy.deepcopy(delin)
        body["bcrequest"]["bcLabels"] = ";".join(codes) if codes else "*"
        return self.request("POST", BCHARS, payload=body, label=f"{label}bchars")

    def estimate(self, region: str, scenarios: List[Dict[str, Any]], bchars: List[Dict[str, Any]],
                 rr_weights: Optional[Dict[str, float]] = None,
                 label: str = "") -> List[Dict[str, Any]]:
        vals = {b["code"].upper(): b.get("value") for b in bchars}
        body = copy.deepcopy(scenarios)
        for sc in body:
            for rr in sc["regressionRegions"]:
                for p in rr.get("parameters", []):
                    v = vals.get(p["code"].upper())
                    if v is None or v <= -999:
                        raise StreamStatsError(f"missing basin characteristic {p['code']}")
                    p["value"] = v
                if rr_weights and rr["code"] in rr_weights:
                    rr["percentWeight"] = rr_weights[rr["code"]]
        return self.request("POST", NSS + "/scenarios/estimate", {"regions": region}, payload=body,
                            label=f"{label}estimate")

    # ---------------- one-shot ----------------
    def run(self, region: str, lat: float, lon: float, label: str = "",
            extra_bchars: Optional[List[str]] = None, allow_unsnapped: bool = False,
            check_versions: bool = True) -> Dict[str, Any]:
        """Delineate, compute basin characteristics and every NSS estimate for the point.
        Raises NotOnStreamError when the point is not on the stream grid."""
        t_query = _utc()
        self._step("Delineating the basin (ss-delineate)…", 5)
        delin = self.delineate(region, lat, lon, label)
        poly, pt, props = self.watershed(delin)
        parts = [m.strip(" ,.") for w in (props.get("WarningMsg"), poly["properties"].get("WarningMsg"))
                 for m in str(w or "").split(".")]
        warn = ". ".join(dict.fromkeys(m for m in parts if m))
        if "not snappable" in warn.lower() and not allow_unsnapped:
            raise NotOnStreamError(
                "Not on the stream network: StreamStats could not snap the point to a stream, so "
                "the basin above it would be a sliver. Move the point onto the channel just below "
                "the dam (zoom in on the creek line) and try again.",
                "StreamStats: %s" % warn)
        self._step("Finding the regression regions (NSS)…", 25)
        rrs = self.regression_regions(poly["geometry"], label)
        rr_codes = [r["code"] for r in rrs]
        weights = {r["code"]: float(r["percentWeight"]) for r in rrs
                   if r.get("percentWeight") is not None}
        self._step("Reading the equations (NSS)…", 35)
        scen = self.scenarios(region, rr_codes, 2, label) if rr_codes else []
        have = {rr["code"] for s in scen for rr in s["regressionRegions"]}
        rest = [c for c in rr_codes if c not in have]
        if rest:  # national groups (Crippen-Bue maximum flood envelope, bankfull geometry)
            scen += self.scenarios(None, rest, None, f"{label}national_")
        need = sorted({p["code"] for s in scen for rr in s["regressionRegions"]
                       for p in rr.get("parameters", [])})
        codes = sorted(set(need) | set(DEFAULT_EXTRA_BCHARS if extra_bchars is None else extra_bchars))
        self._step("Computing basin characteristics (ss-hydro)…", 50)
        bch = self.basin_characteristics(delin, codes, label)
        vals = {b["code"].upper(): b.get("value") for b in bch}
        da = vals.get("DRNAREA")
        if da is None or da <= 0 or (poly["properties"].get("Shape_Area") or 0) <= 0:
            raise NotOnStreamError(
                "Not on the stream network: StreamStats returned no usable basin for this point. "
                "Move the point onto the channel just below the dam (zoom in on the creek line) "
                "and try again.", "drainage area %s; StreamStats: %s" % (da, warn))
        rows: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        groups = [s for s in scen]
        for i, s in enumerate(groups):  # one estimate per statistic group, RRs with inputs only
            self._step("Running the regression equations (NSS)…", 65 + 30 * i / max(1, len(groups)))
            ok_rr = []
            for rr in s["regressionRegions"]:
                miss = [p["code"] for p in rr.get("parameters", [])
                        if vals.get(p["code"].upper()) is None or vals[p["code"].upper()] <= -999]
                if miss:
                    skipped.append({"statistic_group": s.get("statisticGroupName"),
                                    "rr_code": rr["code"], "rr_name": rr["name"],
                                    "missing_basin_characteristics": miss})
                else:
                    ok_rr.append(rr)
            if ok_rr:
                s2 = dict(s, regressionRegions=ok_rr)
                sg = s.get("statisticGroupID")
                rows += summarize_estimate(self.estimate(region, [s2], bch, weights,
                                                         f"{label}sg{sg}_"))
        versions = self.service_versions() if check_versions else {}
        self._step("Done", 100)
        return {"region": region, "input_lonlat": [lon, lat],
                "snapped_lonlat": pt["geometry"]["coordinates"], "huc8": props.get("HUCID"),
                "warning": warn,
                "shape_area_sqmi_service": (poly["properties"].get("Shape_Area") or math.nan)
                / SQM_PER_SQMI,
                "geodesic_area_sqmi_local": polygon_area_sqmi(poly["geometry"]),
                "regression_regions": rrs, "basin_characteristics": bch,
                "statistics": rows, "skipped_regression_regions": skipped,
                "watershed_geojson": poly, "query_utc": t_query, "label": label,
                "service_versions": versions, "requests": list(self.log)}


def polygon_area_sqmi(geom: Dict[str, Any]) -> float:
    """Spherical-excess area (WGS84 authalic radius) of a lon/lat Polygon/MultiPolygon, sq mi."""
    R = 6371007.2

    def ring(c: List[List[float]]) -> float:
        s = 0.0
        for (x1, y1), (x2, y2) in zip(c, c[1:] + c[:1]):
            s += math.radians(x2 - x1) * (2 + math.sin(math.radians(y1)) + math.sin(math.radians(y2)))
        return abs(s) * R * R / 2

    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    tot = sum(ring([p[:2] for p in pg[0]]) - sum(ring([p[:2] for p in h]) for h in pg[1:])
              for pg in polys)
    return tot / SQM_PER_SQMI


def summarize_estimate(est: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten an NSS estimate -> rows {group, region, code, name, value, unit, errors, flags}."""
    rows: List[Dict[str, Any]] = []
    for sc in est:
        for rr in sc.get("regressionRegions", []):
            out_of_range = []
            for p in rr.get("parameters", []):
                lim = p.get("limits") or {}
                v = p.get("value")
                if v is not None and lim and not (lim.get("min", -math.inf) <= v
                                                  <= lim.get("max", math.inf)):
                    out_of_range.append(f"{p['code']}={v:g} outside "
                                        f"[{lim.get('min')}, {lim.get('max')}]")
            for r in rr.get("results", []):
                unit = r.get("unit") or {}
                rows.append({"statistic_group": sc.get("statisticGroupName"),
                             "regression_region": rr.get("name"), "rr_code": rr.get("code"),
                             "percent_weight": rr.get("percentWeight"),
                             "code": r.get("code"), "name": r.get("name"), "value": r.get("value"),
                             "unit": unit.get("abbr") or unit.get("unit"),
                             "errors": {e.get("name"): e.get("value") for e in r.get("errors", [])},
                             "equivalent_years": r.get("equivalentYears"),
                             "equation": r.get("equation"),
                             "interval_bounds": r.get("intervalBounds"),
                             "out_of_range": out_of_range,
                             "disclaimer": sc.get("disclaimer") or rr.get("disclaimer")})
    return rows


# ---------------------------------------------------------------- post-processing helpers
def asep_to_log_se(asep_pct: float) -> float:
    """Average standard error of prediction (percent) -> log10 units (USGS SIR convention)."""
    return math.sqrt(math.log(1.0 + (asep_pct / 100.0) ** 2) / (math.log(10.0) ** 2))


def approx_pi90(q: float, asep_pct: float) -> Tuple[float, float]:
    """Approximate 90% prediction interval from the AVERAGE SEp (ignores the site's leverage;
    NSS gives no interval for these equations)."""
    t = 10 ** (1.645 * asep_to_log_se(asep_pct))
    return q / t, q * t


def bc_value(bchars: List[Dict[str, Any]], code: str) -> Optional[float]:
    for b in bchars:
        if str(b.get("code", "")).upper() == code.upper():
            v = b.get("value")
            return None if v is None or v <= -999 else float(v)
    return None


def kirpich_tc_hr(lfp_mi: float, slope_ft_per_mi: float) -> float:
    """Kirpich (1940) Tc, hours: Tc(min) = 0.0078 L^0.77 S^-0.385, L in ft, S in ft/ft.
    Developed on small, steep agricultural basins; a screening value only."""
    L, S = lfp_mi * 5280.0, slope_ft_per_mi / 5280.0
    return 0.0078 * L ** 0.77 * S ** -0.385 / 60.0


def nrcs_lag_hr(lfp_mi: float, cn: float, basin_slope_pct: float) -> float:
    """NRCS NEH-630 ch. 15 watershed lag, hours: l^0.8 (S+1)^0.7 / (1900 Y^0.5), l in ft,
    S = 1000/CN - 10, Y = average basin slope in percent. Tc = lag / 0.6."""
    s = 1000.0 / cn - 10.0
    return (lfp_mi * 5280.0) ** 0.8 * (s + 1) ** 0.7 / (1900.0 * basin_slope_pct ** 0.5)


def _pick(rows: List[Dict[str, Any]], code: str, group: Optional[str] = None
          ) -> Optional[Dict[str, Any]]:
    """The area-weighted row when NSS gave one, else the single region's row."""
    hits = [r for r in rows if r.get("code") == code
            and (group is None or r.get("statistic_group") == group) and r.get("value") is not None]
    ave = [r for r in hits if r.get("rr_code") == "areaave"]
    return (ave or hits or [None])[0]


def peak_table(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The ND peak flows 50%..0.2% AEP with an approximate 90% interval from the ASEp."""
    out = []
    for code, aep in PEAK_AEPS:
        r = _pick(result.get("statistics", []), code, PEAK_GROUP)
        if r is None:
            continue
        q, asep = float(r["value"]), r.get("errors", {}).get(ASEP)
        lo, hi = approx_pi90(q, float(asep)) if asep else (None, None)
        out.append({"code": code, "aep": aep, "q_cfs": q, "asep_pct": asep, "pi90_lo_cfs": lo,
                    "pi90_hi_cfs": hi, "rr_code": r.get("rr_code"),
                    "regression_region": r.get("regression_region"),
                    "out_of_range": r.get("out_of_range") or []})
    return out


def envelope_cfs(result: Dict[str, Any]) -> Optional[float]:
    """Crippen & Bue (1977) maximum-flood envelope for the basin's area (cfs), if NSS gave one."""
    rows = [r for r in result.get("statistics", []) if r.get("code") == ENVELOPE_CODE]
    r = _pick(rows, ENVELOPE_CODE)
    return float(r["value"]) if r else None


def pmf_inputs(result: Dict[str, Any], cn: Optional[float] = None) -> Dict[str, Any]:
    """StreamStats -> the Inflow hydrograph inputs (HydroConfig fields) and the sanity values.

    area_mi2 = DRNAREA (the total delineated area). The non-contributing area is only a
    SUGGESTED starting value: ISOLAKESUM, the area that drains to isolated lakes / potholes; the
    engineer decides. Tc / lag: Kirpich always (needs LFPLENGTH and CSL1085LFP); NRCS lag only when
    a CN is given (StreamStats has no ND curve number; the CN stays the user's / SSURGO's)."""
    b = result["basin_characteristics"]
    da, iso = bc_value(b, "DRNAREA"), bc_value(b, "ISOLAKESUM")
    lfp, s1085, bsl = bc_value(b, "LFPLENGTH"), bc_value(b, "CSL1085LFP"), bc_value(b, "BSLDEM10M")
    out: Dict[str, Any] = {
        "area_mi2": da, "noncontrib_mi2_suggested": iso if iso is not None else 0.0,
        "noncontrib_source": "ISOLAKESUM (area draining to isolated lakes / potholes)"
        if iso is not None else "not reported by StreamStats (0 used)",
        "contributing_mi2_est": (da - iso) if da is not None and iso is not None else da,
        "lfp_length_mi": lfp, "slope_1085_ft_per_mi": s1085, "basin_slope_pct": bsl,
        "tc_hr_kirpich": kirpich_tc_hr(lfp, s1085) if lfp and s1085 else None,
        "cn_for_lag": cn, "lag_hr_nrcs": None, "tc_hr_nrcs": None,
        "crippen_bue_envelope_cfs": envelope_cfs(result)}
    if cn and lfp and bsl and 0 < cn < 100:
        lag = nrcs_lag_hr(lfp, cn, bsl)
        out.update(lag_hr_nrcs=lag, tc_hr_nrcs=lag / 0.6)
    return out


def timing(inputs: Dict[str, Any], method: str) -> Dict[str, Any]:
    """{'method', 'tc_hr', 'lag_hr', 'note'} for 'kirpich' or 'nrcs' (lag = 0.6 Tc)."""
    if method == "nrcs":
        lag = inputs.get("lag_hr_nrcs")
        if not lag:
            return {"method": "nrcs", "tc_hr": None, "lag_hr": None,
                    "note": "NRCS lag needs a curve number (yours, e.g. from SSURGO) and the basin "
                            "slope"}
        return {"method": "nrcs", "tc_hr": lag / 0.6, "lag_hr": lag,
                "note": "NRCS watershed lag (NEH-630 ch. 15) with CN %g; Tc = lag / 0.6"
                        % inputs.get("cn_for_lag")}
    tc = inputs.get("tc_hr_kirpich")
    if not tc:
        return {"method": "kirpich", "tc_hr": None, "lag_hr": None,
                "note": "Kirpich needs the longest flow path and its 10-85 slope"}
    return {"method": "kirpich", "tc_hr": tc, "lag_hr": 0.6 * tc,
            "note": "Kirpich (1940) Tc from the longest flow path and its 10-85 slope; "
                    "lag = 0.6 Tc"}


def pmf_check(pmf_peak_cfs: float, sanity: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Plain-language check of a computed PMF peak against the 0.2% AEP peak and the Crippen &
    Bue envelope. [(level, text)], level 'OK' or 'WARNING'. A warning never blocks the run.

    `sanity` is the saved streamstats_basin.json (or any dict with 'peaks' and
    'crippen_bue_envelope_cfs')."""
    out: List[Tuple[str, str]] = []
    q02 = next((p for p in sanity.get("peaks", []) if p.get("code") == "PK0_2AEP"), None)
    env = sanity.get("crippen_bue_envelope_cfs")
    pk = float(pmf_peak_cfs)
    if q02:
        hi = q02.get("pi90_hi_cfs") or q02["q_cfs"]
        if pk < q02["q_cfs"]:
            out.append(("WARNING", "The PMF peak (%.0f cfs) is BELOW the 0.2%% AEP (500-year) "
                        "regression peak (%.0f cfs). A PMF should be much larger than a 500-year "
                        "flood: check the area, the losses / CN, the lag and the non-contributing "
                        "area." % (pk, q02["q_cfs"])))
        elif pk < hi:
            out.append(("WARNING", "The PMF peak (%.0f cfs) is inside the approximate 90%% range "
                        "of the 0.2%% AEP regression peak (%.0f, up to %.0f cfs). A PMF this close "
                        "to a 500-year flood is unusual: check the inputs." % (pk, q02["q_cfs"], hi)))
        else:
            out.append(("OK", "The PMF peak (%.0f cfs) is %.1f times the 0.2%% AEP regression "
                        "peak (%.0f cfs)." % (pk, pk / q02["q_cfs"], q02["q_cfs"])))
    if env:
        if pk < env:
            out.append(("WARNING", "The PMF peak (%.0f cfs) is below the Crippen & Bue (1977) "
                        "envelope (%.0f cfs), the largest floods observed in this part of the US "
                        "for a basin this size. That can be right for flat ND prairie basins with "
                        "potholes (the envelope was drawn over steeper basins), but check the "
                        "inputs before relying on it." % (pk, env)))
        else:
            out.append(("OK", "The PMF peak (%.0f cfs) is above the Crippen & Bue (1977) maximum "
                        "observed-flood envelope (%.0f cfs)." % (pk, env)))
    return out


def sanity_summary(result: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, Any]:
    flags = sorted({f for p in peak_table(result) for f in p["out_of_range"]})
    return {"peaks": peak_table(result), "crippen_bue_envelope_cfs": inputs["crippen_bue_envelope_cfs"],
            "out_of_range": flags,
            "regression_regions": [{"code": r.get("code"), "name": r.get("name"),
                                    "percent_weight": r.get("percentWeight")}
                                   for r in result.get("regression_regions", [])],
            "skipped_regression_regions": result.get("skipped_regression_regions", [])}


def write_watershed(result: Dict[str, Any], path: Path) -> Path:
    """The basin polygon as GeoJSON (CRS84 lon/lat)."""
    props = dict(result["watershed_geojson"].get("properties", {}))
    props.update({"DRNAREA_sqmi": bc_value(result["basin_characteristics"], "DRNAREA"),
                  "source": "USGS StreamStats ss-delineate (%s)" % result.get("region"),
                  "query_utc": result.get("query_utc")})
    fc = {"type": "FeatureCollection",
          "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
          "features": [dict(result["watershed_geojson"], properties=props)]}
    path = Path(path)
    path.write_text(json.dumps(fc), encoding="utf-8")
    return path


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def save_run(result: Dict[str, Any], out_dir: Path, name: str, cn: Optional[float] = None,
             timing_method: str = "kirpich", plugin_version: str = "?") -> Dict[str, Path]:
    """Write <name>_watershed.geojson, <name>_streamstats_basin.json (inputs + sanity values)
    and <name>_streamstats_provenance.json into out_dir. -> {'watershed', 'basin', 'provenance'}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = pmf_inputs(result, cn)
    tm = timing(inputs, timing_method)
    ws = write_watershed(result, out_dir / f"{name}_watershed.geojson")
    basin = {"name": name, "inputs": inputs, "timing": tm,
             "sanity": sanity_summary(result, inputs),
             "point_lonlat": result["input_lonlat"], "snapped_lonlat": result["snapped_lonlat"],
             "warning": result.get("warning", ""), "huc8": result.get("huc8"),
             "basin_characteristics": {b.get("code"): b.get("value")
                                       for b in result["basin_characteristics"]},
             "curve_number": "not from StreamStats (use your own / SSURGO value)"}
    bp = out_dir / f"{name}_streamstats_basin.json"
    bp.write_text(json.dumps(basin, indent=1, default=str), encoding="utf-8")
    prov = {"tool": "DamSafety_PMP %s, Get basin data from StreamStats (streamstats.py %s)"
                    % (plugin_version, __version__),
            "query_utc": result.get("query_utc"), "written_utc": _utc(),
            "region": result.get("region"), "point_lonlat": result["input_lonlat"],
            "snapped_lonlat": result["snapped_lonlat"], "service_versions":
                result.get("service_versions", {}),
            "services": {"ss-delineate": DELINEATE.split("/delineate")[0], "ss-hydro":
                         BCHARS.split("/basin-characteristics")[0], "nssservices": NSS},
            "service_note": SERVICE_NOTE,
            "usage_note": "Requests sent one at a time, >= 2 s apart (USGS asks for no more than "
                          "four simultaneous requests and may change its rate limits).",
            "citations": CITATIONS,
            "requests": [{k: r.get(k) for k in ("method", "url", "http_status", "retrieved_utc",
                                                "response_sha256", "cached", "cache_file")}
                         for r in result.get("requests", [])],
            "outputs_sha256": {ws.name: _sha256_file(ws), bp.name: _sha256_file(bp)}}
    pp = out_dir / f"{name}_streamstats_provenance.json"
    pp.write_text(json.dumps(prov, indent=1, default=str), encoding="utf-8")
    return {"watershed": ws, "basin": bp, "provenance": pp}


def check_pmf_run(rows: List[Dict[str, Any]], basin_json: Path, out_dir: Path,
                  area_mi2: Optional[float] = None) -> Tuple[List[Tuple[str, str]], Dict[str, Path]]:
    """After an inflow-hydrograph run: compare the largest peak in `rows` (core.hydro.run_folder
    rows, each with 'peak_cfs' and 'storm_id') with the StreamStats sanity values saved in
    `basin_json`, write streamstats_pmf_check.json and copy the basin + provenance JSON into
    out_dir. -> ([(level, text)], written files). Never raises on a bad peak: a check, not a gate."""
    basin_json = Path(basin_json)
    out_dir = Path(out_dir)
    basin = json.loads(basin_json.read_text(encoding="utf-8"))
    msgs: List[Tuple[str, str]] = []
    top = max(rows, key=lambda r: float(r.get("peak_cfs") or 0.0)) if rows else None
    if top is None:
        msgs.append(("WARNING", "No hydrographs were computed, so the PMF was not compared with "
                                "the StreamStats values."))
        peak = None
    else:
        peak = float(top["peak_cfs"])
        msgs.extend(pmf_check(peak, basin.get("sanity", {})))
    ss_area = (basin.get("inputs") or {}).get("area_mi2")
    if area_mi2 and ss_area and abs(float(area_mi2) - ss_area) > 0.1 * ss_area:
        msgs.append(("WARNING", "The drainage area used (%.2f mi2) differs from the StreamStats "
                                "basin (%.2f mi2) by more than 10%%: the comparison may not be "
                                "like for like." % (float(area_mi2), ss_area)))
    out_dir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}
    for src in (basin_json, basin_json.with_name(basin_json.name.replace(
            "_streamstats_basin.json", "_streamstats_provenance.json"))):
        if src.is_file() and src.parent.resolve() != out_dir.resolve():
            dst = out_dir / src.name
            dst.write_bytes(src.read_bytes())
            written[src.name] = dst
    rep = {"checked_utc": _utc(), "pmf_peak_cfs": peak,
           "pmf_storm_id": top.get("storm_id") if top else None,
           "area_mi2_used": area_mi2, "streamstats_area_mi2": ss_area,
           "streamstats_basin_json": str(basin_json),
           "basin_json_sha256": _sha256_file(basin_json),
           "q0_2aep_cfs": next((p.get("q_cfs") for p in basin.get("sanity", {}).get("peaks", [])
                                if p.get("code") == "PK0_2AEP"), None),
           "crippen_bue_envelope_cfs": basin.get("sanity", {}).get("crippen_bue_envelope_cfs"),
           "messages": [{"level": lvl, "text": t} for lvl, t in msgs],
           "note": "A plain-language check only; it never blocks the run."}
    cp = out_dir / "streamstats_pmf_check.json"
    cp.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    written["check"] = cp
    return msgs, written
