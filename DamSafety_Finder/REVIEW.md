# Reviewing Finder candidates: 100 cards in an hour

The Finder gives a list to review, not a list of dams. Your marks are the only ground truth it
has: they say how good the list is, and they are what the score learns from. This page is the
quickest way we know to turn a list into marks.

One card is one candidate fill. A hundred cards in an hour is about 35 seconds a card. That is
enough: you are making a desk call from an aerial photo and a hillshade, not a site visit.

## Before you start (10 minutes, once per area)

1. **Pick the batch.** Processing Toolbox → DamSafety Finder → *Pick a review batch*. Give it the
   scored fills layer, leave the shares as they are, set *Cards in the batch* to 100, and keep the
   *Random seed* you used last time (0 if this is the first round). If you have reviewed this area
   before, give last round's marks file as *Cards already reviewed* so nothing comes up twice.

   It writes `<name>.gpkg` (layer `review_batch`) and, beside it, `<name>_review_batch.csv`.

2. **Load two pictures under the cards.**
   - An aerial image: the one you normally use (ND GIS Hub imagery, NAIP, or a satellite
     basemap).
   - The LiDAR DEM the Finder ran on, drawn as a hillshade (*Layer Properties → Symbology →
     Render type: Hillshade*).

   Put the aerial on top and bind a key to toggling it (or just tick and untick it). Most calls
   need both: the hillshade shows the fill, the aerial shows what it is.

3. **Add a `mark` column to the batch layer.** Open the layer's *Fields* tab, turn editing on, add
   a text field named `mark`. Then in *Attributes Form* set its widget to *Value Map* with the four
   values `yes`, `no`, `unsure`, `offset`. Now a mark is one click, and it cannot be misspelled.

4. **Open the attribute table in form view** (the button at the bottom right of the table) and
   turn on *Automatically pan to the current feature*, beside the arrows under the form. Zoom the
   map once to about 1:3,000. From here on the arrows move you from card to card and the map
   follows.

## The hour

For each card: look, pick a mark, next. Sort nothing and skip nothing. The cards are in a shuffled
order on purpose.

| Mark | Use it when |
|---|---|
| `yes` | It is an embankment built to hold water across a draw: it ends on the valley sides, and there is a pond, a wet patch or a flat dry pool floor behind it. |
| `no` | It is something else. See the list below. |
| `unsure` | You cannot tell in half a minute. Say so and move on. An honest `unsure` is worth more than a guess. |
| `offset` | There is a real dam, but the point is not on it (it sits on the next rise, the road beside it, the far side of the pond). |

What `no` usually is, from the reviews so far:

- **A road or rail fill.** A road or track runs along the top and carries on both ways. A culvert
  under it does not make it a dam.
- **A canal or ditch bank, or spoil along a channel.** It runs beside the water, not across it,
  and goes on for a long way. Every dam confirmed in the Wells County review was under 400 m long.
- **A divided highway or an approach** that the road layers missed.
- **A dugout or pit.** A hole with spoil around it and no fill across a draw. Bare-earth LiDAR
  cannot tell these from a small stock dam; the aerial can.
- **A farmyard, field edge or natural rise.**

What a real one usually looks like here: a small stock dam, a few feet to about ten feet high,
with an acre or two of water, in a draw with no road on it.

Three habits that keep the hour honest:

- **Mark what it is, not what the score says.** Do not look at the `score` column while you mark.
- **Do not skip the dull ones.** About one card in six is a *random control* (`stratum` =
  `random`). Those are what tell you how many real dams are still in the cards nobody has looked
  at. Most of them are `no`, and that is the point.
- **Do not go back.** If a later card changes your mind about an earlier one, leave it. A second
  reviewer is the check, not a second pass.

If the batch has cards with `second` = `yes`, give those to another person afterwards with the
`mark` column hidden. How often two people agree is worth knowing, and nobody has that number yet.

## Afterwards (5 minutes)

1. **Save the marks.** Turn editing off and save. Right-click the layer → *Export → Save Features
   As…* → format *Comma Separated Value*, and keep at least the `find_id` and `mark` columns. Name
   it by area and date, for example `wells_marks_2026-11-13.csv`. (You can also type the marks into
   a `mark` column of `<name>_review_batch.csv` in a spreadsheet instead of steps 3 and 4 above.
   The file only needs `find_id` and `mark`.)
2. **Feed them back.** *Score fills by how much they look like the listed dams* takes that file as
   *Reviewers' marks*. A `yes` or `no` replaces the card's own listed / not-listed label; an
   `unsure` counts as half of each; an `offset` is left out of the score (it is a placement miss,
   not a ranking one). Learn marks per region: marks from prairie can misrank the badlands.
3. **Next round.** Run *Pick a review batch* again with the same seed and the marks file as
   *Cards already reviewed*. Keep every round's marks file: together they are the record of what
   has been looked at.

## What not to expect

- Most not-listed fills are not dams. In the reviews so far roughly one card in eight was.
  A batch with few `yes` marks is a normal batch.
- A `yes` here is a desk call. It says "worth a closer look", not "this is a regulated dam".
- The Finder cannot see a dam the DEM has been cut through at a culvert, or a breached dam. Those
  will not be in the batch at all.
