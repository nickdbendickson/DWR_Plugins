# Changelog

## 2026-11-13

- **Engine sync check across crest and DamIT.** The old `check-engine-parity.mjs` hashed `engine.ts` and
  `warnings.ts` against a pin kept in the same repo, so it never compared crest with `damit_crest` (the two repos'
  pins already differed), was not run by CI, and did not cover `spread.ts`, `outline.ts` or `routing.ts`.
  `scripts/check-engine-sync.mjs` reads both copies and compares them line for line; the only allowed differences are
  the four line pairs in `scripts/engine-sync-allow.json` (the VERTCON grid import, the disclaimer wording, two comment
  lines), each with its reason. An allow entry whose line has gone fails the check, so the list cannot rot. The
  `Engine sync` workflow runs it on every push / PR and daily (so a damit-side change shows up), checking damit out
  sparsely. The same script and allow list go into damit. Tests: `scripts/check-engine-sync.test.mjs`.
- **Content-Security-Policy and Subresource Integrity.** Crest had neither (the open item from the security passes;
  an earlier test even asserted there was no policy, so the PLSS lookup could not be blocked by one).
  `src/lib/crest/csp.ts` now builds one policy for all three shells: scripts from the app only, `object-src 'none'`,
  no framing, and `connect-src` / `img-src` limited to the services the desk names (USGS tiles, OpenStreetMap, 3DEP,
  ND 911, BLM PLSS, and a `VITE_CREST_TILE_URL` server when set). The web app sends it as a header with a per-request
  nonce (`src/server.ts`; the router puts the nonce on the framework's inline hydration script), the desktop server
  sends it from `web/security-headers.json` on every response, and `Crest.html` carries it in a `<meta>` tag with the
  SHA-256 of its one inline script. No `unsafe-inline` / `unsafe-eval` for scripts. The desktop `index.html` carries
  `sha384` integrity hashes for its script, preloads and stylesheet; `.gitattributes` keeps line endings in
  `Crest.html` and the bundle from being converted (that would break the hashes). `CREST_NO_CSP=1` drops the desktop
  policy if a webview needs it. `vercel.json` adds `nosniff` / `X-Frame-Options` / `Referrer-Policy` to static files.
- **The two Google Fonts are gone.** The web shell linked IBM Plex Sans and Source Serif 4, and no style rule used
  either. The link is removed, so the web app loads nothing from a third-party origin.
- **Browser click-through test.** `npm run test:e2e` (`e2e/`, Playwright + Chromium) opens `Crest.html` from disk, the
  desktop bundle behind its Python server and the built web app, and walks the eight steps on the first sample dam:
  record, breach, build the terrain layer (worker), 911 lookup, results, dashboard, screen, report, download
  `dam.json`. Every public service is stubbed from the committed fixtures, so it needs no network. It fails on an
  uncaught error, a console error, a request to a host it does not know, or anything the policy blocks, and checks an
  injected inline script is refused. CI runs it (new `e2e` job), and the desktop Python tests now also run on
  Windows and macOS runners.

## 2026-11-08

- **README starts with how to run Crest.** The first user instruction sat at line 192, after the scripts table and
  the deploy-migration notes. A **Start here** section now opens the README: the three ways to run the same desk
  (`Crest.html`, the desktop app, the web app), the eight steps in a sentence each, and what needs the network: the
  basemap tiles whenever a map shows; the terrain layer, the 911 points and the PLSS sections one request each on a
  button, saved with the dam; Google Fonts on the web app only. The false "Tiles are the desk's only network use"
  (`__root.tsx` loads two Google Fonts; `outline.ts`, `nd911.ts`, `plss.ts` and the dashboard hillshade all fetch) is
  corrected there, in the Basemap and Desktop sections, in `vite.desktop.config.ts` and in the desktop smoke test's
  wording. The scripts table moved under **For developers**.
- **Warning codes.** Only the two NID height checks had codes (W-H1, W-H2); every other review flag was free text,
  rendered with the text as its React key (two equal texts collided) and impossible to name in a report or a bug.
  `src/lib/crest/warnings.ts` is the catalogue: `Warning = { code, severity: info | warn | stop, text }` with W-D1
  (datum), W-V1 (volume / input band), W-R1 (reach incomplete), W-E1 (extent probe failed), W-P1 (pad terrain
  failed), W-S1 (layer stale) beside W-H1 / W-H2. The engine's datum and flood checks, `volumeCheck`, the NID height
  checks and `outlineState`'s stale message now carry codes; `Analysis.datum.flags` / `flood.flags` sit beside the
  unchanged `warnings` string lists, and `warningText()` / `datumFlags()` / `floodFlags()` adapt either way, so every
  consumer and test that expects strings still gets exactly the text it had. No threshold or wording moved. The
  Record aside, the Flood flags, the guided-setup and dashboard pills and the paper report show the code before the
  text with stable keys; the README lists the codes. Tests: `warnings.test.ts`.
- **Record step validates what is typed.** The number fields took anything and the engine clamped silently to a
  warning shown only on the Record aside. Height (4–200 ft), storage (1–500,000 ac-ft) and length (40–20,000 ft) now
  carry `min` / `max` from the one `INPUT_RANGE` the engine clamps with, every number field has `inputMode="decimal"`
  (the numeric keypad on a phone), and the field's own out-of-range text (the same W-V1 sentence) shows under the box
  in amber with `aria-invalid`. Latitude and longitude carry their ranges, and the two swapped (a North Dakota
  longitude in the latitude box) is caught under both fields and on the guided Record card (`guided.ts
  locationProblem`, shared by `recordProblems`).
- **Results: the "Terrain outline" map mode works.** It was permanently disabled with a note about a branch merged a
  week ago. It now shows the layer saved with the dam (the routed flood or the bathtub outline, read with
  `outlineState` the way the guided setup does, labelled accordingly), fits the map to it, and is disabled with the
  layer's own reason ("not built yet", "inputs changed, build it again", offline) only when there is nothing current
  to show; if the layer goes stale while showing, the map falls back to the desk strip.
- **Batch table: sort, filter, sticky header, copy id, no 760 px floor.** The results table is its own component
  (`BatchTable.tsx`, on `@tanstack/react-table`): every column sorts (triage, screen and status in worklist order,
  numbers with blanks last; a third click restores the worklist order), one box filters on triage, county, id, name
  or status, the header stays while the body scrolls, each row's id has a one-click copy, and the county, screen and
  released columns fold away below the sm / md widths (the CSV has them all) so the table fits a phone. The default
  order is still `sortRows`. The run loop in `BatchScreen.tsx` is untouched.
- **Batch "Open in desk" no longer adds a record on every click.** It selects the record already on the desk for
  that structure when there is one (an earlier open, or a record from the NID / DWR pickers, matched on the structure
  id in `nid` / `nidId` or the batch record id) and only adds one when none exists; a dismissable note on the desk
  says which it did and that the terrain layer is rebuilt on the Flood step in one click.
- **Batch time estimate learns from the run.** It was the fixed 8 s / 25 s prior halved. `batch.ts runningEstimate`
  keeps that prior until three rows of the run are timed, then uses the mean of `row.seconds` per size class (a class
  with no timed row borrows the overall mean scaled by the prior ratio) over what is still to run, two at a time,
  updating as rows finish; the summary line says the measured per-structure times once it has them.
- **Accessibility basics.** The screen-level badge was colour alone: it now carries a glyph-and-letter mark (▲ H,
  ■ M, ● L, ? R) and an `aria-label` with the level's meaning. The batch load error has `role="alert"`. The map
  legend's layer toggles (flood strip, terrain outline, PLSS, 911) are real buttons with `aria-pressed`, in tab order,
  with a visible focus ring over the tiles.
- **Dead app-builder scaffolding removed.** `src/lib/{auth,app-data,multiplayer,og}`, `src/lib/db.ts`, the preview
  bridge and path-prefix rewrite, `server/`, `migrations/`, `scripts/{migrate,migration-plan,preview*,brand-check,
  sign-out-plan,check-auth-invariant,grok-*,browser-*,write-atomic,with-app-env,app-env-plugin}.mjs` and their tests,
  `.grok/`, `public/__grok` and `public/og.jpg`: all imported only by `src/routes/__root.tsx` (AuthProvider, the
  preview bridge) or by nothing, with `VITE_AUTH_ENABLED=false` and `deploy.database=false`. `__root.tsx` now renders
  the desk alone; `vite.config.ts` is TanStack Start + Nitro (Vercel preset) with no plugins of its own; `npm run
  build` is `vite build` and `vercel.json` runs it (no migration step; `build:deploy`, `db:migrate`, `check:auth`,
  `preview:*` are gone). Dependencies dropped after a repo-wide import grep: `@electric-sql/pglite`, `pg`,
  `@types/pg`, `kysely`, `better-auth`, `jose`, `@tanstack/react-query`, `react-hook-form`, `@hookform/resolvers`,
  `zod`, `playwright`, every `@radix-ui/*`, `cmdk`, `vaul`, `sonner`, `react-day-picker`, `date-fns`, `recharts`,
  `react-resizable-panels`, `class-variance-authority`, `tailwind-merge`, `clsx`, `tw-animate-css`. The
  `package-lock.json` was pruned to the packages the remaining tree reaches (kept entries byte-identical, flags
  recomputed); `npm install` after pulling confirms it. `build-deps.test.mjs` now also fails if a removed dependency
  comes back or a listed one is imported by nothing. The SSR shell stays: TanStack Start is not a one-line swap for
  a static SPA (the route tree, `Scripts` / `HeadContent` and the Nitro preset are wired through it).
- **The inundation layer builds in a Web Worker, and can be cancelled.** On a 10 m terrain (ND00199's box is 1,090 ×
  996 cells) the build ran about 4.7 s on the main thread with 2 s stretches that never yielded (the volume spread, the
  polygoniser), so the page froze and a click on Cancel had nowhere to go. The trace, routing, spread, settle and
  polygonisation now run in a dedicated worker (`src/lib/crest/inundation.worker.ts`, inlined into `Crest.html` the
  way the results worker is), the status lines still arrive, and **Cancel** on the Terrain inundation panel stops the
  build. Where a worker cannot start the build runs in-thread as before, now yielding inside the spread and the
  polygoniser too. The layer itself is unchanged: the depth grid and the GeoJSON hash the same as before.
- **Batch screen: a worker pool, and rows kept in IndexedDB.** The batch builds each structure's layer on a pool of
  inundation workers sized to the machine (one per spare core, at most 4; the "at N at a time" estimate follows it),
  so the tab stays live through a county. Result rows go to IndexedDB in one transaction every 10 rows or 2 s instead
  of rewriting one localStorage string per row (a statewide run would have hit the ~5 MB quota); rows an older Crest
  left in localStorage are moved across. The finished records kept for **Open in desk** no longer hold their terrain
  layer (2–5 MB each; it is rebuilt on the desk in one click), and the running worklist is kept in order by inserting
  each arriving row rather than re-sorting.
- **Maps draw the terrain layers on canvas, simplified to the zoom.** A 10 m routed layer (six MultiPolygons, ~80,000
  vertices) was drawn as SVG paths and rebuilt on every render that re-derived it. The Flood-step and dashboard maps
  now draw a copy simplified to about half a pixel at the current zoom level on a Canvas renderer, re-simplify only
  when the zoom level changes, and redraw only when the layer's content changes (`src/lib/crest/geo-simplify.ts`;
  exports, areas and the 911 / PLSS lookups keep the exact rings).
- **Faster passes in the engine, same numbers.** The spread sorts its cell index once (a typed-array merge sort) instead
  of boxing and sorting every stretch's cells; the pool settling visits only the wet span of each row and stops early
  once nothing moves more than 1e-4 ft in a sweep (`SETTLE_TOL_FRACTION`; it reports the sweeps it ran); the band
  polygoniser traces every band's edges in one pass over the grid instead of six; the 3DEP response is read with one
  typed-array copy instead of a DataView call per cell. The 30 m and 10 m ND00199 builds are bit-identical to before.
- **`Crest.html` no longer parses the structure tables on open.** The three data bundles (2.2 MB of JSON, 1.64 MB of
  it the batch's structure list) were flattened into the single file's one script and parsed on every open. They are
  now packed gzip + base64 (about 450 KB; the file is about 1.7 MB smaller) and inflated with `DecompressionStream`
  the first time the picker or the Batch view asks (with fflate's gunzip where a browser lacks that API); the web and
  desktop builds keep them and `geotiff` as their own lazy chunks (`vite.desktop.config.ts`). Rebuild the desktop
  bundle and `Crest.html`.
- **The desk says when it is no longer being saved.** A localStorage overflow — a big record, or the auto-fetched
  `nd911` address cache (the 911 panel fetches on mount, thousands of points over a town) — was only `console.warn`'d,
  so from then on every edit (a hazard-class override, receptors, elevations) lived in memory only and was lost on
  reload with no sign. `deskStorage.setItem` now records the failure on a `useSaveStatus` store and the desk shows a
  banner, so a desk that is no longer being saved says so. No computed value changes. (audit H16, failure channel;
  moving the `nd911` / `plss` caches out of localStorage into IndexedDB so the overflow cannot happen is a separate,
  flagged follow-up.)
- **A stale tab no longer clobbers a newer tab's saved desk.** Two tabs each hold their own records, and zustand
  persist writes the whole desk on every edit, so a tab holding an old snapshot overwrote a newer tab's save and the
  next load's orphan GC then deleted the dam and its IndexedDB layer the other tab had added. Each save now bumps a
  monotonic counter (`crest-desk-seq`); a tab that sees a newer counter from another tab stops saving — so it never
  overwrites the newer desk — and the desk asks the engineer to reload. No rehydrate, no new deletion path, and the
  GC is unchanged (a bare storage-event rehydrate destroys every saved layer, and merge-on-write breaks `restore()` —
  both verified). No computed value changes. (audit H17)

## 2026-11-07

- **A flood faster than the ACER TM 11 chart's velocity axis is high danger, as in the suite.** `acer.ts zone()` read a
  velocity past the chart's last column (house 25 ft/s, mobile home 16 ft/s) AT that column, so a shallow fast flood
  screened judgment zone / Medium, and its header claimed the suite had no velocity-past-axis rule. The suite's
  `tm11_curves.zone()` returns HIGH past either axis ("the figure does not say a faster or deeper flood is safer"),
  and the Helper and the Runner screen with it. `zone()` now does the same. Direction: Medium → High for a wet house
  shallower than 0.73 ft at more than 25 ft/s and a mobile home shallower than 1.86 ft at more than 16 ft/s; imported
  LISFLOOD / HEC-RAS runs ranked on the desk now agree with the Helper and the Runner. `acer.test.ts` locked the wrong
  result (`zone("house", 0.5, 30)` as "judgment") and is inverted. (Grok audit 2026-11-07, 3.3)
- **Every export says which Crest wrote it.** A new build version `crest_version` = `<package.json version>+<short git
  SHA>` (`version.ts`, injected by both Vite configs from `scripts/crest-version.mjs`; `+local` without git) is stamped
  on the flood strip and terrain GeoJSON (collection and features) and KML, in `dam.json` (which carried no version at
  all), as a comment row in the batch CSV and in the Report footer. `ENGINE_VERSION` stays the engine's own method
  version, bumped to 2026.11.07 with a comment that it must move whenever `analyze()` changes its numbers; it had
  stayed at 2026.10.30 through every numerics change while the terrain layer's `TRACE_VERSION` went 2 → 8. The
  version is the committed version + the SHA of HEAD, never a timestamp, so the committed bundles stay reproducible.
  package.json gains `"version": "0.1.0"`. Exports snapshots regenerated.
- **Failures that were silent now say what did not happen.** (a) The terrain outline's extent probe swallowed every
  error and the triage read "no extent check (built before it, or the probe could not be built)": the layer now carries
  `stats.extentError` and the triage says "the routed-spread probe failed (<reason>)". (b) A 3DEP 502 or timeout
  during the 911 pad lookup marked the pads "estimated" with the note "no terrain at the point", on the desk and in the
  batch: the note and the screen's Review text now say "terrain request failed (<reason>), look the pads up again",
  and the Flood step's 911 panel offers that retry; "no terrain at the point" is kept for a NoData hole in a grid that
  came back. (c) A failed 911 lookup in the batch left `structures_inside` null but let the screen and triage run on
  zero receptors; the row's note now reads "911 lookup failed (<reason>); structures unknown", `structures_flooded` is
  null too, and the triage is forced to at least "Uncertain" with that reason (never "Clearly trivial"). (d) An
  IndexedDB error while saving a dam's inundation layer resolved silently while the desk said "saved with this dam":
  `saveOutline` now resolves false, warns, and the desk's terrain panel says "layer not saved (storage error: …)".
- **The batch command line resumes from a `batch.jsonl` cut short by a kill.** A row appended as the process was killed
  left a partial last line that `JSON.parse` threw on, so the next start crashed and the run could not resume. Rows are
  now written as one fsynced line each, a cut-short last line is dropped with a warning (that structure runs again),
  and a corrupt line anywhere else stops the run and names the line number. The CLI keeps every row in the file across
  resumes while the desk's Resume keeps only the current selection; that difference is now documented in the code and
  the README, not changed. New `scripts/batch-dams.test.mjs`.
- **The desk notices a stale dam inventory.** The bundles record when they were pulled (2026-10-29 NID, 2026-10-30 DWR,
  2026-11-05 structures) but nothing compared that to today. `data-provenance.ts bundleAge(now)`: past 180 days the
  picker footer and About the dam data show an amber "Inventory is N months old; `npm run data:nd-dams` refreshes it"
  line, the Report footer and the batch CSV's comment rows carry each bundle's pull date and age.
- **Accuracy envelope brought up to the 2026-11-05 re-score and guarded.** `docs/accuracy-envelope.md` still showed the
  2026-11-04 (34524e0) scores (2801 0.255 / 0.192) while the 11-05 settle (8bc4d20, PR #75) had re-scored them (2801
  0.331 / 0.233, 2865 0.344 / 0.350, 3034 0.840 / 0.765, 4031 0.429 / 0.136, this changelog below); the trust map and the
  LISFLOOD table now carry those, citing the entry and the commit (the per-cell recall / precision / bias detail is
  marked as the 34524e0 measurement until re-measured). `triage.ts`'s module comment quoted older numbers still and is
  fixed. The page now says "Scored on trace-version 8" and `outline.test.ts` fails with "re-score the envelope" when
  `TRACE_VERSION` moves past that line.
- **CSV cells can no longer open as spreadsheet formulas.** The six CSV writers (batch, PLSS, 911, Runner worklist,
  triage, paper report) each had their own quoting and none neutralised a cell starting with `=`, `+`, `-` or `@`,
  which Excel, LibreOffice and Sheets run as a formula; the 911 address fields and DWR text are third-party. One
  `csv.ts csvCell` now prefixes such text with an apostrophe (a formatted number such as `-97.325185` is left alone)
  and all six use it.
- **A judgment-zone home is Medium by rule on an imported run, not High.** `rankSample` and
  `analysisOfRecord` defaulted `judgmentAsProbable` to `true`, so an imported run with the flag
  unset classed a habitable structure in the ACER judgment zone as High (probable loss of life),
  while `acer.ts`, `engine.ts`, `types.ts` and the desk control all default `false` (Medium by
  rule, Nick 2026-10-30). Both now default `false`; this lowers such a structure from High to
  Medium on imported runs with the flag unset. Homes above the high line are unaffected. (audit H22)
- **Batch resume retries dams whose previous attempt failed.** The resume skip-set was built from
  every id in `batch.jsonl` regardless of status, so a dam whose last run failed (3DEP/911 briefly
  down) was treated as done and never screened. The skip-set now excludes `failed` rows; the jsonl
  stays append-only and the CSV dedupes by id, last row winning. Same fix in `scripts/batch-dams.mjs`
  and `BatchScreen.tsx`. (audit H04)
- **Desk panels no longer write one dam's data onto another.** `pickAuto`, `readGround`, the 911
  auto-lookup, the pad lookup, the manual 911 lookup and the PLSS lookup each fired a long 3DEP/911
  read and then wrote the result onto whichever dam was active when it resolved; switching dams
  mid-read corrupted the other dam's geometry, DEM crest, 911 cache, pads or PLSS. Each write is now
  guarded by the active dam id captured before the await, mirroring the inundation-build guard. (audit H25, H26)

## 2026-11-05

- **Auto-derived records aim the schematic strip down the traced channel (two-pass), not the first downhill.** A
  record from the Batch screen or *New record from DWR* has no drawn dam line, so the line was laid across the
  downhill: the lowest ground within 500 m of the inventory point. On DWR-2662 / ND00049 (Swan Buffalo Detention
  Dam 8) that is 46° (the first ~1 km of Buffalo Creek), but the creek bends east and the straight strip ran off to
  the north-east (strip CSI 0.00 against the Runner's LISFLOOD-FP run). The batch now traces the drainage with that
  first line (`traceChannel`), then re-lays the synthesized line across the chord to the trace 4 km down the channel
  (`aimAlongChannel` / `channelAim` in `batch.ts`; kept when the trace is under 1 km or turns under 5°). DWR-2662:
  bearing 46° -> 84°, strip CSI 0.00 -> 0.10 (0.01 -> 0.13 at 1 ft), routed spread 0.29 -> 0.35 (0.26 -> 0.27), on
  the local 1 m mosaic, piping. A drawn dam line is never changed. The strip is still straight: it does not follow
  bends past the chord. One more terrain request per auto-derived structure. Tests: `batch.test.ts` (channelAim;
  a DWR-2662-like valley that leaves north and bends east).
- **New records default to piping** (Nick, 2026-11-05), the failure the Runner and the ND screening use; overtopping
  stays one click away on the Breach step. `blankDam()` and every record built from it (NID, DWR, batch).
- **Building a big inundation layer no longer fails with "The quota has been exceeded."** The desk saved its whole
  state, each dam's built layer included, to localStorage on every edit. A routed terrain flood is a large GeoJSON
  (ND00199 / DWR-2801: 5.2 MB), past Safari's ~5 MB per-site limit, so the save threw a QuotaExceededError out of the
  desk's `update()` and the guided setup showed it on the Terrain layer step. The layers now live in IndexedDB, one
  record per dam (`src/lib/crest/outline-store.ts`), and localStorage keeps only the light records (4.5 KB for the
  sample desk, from 5.2 MB). A layer saved by an older Crest is moved across on load. Saving to localStorage can no
  longer throw into the desk: a full store only logs a warning. Tests: `store.test.ts` (a 200 KB quota stand-in).
- **The routed flood draws as one sheet instead of a fan of wedges.** Each spread stretch is about one routing step
  long while its ground reaches up to 3 km sideways, so on a plain or round a bend its ground is a long thin wedge and
  each slice stood at its own level on its own wedge: next to a wet wedge lay a dry one on lower ground, dry only
  because it belonged to another stretch. The pools now settle (`spread.ts settlePools`, 100 sweeps): across every
  edge of the spread's ground, water moves from the higher surface to the lower, never more than the giver holds above
  the wet threshold, so water is only moved, never added. The reach loop's demand signal is the pour's, unchanged, and
  only the round kept is settled. CSI vs the LISFLOOD baseline (0.1 ft / 1 ft): 2801 0.255/0.192 → 0.331/0.233,
  2865 0.327/0.303 → 0.344/0.350, 3034 0.765/0.714 → 0.840/0.765, 4031 0.362/0.165 → 0.429/0.136 (4031 at 1 ft is
  8 ac of reference). TRACE_VERSION 8. `score-routed.mjs --settle N` (0 = as poured).
- **The terrain layer has an outline.** A dark line around the flood (Nick picked option 3 of the mock-up): the wet
  cells closed by 30 m (gaps under about 60 m bridged), islands and holes under 5 ac left out, edges smoothed
  (`outline.ts extentOf`). The depth bands stay the exact wet cells and lose their own thin edges. It is drawn on the
  guided-setup, Flood and Results maps, and goes out in the terrain GeoJSON as one more feature (`kind:
  "flood_extent"`). A layer built before this keeps its old look until it is built again.
- **Batch screen: many structures at once, including every DWR dam and the DWR dugouts.** A new **Batch** view picks
  structures one by one, by county, by HUC8 watershed or statewide, from the NID main dams (948), the DWR Dams layer
  (8,955 more, all statuses but Removed / Not Built) and the DWR Dugouts layer (1,766), and runs each through the
  desk's own breach, inundation layer, 911 lookup and triage with an auto-derived record (crest from DWR NAVD 88 or
  3DEP at the point, direction from the 3DEP downhill). Results sort as a Runner worklist with a CSV and Open in desk.
  `npm run batch:dams` does the same from the command line, resumable, and reads a local DEM mosaic with `--dem`.
  New bundle `nd_structures.json` (`npm run data:nd-structures`).
- **DWR Unknown Structures in the batch and the DWR fill.** From Nick's DWR export (not on the map service). Most
  (3,611 of 3,770) are already DWR Dams records under a new number, or NID dams: their UNK- number is now an alias that
  finds that record (UNK-2400 is DWR-16431). The other 144 are listed as "DWR unknown structure", status Unknown
  (on by default).
- **Local DEM, DWR fill, map location and breach point on the desk.** The desktop app can use a local DEM file (e.g.
  the statewide 1 m VRT) for all terrain instead of 3DEP, chosen in a file dialog and remembered. The Record step can
  make a new record from any DWR dam or dugout, and a new dam's latitude and longitude can be set by clicking the map.
  The Flood step picks the breach point automatically (lowest downstream toe along the dam line) or by a click on the
  map, and the reservoir point by a click.

## 2026-11-04

- **The web app renders on the server without crashing, and hydrates without tearing the page down.** Crest is a
  TanStack Start (SSR) app. Two things broke a production (`vite preview`) load. The Leaflet maps touch `window` at
  module load, and they were `lazy()` inside `Suspense` with no client guard, so a map in the first render threw
  `ReferenceError: window is not defined` on the server. And the guided-setup step card read `navigator.onLine`
  straight during render, so the server (no `navigator`, treated as online) and a first client render that happened to
  be offline produced different step text and React threw a #418 hydration mismatch, regenerating the whole tree on
  every load. Maps now render inside a `ClientOnly` boundary (`useSyncExternalStore`, so the server and the first
  client pass agree on the fallback), and online status comes from the shared SSR-safe `useOnline` hook (starts
  `true`, updates after mount) instead of an inline `navigator.onLine` read. Verified against the production preview:
  no server `window` error, no #418. The static desktop bundle was already clean.
- **The Results step no longer overflows a phone screen.** Its top `<section>` was a grid with an unconstrained
  (`auto`) column, so its cards sized to their content (573 px) and ran off a 390 px viewport. The column is now
  `minmax(0,1fr)` and the map cell `min-w-0`, so the content wraps to fit; the wide layout is unchanged.
- **crest-desktop fits an 800 px screen.** The pywebview window's `min_size` was `(900, 600)`, wider than an 800 px
  display; it is now `(760, 560)`.

- **Desktop "Run the flood model" without a DEM point works again: Crest sends the Runner's option numbers, not
  their text.** With Point X / Y blank the desk runs `ndbreach:rundam` through `qgis_process`, passing METHOD and TIER
  as the option text. QGIS 3.34's `qgis_process` rejects that text ("Incorrect parameter value for METHOD", then
  TIER), so the button failed on both plugin sets (ND Runner 3.27.5, DamSafety Runner 3.14.5). Crest now reads the
  option list from `qgis_process help ndbreach:rundam` once per run and sends the matching index, so a reordered
  plugin still gets the right option; if help cannot be read it sends the text as before. The `ndbreach_cli.py` path
  (Point X / Y filled in) was not affected. Test: `crest-desktop/tests/test_handoff.py`
  (`test_model_sends_enum_indexes_read_from_help`).

- **The routing reads the terrain: a Muskingum-Cunge-Todini wave in defined valleys, a frozen Muskingum-Cunge wave on
  flat plains and tiny pools.** The frozen-coefficient Muskingum-Cunge translates a wave down a flat, wide reach about
  right but attenuates it too little in a cut valley, where the real wave slows and flattens as it fills the overbank.
  The variable-parameter Muskingum-Cunge-Todini scheme (Todini 2007) recomputes the celerity and width each step at the
  local flow and captures that attenuation, but it over-damps a shallow overbank on a plain, where the celerity
  collapses and the wave bogs. The router now chooses per reach, not per dam: a reach steeper than 0.0015 whose section
  carries the peak within its banks routes with MCT, the rest stay on frozen MC. One dam can be confined upstream and a
  plain downstream and gets both. The MCT coefficients are the Courant/cell-Reynolds form, which sum to one every step,
  so the scheme conserves mass (no manufactured volume, unlike naive variable-parameter MC). Four-dam re-score against
  LISFLOOD-FP (CSI at 0.1 ft / 1 ft): DWR-2801 0.255 / 0.191 → 0.255 / 0.192 (flat plain, held on MC), DWR-2865 0.318 /
  0.282 → 0.327 / 0.303 (incised), DWR-3034 0.694 / 0.653 → 0.765 / 0.714 (confined), DWR-4031 0.369 / 0.159 → 0.362 /
  0.165 (tiny pool, held on MC). The confinement trigger is insensitive from 0.0005 to 0.002 on all four dams. Saved
  routed layers ask to be built again.
- **The spread pours each stretch on the ground beside it, not on a stripe due north or south of it.** Each stretch of
  the routed channel pours its water on the cells nearest it. "Nearest" was counted in grid steps (a 4-neighbour
  search), and by that count a point off a slanting channel is equally near a long run of it: whole grid columns went
  to one channel cell, so on the flat DWR-2801 plain the flood came out as a comb of north-south stripes up to 3 km off
  the channel, deep where a stripe crossed a low and dry between. Nearest is now the straight-line distance. Four-dam
  re-score against LISFLOOD-FP (CSI at 0.1 ft / 1 ft): DWR-2801 0.227 → 0.255 / 0.175 → 0.191, DWR-2865 0.295 → 0.318 /
  0.311 → 0.282, DWR-3034 0.701 → 0.694 / 0.633 → 0.653, DWR-4031 0.354 → 0.369 / 0.101 → 0.159. Saved routed layers ask
  to be built again.
- **Water that tops a saddle fills the low behind it lowest ground first.** Every cell behind a saddle shares its spill
  height; the pour laid the overflow over them in raster order (north row first) and solved its level with cells above
  it counted at negative depth. It now fills the low from its lowest cell and holds the slice exactly.
- **Per-dam triage: "Clearly trivial: screen only", "Uncertain: run it" or "Needs a Runner run".** Crest is the suite's
  fast first-pass screener, never the layer of record, so each dam now gets an explicit call and a terrain confidence
  instead of only a footprint. Needs a Runner run: a large flood (the flood-size gate), a routed layer that is
  incomplete downstream, or anything flooded in Crest's screen. Clearly trivial: nothing flooded, on a complete footprint
  with high confidence. Everything else is uncertain. The confidence comes from a new extent check saved with the layer
  (`stats.extent`): how far the flood's edge moves with a foot of depth error (Crest's depth bias against the 2-D models
  is about a foot), measured on Crest's routed spread and the DEM it was built on. High at 60 m or less (confined valley
  or closed basin), Medium above it (open terrain: the footprint is a minimum), Low when the footprint is incomplete or
  unchecked; the words match the published accuracy envelope. A terrain-outline dam gets the check from a routed-spread
  probe on the same terrain (the outline stays the layer). On the four-dam baseline: DWR-3034 26 m (High; uncertain only
  because it ends at a road-fill basin a culvert could drain), DWR-2801 127 m (Needs a Runner run, Low: large and
  incomplete), DWR-4031 149 m and DWR-2865 158 m (Uncertain, Medium). The threshold is fit on four dams and is
  provisional. The Flood step shows the call with its reasons, the Dashboard carries it as a flag, and
  `scripts/triage-dams.mjs` triages a list of dam records into a CSV, Runner jobs first. Saved layers built before this
  show "build it again for a confidence".
- **Published accuracy envelope (`docs/accuracy-envelope.md`).** A trust map of where a Crest footprint can screen and
  where it needs a Runner run, scored on all four baselines at 0.1 ft and 1 ft (1 ft is the headline: the 2-D
  references agree with each other there). Confined valley / basin (DWR-3034) high, CSI 0.71 at 1 ft; incised valley
  (2865) medium, 0.30; tiny pool (4031) extent only, 0.17 at 1 ft; flat multi-channel plain (2801) low, 0.19, run the
  Runner. An *incomplete downstream* alert on the routed layer means the dam needs a Runner run. The 0.35 CSI floor is
  retired: LISFLOOD-FP, ANUGA and DSS-WISE agree with each other on 2801 only at 0.34–0.49 at 0.1 ft. 2801 against
  five references (LISFLOOD-FP 20/10/5 m, ANUGA, DSS-WISE): CSI 0.19–0.25 at 1 ft (median 0.21). Scored on 34524e0.

## 2026-11-03

- **The routed flood grows its terrain downstream on demand (issue #62).** The terrain box was sized from the schematic
  stations before any water was routed, and the drainage trace stopped at the schematic's last station or the box's
  edge; on DWR-2801 59 % of the Runner's flood lay outside the terrain entirely. The desk now grows the box and the
  trace downstream only while the routed wave still reaches the end of the traced channel wet, with water above a floor
  (5 % of the release or 10 ac-ft, the lesser) still travelling, and not stopped in a closed basin: at most four rounds,
  each a quarter of the channel (1–4 km) along the channel's own end direction, on the same cell lattice. A contained
  flood gets the single build it always got, cell for cell (DWR-4031, 0.354 unchanged). When the loop stops with water
  still at the edge the layer says so (an "incomplete downstream" alert with the ac-ft stranded) and a Runner run is the
  layer of record there. Four-dam re-score against LISFLOOD-FP at 0.1 ft: DWR-2801 CSI 0.157 → 0.227 (recall 17 → 31 %,
  +13.5 km, flagged incomplete), DWR-2865 0.299 → 0.295, DWR-3034 0.701 (ends in a basin, not grown), DWR-4031 0.354.
  Saved routed layers ask to be built again.
- **An exhausted spread stretch is held at the cap, not stacked.** A stretch whose pool had risen over every reachable
  cell took the whole rest of its slice as depth: 277 ft of water on the 27.5 ft DWR-2801 and a +3.5 ft depth bias
  against both LISFLOOD-FP and DSS-WISE. The pool is now held at the routed stage + 1 ft (or the level that just reaches
  the last cell), never above 2× the dam height; the surplus is withheld and reported. 2801's deepest cell falls from
  277.6 to 23.4 ft and its depth bias against the Runner from +3.6 to +1.0 ft; no dam's footprint changes.
- **Scoring at a chosen wet depth.** `validate-crest.mjs` and `score-routed.mjs` take `--wet-ft` (default 0.1 ft) and
  the report says which threshold it used. At 0.1 ft the 2-D models disagree with each other on a flat plain by
  thousands of acres of thin sheet (LISFLOOD-FP vs ANUGA on DWR-2801: CSI 0.46); at 1 ft they agree (0.58), so a 1 ft
  score says how a layer does where the water matters.
- **The routed flood is now the volume spread.** For large dams (the flood-size gate's routed side) the desk now pours
  the water each stretch of the routed channel holds at its own peak onto the ground beside it, instead of carrying the
  Manning stage across the floodplain. A closed basin on the channel (for example behind a road fill with no culvert in
  the terrain) has to fill before the flood passes, and ends the flood if it can hold it all. Against the four-dam
  LISFLOOD-FP baseline it beats the terrain outline on every dam (CSI 0.16/0.30/0.70/0.35 vs 0.10/0.23/0.58/0.23) and
  lays down 2–6× the released water, where the old stage fill laid down 31–779×. A saved routed layer built with the
  old stage fill asks to be built again. Small dams still get the terrain outline.
- **A Runner run is the layer of record.** When a dam has an imported ND Breach Runner (LISFLOOD-FP) run, that run is the
  inundation layer of record whatever the flood-size gate picks; the terrain layer stays only as a comparison. A run that
  passes the import checks becomes the source of record on its own (choosing the desk estimate on Results is remembered
  and stops that). The PLSS and 911 lists, the Dashboard depth layer and the Results map follow the run once its rasters
  are loaded. Reason: on large floods the terrain layers can badly under-predict (the outline covered about 0.10× of the
  Runner's flooded area on DWR-2801).
- **Runner worklist.** Results lists the large dams (released volume at or above the gate) whose layer of record is still a
  terrain layer, largest release first, with a CSV download. Those dams get a critical flag on the Dashboard and a warning
  on the Flood step's terrain panel until a run is imported.
- **Guided setup (Style C).** A new first step, "Guided setup": a rail of setup steps (Dam record, Breach, Terrain
  layer, Review results) beside a live map of the current record and flood layer. Each step card shows its state
  (done, active, broken) and a broken step says why on its own card, never in a modal. The terrain build shows its
  progress on the rail, and the map redraws as soon as the layer is saved. At most three flag pills sit bottom-left on
  the map, then "+N more". Every field stays editable (nothing is locked); the forms are the same as the classic steps.
- **Dashboard (Style B).** A map-first results dashboard after Results: independent depth, arrival, structures and
  hillshade layers, a labelled hydrograph peak, a 10-row sortable structure table with class badges, a status bar with
  severity pills (a critical volume flag needs acknowledging), and a compare-with-Runner overlay.

## 2026-11-02

- **Paper report (Style 3).** The Report step's printable sheet: header block, KPI table, notice box, Figure 1
  hydrograph with its peak labelled, signature lines, and the disclaimer on every printed sheet.
