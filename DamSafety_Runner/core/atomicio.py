"""Write a whole text file or nothing (DamSafety Runner 3.74.1, ported from DamIT Runner 0.7.5 / damit #150).

A plain open/write leaves a truncated or zero-length file in place when QGIS or the process is killed mid-write;
the stage / timeout / setup files written through here are read back on resume, so a torn one breaks the resume.
The text goes to a temp file unique per writer (<path>.<pid>.<thread>.tmp, as workflow._tmp_name since 0.7.1, so two
drivers of one folder never share a temp file), is flushed and fsynced, and replaces `path` with os.replace. When
the write fails the temp file is removed and the error raised; `path` keeps its previous content.

Pure Python (no GDAL, no QGIS).
"""
import os
import threading


def write_text(path, text):
    """Replace `path` with `text` (UTF-8) only once the whole text is on disk."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = "{}.{}.{}.tmp".format(path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
