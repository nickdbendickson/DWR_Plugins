# -*- coding: utf-8 -*-
"""The DWR dams of the suite data folder, for the dam wizard (moved from DamSafety Setup
1.0.13 when Setup's own dam pages went: the Runner's wizard is the suite's one per-dam
wizard, docs/gui-grammar.md).

    dams()        the DWR records of one county / name, read straight from the GeoPackage,
                  with the NID filling blank heights and storages (nid_fill) and the records
                  that are not a dam to model set aside (dam_filter)
    nid_get()     the NID web service through QGIS's network manager (only on a click)
    run_task()    dem_estimate in a QgsTask, off the window's thread

nid_fill, dam_filter and dem_estimate have no Qt; dams() needs qgis.core only for a dam line.
"""

from . import nid_fill

ND_COUNTIES = (
    "Adams", "Barnes", "Benson", "Billings", "Bottineau", "Bowman", "Burke",
    "Burleigh", "Cass", "Cavalier", "Dickey", "Divide", "Dunn", "Eddy",
    "Emmons", "Foster", "Golden Valley", "Grand Forks", "Grant", "Griggs",
    "Hettinger", "Kidder", "LaMoure", "Logan", "McHenry", "McIntosh",
    "McKenzie", "McLean", "Mercer", "Morton", "Mountrail", "Nelson", "Oliver",
    "Pembina", "Pierce", "Ramsey", "Ransom", "Renville", "Richland", "Rolette",
    "Sargent", "Sheridan", "Sioux", "Slope", "Stark", "Steele", "Stutsman",
    "Towner", "Traill", "Walsh", "Ward", "Wells", "Williams",
)


def source_text(source):
    """'from NID ND00182' for the label next to a value ('' for none)."""
    if not source:
        return ""
    if source == "typed in":
        return "typed in"
    if source.startswith("NID "):
        return "from NID {}".format(source.split()[1])
    if source.startswith("estimated from DEM"):
        return "estimated from DEM (screening only)"
    if source.startswith("DWR normal pool"):
        return "from DWR (normal pool volume)"
    return "from {}".format(source.split(" (")[0])


def run_task(work, done, progress):
    """Run work(progress) in a QgsTask (not on the window's thread); done(result, '') or
    done(None, error) back on the window's thread. progress(pct) follows the task."""
    from qgis.core import QgsApplication, QgsTask

    def fn(task):
        return work(lambda pct: task.setProgress(float(pct)))

    def finished(exc, result=None):
        _TASKS.discard(task)
        if exc is not None:
            done(None, str(exc) or exc.__class__.__name__)
        elif result is None:
            done(None, "cancelled")
        else:
            done(result, "")
    task = QgsTask.fromFunction("DamSafety Runner: estimate the dam from the DEM", fn,
                                on_finished=finished)
    task.progressChanged.connect(progress)
    _TASKS.add(task)
    QgsApplication.taskManager().addTask(task)
    return task


_TASKS = set()


def nid_get(url, done):
    """GET url through QGIS's network manager (its proxy and SSL settings), without
    blocking the window; done(text, '') or done('', error). Times out after
    nid_fill.TIMEOUT_S seconds."""
    from qgis.core import QgsNetworkAccessManager
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
    req = QNetworkRequest(QUrl(url))
    req.setRawHeader(b"Accept", b"application/json")
    if hasattr(req, "setTransferTimeout"):
        req.setTransferTimeout(int(nid_fill.TIMEOUT_S * 1000))
    reply = QgsNetworkAccessManager.instance().get(req)
    _REPLIES.add(reply)

    def finished():
        _REPLIES.discard(reply)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        body = bytes(reply.readAll()).decode("utf-8", "replace")
        if reply.error() == QNetworkReply.NoError:
            done(body, "")
        elif status == 404:
            done("{}", "")                 # not in the NID: an answer, not a failure
        else:
            done("", reply.errorString() or "HTTP {}".format(status))
        reply.deleteLater()
    reply.finished.connect(finished)
    return reply


_REPLIES = set()


DAM_LIMIT = 400


def match_count_text(rows, county="", name="", limit=None):
    """'12 dams match (Cass, "lake")' for the dam page; says when the list is cut off."""
    limit = DAM_LIMIT if limit is None else limit
    rows = [r for r in rows or [] if not r.get("error")]
    where = ", ".join(x for x in (county, '"{}"'.format(name) if name else "") if x) or \
        "all counties"
    if not rows:
        return "No dam matches ({}).".format(where)
    n = len(rows)
    return "{} dam{} match{} ({}){}".format(
        n, "" if n == 1 else "s", "es" if n == 1 else "", where,
        " - only the first {} are listed; narrow the search".format(limit) if n >= limit else "")


def dams(uri, name="", county="", limit=DAM_LIMIT, lines_uri=None, hidden=None):
    """Dams in one county. Read the GeoPackage directly so a QGIS filter cannot
    return an empty list, and do not scan every dam line before the list shows.
    Height, storage and area of 0 are blank (DWR stores "not recorded" as 0). A dam
    without a point gets the midpoint of its dam line (lines_uri, else a dwr_dam_lines
    table in the same file), only for the listed dams.
    hidden: a list -> records that are not a dam to model (dam_filter: status Breached,
    remarks 'VOID ...') go there instead of the result, each with row['hidden'] = the
    reason. None (default) lists every record, with the reason on the hidden ones."""
    from . import dam_filter
    rows = []
    if not uri:
        return rows
    path, layer = uri, None
    if "|layername=" in uri:
        path, layer = uri.split("|layername=", 1)
    try:
        import sqlite3
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        tables = [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents")]
        table = layer if layer in tables else ("dwr_dams" if "dwr_dams" in tables else None)
        if table:
            cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(table))]
            low = {c.lower(): c for c in cols}
            def col(*keys):
                for key in keys:
                    if key.lower() in low:
                        return low[key.lower()]
                return None
            where, args = [], []
            ccol = col("county")
            if county and ccol:
                where.append('lower("{}") = ?'.format(ccol))
                args.append(county.lower())
            ncol = col("dam_name")
            if name and ncol:
                where.append('lower("{}") LIKE ?'.format(ncol))
                args.append("%{}%".format(name.lower()))
            sql = 'SELECT * FROM "{}"'.format(table)
            if where:
                sql += " WHERE " + " AND ".join(where)
            if hidden is None:
                sql += " LIMIT {}".format(int(limit))
            for rec in con.execute(sql, args):
                if len(rows) >= limit:
                    break
                data = dict(zip(cols, rec))
                def pick(*keys):
                    for key in keys:
                        c = col(key)
                        if c and data.get(c) not in (None, ""):
                            return data.get(c)
                    return None

                def amount(*keys):          # a number above 0, else None
                    for key in keys:
                        val = _positive(pick(key))
                        if val is not None:
                            return val
                    return None
                sid = pick("structure_id", "structure_")
                lon, lat = _coord(pick("longitude")), _coord(pick("latitude"))
                rows.append({
                    "name": str(pick("dam_name") or sid or ""),
                    "id": str(sid or ""),
                    "structure_id": "" if sid is None else str(sid),
                    "nid": str(pick("national_id", "national_i") or ""),
                    "height": amount("max_dam_height", "max_dam_he"),
                    # max pool volume, else the DWR normal pool volume (as before 1.0.10);
                    # the NID only fills what the DWR record has neither of
                    "storage": amount("max_pool_vol", "max_pool_v"),
                    "normal_vol": amount("normal_vol"),
                    "area": amount("max_pool_area", "normal_area", "surface_area", "max_pool_a"),
                    "point": "{},{} [EPSG:4326]".format(lon, lat) if lon and lat else "",
                    "county": str(pick("county") or county or ""),
                    "status": str(pick("status") or ""),
                })
                why = dam_filter.reason(pick("status"), pick("remarks"))
                if why:
                    rows[-1]["hidden"] = why
                    if hidden is not None:
                        hidden.append(rows.pop())
            same = next((t for t in tables if t.lower() == "dwr_dam_lines"), None)
            con.close()
            points_from_lines(rows, lines_uri or (
                "{}|layername={}".format(path, same) if same else ""))
            rows.sort(key=lambda r: r["name"].lower())
            return rows
        con.close()
    except Exception:
        pass
    try:
        from qgis.core import QgsVectorLayer
        lyr = QgsVectorLayer(uri, "dwr", "ogr")
        if not lyr.isValid():
            return [{"name": "Dam layer did not open", "id": "", "point": "", "error": True}]
        want = (county or "").lower()
        needle = (name or "").lower()
        for feat in lyr.getFeatures():
            names = feat.fields().names()
            low = {n.lower(): n for n in names}
            got = str(feat[low["county"]] or "") if "county" in low else ""
            dam_name = str(feat[low["dam_name"]] or "") if "dam_name" in low else ""
            if want and got.lower() != want:
                continue
            if needle and needle not in dam_name.lower():
                continue
            sid = feat[low["structure_id"]] if "structure_id" in low else feat[low.get("structure_", "")] if "structure_" in low else ""
            def pick(*keys):
                for key in keys:
                    if key in low:
                        val = feat[low[key]]
                        if val not in (None, ""):
                            return val
                return None
            point = ""
            geom = feat.geometry()
            if geom is not None and not geom.isEmpty():
                p = geom.centroid().asPoint()
                point = "{:.6f},{:.6f}".format(p.x(), p.y())
            rows.append({"name": dam_name or str(sid), "id": str(sid or ""),
                         "structure_id": str(sid or ""),
                         "nid": str(pick("national_id", "nidid", "nid_id") or ""),
                         "height": _positive(pick("max_dam_height", "dam_height", "height")),
                         "storage": _positive(pick("max_pool_vol", "max_storage")),
                         "normal_vol": _positive(pick("normal_vol", "normal_pool_vol")),
                         "area": _positive(pick("max_pool_area", "normal_area", "surface_area")),
                         "crest": pick("crest_elev", "top_of_dam", "crest", "elev_crest"),
                         "point": point, "county": got,
                         "status": str(pick("status") or "")})
            why = dam_filter.reason(pick("status"), pick("remarks"))
            if why:
                rows[-1]["hidden"] = why
                if hidden is not None:
                    hidden.append(rows.pop())
            if len(rows) >= limit:
                break
    except Exception as e:
        return [{"name": "Could not read dams ({})".format(e), "id": "", "point": "",
                 "error": True}]
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def _positive(value):
    """A number above 0, else None (DWR stores "not recorded" as 0)."""
    from .nid_fill import number
    return number(value)


def _coord(value):
    """A coordinate, or None for blank or 0 (three DWR dams have 0, 0 for no point)."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if val else None


def points_from_lines(rows, lines_uri):
    """A listed dam without a point gets the midpoint of its dam line (the crest), in
    EPSG:4326 like the inventory points, and row['from_line'] = True. A dam that has a
    point keeps it. Only the lines of those dams are read."""
    want = [r for r in rows if not r.get("point") and r.get("structure_id")]
    if not want or not lines_uri:
        return 0
    try:
        from qgis.core import (QgsVectorLayer, QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)
        lines = QgsVectorLayer(lines_uri, "lines", "ogr")
        if not lines.isValid() or "structure_id" not in lines.fields().names():
            return 0
        ids = ",".join("'{}'".format(str(r["structure_id"]).replace("'", "''")) for r in want)
        lines.setSubsetString('"structure_id" IN ({})'.format(ids))
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = None
        if lines.crs().isValid() and lines.crs() != wgs:
            xform = QgsCoordinateTransform(lines.crs(), wgs, QgsProject.instance())
        by_id = {}
        for feat in lines.getFeatures():
            g = feat.geometry()
            if g is None or g.isEmpty():
                continue
            parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            parts = [pl for pl in parts if len(pl) >= 2]
            if not parts:
                continue
            mid = _midpoint(max(parts, key=_length))
            if xform is not None:
                mid = xform.transform(mid)
            by_id.setdefault(str(feat["structure_id"]), mid)
        n = 0
        for row in want:
            mid = by_id.get(str(row["structure_id"]))
            if mid is not None:
                row["point"] = "{:.6f},{:.6f} [EPSG:4326]".format(mid.x(), mid.y())
                row["from_line"] = True
                n += 1
        return n
    except Exception:
        return 0


def _length(part):
    return sum(((b.x() - a.x()) ** 2 + (b.y() - a.y()) ** 2) ** 0.5
               for a, b in zip(part, part[1:]))


def _midpoint(part):
    if len(part) < 2:
        return part[0]
    if len(part) == 2:
        return type(part[0])((part[0].x() + part[1].x()) / 2.0, (part[0].y() + part[1].y()) / 2.0)
    lengths = [0.0]
    for a, b in zip(part, part[1:]):
        lengths.append(lengths[-1] + ((b.x() - a.x()) ** 2 + (b.y() - a.y()) ** 2) ** 0.5)
    half = lengths[-1] / 2.0
    for i in range(1, len(lengths)):
        if lengths[i] >= half:
            span = lengths[i] - lengths[i - 1] or 1.0
            t = (half - lengths[i - 1]) / span
            a, b = part[i - 1], part[i]
            return type(a)(a.x() + t * (b.x() - a.x()), a.y() + t * (b.y() - a.y()))
    return part[len(part) // 2]
