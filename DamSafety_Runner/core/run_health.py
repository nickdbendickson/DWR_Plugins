"""The run health card: one pass / warn / fail line on every run report, read from what the run
report already records (no new computation, no result change).

    assess(report) -> {"status": "pass"|"warn"|"fail", "checks": [{"name", "status", "text"}]}
    card_line(health) -> "Run health: FAIL - extent FAIL (...) · mass balance pass (...) · ..."

Checks (each skipped when the report does not carry what it needs):
  extent        fail when the run was cut off (report["cut_off"]); warn when the extent is a
                minimum (report["extent_is_minimum"]: a significant share of the breach volume left
                through the edges); pass otherwise.
  mass balance  the last flood run's worst volume error: fail over 1 %, warn over 0.5 %.
  spreading     warn when the last run was still spreading at the end of its clock.
  duration      warn when the simulation is shorter than the breach hydrograph (a run warning).
  terrain       warn when the last domain's DEM coverage is under 98 %.
  velocity      warn when the flood engine wrote no velocity.
  engine        warn for the built-in numpy engine (not LISFLOOD-FP); pass, with its name, otherwise.
The overall status is the worst check. Pure Python (stdlib only)."""

RANK = {"pass": 0, "warn": 1, "fail": 2}
MASS_FAIL_PCT = 1.0
MASS_WARN_PCT = 0.5
COVERAGE_WARN = 0.98


def _last_run(rep):
    runs = rep.get("lisflood_runs") or []
    return runs[-1] if runs and isinstance(runs[-1], dict) else {}


def assess(rep):
    rep = rep or {}
    checks = []

    def add(name, status, text):
        checks.append({"name": name, "status": status, "text": text})

    co = rep.get("cut_off")
    pct = rep.get("boundary_outflow_pct")
    pct_txt = "{:.0f}% of the breach volume left the model".format(pct) \
        if isinstance(pct, (int, float)) else ""
    if co:
        reason = co.get("reason") if isinstance(co, dict) else "cut off"
        add("extent", "fail", "cut off ({}){}".format(reason, "; " + pct_txt if pct_txt else ""))
    elif rep.get("extent_is_minimum"):
        add("extent", "warn", "a minimum: " + (pct_txt or "the flood was still leaving the model"))
    elif rep.get("lisflood_runs"):
        add("extent", "pass", "the flood stays inside the model")

    last = _last_run(rep)
    stats = last.get("stats") or {}
    err = stats.get("max_volume_error_pct")
    if isinstance(err, (int, float)):
        st = "fail" if err > MASS_FAIL_PCT else "warn" if err > MASS_WARN_PCT else "pass"
        add("mass balance", st, "worst volume error {:.3g}%".format(err))

    spread = last.get("spread") or {}
    if spread.get("still_spreading"):
        add("spreading", "warn", "still spreading at the end of the clock")

    warns = [str(w) for w in (rep.get("warnings") or [])]
    if any("is shorter than the breach hydrograph" in w for w in warns):
        add("duration", "warn", "the simulation is shorter than the breach hydrograph")

    cov = last.get("dem_coverage")
    if isinstance(cov, (int, float)) and cov < COVERAGE_WARN:
        add("terrain", "warn", "DEM covers {:.1%} of the model".format(cov))

    if stats and stats.get("velocity_written") is False:
        add("velocity", "warn", "the flood engine wrote no velocity")

    eng = str(rep.get("flood_engine") or "")
    if eng:
        if eng.lower().startswith("built-in"):
            add("engine", "warn", "built-in screening engine, NOT LISFLOOD-FP")
        else:
            add("engine", "pass", (rep.get("flood_engine_short") or eng.split(" (")[0]))

    status = "pass"
    for c in checks:
        if RANK[c["status"]] > RANK[status]:
            status = c["status"]
    return {"status": status, "checks": checks}


def card_line(h):
    """One line for the top of run_report.md (and the docx / HTML if they want it)."""
    if not h or not h.get("checks"):
        return ""
    order = sorted(h["checks"], key=lambda c: -RANK[c["status"]])
    bits = ["{} {} ({})".format(c["name"], c["status"].upper() if c["status"] != "pass" else "pass",
                                c["text"]) for c in order]
    return "**Run health: {}** · {}".format(h["status"].upper(), " · ".join(bits))
