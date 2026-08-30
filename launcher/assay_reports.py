"""Per-assay report: which metrics, what they mean, and the outputs.

One function per assay, each doing the same four things — aggregate to plate and
condition, add the sheets to the workbook, write two figures, write the
explorer. The assay-specific part is the metric list and the prose that goes
with it, which is exactly the part that should not be shared.

CHOOSING THE METRICS. Each list is short on purpose. Every pipeline computes far
more than appears here (crawling alone has 47 per-worm columns) and all of it
stays in the per-item sheet. What earns a panel is a quantity that answers a
different biological question from its neighbours; a second view of the same
question belongs in the workbook, where it costs nothing, rather than on a page
someone has to read.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional, Sequence

import assay_common as AC
import assay_excel as AX
import assay_explorer as AE
import assay_figures as AF

log = logging.getLogger(__name__)


def _safe_log(write_log: Optional[Callable[[str], None]]) -> Callable[[str], None]:
    """Wrap the caller's logger so that logging can never end a run.

    This is not defensive programming for its own sake. Two of the three
    pipelines build their log.txt inside a ``with open(...)`` block that has
    already closed by the time the workbook is written, so a ``write_log`` call
    from here hit a closed file handle and raised ValueError. That aborted the
    report layer AFTER it had written some of its outputs and, because the
    handler then tried to log the failure the same way, it raised a second time
    and escaped, taking the pipeline's own summary CSV and overview figure with
    it. A run was lost to a log line.

    Whatever the caller hands us, a message that cannot be delivered is
    dropped to the module logger and the analysis carries on.
    """
    def safe(msg: str) -> None:
        if write_log is not None:
            try:
                write_log(msg)
                return
            except Exception:                                  # noqa: BLE001
                pass
        log.info("[assay] %s", msg)
    return safe

# ---------------------------------------------------------------------------
# Metric declarations
# ---------------------------------------------------------------------------

# One experiment gives one number per condition per timepoint. The plates of a
# condition are not replicates of each other; they exist so more worms could be
# imaged. Said once here and reused, so no figure can drift out of step with it.
CAPTION_POOLED = (
    "Marker is the condition mean POOLED OVER WORMS with ±1 SEM. The bar says "
    "how well the mean is pinned by the animals imaged, not how far it would "
    "move if the experiment were repeated, which one experiment cannot tell "
    "you. Nothing is scattered behind it by default: a dot per plate presented "
    "the dish as a unit and read as three replicates however it was captioned. "
    "Switch on \u201cworms\u201d to put every animal behind its marker. "
)

MOTILITY_METRICS = [
    AC.Metric("bpm", "Bend rate", "bends/min", agg="mean",
              note="Head-swing angle peaks, counted by head_angle_peaks_v2 — "
                   "the assay's headline. Half-peaks are halved, so one bend is "
                   "a full excursion and back."),
    AC.Metric("bend_interval_cv", "Bend-interval CV", "", agg="median",
              note="Spread of the time between bends. A healthy swimmer is "
                   "rhythmic; an irregular one can share a bend rate with it "
                   "and not share a phenotype. NaN below three peaks."),
    AC.Metric("amplitude_deg", "Bend amplitude", "deg", agg="mean",
              note="Mean head-swing excursion from the detrended head-angle "
                   "signal, in degrees. Bend RATE and bend AMPLITUDE come "
                   "apart: an animal can keep its rhythm while the stroke "
                   "shrinks, and that is a phenotype the rate alone hides. "
                   "NaN below three peaks."),
    AC.Metric("amplitude_cv", "Amplitude CV", "", agg="median",
              note="Spread of the per-peak amplitude, scale-free. High means "
                   "an irregular stroke rather than a small one. Sits beside "
                   "bend_interval_cv, which is the same idea in time. NaN "
                   "below three peaks."),
    # Median speed was dropped from the metric set on David's call: a worm in
    # a drop does not translate, so px/s measured how much the animal drifted
    # rather than how hard it swam, and it took a panel that bend amplitude
    # now uses. It is still on every per-worm row and still drives the debris
    # rules — only the reporting layer stopped showing it.
    AC.Metric("duration_s", "Track duration", "s", agg="median",
              note="Clean observation time after the flicker filter, not video "
                   "length. Short tracks are the ones the quality gate drops."),
]

# CRAWLING. The first four are what the figures draw; the rest stay in the
# workbook and the explorer, where a metric costs a line in a dropdown rather
# than a row of a seven-high facet grid nobody reads to the bottom of.
#
# SPEED IS IN BODY LENGTHS, NOT PIXELS. px/s confounds a paralysed animal with
# a small one, and worm length here moves with both the day and the treatment —
# exactly the axis the timecourse is plotted against. mean_speed_pxs is kept
# below, unpromoted, so the older numbers stay readable.
CRAWLING_METRICS = [
    AC.Metric("mean_speed_bls", "Mean speed", "BL/s", agg="mean", log=True,
              note="Body lengths per second — each worm's mean |speed| over "
                   "that plate's mean worm length. Body lengths rather than "
                   "pixels because a shrinking worm and a slowing worm are not "
                   "the same finding and px/s cannot tell them apart."),
    AC.Metric("fraction_paused", "Time paused", "fraction", agg="mean",
              note="Fraction of a worm's OBSERVED frames below 0.01 BL/s. The "
                   "threshold is fixed across every video and day, so a "
                   "condition where most animals stopped cannot rescale its "
                   "own definition of stopped."),
    AC.Metric("is_immobile", "Animals immobile", "fraction", agg="mean",
              note="Fraction of the plate's GATED animals whose whole-track "
                   "mean speed is under 0.02 BL/s — of the worms that passed "
                   "passed_filter, not of every animal on the agar, so read it "
                   "with n_kept beside it. A proportion, not an average: when "
                   "half a plate stops and half is unaffected the mean reports "
                   "a middle speed no animal had, and this does not."),
    AC.Metric("directionality", "Directionality", "", agg="median",
              note="Net displacement over path length. 1 is a straight line, "
                   "0 is covering ground without leaving. Replaces tortuosity, "
                   "which is this upside down and unbounded — it diverges "
                   "exactly where the animal stops, so its mean was set by "
                   "whichever worm came closest to not moving."),
    AC.Metric("mean_speed_pxs", "Mean speed", "px/s", agg="mean", log=True,
              headline=False,
              note="The same speed in pixels, kept so numbers from before the "
                   "body-length change stay reproducible. Not the headline: it "
                   "carries magnification drift and worm growth in it."),
    AC.Metric("reversal_rate_moving_per_min", "Reversal rate (moving)",
              "per min", agg="mean", headline=False,
              note="Forward-to-backward transitions per minute of MOVING time. "
                   "Per observed minute — the column beside it — falls whenever "
                   "speed falls, because a paused animal cannot reverse, so it "
                   "restates the speed panel instead of adding to it."),
    AC.Metric("reversal_rate_per_min", "Reversal rate (observed)", "per min",
              agg="mean", headline=False,
              note="The original: transitions per observed minute. Kept for "
                   "continuity and confounded with pausing — see above."),
    AC.Metric("turn_rate_per_min", "Turn rate", "per min", agg="mean",
              headline=False,
              note="From the velocity-arrow detector: 60–140° counts as a turn, "
                   "≥140° as a reversal."),
    AC.Metric("net_displacement_bl", "Net displacement", "body lengths",
              agg="median", headline=False,
              note="Normalised by that plate's mean worm length, so it survives "
                   "a magnification change that the pixel column does not. "
                   "Scales with how long the track was, so read it against "
                   "track_duration_s."),
    AC.Metric("bpm", "Bend rate", "bends/min", agg="mean", headline=False,
              note="Head-swing angle peaks on a crawling animal; half-peaks "
                   "count for half, so one bend is a full excursion and back. "
                   "NaN below three peaks."),
    AC.Metric("tortuosity", "Tortuosity", "", agg="median", headline=False,
              note="Path length over net displacement, unbounded. Superseded "
                   "by directionality; kept so older numbers reproduce."),
]

COUNTING_METRICS = [
    AC.Metric("colony_count", "Colonies", "count", agg="mean",
              note="Colonies surviving the size, solidity and edge filters. A "
                   "plate flagged confluent still reports a count, and that "
                   "count is not to be trusted — read stained fraction instead."),
    AC.Metric("stained_fraction", "Stained fraction", "fraction", agg="mean",
              note="Stained pixels over well area. The readout that still works "
                   "when colonies have merged."),
    AC.Metric("mean_area_mm2", "Mean colony area", "mm²", agg="mean",
              note="Physical units, from the known well diameter — this is the "
                   "one pipeline whose scale comes from geometry rather than "
                   "from image tags."),
    AC.Metric("total_colony_area_mm2", "Total colony area", "mm²", agg="mean",
              note="Count times mean area, in effect. Less sensitive than count "
                   "to the watershed splitting two touching colonies wrongly."),
]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _finish(*, wb, agg, dist, metrics, out_dir: Path, stem: str, assay: str,
            title: str, subtitle: str, dr_caption: str, caveat: str,
            dist_title: str, dist_caption: str, dist_label: str,
            dist_unit: str, readme_extra, run_info_extra,
            keep_note: str, unit_note: str,
            write_log: Callable[[str], None],
            survival_metric: Optional[AC.Metric] = None,
            norm_metric: Optional[AC.Metric] = None,
            scatter: str = AF.SCATTER_NONE) -> list:
    """The half of every report that is identical: sheets, figures, explorer.

    ``scatter`` is what the static figures draw behind their markers. The worm
    assays pass nothing and offer the individual animals in the explorer
    instead, where they can be toggled; colony survival passes SCATTER_PLATE
    because a well is one measurement and its dots are its data.
    """
    write_log = _safe_log(write_log)
    tps = list(getattr(agg, "timepoints", []) or [])
    AX.write_readme(wb, assay,
                    list(readme_extra) + AX.readme_common(metrics, keep_note,
                                                          unit_note, tps))
    AX.write_run_info(wb, AX.run_info_common(
        assay, out_dir, len(agg.per_condition), len(agg.per_plate),
        agg.n_items, agg.n_kept, run_info_extra))
    AX.write_aggregate_sheets(wb, agg)
    AX.write_distribution_sheets(wb, dist, dist_label, dist_unit)

    written = []
    csv_path = out_dir / f"{stem}_condition_summary.csv"
    n = AX.write_condition_csv(csv_path, agg)
    write_log(f"Wrote {csv_path} ({n} condition(s); pooled over items with "
              "SEM, not over plate means — see the workbook README)")
    written.append(csv_path)
    f = AF.fig_dose_response(out_dir / f"{stem}_dose_response.png", agg,
                             metrics, title, write_log, scatter=scatter)
    if f:
        written.append(f)
    # The timecourse headline. Self-skipping below two timepoints, so this is
    # unconditional and a single-folder run is unaffected.
    f = AF.fig_timecourse(out_dir / f"{stem}_timecourse.png", agg, metrics,
                          f"{title} over time", write_log, scatter=scatter)
    if f:
        written.append(f)
    # The sensitivity curve: every treated condition against its OWN control at
    # the same hour. Self-skipping below two timepoints, like the timecourse.
    # It uses the first headline metric because that is the one the assay is
    # read for; normalising a bounded fraction would be meaningless.
    hl = AC.headline(metrics)
    norm_metric = hl[0] if hl else None
    if norm_metric is not None and len(tps) > 1:
        f = AF.fig_normalised(out_dir / f"{stem}_normalised.png", agg,
                              norm_metric,
                              f"{norm_metric.label} relative to same-day "
                              "control", write_log, scatter=scatter)
        if f:
            written.append(f)
    if survival_metric is not None:
        f = AF.fig_survival(out_dir / f"{stem}_survival.png", agg,
                            survival_metric, f"{title} relative to untreated",
                            write_log)
        if f:
            written.append(f)
    strains = sorted({c["strain"] for c in agg.per_condition},
                     key=AC.strain_sort_key)
    doses = sorted({c["dose"] for c in agg.per_condition
                    if c.get("dose") is not None})
    f = AF.fig_distribution(out_dir / f"{stem}_distribution.png", dist,
                            dist_label, dist_unit, doses, strains,
                            AC.dose_unit_of(agg.per_condition),
                            dist_title, write_log, timepoints=tps)
    if f:
        written.append(f)

    payload = AE.build_payload(
        title=title, subtitle=subtitle, dr_caption=dr_caption, caveat=caveat,
        agg=agg, metrics=metrics, dist=dist, dist_title=dist_title,
        dist_caption=dist_caption, dist_label=dist_label, dist_unit=dist_unit,
        survival_metric=survival_metric,
        norm_metric=norm_metric,
        survival_caption=(
            "Every item as a percentage of the mean of its own strain's "
            "untreated items, so strains that plated at different densities "
            "can be compared. Marker is the condition mean POOLED OVER those "
            "normalised items, bar is ±1 SEM, faint dots are the plates — QC, "
            "not replicates. The "
            "untreated point is 100% by construction and its bar is the spread "
            "of the controls themselves. Linear is how the number is spoken and "
            "is the only scale that can draw a zero; log is how a "
            "multiplicative quantity behaves — 100%→10% and 10%→1% are the "
            "same step. Switch and read both."),
        meta={"assay": assay})
    f = AE.write_explorer(out_dir / "explorer.html", payload, write_log)
    if f:
        written.append(f)
    return written


def _dist_by_strain_dose(items: Sequence[dict], key: str, log: bool,
                         keep, write_log,
                         by_timepoint: bool = False) -> Optional[dict]:
    """Group per-item values by strain, dose and — in a timecourse — timepoint.

    The key is AC.dist_key's, so the figure and the explorer index the groups
    with the same string this builds them with. ``by_timepoint`` adds the hour:
    without it a timecourse pools five imaging days into one density per
    condition, which is not a summary of them but a shape none of the five had.
    Conditions outside the dose grammar fall back to their own name so they are
    still drawn.
    """
    groups: dict[str, list] = {}
    meta: dict[str, dict] = {}
    for r in items:
        if not keep(r):
            continue
        info = AC.split_condition(str(r.get("condition", "")))
        strain = info["strain"] if info["parsed"] else info["condition"]
        dose = info["dose"] if info["parsed"] else None
        tp = None
        if by_timepoint:
            try:
                tp = float(r.get("timepoint_h"))
            except (TypeError, ValueError):
                tp = None
        gk = AC.dist_key(strain, dose, tp)
        groups.setdefault(gk, []).append(r.get(key))
        meta.setdefault(gk, {"strain": strain, "dose": dose, "tp": tp,
                             "condition": info["condition"]})
    dist = AC.distribution(groups, log=log, write_log=write_log)
    if dist is not None:
        dist["group_meta"] = {k: meta[k] for k in dist["groups"] if k in meta}
    return dist


def motility_report(wb, worm_rows: Sequence[dict], out_dir: Path,
                    write_log: Callable[[str], None], *,
                    long_threshold_s: Optional[float] = None,
                    bend_method: str = "head_angle_peaks_v2",
                    by_timepoint: bool = False) -> list:
    """Motility: items are worms, the gate is is_long.

    ``by_timepoint`` splits every plate and condition row by timepoint and adds
    the metric-vs-time figure — a multi-folder timecourse.
    """
    metrics = MOTILITY_METRICS

    write_log = _safe_log(write_log)

    def keep(r):
        return bool(r.get("is_long"))

    agg = AC.aggregate(worm_rows, metrics, keep=keep,
                       by_timepoint=by_timepoint)
    dist = _dist_by_strain_dose(worm_rows, "bpm", log=False, keep=keep,
                                write_log=write_log,
                                by_timepoint=by_timepoint)
    return _finish(
        wb=wb, agg=agg, dist=dist, metrics=metrics, out_dir=out_dir,
        stem="motility", assay="Motility (body bends, swimming in M9)",
        title="Motility — bend rate",
        subtitle=(f"{agg.n_kept:,} worms over the quality gate, of "
                  f"{agg.n_items:,} tracked, on {len(agg.per_plate)} plate(s) "
                  f"in {len(agg.per_condition)} condition(s)."),
        dr_caption=CAPTION_POOLED + "The y-axis is shared across "
                   "each row, so columns compare directly.",
        caveat="Every quantity here is in PIXELS or in bends and seconds — "
               "motility_params.json sets microns_per_pixel to -1, so nothing "
               "on this page is in physical distance units.",
        dist_title="Bend-rate distribution",
        dist_caption="Every worm that passed the gate, no condition means — the "
                     "continuous quantity behind the panel above. Linear space, "
                     "not log: a dying animal at 0 bends/min is a real "
                     "observation and log space cannot hold it.",
        dist_label="Bend rate", dist_unit="bends/min",
        readme_extra=[
            ("This assay",
             "Body bends of animals swimming in a drop of M9, counted from the "
             "head-swing angle. The per-condition sheets of individual worms "
             "that this workbook already carried are unchanged; the sheets "
             "described below are the plate and condition layer added on top."),
            ("Why the bend rate is not a curvature measure",
             "A detrended midbody-curvature counter roughly doubled the count "
             "on slow animals in calibration, and slow animals are exactly what "
             "a dose experiment has to resolve. The head-swing counter does "
             "not. See docs/calibration/."),
        ],
        run_info_extra=[
            ("bend_method", bend_method),
            ("long_threshold_s", long_threshold_s if long_threshold_s
             else "(run default)"),
            ("quality_gate", "is_long — a worm is included when its clean "
                             "observation time reaches the long threshold"),
        ],
        keep_note="A worm counts when is_long is true. Worms below the "
                  "threshold, worms killed by the flicker filter, and objects "
                  "removed by the debris filter are in n_items but in no "
                  "statistic; the per-video analysis_log.json names each drop "
                  "and its reason.",
        unit_note="Bend rate in bends per minute, durations in seconds, speed "
                  "in pixels per second, CV dimensionless. No physical distance "
                  "units anywhere.",
        write_log=write_log)


def crawling_report(wb, worm_rows: Sequence[dict], out_dir: Path,
                    write_log: Callable[[str], None], *,
                    min_span_s: Optional[float] = None,
                    by_timepoint: bool = False,
                    min_fragment_coverage: Optional[float] = None) -> list:
    """Crawling: items are worms, the gate is passed_filter.

    ``by_timepoint`` splits every plate and condition row by timepoint and adds
    the metric-vs-time figure — a multi-folder timecourse.
    """
    metrics = CRAWLING_METRICS

    write_log = _safe_log(write_log)

    def keep(r):
        return bool(r.get("passed_filter"))

    agg = AC.aggregate(worm_rows, metrics, keep=keep,
                       by_timepoint=by_timepoint)
    dist = _dist_by_strain_dose(worm_rows, "mean_speed_bls", log=True,
                                keep=keep, write_log=write_log,
                                by_timepoint=by_timepoint)
    return _finish(
        wb=wb, agg=agg, dist=dist, metrics=metrics, out_dir=out_dir,
        stem="crawling", assay="Crawling (locomotion on agar)",
        title="Crawling — population kinematics",
        subtitle=(f"{agg.n_kept:,} worms over the quality gate, of "
                  f"{agg.n_items:,} tracked, on {len(agg.per_plate)} plate(s) "
                  f"in {len(agg.per_condition)} condition(s)."),
        dr_caption=CAPTION_POOLED + "The y-axis is shared across "
                   "each row. In a timecourse there is one line per timepoint "
                   "— nothing here is averaged across days.",
        # No caveat banner. It said per_condition aggregates plates rather
        # than worms, which has been true since 26 Aug, is stated in the
        # workbook README where a reader meets those columns, and by now was
        # a red bar at the top of every page carrying news from two days ago.
        # A standing warning that is always there is not read.
        caveat="",
        dist_title="Speed distribution",
        dist_caption="Every worm that passed the gate. Log space, because speed "
                     "is multiplicative and a fixed bandwidth in linear space "
                     "over-smooths the slow peak. This is the panel that says "
                     "whether a falling mean is a whole population slowing or "
                     "part of it stopping — the two are different results and "
                     "the condition mean cannot tell them apart.",
        dist_label="Mean speed", dist_unit="BL/s",
        readme_extra=[
            ("This assay",
             "Crawling animals on agar, tracked by Tierpsy and regrouped by a "
             "linker that refuses ambiguous crossings rather than risk swapping "
             "two animals' identities. The per_worm sheet carries every column "
             "the pipeline computes, including source_folder and timepoint_h, "
             "and is the same table as per_worm_rows.csv beside the workbook."),
            ("Objects that were never skeletonised",
             "Tierpsy tracks blobs whether or not it can trace an outline for "
             "them, and the linker groups that table, so an object it never "
             "once skeletonised used to become a worm row with a speed "
             "averaged over a handful of frames — landing at nearly zero and "
             "indistinguishable from an animal that did not move. Fragments "
             "below the min_fragment_skeleton_coverage on the run_info sheet "
             "are now dropped BEFORE linking, so they also stop occupying "
             "plate positions the linker reasons about. It is a no-op on a "
             "cleanly tracked video. Each video's analysis_log.json says how "
             "many fragments it removed."),
            ("Which columns the figures draw",
             "Mean speed (BL/s), time paused, fraction of animals immobile and "
             "directionality. The rest — px/s speed, both reversal rates, turn "
             "rate, net displacement, bend rate, tortuosity — are computed and "
             "kept in every sheet and selectable in explorer.html, but do not "
             "take a row in the figures. Reversal rate and tortuosity are "
             "there with a warning attached: see their METRICS rows."),
            ("Motility and crawling are not the same assay",
             "They share a tracker and a bend counter and nothing else: "
             "different Tierpsy parameters, a different linker, a different "
             "quality gate and a different output schema. A bend rate from one "
             "is not comparable with a bend rate from the other."),
        ],
        run_info_extra=[
            ("min_span_s", min_span_s if min_span_s else "(run default)"),
            # This said "AND skeleton coverage at or above 0.70" long after
            # that floor was removed. A run_info line that describes a gate the
            # run did not apply is worse than no line: it is the first thing
            # anyone checks when a number looks wrong.
            ("quality_gate", "passed_filter — track span at or above "
                             "min_span_s. Track length only; skeleton coverage "
                             "is an information column, not a gate."),
            ("min_fragment_skeleton_coverage",
             min_fragment_coverage if min_fragment_coverage is not None
             else "(not applied)"),
        ],
        keep_note="A worm counts when passed_filter is true. Worms failing it "
                  "stay in the per_worm sheet with passed_filter false, so a "
                  "gate can be re-tuned without re-running the tracker. A worm "
                  "with no timeseries rows carries NaN, never 0, so unmeasured "
                  "animals cannot manufacture a dose-response.",
        unit_note="The headline speed is in BODY LENGTHS per second; the px/s "
                  "column is kept beside it. Distances in pixels or body "
                  "lengths; rates per observed or per moving minute as the "
                  "column name says; fractions 0–1. crawling_params.json sets "
                  "microns_per_pixel to -1, so nothing is in microns — body "
                  "lengths are the only scale-free unit this assay has.",
        write_log=write_log)


def counting_report(wb, plate_rows: Sequence[dict],
                    colony_rows: Sequence[dict], out_dir: Path,
                    write_log: Callable[[str], None], *,
                    options_note: str = "") -> list:
    """Colony survival: the measurement IS the plate, so plate rows go in
    directly. The distribution comes from the individual colonies."""
    write_log = _safe_log(write_log)
    metrics = COUNTING_METRICS
    agg = AC.aggregate_from_plates(plate_rows, metrics,
                                   n_items_key="colony_count")
    dist = _dist_by_strain_dose(colony_rows, "equiv_diam_um", log=True,
                                keep=lambda r: True, write_log=write_log)
    n_confluent = sum(1 for r in plate_rows if r.get("confluent"))
    return _finish(
        wb=wb, agg=agg, dist=dist, metrics=metrics, out_dir=out_dir,
        stem="counting", assay="Colony survival (clonogenic, stained wells)",
        title="Colony survival",
        # The well IS the item here, so its dots are the data, not a second
        # layer of aggregation. The worm assays draw nothing.
        scatter=AF.SCATTER_PLATE,
        subtitle=(f"{len(plate_rows)} well(s) in {len(agg.per_condition)} "
                  f"condition(s), {len(colony_rows):,} colonies measured"
                  + (f"; {n_confluent} well(s) flagged confluent."
                     if n_confluent else ".")),
        dr_caption="Marker is the condition mean across wells with ±1 SEM; "
                   "grey dots are wells. Each well is one measurement, so the "
                   "well IS the item here — there is no within-plate averaging "
                   "step to skip. The wells of a condition are not replicates "
                   "of each other either; they are more colonies from the same "
                   "experiment, so the bar says how well the mean is pinned by "
                   "the wells counted, not how far it would move on a repeat.",
        caveat=("{} well(s) are flagged confluent: their colonies have merged, "
                "so the count is unreliable and the stained fraction is the "
                "readout to use. They are included in every panel — excluding "
                "them would silently drop the highest-density conditions."
                ).format(n_confluent) if n_confluent else "",
        dist_title="Colony-size distribution",
        dist_caption="Every colony kept by the filters, as equivalent circular "
                     "diameter. Log space, because colony growth is "
                     "multiplicative. Colonies removed by the size, solidity or "
                     "edge filters are not here — they are the faint grey "
                     "outlines in the overlay images.",
        dist_label="Equivalent diameter", dist_unit="µm",
        readme_extra=[
            ("This assay",
             "Clonogenic survival of stained cells in single-well plates. It "
             "uses the same capture head as the worm assays and none of the "
             "worm analysis — it is here to show the instrument is not limited "
             "to nematodes. The per_colony and per_plate sheets are unchanged."),
            ("Scale is real here",
             "Micrometres per pixel comes from the known well diameter, so "
             "areas and diameters are physical. This is the only pipeline whose "
             "scale is set by geometry rather than by image metadata."),
        ],
        run_info_extra=[
            ("quality_gate", "colonies touching the well edge, below the "
                             "minimum diameter, or below the minimum solidity "
                             "are removed before counting"),
            ("n_wells_confluent", n_confluent),
            ("options", options_note or "(see log.txt)"),
        ],
        keep_note="Dropped colonies are not in any sheet — they appear as faint "
                  "grey outlines in overlays/ and as the n_raw versus n_kept "
                  "pair in log.txt. A whole well that could not be read, or "
                  "where no well circle was found, produces no row at all.",
        unit_note="Areas in mm², diameters in µm, stained fraction 0–1 (not a "
                  "percentage), counts as counts.",
        write_log=write_log,
        survival_metric=metrics[0])
