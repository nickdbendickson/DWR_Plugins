#!/usr/bin/env bash
# use_local_terrain.sh 1.0 - run absaraka, nickreview and the 44-dam batches on a local
# terrain VRT (an external SSD) instead of downloading or copying 3DEP tiles.
#
#   bash use_local_terrain.sh check                 # check the VRT and print what each run would do
#   bash use_local_terrain.sh absaraka              # DWR-2801 on the local terrain
#   bash use_local_terrain.sh nickreview            # every nickreview dam
#   bash use_local_terrain.sh batch44-lisflood      # the 44 dams, ND profile, LISFLOOD-FP
#   bash use_local_terrain.sh batch44-anuga         # the 44 dams, DamSafety profile, ANUGA + culverts
#
# Options (before the command):
#   --vrt PATH        the terrain VRT          [$ND_LOCAL_TERRAIN, else the Extreme SSD path below]
#   --batch44 DIR     the batch-44-dams folder [$BATCH44_DIR, else searched next to this script and in ~]
#   --dry-run         print the command and the checks, run nothing
#   --no-redo         nickreview/absaraka: do NOT add --redo (see "record" below; rarely right)
#   --yes             skip the "press return" pause before a multi-hour run
# Anything after the command is passed through, e.g.
#   bash use_local_terrain.sh batch44-lisflood --only DWR-2801 --parallel 1
#
# WHAT THIS CHANGES, and it is not only the file path:
#
#  1. THE MODEL CRS FOLLOWS THE DEM. Both Runners do
#        if dem_crs.isValid() and not dem_crs.isGeographic() and dem_unit == 1.0: work = dem_crs
#     (nd_breach_runner/algorithms.py, DamSafety_Runner/algorithms.py), so a projected-metre DEM
#     REPLACES the work CRS: WORK_CRS is consulted only when the DEM is NOT projected in metres.
#     The nickreview runs fix WORK_CRS at EPSG:5070, and that setting is overridden by any metre
#     DEM, this VRT included. The run log prints "Model CRS <authid>", so the grid it actually
#     used is readable; what it never says is that WORK_CRS was overridden. NOTE this does not
#     by itself mean the grid moves: the DWR-2801 kit DEM is built from NATIVE 3DEP tiles
#     (USGS_1M_14_... = UTM zone 14, metres), so the box run was most likely already on a UTM 14N
#     grid. Settle it with: gdalsrsinfo -o epsg <the kit composite VRT>.
#
#  2. ABSARAKA LOSES THE COMPOSITE. The kit DEM for DWR-2801 is a composite: native 1 m over the
#     dam with a 5 m DEM underneath outside the 1 m tiles. A statewide 1 m VRT has
#     no 5 m underlay, so anywhere the 1 m coverage stops, the model gets nodata instead of
#     coarse ground.
#
#  3. THE OTHER NICKREVIEW DAMS CHANGE RESOLUTION. DEFAULT_DEM is "3DEP 1 m averaged to 5 m"
#     (data_files_nickreview.tsv holds 874 *_avg5m.tif rows against 36 native 1 m tiles, all of
#     them DWR-2801's). Moving them to true 1 m changes the source resolution, the run time and
#     the results for every one of them.
#
#  4. "record" MUST BE REDONE WITH "rundam". write_7b passes the DEM to rundam_params, but
#     READS["record"] is empty, so a DEM change does not invalidate record.ok. A run that redoes
#     rundam while keeping an old record.ok models the new terrain against a dam.json derived
#     from the old DEM. That is why --redo is added by default here.
#
#  5. batch44-lisflood ONLY: Get data keeps a DEM it has already recorded unless OVERWRITE is
#     set (damdata.exists() returns True for a role already in dam.json). A dam folder from an
#     earlier 3DEP run would silently model its old terrain while the log says "get data: ok".
#     This script passes OVERWRITE=true for that reason.
#
# NEVER put this path in DAMCHAIN_BREACH_PARAM / DAMCHAIN_GETDATA_PARAM / DAMCHAIN_SCREEN_PARAM.
# dam_chain splits those on whitespace with no quoting (_env_kv), so "Extreme SSD" becomes two
# tokens and parse_kv exits on the second. The --dem and --breach-param flags used here are argv
# and split on the first "=" only, so a path with spaces is safe through them.
set -uo pipefail

VRT="${ND_LOCAL_TERRAIN:-/Volumes/Extreme SSD/ND_DEM_1m/ND_1m_26914.vrt}"
B44="${BATCH44_DIR:-}"
DRY=0; REDO=1; YES=0
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32mOK\033[0m  %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m   %s\n' "$*"; }
die()  { printf '  \033[1;31mX\033[0m   %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --vrt) VRT="${2:-}"; shift 2 ;;
    --batch44) B44="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --no-redo) REDO=0; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,53p' "$0"; exit 0 ;;
    --) shift; break ;;
    -*) die "unknown option $1 (see --help)" ;;
    *) break ;;
  esac
done
CMD="${1:-check}"; [ $# -gt 0 ] && shift
RDO=""; [ "$REDO" -eq 1 ] && RDO=" --redo"

# --------------------------------------------------------------------------- a python with GDAL
find_py() {
  local c
  for c in "${DAMSAFETY_QGIS_PYTHON:-}" "$HOME/miniforge3/envs/ndqgis/bin/python" \
           "$HOME/miniforge3/envs/damsafety/bin/python" "$HOME/mambaforge/envs/ndqgis/bin/python" \
           /Applications/QGIS.app/Contents/MacOS/bin/python3 "$(command -v python3 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'from osgeo import gdal' >/dev/null 2>&1 && { echo "$c"; return 0; }
  done
  return 1
}

# --------------------------------------------------------------------------- check the VRT
check_vrt() {
  say "Terrain"
  echo "  $VRT"
  case "$VRT" in
    /Volumes/*)
      local vol; vol="$(printf '%s' "$VRT" | awk -F/ '{print "/"$2"/"$3}')"
      if [ -d "$vol" ]; then ok "volume mounted: $vol"
      else die "volume not mounted: $vol  (plug the drive in, then run this again)"; fi ;;
  esac
  [ -f "$VRT" ] || die "no such file: $VRT"

  local py; py="$(find_py)" || { warn "no python with GDAL found; skipping the raster checks"; return 0; }
  "$py" - "$VRT" <<'PY'
import sys, os
from osgeo import gdal, osr
gdal.UseExceptions()
p = sys.argv[1]
G, Y, R, N = "\033[1;32mOK\033[0m", "\033[1;33m!\033[0m ", "\033[1;31mX\033[0m ", "\033[0m"
def ok(m):   print("  %s  %s" % (G, m))
def warn(m): print("  %s  %s" % (Y, m))
bad = []
try:
    d = gdal.Open(p)
except Exception as e:
    print("  %s  cannot open: %s" % (R, e)); sys.exit(3)
b = d.GetRasterBand(1)
gt = d.GetGeoTransform()
cell = abs(gt[1])
srs = osr.SpatialReference(wkt=d.GetProjection() or "")
epsg = srs.GetAuthorityCode(None) if d.GetProjection() else None
ok("%d x %d px, %.3f m cells, EPSG:%s" % (d.RasterXSize, d.RasterYSize, cell, epsg))

if not d.GetProjection():
    bad.append("the VRT has no coordinate system; every consumer refuses or misplaces it")
elif srs.IsGeographic() or abs(srs.GetLinearUnits() - 1.0) > 1e-9:
    bad.append("the CRS is not projected in metres; the Runner needs one, and with this CRS it "
               "will NOT adopt the DEM's CRS, so the work CRS stays whatever was configured")
else:
    ok("projected metre CRS -- NOTE: the model grid will BE EPSG:%s, not the configured work CRS" % epsg)

unit = (b.GetUnitType() or "").strip()
if unit.lower() in ("m", "metre", "meter", "metres", "meters"):
    ok("elevation unit tag: %r" % unit)
elif not unit:
    warn("no elevation unit tag. Fine here -- every path this script drives passes an explicit "
         "Meters (DEM_Z=3 / --dem-units meters) -- but tag it if you ever open it by hand: "
         "gdal_edit.py -units m")
else:
    bad.append("elevation unit tag is %r, not metres" % unit)

nd = b.GetNoDataValue()
if nd is None:
    warn("no nodata value. Gaps would be warped in as real elevations; -999999 is what nd3dep writes")
elif nd > -1000:
    bad.append("nodata is %r. A nodata ABOVE -1000 survives the crest burn's filter and is "
               "burned into the road cells as ground" % nd)
else:
    ok("nodata %r" % nd)

if cell > 2.0:
    warn("cells are %.2f m. The crest burn refuses anything coarser than 2.0 m and switches "
         "itself off for the run, with only a line in the report" % cell)

files = [f for f in (d.GetFileList() or []) if os.path.abspath(f) != os.path.abspath(p)]
missing = [f for f in files if not os.path.exists(f)]
if missing:
    bad.append("%d of the %d source tiles are missing, e.g. %s" % (len(missing), len(files), missing[0]))
else:
    ok("%d source tiles, all present" % len(files))

try:
    rel = open(p, "rb").read().count(b'relativeToVRT="1"')
    if rel:
        ok('%d sources are relativeToVRT="1" -- fine: every path here hands GDAL the VRT where '
           'it lives, and nickreview rewrites them absolute when it copies it' % rel)
except Exception:
    pass

found = False
nx, ny = d.RasterXSize, d.RasterYSize
import numpy as np
for k in range(40):
    x = (k * 7919) % max(1, nx - 256); y = (k * 104729) % max(1, ny - 256)
    try:
        a = b.ReadAsArray(int(x), int(y), min(256, nx), min(256, ny))
    except Exception:
        continue
    if a is None: continue
    a = a.astype("float64")
    good = np.isfinite(a) & (a > -1e5)
    if nd is not None: good &= a != nd
    if good.mean() > 0.5:
        ok("sample read at %d,%d: median %.2f m" % (x, y, float(np.median(a[good]))))
        found = True; break
if not found:
    bad.append("read 40 windows and found no block that is more than half real data. "
               "The VRT opens but may be empty, or the drive is not readable")

for m in bad:
    print("  %s  %s" % (R, m))
d = b = srs = None            # drop the GDAL/OSR handles before exit (swig warns otherwise)
sys.exit(1 if bad else 0)
PY
  local rc=$?
  [ $rc -eq 0 ] || die "the terrain VRT did not pass its checks (above). Nothing was started."
}

# --------------------------------------------------------------------------- locate batch44
find_b44() {
  local c
  [ -n "$B44" ] && { [ -f "$B44/run_lisflood_44.py" ] && { echo "$B44"; return 0; }
                     die "no run_lisflood_44.py in --batch44 $B44"; }
  for c in "$HERE/batch-44-dams" "$HERE/../batch-44-dams" "$HOME/batch-44-dams-2026-11-06" \
           "$HOME/Downloads/batch-44-dams-2026-11-06" "$HOME/Documents/batch-44-dams-2026-11-06"; do
    [ -f "$c/run_lisflood_44.py" ] && { echo "$c"; return 0; }
  done
  return 1
}

run_it() {
  printf '\n  '
  local a; for a in "$@"; do printf '%q ' "$a"; done; printf '\n\n'
  if [ "$DRY" -eq 1 ]; then ok "--dry-run: nothing was started"; return 0; fi
  if [ "$YES" -eq 0 ]; then
    printf '  This runs for hours. Press return to start, or Ctrl-C to stop. '
    read -r _ || true
  fi
  "$@"
}

consequences() {
  say "What changes"
  echo "  * the model grid becomes the DEM's CRS, not the configured work CRS"
  echo "    (the run log prints 'Model CRS <authid>'; it never says WORK_CRS was overridden)"
  case "$CMD" in
    absaraka|check)
      echo "  * absaraka: DWR-2801 loses the kit composite's 5 m underlay outside the 1 m"
      echo "    coverage, and the run is NOT comparable to the Grok box run cell for cell" ;;
  esac
  case "$CMD" in
    nickreview|check)
      echo "  * nickreview: the non-kit dams move from a 5 m averaged DEM to true 1 m," \
           "slower and different" ;;
  esac
  case "$CMD" in
    batch44-lisflood|check)
      echo "  * batch44-lisflood: Get data is told OVERWRITE=true, so dam folders from earlier"
      echo "    runs re-cut their terrain instead of keeping the old dem.vrt" ;;
  esac
  case "$CMD" in
    absaraka|nickreview|check)
      if [ "$REDO" -eq 1 ]; then
        echo "  * --redo is added, so 'record' is rebuilt from the new DEM along with 'rundam'"
      else
        warn "--no-redo: if record.ok survives, dam.json keeps values derived from the OLD DEM"
      fi ;;
  esac
  echo "  Full reasoning: the header of this script."
}

# --------------------------------------------------------------------------- commands
case "$CMD" in
  check)
    check_vrt
    say "What each command would run"
    echo "  absaraka          run_nickreview.sh --dam DWR-2801 --dem <vrt>$RDO"
    echo "  nickreview        run_nickreview.sh --dem <vrt>$RDO"
    echo "  batch44-lisflood  run_lisflood_44.py --dem <vrt>"
    echo "  batch44-anuga     run_anuga_culverts_44.py --dem <vrt>"
    if b="$(find_b44)"; then ok "batch-44 folder: $b"; else warn "batch-44 folder not found; pass --batch44 DIR"; fi
    [ -f "$HERE/run_nickreview.sh" ] && ok "nickreview: $HERE/run_nickreview.sh" \
      || warn "run_nickreview.sh is not next to this script"
    consequences
    ;;

  absaraka|nickreview)
    check_vrt
    NR="$HERE/run_nickreview.sh"
    [ -f "$NR" ] || die "run_nickreview.sh is not next to this script ($HERE)"
    grep -q -- '--dem)' "$NR" || grep -q -- '|--dem)' "$NR" \
      || die "this run_nickreview.sh has no --dem passthrough: update tools/mac from the DWR repo"
    CMDLINE=(bash "$NR" --dem "$VRT")
    [ "$REDO" -eq 1 ] && CMDLINE+=(--redo)
    [ "$CMD" = absaraka ] && CMDLINE+=(--dam DWR-2801)
    CMDLINE+=("$@")
    consequences
    say "Starting"
    run_it "${CMDLINE[@]}"
    ;;

  batch44-lisflood)
    check_vrt
    B="$(find_b44)" || die "batch-44-dams folder not found; pass --batch44 DIR"
    S="$B/run_lisflood_44.py"
    grep -q -- '"--dem"' "$S" \
      || die "this run_lisflood_44.py has no --dem flag. Use the copy delivered with this script
      (the ND path has no DEM lever of its own: the DEM has to go in as a --breach-param)."
    consequences
    say "Starting"
    run_it python3 "$S" --dem "$VRT" "$@"
    ;;

  batch44-anuga)
    check_vrt
    B="$(find_b44)" || die "batch-44-dams folder not found; pass --batch44 DIR"
    S="$B/run_anuga_culverts_44.py"
    [ -f "$S" ] || die "no run_anuga_culverts_44.py in $B"
    consequences
    say "Starting"
    run_it python3 "$S" --dem "$VRT" "$@"
    ;;

  *) die "unknown command '$CMD' (check, absaraka, nickreview, batch44-lisflood, batch44-anuga)" ;;
esac
