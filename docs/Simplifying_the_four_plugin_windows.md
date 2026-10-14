# Simplifying the four plugin windows

Release 1.22, opened and clicked through in real QGIS 3.34 on 2026-10-13. Every window of every
plugin was opened twice, once in a clean QGIS with nothing set up and once with a suite folder
adopted and Absaraka (DWR-2801) chosen, and photographed tab by tab. The screenshots are in
`screenshots/`, the full measurements in `measurements/`.

This is the simplification half of what you asked for. The code audit is in `CODE-AUDIT.md`.

## What the suite asks of you today

| | Count | Where |
|---|---|---|
| Windows | 8, reachable 12 ways | the three plugin windows, the updater's two, the wizard, and the home and data windows that each plugin opens its own copy of |
| Tabs | 29 top-level, 19 more nested inside them | Hazard 9, Runner 7, Screening 7, home 3, updater pack 3 |
| Controls on the three main windows | 270, 285, 270 | Hazard, Runner, Screening |
| Buttons that only open another window | 21 on one Hazard tab, 43 on the Runner window, 26 on one Screening tab | |
| Rows in the settings grids | 1,057 | Hazard 110, Runner 786, Screening 161, in three separate copies of the same editor |
| Processing tools | 83 | ndhazard 25, ndbreach 35, nddam 23 |
| Plugins menu entries | 58 in 6 menus | one shared ND Dam Suite menu plus one per plugin |

The suite does a great deal and most of this is the price of that. But a large part of it is the
same thing offered in several places, and that is what can go without losing a feature.

## The ten changes worth making first

Ranked by how much they take off the screen for the work they cost. Every one keeps every
Processing tool, so scripts, models and `qgis_process` are unaffected.

**1. One settings editor, not three.** The same grid is built into the Hazard window
("Suite settings"), the Runner window ("Settings") and the Screening window ("Settings"), with
110, 786 and 161 rows. They edit the same file. Keep one, in the Dam suite home, and open it from
the menu. Removes two tabs, fourteen buttons and about 950 rows from the windows you work in.
Medium effort. (`suite_settings.py` is shared, so this is one change copied to three plugins.)

**2. Tick "show only YOUR values" by default, and keep runs out of the grid.** The grid opens
showing every parameter of every tool, including things that are not settings at all: "Dam id"
appears three times on the Hazard grid's "Other" tab, next to free-text boxes for a run's
parameters. Starting on the engineer's own values (the box exists already, at
`suite_settings.py:123`) and leaving per-run parameters out of the catalogue cuts the first view
from 110 rows to the handful you have actually set. Small effort, and it makes change 1 much more
useful.

**3. Hide the fifteen tools nobody runs by hand.** Of the 83 tools in the Processing toolbox,
fifteen are chain steps, self-tests, verification and internal hand-offs that are only ever run by
another tool or a window button. `FlagHideFromToolbox` keeps them fully runnable from scripts and
the Model Designer while taking them out of the list you scroll. It also removes the "Dam suite
chain" group and two duplicate names ("Get data for this dam" is registered by all three
plugins). Small effort, 83 visible tools become 68.

**4. One Plugins menu.** Today there are six: the shared ND Dam Suite menu with 6 entries, and one
per plugin, with "Dam suite home..." appearing four times, "Suite data and servers..." three
times, and each plugin's window twice. The Runner's "Breach tools" submenu alone is 31 entries,
every one of them a tool already in the toolbox, and two of them start a run with nothing filled
in (the wizard opened that way starts on `~/ndbreach_dams`, not your Dams folder). Keep one menu
of about eight entries in step order. Removes 47 menu lines. Medium effort.

**5. The Runner window: three tabs instead of seven.** Five of the seven tabs are rows of buttons
that open a Processing dialog: 43 buttons in all. On a fresh dam most of them are greyed and the
ones that are not are batch, cascade and queue tools, while "Run breach model" is grey and "Get
data for this dam", the step the status line tells you to do first, is on a different tab. Keep a
"Dam" tab with the five steps in order (Get data, Download DEM, Run breach model, Add results to
the project, Open ND Dam Hazard), one "More tools" page with the rest under their present
headings, and the Settings tab (or nothing, after change 1). Medium effort.

**6. One Run button in the Hazard window.** There are three, named differently, on three tabs, and
the Setup & Run tab has none unless a hand-off happened. "Run everything" also runs the raster
checks and refuses on an error; "Run Hazard Classification" does not, so the same inputs can start
from one button and be refused by the other. One Run in a footer visible on every tab, with
"Check inputs" beside it and the preflight reason on it. It also makes the walkthrough guide true:
the guide already tells you to click Run on the Setup & Run tab, a button that does not exist
today. Medium effort.

**7. The Download a DEM box, once.** The Runner window has a 13-control box (source, area,
resolution, two format ticks, dam point, county, folder, two buttons) which passes twelve values
to the DEM tool and then opens that tool's own full dialog anyway. The Screening window's Run
options has the same box again, and the tool itself is in the toolbox a third time. One button,
"Download DEM for this dam", with a resolution beside it, in each window. Removes about 24
controls. Small effort. In the configured screenshot the Runner's box also draws its rows on top
of each other (`screenshots/runner_window_with_dam.png`), so this fixes a layout defect for free.

**8. The Hazard Classification tab: put the tool launchers in a menu.** The tab holds 135
controls, of which 21 are buttons that only open another window, and the row of nine at the bottom
is wider than the window, which is why the whole tab scrolls sideways and the groups below the
fold have no visible scrollbar. One "Tools..." button with a menu replaces 21 buttons and fixes
the layout. With advanced options off the tab would then show Run, Open PDF report, Open outputs
and the result. Medium effort.

**9. The updater reads the suite folder instead of keeping its own.** The Dam suite home sets one
folder for the whole suite, but the updater keeps a separate "Data folder" setting. In the
screenshots taken after the home adopted a folder, both updater windows still point at
`/root/Documents/ND Structure Data`. One folder, read from the suite, removes two folder boxes,
four buttons and a whole class of "the pack is not where the plugins look" problems. Medium
effort. Its two windows can then be one, with the schedule and the non-pack datasets behind an
Advanced section.

**10. Fold "Suite data and servers" into the home.** It is a 920 x 800 window (too tall for a
1366 x 768 laptop) with 50 controls that repeats the home's two folder rows and its readiness
list, in a different form. Its unique parts are the servers table (21 combo boxes, worth hiding
behind one toggle while the data source is the offline pack) and "Get data for this dam". As a
fourth tab of the home it stops being a second place to set the same folder. Medium effort.

## Things that are broken, not merely complicated

These came out of clicking, not reading, and each is small to fix.

- **The home's step 1 "Data..." button does nothing.** The home opens a plugin by calling its
  `run_plugin`, and the Data Updater is the one plugin without that method
  (`nd_data_updater/plugin.py` has `show_pack` and `show_settings`), so step 1 of the four-step
  front door is a dead end. Two lines fix it.
- **The updater starts downloading 20 seconds after QGIS opens.** `auto_check` defaults to true
  (`settings.py:33`) and a timer fires at `plugin.py:183`. On a new machine with no suite folder
  that means an unasked-for multi-gigabyte download into a default folder the plugins are not
  looking at. It should wait until a folder is set and a pack exists, and be off by default.
- **"Sign it off in Suite settings > Loss of life" names a tab that does not exist.** The string
  is in all four copies of `damsuite/loss.py:134` and `:138`. The real place is the Runner's
  Checks tab. Two strings.
- **The quick screen is on in the guide and in five of the six presets, and off in the window.**
  `DEFAULT_SETTINGS["chain"]` is empty (`core/workflow.py:204`), so "Run county" gives every dam a
  full 2D flood unless you tick step 6. This changes run time by a large factor and which dams get
  modelled, so it is your call which way it should go, but the window, the guide and the presets
  should agree.
- **Three tool defaults disagree with the window that drives them.** Tool 2's cell size starts at
  3.3 ft while the window passes 32.8 ft; tools 7, 9 and 12 start on LISFLOOD-FP while the window,
  the guide and tool 14 start on the built-in engine. Nobody sees this through the window, but
  anyone running the tool from the toolbox gets a different run.
- **The Runner and the Screening ship an older "Suite data and servers" than the Hazard Helper.**
  The Hazard copy is 878 lines with the DWR / HIFLD files row and the full "Build / update the
  offline pack..." button; the other two are a 830-line pair that says "Update data..." and has no
  files row. Six shared window files are copied by hand at release time and are not in the hash
  self-check that guards `damsuite`. Add them to it.
- **The hand-off tells you to press a button it has just pressed.** After the Runner hands a dam
  over, the Hazard window's red line says to press "Find rasters" on Setup & Run, which the
  hand-off already ran, and the log says the same thing twice.
- **The README tab says version 2.30.0** against 2.30.3 everywhere else. It has not been updated
  for four releases; the version line should be read from the code or dropped.
- **The walkthrough guide names controls that do not exist**: "Run" on Setup & Run, the "Reports"
  tab (it is "Results"), "Run all" (it is "Run county"). Worth a re-walk against 1.22.

## Window by window

### Dam suite home and Suite data and servers

The home, built in 1.20, is the right front door and the readiness lights are the best thing in
the suite: they say what is wrong and what to press. Three things work against it.

Each plugin opens its own copy of both windows, so you can have three identical "Dam suite home"
windows open at once; the three home screenshots are byte-identical. One window per QGIS, raised
by whichever entry you use, is a small change in `open_home` and `open_suite_data`.

The home opens 1000 x 490 against a readiness list of fifteen rows, so you see four of them and
scroll. Opening at about 1000 x 760 shows the list.

The Dams tab has eleven columns, of which the "Next" sentence is cut mid-word and pushes two
columns off screen, and nine buttons, four of which repeat the Start tab's four steps. The short
word ("screen", "model", "classify") in the cell with the sentence as its tooltip, and four fewer
buttons, fixes both.

### ND Dam Breach Screening

The Simple tab is the one tab a new user should be able to work from, and it nearly is. Three
things stop it: the three radio buttons in a row set the grid width so the tab scrolls sideways at
960 px, the Run row is below the fold so you cannot see the button you came for, and the steps
list shows three of its fourteen rows. One combo instead of the three radios and the run row moved
above the steps list fixes all three.

"All steps" is 26 buttons: five of them are the Advanced tools that the toolbox already files
under Advanced, four repeat the Simple tab's own Run, Stop, Preflight and Continue, and two
duplicate the Simple tab's "What to screen" choice as check boxes. Keeping the eleven main steps
and the optional-step ticks takes nine buttons off it.

The Settings tab's grid shows rows the window itself overwrites on every run: 32 always, up to 49
of the 161. Those should either not be in the grid or be greyed with "set on Run options", because
today you can change a value there and watch the run ignore it.

The Priority tab is a second dam list with its own folder box and Refresh, next to the home's Dams
tab. Its ranking columns belong in the home's table.

### ND Dam Breach Runner

Covered by changes 5 and 7 above. Two more worth doing.

The window's own run tools are reached three ways (window button, wizard, menu), and only the
window button fills in the dam. The wizard cannot be opened at all until a DEM exists, although
page 2 of the wizard downloads one, so the one guided path is blocked exactly when a beginner
needs it.

"Run breach model for one dam" has 124 parameters, 82 of them advanced, and the ones you always
set are scattered: DAM_ID is first, breach method is 12th, scenario 26th, DEM 83rd, cell size
87th, and the Dams folder is 124th of 124. Registering the six you always set right after DAM_ID
costs nothing and changes no parameter name, so saved models and scripts keep working.

The Advanced tab carries a fatality-rate table editor (17 controls) that writes the same file as
the Checks tab's sign-off tool, and four grid and time boxes that silently override the settings
file. One owner each.

### ND Dam Hazard Classification Helper

Covered by changes 6 and 8. Beyond those: the DSS-WISE Lite and HEC-RAS plan tabs are two tabs for
"bring in a model result" and could be one with a source chooser; the Readme tab is a ninth tab
for a text file and could be a Help button, which would also stop the tab bar from hiding tabs at
the window's own minimum width; and the group boxes are numbered and named by release ("7. Version
2.9 options") rather than by what they decide.

Five check boxes across three groups switch off parts of the same run. One "Run type" choice with
three entries (quick check, full run with report, full run with report and EAP) says what you get.

### ND Data Updater

Covered by change 9. Its Set-up tab draws the same readiness list as the home and, in the
screenshots, the two disagreed about the state of the same folder. The pack window should keep the
pack; the home should own setting up.

## Layout and wording defects

A checklist of small fixes, each seen in a screenshot:

| Where | What |
|---|---|
| Hazard, status line | One line, no word wrap, cut at "(4 more: Depth raster, Velocity raster, Suite folder, Offl" |
| Hazard, Hazard Classification | Button row outside the scroll area makes the whole tab scroll sideways; the inner scrollbar is off screen |
| Hazard, Data & Output | A scroll area inside a scroll area; long check-box labels do not wrap |
| Hazard, Review | Table headers cut ("hat would change", "Vel ft/s"); the review table gets only its header row |
| Hazard, Suite settings | The Reset / Load / Save row is below the window edge in every capture |
| Hazard, tab bar | Nine tabs do not fit the window's own minimum width, so tabs hide behind arrows |
| Runner, Dam tab | The Download a DEM rows are drawn on top of each other, buttons about 15 px tall |
| Runner, header | Two blank header rows, 72 px, from a fixed minimum height set before the layout |
| Screening, Simple | Sideways scroll; three of fourteen step rows visible; no Run row on screen |
| Screening, settings grid | A 151-character parameter label pushes the Value column off screen on seven of eight sub-tabs |
| Updater, Settings and Status | Minimum height 858 px, taller than a 1366 x 768 laptop screen; status table shows 1.5 rows |
| Updater, pack Set-up | "What to do" column unreadable ("Press Set u...", "Run the ND...") |
| Suite home, Dams | "Next" cut mid-word, two columns off screen |

## What works well and should stay

- The readiness lights on the home's Start tab, and the four numbered steps with their blocked-by
  reasons. This is the thing a new user needs and it is well done.
- Every Run button being disabled with the reason on it, and the red status line naming what is
  missing and which tab fixes it.
- The hand-off banner with "Run everything now" and "Run automatically next time".
- The Review tab's result card and the "What would change the class" table, and review decisions
  being applied on later runs.
- Keeping every window button on top of a Processing tool, so everything the window does can also
  be scripted. None of the changes above gives that up.
- The Runner's dam.json status list and its "next step" suggestion, which is the clearest
  statement of where a dam stands anywhere in the suite.

## How this was done

QGIS 3.34.4 in this container with the four 1.22 zips installed in a clean profile, driven
headless under xvfb at 1600 x 1000. A probe script opened every window through the plugins' own
entry points, walked every tab, and recorded for each one a screenshot and an inventory of every
control (type, text, tooltip, enabled, combo entries, table headers), plus the Processing tools of
the three providers with their parameter counts, and the Plugins menus. It then adopted a suite
folder (the current offline pack), created the Absaraka dam folder, pointed each window at it
through the suite's own hand-off, and repeated the walk. No pop-up appeared in either run.

Six reviewers then worked over the screenshots, the inventories and the code that builds each
window, and a seventh pass re-checked every recommendation against the code, the earlier usability
work (the 1.20 novice recommendations, the 1.18 UX notes, the walkthrough guide) and what it might
break. 85 recommendations were made, 84 survived that check, and 23 more were added during it.
Everything above that names a number was measured, not estimated.
