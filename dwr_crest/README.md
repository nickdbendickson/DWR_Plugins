# Crest

A standalone dam-breach desk. Edit a dam record, estimate a breach, sketch a downstream flood, and read a desk screen.

It does not run LISFLOOD, ANUGA, or HEC-RAS, and it does not assign an ND Dam Safety or ACER hazard class.

## Run

```bash
npm install
npm run dev
```

The desk opens on port 8080. Edits stay in the browser.

## What the steps do

- **Record.** Height, storage, and elevations. A crest in the 0–300 ft band is treated as local and tied to the DEM crest. A local streambed on a real-datum record is replaced with crest minus height. NGVD29 is a flat +1 ft desk shift, not VERTCON.
- **Breach.** Froehlich 2008, MacDonald & Langridge-Monopolis, or a TR-60 screening width. The plotted hydrograph is scaled so its volume matches storage. The Froehlich peak is shown beside that fitted peak.
- **Flood.** A schematic valley. The desk engine only changes how fast depth falls downstream.
- **Screen.** High if a house or barn is at least 2 ft deep, Significant if a building is wet or a road is at least 1 ft, otherwise Low. Review if the crest cannot be tied.
- **Report.** One page of the numbers above.

D×V is depth times velocity. It is not a pass or fail.

The three sample dams are illustrative. Swan Buffalo Detention 12 uses public-style NID figures; the other two are worked examples.
