# Crest

Desk screening of small embankment dams: breach hydrograph, schematic valley flood, receptor ranking
(ACER TM 11), and a datum-aware export. The engine is the pure `analyze()` in `src/lib/crest/engine.ts`.

## Scripts

| Script | What it does |
|---|---|
| `npm run dev` | Vite dev server on :8080 |
| `npm run build` | Production build only (`vite build`). Never touches a database. |
| `npm run build:deploy` | `npm run build && npm run db:migrate`. This is what Vercel runs (`buildCommand` in `vercel.json`). |
| `npm run db:migrate` | Applies pending top-level `migrations/*.sql` to `DATABASE_URL` once each (recorded in `_migrations`); skips when `DATABASE_URL` is unset. |
| `npm test`, `npm run typecheck`, `npm run lint` | Tests, `tsc --noEmit`, ESLint |

### Deploys and migrations

The production migration runs on every Vercel deploy through `vercel.json` → `buildCommand: npm run build:deploy`
(`vercel build` uses the same setting). Until this change it was chained onto `npm run build` itself, so any local or
CI build with `DATABASE_URL` set would migrate. If you deploy some other way, for example `npm run build` then
`vercel deploy --prebuilt`, run `npm run db:migrate` as a separate step before switching traffic. Locally, without
`DATABASE_URL`, the PGLite fallback applies the same files at startup (`src/lib/db.ts`).

## Drawn area of interest (AOI)

A drawn area limits the schematic flood. The valley is laid from the breach in the downstream direction; the reach
stops where it leaves the area, the wet area is clipped to it, and receptors outside it are unresolved (one that would
rank higher is still named, so a known High is never hidden).

**Downstream direction.**

1. Dam line + reservoir point: the normal to the dam line's **chord** (the straight line from its first to its last
   point), on the side away from the reservoir point. The vertices in between are ignored. For a straight or gently
   curved dam that is the valley axis; for a strongly curved, L- or U-shaped dam the chord can point off-axis or even
   upstream. Draw just the straight stretch through the breach instead. (The real ND00199 embankment is an L whose
   chord normal points WNW while the flood runs SE; the sample uses its straight 131 m breach segment, ~130°.)
2. No dam line: the bearing from the reservoir point to the breach.
3. Neither:
   - breach inside the area: the reach is cut at the area's nearest edge in any direction (the shortest it could be)
     and the wet area is cut by distance only, with a warning;
   - breach outside the area (or on its edge): the area is **not applied**, since it cannot be put upstream or
     downstream; the flood and every receptor are screened in full and a warning says why.

**Breach on the outline.** A breach within 1 m of the outline (on an edge or a corner) counts as inside, so an area
drawn from the dam line downstream is not lost to rounding. The same 1 m tolerance applies on both sides of every
edge along the valley, so the flow line counts as inside when it runs on or less than 1 m beside the outline, and
gaps under 2 m (a slot between two parts or a hole less than 2 m across the valley) are closed; a gap of 2 m or
more ends the reach. Cross-sections use the same test, which adds about 0.01 ac.

**Upstream warning.** If more than half of the area's filled outline (holes ignored) lies upstream of the breach, a
warning asks you to check the drawing.
