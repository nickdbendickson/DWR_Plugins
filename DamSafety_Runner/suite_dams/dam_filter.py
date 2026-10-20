# -*- coding: utf-8 -*-
"""DWR records that are not a dam to model: hidden on the dam page unless "Show all records"
is ticked. No Qt here.

A record is hidden when
* its status says the dam is gone or never stood: Removed, Breached, Not Built, Submerged
  (an old dam under a newer reservoir); or
* its remarks say there is no dam (patterns below, case-insensitive) - unless the status
  says Intact, Under Construction or Pending: a DWR status of Intact is a later check and
  wins over an old remark ("no dam visible in the 1962 aerial").
"""
import re

HIDE_STATUS = ("removed", "breached", "not built", "submerged")
KEEP_STATUS = ("intact", "under construction", "pending")

# (pattern, short reason). Matched case-insensitively against the remarks.
REMARK_PATTERNS = (
    (r"^\W*void\b", "remarks start with VOID"),
    (r"\bno dam (?:is )?(?:visible|found|located|present|constructed|built|exists?|there)\b",
     "remarks: no dam visible / found"),
    (r"\bnot a dam\b", "remarks: not a dam"),
    (r"\bdam does not exist\b|\bdoes not exist anymore\b", "remarks: does not exist"),
    (r"\b(?:did|does) not show ?a dam\b", "remarks: aerial / LiDAR shows no dam"),
)
_COMPILED = tuple((re.compile(p, re.I), why) for p, why in REMARK_PATTERNS)


def reason(status, remarks):
    """Why the record is hidden ('status Breached', 'remarks start with VOID: "VOID SEE
    #80"'), or '' when it is shown."""
    st = str(status or "").strip()
    low = st.lower()
    if low in HIDE_STATUS:
        return "status {}".format(st)
    if low in KEEP_STATUS:
        return ""
    text = str(remarks or "")
    for rx, why in _COMPILED:
        m = rx.search(text)
        if m:
            a, b = max(0, m.start() - 20), min(len(text), m.end() + 20)
            snip = " ".join(text[a:b].split())
            return '{}: "{}{}{}"'.format(why, "\u2026" if a else "", snip,
                                       "\u2026" if b < len(text) else "")
    return ""
