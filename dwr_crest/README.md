# Crest

A standalone dam-breach desk. Edit a dam record, estimate a breach, sketch a downstream flood, screen with the same ACER TM 11 / ND 5.2/5.3 rules as the DWR plugins, and export handoff packages for FIMsim or the QGIS Breach Runner.

It does not run a full 2D hydraulic solver in the browser. Official classification remains the ND Department of Water Resources'.

## Run

```bash
npm install
npm run dev
```

The desk opens on port 8080. Edits stay in the browser.

## What the steps do

* **Record.** Height, storage, and elevations. Local crest-tie and datum handling as before.
* **Breach.** Froehlich 2008, MacDonald & Langridge-Monopolis, or TR-60. Volume-fitted hydrograph. **Export CSV** (`time_hours,discharge_cms`) and minimal `dam.json` for the Runner / FIMsim.
* **Flood.** Schematic valley with depth decay. Receptors are seeds.
* **Screen.** Now applies the real ACER TM 11 depth-velocity charts (ported from `damsuite/tm11_curves.py`) plus ND ranking rules on the receptors. Simple threshold screen is still shown for comparison.
* **Report.** Summary plus download links for the LISFLOOD package (`.par` / `.bci` / `.bdy` with point source just downstream of the dam).

## Handoff

Crest prepares the inputs and screens with the same rules. Use the exported files in FIMsim or the ND Dam Breach Runner for full routing and official review.

D×V is shown but is not a pass/fail. Schematic depths are for screening only.

The three sample dams are illustrative.
