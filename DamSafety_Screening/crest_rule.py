"""One top-of-dam number for the screener and the runner.

Priority: user crest, then inventory crest, then median DEM on the crest line,
then DEM near the dam point. The source string is printed on both reports.
"""

def resolve_top_of_dam(user_ft=None, dwr_ft=None, line_median_ft=None, near_ft=None):
    def ok(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None
    for v, src in ((user_ft, "as-built crest given by the user"),
                   (dwr_ft, "inventory crest"),
                   (line_median_ft, "median DEM along the crest line"),
                   (near_ft, "DEM near the dam point")):
        n = ok(v)
        if n is not None:
            return n, src
    return None, "not resolved"
