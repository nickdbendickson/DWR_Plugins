"""Culverts the model can use.

A county inventory is surveyed. A road crossing a channel or a depression is
only a guess: the name starts with 'guess:' and surveyed is false. A guess is
not a culvert that was measured.
"""


def _cross(a, b, c, d):
    """Intersection of segments a-b and c-d, or None."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    den = rx * sy - ry * sx
    if abs(den) < 1e-9:
        return None
    t = ((cx - ax) * sy - (cy - ay) * sx) / den
    u = ((cx - ax) * ry - (cy - ay) * rx) / den
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return (ax + t * rx, ay + t * ry)
    return None


def _first_cross(line_a, line_b):
    for i in range(len(line_a) - 1):
        for j in range(len(line_b) - 1):
            hit = _cross(line_a[i], line_a[i + 1], line_b[j], line_b[j + 1])
            if hit:
                return hit
    return None


def write_candidates_csv(path, rows):
    """Write road-crossing guesses. They are not surveyed and are not model structures."""
    import os
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("name,kind,shape,width_m,height_m,source,surveyed,x1,y1,x2,y2,note\n")
        for s in rows or []:
            line = [] if s.get("line") is None else list(s.get("line"))  # may be an ndarray
            a = line[0] if line else ("", "")
            b = line[-1] if line else ("", "")
            fh.write("{},{},{},{},{},{},{},{},{},{},{},{}\n".format(
                s.get("name") or "", s.get("kind") or "culvert", s.get("shape") or "",
                s.get("width_m"), s.get("height_m"), s.get("source") or "guess",
                "yes" if s.get("surveyed") else "no",
                a[0], a[1], b[0], b[1],
                (s.get("note") or "not surveyed").replace(",", ";")))
    return path


def from_inventory(rows):
    """Mark inventory culverts as surveyed. Rows are dicts with a line."""
    out = []
    for i, row in enumerate(rows or []):
        item = dict(row)
        item["kind"] = item.get("kind") or "culvert"
        item["source"] = "inventory"
        item["surveyed"] = True
        item["name"] = item.get("name") or "inventory culvert {}".format(i + 1)
        out.append(item)
    return out


def guess_crossings(roads, channels, length_m=12.0):
    """A road crossing a channel or a mapped depression is a guess, not a survey.

    roads and channels are lists of polylines, each a list of (x, y).
    """
    out = []
    for ri, road in enumerate(roads or []):
        if len(road) < 2:
            continue
        for ci, chan in enumerate(channels or []):
            if len(chan) < 2:
                continue
            hit = _first_cross(road, chan)
            if not hit:
                continue
            # a short line across the road, so the barrel has two ends
            ax, ay = road[0]
            bx, by = road[-1]
            dx, dy = bx - ax, by - ay
            n = (dx * dx + dy * dy) ** 0.5 or 1.0
            px, py = -dy / n * (length_m / 2.0), dx / n * (length_m / 2.0)
            out.append({
                "name": "guess: road {} crosses channel {}".format(ri + 1, ci + 1),
                "kind": "culvert",
                "shape": "box",
                "barrels": 1,
                "width_m": 1.2,
                "height_m": 0.9,
                "source": "guess",
                "surveyed": False,
                "line": [(hit[0] - px, hit[1] - py), (hit[0] + px, hit[1] + py)],
                "note": "not surveyed; a road crossing a depression or channel",
            })
    return out
