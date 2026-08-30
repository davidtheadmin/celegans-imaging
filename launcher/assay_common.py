"""Shared plumbing for the assay readouts: condition grammar, aggregation to
plate and condition level, and log-space distributions.

WHY THIS EXISTS. Development grew a full aggregation layer — per image, then per
plate, then per condition, with strain and dose parsed out of the folder name —
and its explorer only draws what that layer computes. Motility, crawling and
counting had no equivalent: motility summarised per video and stopped, crawling
pooled every worm in a condition regardless of which plate it came from, and
counting never parsed a dose. This module is that missing layer, written once so
the four assays report n the same way and can be read side by side.

THE REPLICATION UNIT IS THE PLATE. Items (worms, colonies) are averaged within a
plate first, and the condition statistic is taken across plate means. That
matches how Development already computes mean stage index, and it is the reason
a 200-worm plate does not outvote a 12-worm one. Crawling's previous per-worm
pooling is still available — ``pooled_*`` columns — so its old numbers stay
reproducible and the difference between the two is visible rather than argued
about.

WHAT IS NOT MEASURED IS NOT ZERO. Every aggregate carries its own n, non-finite
values are dropped per column rather than per row, and a column with nothing
finite in it returns None. A blank is a claim about the run; a zero would be a
claim about the plate (invariant 7 in ARCHITECTURE.md).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Condition grammar
#
# "<strain> <dose><unit>", e.g. "601 20J" / "N2 100 uM". THE single definition —
# survival.py imports it from here rather than keeping its own copy, so a change
# to the grammar cannot reach one assay and miss another.
# ---------------------------------------------------------------------------

_COND_RE = re.compile(r"^(?P<strain>.+?)\s+(?P<dose>\d+)\s*(?P<unit>[Jj]|[uUµ][Mm])$")


def canon_unit(token: str) -> str:
    """Display form of a dose unit token."""
    return "J/m²" if token in ("J", "j") else "µM"


def parse_condition(name: str):
    """Return (strain, dose:int, unit) or None if the name isn't the grammar."""
    m = _COND_RE.match(name.strip())
    if not m:
        return None
    return m.group("strain"), int(m.group("dose")), canon_unit(m.group("unit"))


def split_condition(name: str) -> dict:
    """parse_condition with the fallback every assay uses.

    A name outside the grammar is NOT dropped — it becomes its own strain with
    no dose, so it still appears in every table and in the explorer, just
    without a position on a dose axis. Losing a condition because someone named
    a folder "control" would be worse than showing it unplaced.
    """
    hit = parse_condition(name)
    if hit is None:
        return {"condition": name, "strain": name, "dose": None, "unit": "",
                "parsed": False}
    strain, dose, unit = hit
    return {"condition": name, "strain": strain, "dose": dose, "unit": unit,
            "parsed": True}


# ---------------------------------------------------------------------------
# Wild type
#
# The ONLY thing this codebase is willing to infer about a condition name
# beyond the dose grammar, and it is kept deliberately small.
# ---------------------------------------------------------------------------

_WT_NAMES = frozenset({"n2", "wt", "wildtype", "wild type"})


def is_wildtype(strain) -> bool:
    """True when a strain name IS an unambiguous spelling of wild type.

    A short exact list, not a guess. It matches "N2", "WT", "wildtype",
    "wild-type", "wild_type", "wild type" — and nothing else. Not "control" or
    "ctrl", because a control condition is not necessarily a wild-type strain:
    in a rescue experiment the control is a mutant. Not "WT rescue" or "N2
    outcross" either, which are their own strains.

    THIS AFFECTS PRESENTATION ONLY: which colour a curve gets and where it sits
    in a legend. No name is ever rewritten, no conditions are merged, and no
    statistic is computed differently. A name we cannot read as wild type is
    treated as an ordinary strain, which is the safe failure — it keeps its own
    name and colour and sorts alphabetically, and nothing about the numbers
    moves.

    AND IT DOES NOT MAKE THAT STRAIN THE CONTROL. Not every experiment uses a
    wild type as its comparator: a rescue line, a parental strain or a vehicle
    arm can be the thing everything else is read against, and plenty of runs
    have no wild type in them at all. Every "percent of control" in this
    codebase is a condition against ITS OWN strain — the colony survival figure
    divides each strain by its own untreated plates, and the worm assays divide
    each condition by its own strain's lowest dose at the same timepoint.
    Recognising the name changes none of that; it only means someone can find
    the wild type in a legend without hunting for it.
    """
    return re.sub(r"[\s_-]+", " ", str(strain).strip().lower()) in _WT_NAMES


def strain_sort_key(name):
    """Display order: a recognised wild type first, then alphabetical.

    A convention for reading, not a statement about the experiment's design —
    see is_wildtype. Two wild-type spellings in one experiment stay two
    strains; they just sort next to each other."""
    return (not is_wildtype(name), str(name).lower())


def dose_unit_of(rows: Iterable[dict]) -> str:
    """The single dose unit in use, or "" when there is none or several."""
    units = {r.get("unit") for r in rows if r.get("unit")}
    return units.pop() if len(units) == 1 else ""


# ---------------------------------------------------------------------------
# Metric declaration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Metric:
    """One measured quantity, and how to present it.

    ``key``    column name in the per-item table
    ``label``  axis / panel title, without the unit
    ``unit``   unit string for axes and tooltips ("" for dimensionless)
    ``agg``    how a plate summarises its items: "mean" or "median". Counts and
               rates use "mean"; anything with a long tail uses "median".
    ``log``    draw this metric's distribution in log space (multiplicative
               quantities: sizes, speeds). Linear otherwise.
    ``note``   one line shown under the panel — where a number is a proxy, or
               in pixel units, this is where it says so.
    ``headline`` draw this metric in the static PNGs. Every metric reaches the
               workbook and the explorer regardless; this flag only decides
               what the figures spend their rows on. A seven-row facet grid is
               not a figure anyone reads, and the metrics that earn a row are
               not the same set as the metrics worth keeping.
    """
    key: str
    label: str
    unit: str = ""
    agg: str = "mean"
    log: bool = False
    note: str = ""
    headline: bool = True

    @property
    def axis(self) -> str:
        return f"{self.label} ({self.unit})" if self.unit else self.label


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

def _finite(values: Iterable) -> list:
    out = []
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def _n_finite(values) -> int:
    """How many values actually contributed — the denominator of the SEM."""
    return len(_finite(values))


def sem(values) -> Optional[float]:
    """Standard error of the mean over the ITEMS (worms), sd / sqrt(n).

    The honest reading: how well the mean of these animals is pinned down by
    this many of them. NOT how much the number would move if the experiment
    were repeated — worms in one experiment share a plate, a day and a
    handling, so they are not fully independent and this is an optimistic
    bound on precision. It is reported because it answers the question extra
    plates are actually run to improve, and captioned so it cannot be mistaken
    for replicate error.
    """
    v = _finite(values)
    if len(v) < 2:
        return None
    s = sd(v)
    return None if s is None else s / (len(v) ** 0.5)


def mean(values) -> Optional[float]:
    v = _finite(values)
    return sum(v) / len(v) if v else None


def median(values) -> Optional[float]:
    v = sorted(_finite(values))
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else 0.5 * (v[m - 1] + v[m])


def sd(values) -> Optional[float]:
    """Sample SD (ddof=1). None below two finite values — a single plate has no
    spread, and reporting 0.0 there would draw an error bar that claims one."""
    v = _finite(values)
    if len(v) < 2:
        return None
    mu = sum(v) / len(v)
    return math.sqrt(sum((x - mu) ** 2 for x in v) / (len(v) - 1))


def quantile(values, q: float) -> Optional[float]:
    v = sorted(_finite(values))
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = q * (len(v) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class Aggregation:
    per_plate: list = field(default_factory=list)
    per_condition: list = field(default_factory=list)
    metrics: Sequence[Metric] = ()
    n_items: int = 0
    n_kept: int = 0
    # Sorted unique timepoints in hours, empty for a run that has none. A
    # single-folder run leaves this empty rather than putting [0.0] in it, so
    # downstream code can ask "is this a timecourse?" with a plain truth test.
    timepoints: list = field(default_factory=list)
    # The items that passed the gate, each carrying its parsed strain / dose /
    # unit / timepoint alongside its metric columns. Kept because the
    # normalisation helpers below have to divide worms by a control, not plate
    # means by a control, and they cannot re-derive the items from the plate
    # rows. Empty when the item IS the plate (colony survival), and the
    # helpers fall back to per_plate in that case — see `_units`.
    items: list = field(default_factory=list)


def _units(agg: "Aggregation") -> list:
    """The rows a normalisation should be pooled over.

    The kept items when there are any, otherwise the plate rows. That fallback
    is not a degradation: colony survival measures a whole well, so its plate
    row IS its item row and pooling over per_plate is pooling over items.
    """
    return list(getattr(agg, "items", None) or agg.per_plate)


def _tp_of(r: dict) -> Optional[float]:
    """A row's timepoint in hours, or None when the run has no timepoints."""
    v = r.get("timepoint_h")
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def aggregate(items: Sequence[dict], metrics: Sequence[Metric],
              keep: Optional[Callable[[dict], bool]] = None,
              by_timepoint: bool = False) -> Aggregation:
    """Items (worms, colonies) -> per-plate rows -> per-condition rows.

    ``items`` each need "condition" and "plate"; everything else is metric
    columns. ``keep`` is the quality gate — items failing it are counted in
    ``n_items`` and excluded from every statistic, so a condition that lost most
    of its animals says so in n rather than quietly reporting the survivors'
    mean as the condition's.

    Per-plate rows carry, for each metric, the plate's own summary
    (``<key>``, by the metric's ``agg``) and its ``n_<key>``.

    Per-condition rows carry ``<key>_mean`` / ``<key>_sd`` / ``<key>_sem``
    POOLED OVER ITEMS, with ``<key>_n`` as the n and ``<key>_pooled_median``
    as the robust companion. The plates of a condition are not replicates of
    each other, so their means are not averaged; ``<key>_plate_spread_sd`` —
    the SD across plate means, which is what this used to report as the error
    bar — is kept beside them purely so a rogue dish stays visible.

    ``by_timepoint`` adds the timepoint to both grouping keys, so a multi-folder
    timecourse yields one plate row per (timepoint, condition, plate) and one
    condition row per (timepoint, condition). Items must then carry
    ``timepoint_h``. This mirrors what survival.aggregate does for Development,
    and it is opt-in so a single-folder run produces byte-identical output to
    before. The same plate name at two timepoints is two different plates —
    which is correct: it was imaged twice, and averaging across the two would
    hide exactly the change the timecourse exists to measure.
    """
    keep = keep or (lambda r: True)

    def key_of(r: dict) -> tuple:
        base = (str(r.get("condition", "")), str(r.get("plate", "")))
        return ((_tp_of(r),) + base) if by_timepoint else base

    def cond_key_of(r: dict) -> tuple:
        c = str(r.get("condition", ""))
        return ((_tp_of(r), c) if by_timepoint else (c,))

    by_plate: dict[tuple, list] = {}
    n_items = n_kept = 0
    for r in items:
        n_items += 1
        if not keep(r):
            continue
        n_kept += 1
        by_plate.setdefault(key_of(r), []).append(r)

    # every plate that produced items, plus the ones the gate emptied — a plate
    # with zero surviving worms is a result, not an absence
    seen_plates = {key_of(r) for r in items}
    counts: dict[tuple, int] = {}
    for r in items:
        k = key_of(r)
        counts[k] = counts.get(k, 0) + 1

    def _sortable(k: tuple) -> tuple:
        # None sorts before any number without raising
        return tuple((v is not None, v) if isinstance(v, (int, float)) or v is None
                     else (True, v) for v in k)

    per_plate = []
    for k in sorted(seen_plates, key=_sortable):
        tp = k[0] if by_timepoint else None
        cond, plate = (k[1], k[2]) if by_timepoint else (k[0], k[1])
        rows = by_plate.get(k, [])
        info = split_condition(cond)
        out = {"condition": cond, "strain": info["strain"], "dose": info["dose"],
               "unit": info["unit"], "plate": plate,
               "n_items": counts.get(k, 0),
               "n_kept": len(rows)}
        if by_timepoint:
            out = {"timepoint_h": tp, **out}
        for m in metrics:
            vals = [r.get(m.key) for r in rows]
            out[m.key] = median(vals) if m.agg == "median" else mean(vals)
            out[f"n_{m.key}"] = len(_finite(vals))
        per_plate.append(out)

    pooled_by_cond: dict[tuple, list] = {}
    for r in items:
        if keep(r):
            pooled_by_cond.setdefault(cond_key_of(r), []).append(r)

    per_condition = []
    cond_keys = {((p.get("timepoint_h"), p["condition"]) if by_timepoint
                  else (p["condition"],)) for p in per_plate}
    for ck in sorted(cond_keys, key=_sortable):
        cond = ck[1] if by_timepoint else ck[0]
        tp = ck[0] if by_timepoint else None
        plates = [p for p in per_plate
                  if p["condition"] == cond
                  and (not by_timepoint or p.get("timepoint_h") == tp)]
        info = split_condition(cond)
        pooled = pooled_by_cond.get(ck, [])
        out = {"condition": cond, "strain": info["strain"], "dose": info["dose"],
               "unit": info["unit"], "parsed": info["parsed"],
               "n_plates": len(plates),
               "n_plates_with_data": sum(1 for p in plates if p["n_kept"]),
               "n_items": sum(p["n_items"] for p in plates),
               "n_kept": sum(p["n_kept"] for p in plates)}
        if by_timepoint:
            out = {"timepoint_h": tp, **out}
        for m in metrics:
            # POOLED OVER WORMS, not averaged over plates.
            #
            # Several plates of one condition at one timepoint are not
            # replicates. They are more worms from the same experiment, split
            # across dishes so more animals could be imaged. Averaging the
            # plate means made a plate of 3 worms count as much as a plate of
            # 30, and the SD across those means was drawn as an error bar —
            # which measured how unevenly worms were distributed between
            # dishes, and read as biological replication.
            #
            # `_sem` is what the figures draw: the spread of the WORMS divided
            # by the root of how many there were. It says how well this number
            # is known given the animals actually imaged, and it shrinks when
            # an extra plate adds worms, which is what the extra plate is for.
            # It is not replicate error and must never be captioned as such —
            # one experiment cannot produce that.
            vals = [r.get(m.key) for r in pooled]
            out[f"{m.key}_mean"] = mean(vals)
            out[f"{m.key}_sd"] = sd(vals)
            out[f"{m.key}_sem"] = sem(vals)
            out[f"{m.key}_n"] = _n_finite(vals)
            out[f"{m.key}_pooled_median"] = median(vals)
            # Kept for QC: how the plates of this condition sat relative to one
            # another. A rogue dish is worth being able to see; it is simply
            # not an error bar.
            out[f"{m.key}_plate_spread_sd"] = sd([p[m.key] for p in plates])
        per_condition.append(out)

    tps = sorted({t for t in (_tp_of(r) for r in items) if t is not None}) \
        if by_timepoint else []
    # The kept items, each stamped with its parsed condition so the
    # normalisation helpers can group them the same way the plate rows are
    # grouped. Shallow copies: the caller's rows are not touched.
    kept_items = []
    for r in items:
        if not keep(r):
            continue
        info = split_condition(str(r.get("condition", "")))
        kept_items.append({**r, "strain": info["strain"], "dose": info["dose"],
                           "unit": info["unit"],
                           "plate": str(r.get("plate", "")),
                           "timepoint_h": _tp_of(r)})
    return Aggregation(per_plate=per_plate, per_condition=per_condition,
                       metrics=list(metrics), n_items=n_items, n_kept=n_kept,
                       timepoints=tps, items=kept_items)


def survival_series(agg: "Aggregation", metric_key: str,
                    write_log: Optional[Callable[[str], None]] = None):
    """Per-strain survival: every plate as a percentage of its own strain's
    untreated control. Returns {"series", "notes", "unit"} or None.

    THE ONE DEFINITION. The figure and the explorer both call this, so the two
    cannot disagree about what "percent survival" means — the same reason the
    condition grammar lives here rather than in each assay.

    NORMALISATION IS PER STRAIN, AND ONLY EVER AGAINST ITS OWN CONTROL. Each
    ITEM is divided by the mean of that strain's own untreated items, and the
    condition mean, SD and SEM are pooled over those normalised items. So the
    control sits at 100% by construction and still carries the spread of the
    control itself: a control that disagrees with itself has to look like one.
    A strain with no untreated condition falls back to its lowest dose and says
    so in ``notes``; a strain whose control is zero or unmeasured is dropped
    entirely rather than normalised against another strain's control, because
    that number would not be survival.

    ``pts`` carries ``mean`` / ``sd`` / ``sem`` / ``n`` over the items and
    ``vals`` over the PLATES, because the plate dots are QC — a rogue dish has
    to stay visible — while the marker and its bar are the pooled item
    statistic. For colony survival the two sets are the same rows: a well is
    one measurement, so its plate row is its item row.
    """
    log_ = write_log or (lambda _m: None)
    plates = [p for p in agg.per_plate
              if p.get("dose") is not None and p.get(metric_key) is not None]
    units = [r for r in _units(agg)
             if r.get("dose") is not None and r.get(metric_key) is not None]
    if not plates or not units:
        log_(f"survival: no dosed plates carrying {metric_key}.")
        return None
    unit = dose_unit_of(agg.per_condition)
    series, notes = [], []
    for s in sorted({p["strain"] for p in plates}, key=strain_sort_key):
        rows = [p for p in plates if p["strain"] == s]
        urows = [r for r in units if r["strain"] == s]
        doses = sorted({p["dose"] for p in rows})
        ctrl = 0 if 0 in doses else doses[0]
        base = mean([r[metric_key] for r in urows if r["dose"] == ctrl])
        if not base or base <= 0:
            log_(f"survival: {s} has no usable control at {ctrl} {unit} — "
                 "strain dropped.")
            continue
        if ctrl != 0:
            notes.append(f"{s} is normalised to {ctrl} {unit}".strip())
        pts = []
        for d in doses:
            vals = [100.0 * p[metric_key] / base
                    for p in rows if p["dose"] == d]
            uvals = [100.0 * r[metric_key] / base
                     for r in urows if r["dose"] == d]
            pts.append({"dose": d, "vals": vals, "mean": mean(uvals),
                        "sd": sd(uvals) or 0.0,
                        "sem": sem(uvals) or 0.0, "n": _n_finite(uvals),
                        "plates": [p["plate"] for p in rows if p["dose"] == d]})
        series.append({"strain": s, "ctrl_dose": ctrl, "base": base,
                       "pts": pts})
    if not series:
        log_("survival: no strain had a usable untreated control.")
        return None
    return {"series": series, "notes": notes, "unit": unit}


def dist_key(strain, dose, tp=None) -> str:
    """The ONE spelling of a distribution group's key.

    The report builder writes it, the figure reads it and the explorer reads it
    again; three hand-rolled f-strings is how a group silently stops being
    found and a curve quietly disappears. A condition outside the dose grammar
    has dose None and keys on its own name, which is what makes it still get
    drawn instead of dropped.
    """
    base = f"{strain}|{dose}" if dose is not None else str(strain)
    return base if tp is None else f"{base}@{tp:g}h"


def headline(metrics: Sequence[Metric]) -> list:
    """The metrics the static figures draw. Falls back to all of them rather
    than to none, so a metric set that forgot to flag anything still produces a
    figure instead of an empty page."""
    hl = [m for m in metrics if getattr(m, "headline", True)]
    return hl or list(metrics)


def normalised_series(agg: "Aggregation", metric_key: str,
                      write_log: Optional[Callable[[str], None]] = None):
    """Treated plates as a percentage of their OWN control, AT THE SAME TIME.

    survival_series answers "how far did this dose fall", once. This answers
    "how far had it fallen by hour t", which is the only form of the question a
    timecourse can be read for. It is a separate function rather than a flag on
    that one because the normalisation differs in the way that matters: the
    denominator here is the same strain's control *at the same timepoint*, so a
    control that itself declines — ours all do by 96 h, plates run down — is
    divided out instead of being charged to the treatment.

    Returns {"series", "notes", "unit", "timepoints"} or None. Each series is
    one (strain, dose) with a point per timepoint and ``t_half``: the time at
    which its mean first crosses 50% of control, linearly interpolated between
    the two timepoints that bracket the crossing. t_half is None when the curve
    never crosses — which is a result, not a gap, and the caller says so.

    The denominator and the marker are both POOLED OVER ITEMS: the base is the
    mean of the control's worms at that timepoint, and each point is the mean
    of the treated worms over it with ±1 SEM. The plates are still carried, as
    ``vals``, so the figure can scatter them for QC — but they are not what the
    bar measures, because the plates of a condition are not replicates of each
    other.
    """
    log_ = write_log or (lambda _m: None)
    tps = list(getattr(agg, "timepoints", []) or [])
    if len(tps) < 2:
        log_(f"normalised {metric_key}: fewer than two timepoints — skipped.")
        return None
    plates = [p for p in agg.per_plate
              if p.get("dose") is not None and p.get(metric_key) is not None
              and p.get("timepoint_h") is not None]
    units = [r for r in _units(agg)
             if r.get("dose") is not None and r.get(metric_key) is not None
             and r.get("timepoint_h") is not None]
    if not plates or not units:
        log_(f"normalised {metric_key}: no dosed plates carrying the metric.")
        return None
    unit = dose_unit_of(agg.per_condition)
    series, notes = [], []
    for s in sorted({p["strain"] for p in plates}, key=strain_sort_key):
        rows = [p for p in plates if p["strain"] == s]
        urows = [r for r in units if r["strain"] == s]
        doses = sorted({p["dose"] for p in rows})
        ctrl = 0 if 0 in doses else doses[0]
        if len(doses) < 2:
            notes.append(f"{s} has only one dose — nothing to normalise.")
            continue
        if ctrl != 0:
            notes.append(f"{s} is normalised to {ctrl} {unit}".strip())
        # One base per timepoint. A timepoint whose control was not imaged has
        # no base and therefore no normalised point — never the neighbouring
        # day's base, which would silently compare across a day.
        base_at = {}
        for t in tps:
            b = mean([r[metric_key] for r in urows
                      if r["dose"] == ctrl and r["timepoint_h"] == t])
            if b and b > 0:
                base_at[t] = b
        missing = [t for t in tps if t not in base_at]
        if not base_at:
            log_(f"normalised {metric_key}: {s} has no usable control at any "
                 "timepoint — strain dropped.")
            continue
        if missing:
            notes.append(f"{s}: no control at "
                         + ", ".join(f"{t:g} h" for t in missing)
                         + " — those points are absent, not interpolated.")
        for d in doses:
            if d == ctrl:
                continue
            pts = []
            for t in tps:
                b = base_at.get(t)
                if b is None:
                    continue
                here = [p for p in rows
                        if p["dose"] == d and p["timepoint_h"] == t]
                uhere = [r for r in urows
                         if r["dose"] == d and r["timepoint_h"] == t]
                vals = [100.0 * p[metric_key] / b for p in here]
                uvals = [100.0 * r[metric_key] / b for r in uhere]
                if not uvals:
                    continue
                # `vals` are the PLATES (the old QC dots, still carried
                # for the workbook and for colony survival) and `wvals` are
                # the individual animals, which is what the page scatters when
                # the reader asks for points.
                pts.append({"tp": t, "vals": vals, "wvals": uvals,
                            "mean": mean(uvals),
                            "sd": sd(uvals), "sem": sem(uvals),
                            "n": _n_finite(uvals), "base": b,
                            "plates": [p["plate"] for p in here]})
            if not pts:
                continue
            series.append({"strain": s, "dose": d, "ctrl_dose": ctrl,
                           "pts": pts, "t_half": _cross_time(pts, 50.0)})
    if not series:
        log_(f"normalised {metric_key}: no strain had a usable control.")
        return None
    return {"series": series, "notes": notes, "unit": unit,
            "timepoints": tps}


def _cross_time(pts: Sequence[dict], level: float) -> Optional[float]:
    """First time the mean crosses DOWN through ``level``, interpolated.

    Only a downward crossing counts: a curve that starts below the level was
    already there at the first timepoint and the honest answer is "at or before
    t0", which is the first timepoint itself. A curve that never goes below
    returns None, and None here means "did not cross", not "unknown".
    """
    seq = [(p["tp"], p["mean"]) for p in pts
           if p.get("mean") is not None and math.isfinite(p["mean"])]
    if not seq:
        return None
    seq.sort()
    if seq[0][1] <= level:
        return seq[0][0]
    for (t0, y0), (t1, y1) in zip(seq, seq[1:]):
        if y1 <= level:
            if y0 == y1:
                return t1
            return t0 + (t1 - t0) * (y0 - level) / (y0 - y1)
    return None


def aggregate_from_plates(plate_rows: Sequence[dict], metrics: Sequence[Metric],
                          n_items_key: Optional[str] = None) -> Aggregation:
    """Same output shape when the measurement IS the plate.

    Colony survival's headline quantities — colony count, stained fraction —
    are properties of a whole well, not of one colony, so there is no item level
    to average first. Those rows go straight in as plate rows and only the
    across-plate step runs. ``n_items_key`` names the column that says how many
    things the plate contained (colonies), purely so the qc sheet and the
    explorer can show it; it never enters a statistic.
    """
    per_plate = []
    for r in plate_rows:
        cond = str(r.get("condition", ""))
        info = split_condition(cond)
        n = r.get(n_items_key) if n_items_key else None
        try:
            n = int(n) if n is not None and math.isfinite(float(n)) else 0
        except (TypeError, ValueError):
            n = 0
        out = {"condition": cond, "strain": info["strain"], "dose": info["dose"],
               "unit": info["unit"], "plate": str(r.get("plate", "")),
               "n_items": n, "n_kept": n}
        for m in metrics:
            out[m.key] = (float(r[m.key])
                          if r.get(m.key) is not None
                          and _finite([r.get(m.key)]) else None)
            out[f"n_{m.key}"] = 1 if out[m.key] is not None else 0
        per_plate.append(out)

    per_condition = []
    for cond in sorted({p["condition"] for p in per_plate}):
        plates = [p for p in per_plate if p["condition"] == cond]
        info = split_condition(cond)
        out = {"condition": cond, "strain": info["strain"], "dose": info["dose"],
               "unit": info["unit"], "parsed": info["parsed"],
               "n_plates": len(plates),
               "n_plates_with_data": sum(1 for p in plates if p["n_kept"]),
               "n_items": sum(p["n_items"] for p in plates),
               "n_kept": sum(p["n_kept"] for p in plates)}
        for m in metrics:
            # Here the well IS the item: one well yields one number, so there
            # is no within-plate averaging step to skip and this mean was
            # already pooled over items. What it was missing is `_sem`, which
            # the figures and the explorer now draw — without it a counting
            # run would silently lose every error bar.
            #
            # `_plate_spread_sd` is emitted equal to `_sd` rather than blank:
            # its definition is the SD across plate means, and with one number
            # per plate that is exactly the SD across items. The two agreeing
            # is the honest answer for this assay, not a placeholder.
            vals = [p[m.key] for p in plates]
            out[f"{m.key}_mean"] = mean(vals)
            out[f"{m.key}_sd"] = sd(vals)
            out[f"{m.key}_sem"] = sem(vals)
            out[f"{m.key}_n"] = _n_finite(vals)
            out[f"{m.key}_pooled_median"] = median(vals)
            out[f"{m.key}_plate_spread_sd"] = sd(vals)
        per_condition.append(out)

    return Aggregation(per_plate=per_plate, per_condition=per_condition,
                       metrics=list(metrics),
                       n_items=sum(p["n_items"] for p in per_plate),
                       n_kept=sum(p["n_kept"] for p in per_plate))


# ---------------------------------------------------------------------------
# Distributions
#
# Moved here from survival_size so the four assays draw a distribution the same
# way. The numerics are that module's, unchanged: the bandwidth rule, the grid
# padding and the log-density -> value-density conversion are the ones whose
# output David already signed off on for body size.
# ---------------------------------------------------------------------------

GRID_N = 150
MIN_FOR_CURVE = 8


def kde_log(values, grid, np):
    """Normalised kernel density over ``grid`` (log space), or None if n < 8.

    Below 8 items a KDE is a picture of the bandwidth, not of the data, so we
    return nothing and the caller draws no curve for that group.
    """
    v = np.asarray([x for x in values if x and x > 0], dtype=float)
    if len(v) < MIN_FOR_CURVE:
        return None
    lv = np.log(v)
    # Silverman, eased up for small samples so a 23-item group does not sprout
    # spurious modes next to a 639-item one.
    bw = float(np.clip(0.62 * len(v) ** (-1 / 5), 0.17, 0.42)) * max(
        float(lv.std(ddof=1)) if len(lv) > 1 else 1.0, 1e-6)
    z = (grid[:, None] - lv[None, :]) / bw
    dens = np.exp(-0.5 * z * z).sum(1) / (len(v) * bw * np.sqrt(2 * np.pi))
    dens = dens / np.exp(grid)          # log-density -> value-density
    m = dens.max()
    return (dens / m) if m > 0 else None


def kde_linear(values, grid, np):
    """Same, without the log transform, for quantities that can be zero or that
    are not multiplicative — bend rates, time fractions, reversal rates. A dying
    animal at 0 bpm is a real observation and log space cannot hold it."""
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)],
                   dtype=float)
    if len(v) < MIN_FOR_CURVE:
        return None
    bw = float(np.clip(0.62 * len(v) ** (-1 / 5), 0.17, 0.42)) * max(
        float(v.std(ddof=1)) if len(v) > 1 else 1.0, 1e-9)
    z = (grid[:, None] - v[None, :]) / bw
    dens = np.exp(-0.5 * z * z).sum(1) / (len(v) * bw * np.sqrt(2 * np.pi))
    m = dens.max()
    return (dens / m) if m > 0 else None


def distribution(values_by_group: dict, log: bool, write_log=None) -> Optional[dict]:
    """A shared grid plus one density curve and percentile set per group.

    Returns None when nothing usable came in, so the caller skips the panel
    rather than drawing an empty axis.
    """
    import numpy as np

    allv = np.array([v for vals in values_by_group.values()
                     for v in _finite(vals)], dtype=float)
    if log:
        allv = allv[allv > 0]
    if not allv.size:
        return None

    if log:
        lo = math.log(max(float(np.quantile(allv, 0.002)), 1e-9))
        hi = math.log(float(np.quantile(allv, 0.998)))
        if not (hi > lo):
            hi = lo + 1.0
        pad = 0.18 * (hi - lo)
        grid = np.linspace(lo - pad, hi + pad, GRID_N)
        edges = np.exp(grid)
    else:
        lo = float(np.quantile(allv, 0.002))
        hi = float(np.quantile(allv, 0.998))
        if not (hi > lo):
            hi = lo + 1.0
        pad = 0.12 * (hi - lo)
        grid = np.linspace(max(lo - pad, 0.0), hi + pad, GRID_N)
        edges = grid

    groups: dict[str, dict] = {}
    n_total = 0
    for key in sorted(values_by_group):
        vals = np.array(_finite(values_by_group[key]), dtype=float)
        if not vals.size:
            continue
        n_total += int(vals.size)
        y = kde_log(vals, grid, np) if log else kde_linear(vals, grid, np)
        hist, _ = np.histogram(vals, bins=edges)
        groups[key] = {
            "n": int(vals.size),
            "hist": [int(v) for v in hist],
            "y": None if y is None else [round(float(v), 5) for v in y],
            "p10": _r(np.percentile(vals, 10)), "p25": _r(np.percentile(vals, 25)),
            "p50": _r(np.percentile(vals, 50)), "p75": _r(np.percentile(vals, 75)),
            "p90": _r(np.percentile(vals, 90)), "mean": _r(vals.mean()),
        }
        if y is None and write_log:
            write_log(f"distribution: {key} has {vals.size} item(s) — under the "
                      f"{MIN_FOR_CURVE} needed for a density curve; its "
                      "percentiles are still reported, but it draws no curve.")
    if not groups:
        return None
    return {"x": [round(float(v), 4) for v in edges],
            "bin_edges": [float(v) for v in edges],
            "groups": groups, "n_total": n_total, "log": bool(log)}


def _r(v) -> float:
    return round(float(v), 3)
