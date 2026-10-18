# Plan: gmsh breakline-graded mesh for ANUGA (DamSafety Runner)

Status: design only. Nothing in the Runner changes in this PR. The current mesh stays the default, and its results stay byte-identical.

## 1. Goal
The 20x20 mi Absaraka ANUGA run takes about 30 h at 100/1000 m2. The aim is a mesh with small triangles only where hydraulics need them: the flow path or stream lines, road and rail embankments, culverts and STRUCTURES, and the breach and dam. Everything else gets coarse triangles. This option is meant to cut run time without losing that detail.

Option: `ANUGA_MESHER = default | gmsh` (Runner setting `anuga_mesher`, default `"default"`), plus:
`ANUGA_GMSH_MIN_AREA_M2` (100), `ANUGA_GMSH_MAX_AREA_M2` (20000), `ANUGA_GMSH_CHANNEL_AREA_M2` (250),
`ANUGA_GMSH_LINE_DIST_M` (60 to 1500 grading band) and `ANUGA_GMSH_MIN_ANGLE_DEG` (20).

## 2. How the Runner meshes today
- `core/anuga_model.py: mesh_polygons()` builds the outer polygon (the flooded area plus `anuga_buffer_m` = 500 m, clipped off no-data) and a `fine` polygon (flooded ∩ a disk of `anuga_near_field_mi` = 1 mi around the inflow).
- `run()` adds 8-gon regions of `min(fine_area, 25) m2` at both ends of each culvert. `refine_lines` (embankment crests) become breaklines, densified to sqrt(fine_area) and clipped to the outer polygon. Everything is written to `anuga_job.npz`.
- `core/anuga_worker.py` runs in the separate ANUGA Python environment, not in QGIS. It calls `anuga.create_domain_from_regions(boundary, boundary_tags={"outflow": all segments}, maximum_triangle_area=coarse_area, interior_regions=..., breaklines=...)`, which uses ANUGA's bundled Triangle. If the breaklines cross, it retries without them.
- Boundaries: there is one tag, `outflow` (Transmissive), on the whole outer ring. Inflow is an `Inlet_operator` on the `inlet` line, not a boundary tag. Culverts are Boyd box or pipe operators.
- Elevation and friction are bilinear-sampled from `model_dem` at the centroids.

## 3. How ANUGA can take an external mesh
1. **`anuga.Domain(points, vertices, boundary)`** (recommended). `points` is (Np, 2), `vertices` is (Nt, 3) and must be counter-clockwise, and `boundary` is a dict `{(tri_id, edge_id): tag}`. Edge *i* is the edge opposite vertex *i*. You can also pass `geo_reference=Geo_reference(0, 0)`. All of this is plain numpy, so gmsh is not needed inside ANUGA.
2. `anuga.create_domain_from_file("x.tsh" or ".msh")`. ANUGA's `.msh` is its own NetCDF format, not gmsh's `.msh`, so this would need a converter. It isn't worth it.
3. `create_domain_from_regions(..., breaklines=...)` is today's path. It can't grade size by distance from a line, only by region polygon.

Decision: gmsh runs in the *QGIS-side or ANUGA-side* Python and writes `points`, `vertices` and `boundary` into the job npz (`mesh_points`, `mesh_tris`, `mesh_edge_tags`). When those keys are present, the worker calls `anuga.Domain(...)`. Otherwise it keeps the current code untouched. That is what guarantees byte-identical default results.

## 4. Pipeline (gmsh mode)
1. **Outer boundary**: the same `mesh_polygons()` outer ring, so the extent is identical to the default. Each boundary segment keeps its physical tag `outflow`. Room is left for later tags, such as a Dirichlet inflow boundary.
2. **Breaklines**, all clipped to the outer ring:
   - The flow path from the inflow point: the existing D8 flow path or `refine_lines`, plus NHD or stream lines in the pack where present.
   - Road and rail embankments from the existing road layers and `refine_lines`.
   - The `structures.json` and culvert lines: the culvert centreline and both end points as embedded points.
   - The breach polygon and the dam crest line.
   - Pre-processing: union and node the lines with shapely (`unary_union`), so crossings become shared vertices. Snap vertices closer than `h_min/2`. Simplify to `h_min/2`. Drop pieces shorter than `h_min`. Unnoded crossings are where the slivers came from in the prototype.
3. **Geometry**: one plane surface with the lines embedded (`gmsh.model.mesh.embed(1, curves, 2, surf)`). Closed rings reuse their first point, because duplicate points made gmsh hang in the prototype.
4. **Size fields** (`Distance` + `Threshold`, combined with `Min`, set as the background mesh). `MeshSizeExtendFromBoundary`, `FromPoints` and `FromCurvature` are all set to 0.
   - Roads, rail, breach and dam: SizeMin = h(min_area) inside 60 m, ramping to SizeMax by 1500 m.
   - Channel: SizeMin = h(channel_area) inside 250 m, ramping to SizeMax by 2500 m.
   - Culvert ends and the inflow: h(min_area) inside 40 m, ramping out by 600 m. **No 25 m2 triangles**, so the smallest triangle doesn't set the timestep (see 6).
   - Optional: a slope or curvature field from the DEM (|∇z|) for terraces and bluffs. This is stage 3.
   - Here h(A) = sqrt(4A/√3), the edge of an equilateral triangle of area A.
5. **Mesh**: `Mesh.Algorithm = 5` (Delaunay; 6, Frontal-Delaunay, was much slower with strong grading), then `optimize("Laplace2D")`. Check the minimum angle. If any triangle is under `MIN_ANGLE_DEG`, run a local fix-up (collapse or flip) and report the count.
6. **Export**: node coordinates, triangles reordered counter-clockwise, and boundary edges mapped to `(tri, edge): "outflow"`. A mesh QA block (count, min and 1 % angle, min area, min inradius) is written into the job and into `run_report.json`.
7. **Worker**: `anuga.Domain(points, tris, boundary, geo_reference=Geo_reference(0, 0))`. Quantities, inlet, culverts, boundary and envelopes are unchanged.
8. **Fallback**: if gmsh is missing or fails, the run uses the default mesher and the report says so. It never fails silently.

## 5. Where gmsh runs, and licensing
- gmsh is **GPL-2.0-or-later**, with an exception for linking to some libraries. QGIS itself and QGIS plugins are GPL-2+ as well, so using gmsh is license-compatible. The repo has no LICENSE file today, so the plugins should state GPL-2.0-or-later in metadata and a LICENSE file. That is worth a separate PR.
- It should still be an **optional runtime dependency**, imported lazily, and never vendored or bundled in the plugin zip. The reasons are a ~60 MB binary, GL and X libraries on Linux, and keeping the plugin redistributable regardless of how gmsh is installed. Run it **in the ANUGA environment** (the worker interpreter), which already holds heavy optional dependencies, and not inside QGIS. Installation: `pip install gmsh` in the ANUGA env. `check_anuga()` reports whether gmsh is importable.

## 6. Cross-platform wheels (PyPI gmsh 4.15.2)
Wheels: `win_amd64`, `macosx_10_15_x86_64`, `macosx_12_0_arm64`, `manylinux_2_24_x86_64`. All of them are tagged **`py2.py3-none`**: the Python module loads `libgmsh` with ctypes, so the wheels don't depend on the Python version. They work with whatever Python QGIS 3.34 or the ANUGA env uses (OSGeo4W Python 3.12, the macOS bundle's 3.9 to 3.12, distro 3.10 to 3.12).
Gaps: there is **no Linux aarch64 wheel**. On Linux, libgmsh needs `libGLU.so.1`, libXcursor, libXinerama and libXft. Here it failed with `libGLU.so.1` missing until `libglu1-mesa libxcursor1 libxinerama1 libxft2` were installed, so the check must report this. `pip install gmsh-sdk`, or conda-forge `gmsh`, are alternatives.

## 7. Prototype (mesh only, no ANUGA): `docs/gmsh_prototype/`
Synthetic 32.2 x 32.2 km (20 x 20 mi) square with a meandering channel, two roads and a rail line, a breach box and two culvert points. The baseline is emulated in gmsh (a 5 km radius at 100 m2, 1000 m2 elsewhere). It is not ANUGA's own Triangle mesh, so it shows orders of magnitude, not exact counts.

| mesh | triangles | mesh time | min angle | min inradius |
|---|---|---|---|---|
| current-style 100/1000 m2 | 1,878,331 | 61 s | 24.8° | 2.25 m |
| gmsh, 100 m2 lines / 5,000 m2 far | 942,153 | 61 s | 15.7° | 1.63 m |
| gmsh, 100 m2 roads, 250 m2 channel / 20,000 m2 far | 368,209 | 20 s | 13.3° | 1.31 m |

(The first gmsh runs, with 25 m2 culvert regions, reached a min inradius of 0.42 m. Sizes now have a floor at 100 m2.) The remaining slivers sit where unnoded lines cross. That is fixed by noding in step 4.2.

## 8. Expected speed-up (reasoning)
ANUGA uses one global timestep set by the CFL limit of the smallest wet triangle: dt ∝ r_in / (|u| + √(gh)). Cost scales with N_triangles / dt, so it is ∝ N / r_in,min.
- Option B: 0.50 x N, r 1.63 vs 2.25 → about 0.69 x the cost, **about 1.4x faster**.
- Option C: 0.20 x N, r 1.31 → about 0.34 x the cost, **about 3x faster**.
- If noding brings r_in,min back up to ≥ 2.25 m (the target), B is about 2x faster and C about 5x. Absaraka would go from ~30 h to ~6 to 15 h.
- Caveat: the dt limit comes from the triangle that is both small and fast, usually at the breach and culverts. Making the coarse area larger only helps if the minimum size doesn't go down. The test must log dt statistics.

## 9. Risks
- Slivers or small triangles at line crossings cut dt. Mitigation: noding, snapping, a min-angle gate, and an `h_min` floor.
- Coarse far-field cells (20,000 m2, about 215 m edge) smear arrival times and depth on floodplains with no lines. Limit SizeMax in the flooded area, or add a DEM slope field.
- Embedded lines can hit the outer ring tangentially or overlap, making gmsh hang. That happened in the prototype with duplicate points. Use a timeout and fall back to the default mesher.
- Culvert operators need enough triangles at the end points. Keep h ≤ 2 x culvert width within 40 m.
- Linux GL dependencies, and no ARM64 Linux wheel.
- Results differ from the default by design, so they need their own validation (below), and the default stays the reference.

## 10. Test plan
Unit, light (any bot):
- With `ANUGA_MESHER=default`, `anuga_job.npz` is byte-identical to `main` (hash test) and has no `mesh_*` keys.
- The gmsh mesher on synthetic and small real inputs: triangles counter-clockwise, all boundary edges tagged, no orphan nodes, min angle ≥ 20°, min area ≥ min_area/2, and all breakline vertices present as mesh nodes.
- When gmsh is absent, it falls back and leaves a note.
- Worker: `anuga.Domain(points, tris, boundary)` on a tiny mesh, built only, with no evolve.

Benchmark for **Grok Bot** (heavy runs, Absaraka, ANUGA then LISFLOOD, same breach and hydrograph):
1. Baseline: current default mesh (existing r-runs or a rerun). Record triangles, wall time, dt min, median and max.
2. gmsh B (100 / 5,000 m2) and gmsh C (100 / 250 / 20,000 m2), both with STRUCTURES and culverts on.
3. Compare flooded acres, max depth at the dam and at 1, 5 and 10 mi, arrival times at the hazard points, max depth and velocity on each overtopped road, the depth-difference raster (P50 and P95 |Δ|), and the hazard class. Pass if the class is unchanged, flooded area is within ±3 %, arrival is within ±10 % or ±15 min, and road max depth is within ±0.2 ft.
4. DWR-2801 and DWR-3161 as second sites.

## 11. Staged PRs
1. **This PR**: the doc and mesh-only prototype scripts.
2. `core/gmsh_mesher.py` (lazy import, noding, size fields, export and QA) plus `anuga_worker` support for `mesh_points`, `mesh_tris` and `mesh_edge_tags`, plus the `anuga_mesher` setting (default `default`, hidden), plus unit tests and the gmsh check in `check_anuga`.
3. Exposing the option in the Runner and tool 7b with help text, and mesh QA in run_report. Then Grok Bot's benchmark.
4. Tuning (slope field, SizeMax inside the flood extent), and the default changes only after the user signs off.

## 12. After the first real run (Absaraka 20 x 20 mi, 2026-10-18)
- **Gate.** gmsh's smallest triangle was 49.93 m2 against `0.5 x 100 m2`, on a mesh with a 31.3° minimum angle, so the Runner quietly used the default mesh. The default `min_area_tol` is now **0.45** (`anuga_gmsh["min_area_tol"]`, or `NDBREACH_ANUGA_GMSH_MIN_AREA_TOL`). The gate is `tol x` the **smallest intended size**, `min(min_area_m2, culvert_area_m2)`, not always the base area. A smaller culvert size makes small triangles on purpose and must not fail the gate. What the gate has to catch is triangles much smaller than anything that was asked for. Slivers are caught by `min_angle_deg`.
- **Mesher choice.** `ANUGA_MESHER` / `NDBREACH_ANUGA_MESHER`:
  - `default`: unchanged.
  - `gmsh`: **strict**. A rejected mesh, a gmsh error or the 600 s timeout stops the run with the reason.
  - `gmsh_or_default`: falls back to the default mesh, with a `WARNING:` log line and `gmsh mesh rejected: <reason>; used the default mesh` first in "Things to check" and in its own "ANUGA mesh" section of run_report.md.

  `run_report.json` has `anuga_mesh` (requested, used, rejected, gmsh stats, embankments, every pass) and `summary.anuga_mesher`.
- **Culverts.** As planned in 4.4, the culvert ends get the point field at `h(min_area)` within 40 m, measured at about 140 m2 mean near each Absaraka culvert. There are no 25 m2 octagons. `culvert_area_m2` (default = `min_area_m2`) can ask for smaller ones, and the gate follows it.
- **Road and rail embankments (4.2).** These weren't wired before: only `REFINE_LINES` reached the mesher. Now, with a gmsh mesher, the RECEPTOR_ROADS and RECEPTOR_RAIL centrelines go into the mesh. A centreline counts as being on an embankment where the crown (the highest ground within 3 m) is ≥ 0.3 m above the ground 20 m to both sides, read at ≤ 5 m. Gaps ≤ 30 m are bridged, pieces must be ≥ 100 m long, and only roads within 150 m of the LISFLOOD-FP flood are used. Those pieces become embedded mesh lines (options `embankment_*` in `anuga_gmsh`; `embankment_lines: false` turns this off).

  Before noding, `declutter()` drops stretches within `h_min` of, and within 30° of parallel to, a line that is already kept (double track, a road beside the rail). It also moves loose ends that stop within `h_min` of a line onto that line. `node_lines()` then splits an edge at any node within the snap distance. Without these steps, the Absaraka NG911 + NARN lines gave a 1.5° minimum angle.
- **Caveat.** ANUGA takes its elevation from the model DEM at the triangle centroids. With CELL 40 m, an embankment narrower than a cell is still smeared in elevation. The line keeps the triangle edges along the road, but not the crest height.
