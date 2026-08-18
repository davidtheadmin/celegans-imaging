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

from pathlib import Path
from typing import Callable, Optional, Sequence

import assay_common as AC
import assay_excel as AX
import assay_explorer as AE
import assay_figures as AF

# ---------------------------------------------------------------------------
# Metric declarations
# ---------------------------------------------------------------------------

MOTILITY_METRICS = [
    AC.Metric("bpm", "Bend rate", "bends/min", agg="mean",
              note="Head-swing angle peaks, counted by head_angle_peaks_v2 — "
                   "the assay's headline. Half-peaks are halved, so one bend is "
                   "a full excursion and back."),
    AC.Metric("bend_interval_cv", "Bend-interval CV", "", agg="median",
              note="Spread of the time between bends. A healthy swimmer is "
                   "rhythmic; an irregular one can share a bend rate with it "
                   "and not share a phenotype. NaN below three peaks."),
    AC.Metric("speed_median_abs", "Median speed", "px/s", agg="median",
              note="Pixels, not microns — motility_params.json sets "
                   "microns_per_pixel to -1, so Tierpsy reports pixels and "
                   "frames throughout."),
    AC.Metric("duration_s", "Track duration", "s", agg="median",
              note="Clean observation time after the flicker filter, not video "
                   "length. Short tracks are the ones the quality gate drops."),
]

CRAWLING_METRICS = [
    AC.Metric("mean_speed_pxs", "Mean speed", "px/s", agg="mean", log=True,
              note="Pixels per second. The body-length companion "
                   "(mean_speed_bls) is in the per_worm sheet and cancels "
                   "magnification drift between days."),
    AC.Metric("fraction_paused", "Time paused", "fraction", agg="mean",
              note="Frames below 10% of that video's median speed. The "
                   "threshold is per video, so this compares within a day more "
                   "safely than across days."),
    AC.Metric("reversal_rate_per_min", "Reversal rate", "per min", agg="mean",
              note="Forward-to-backward transitions per observed minute — "
                   "observed, not elapsed, so gaps do not dilute it."),
    AC.Metric("turn_rate_per_min", "Turn rate", "per min", agg="mean",
              note="From the velocity-arrow detector: 60–140° counts as a turn, "
                   "≥140° as a reversal."),
    AC.Metric("tortuosity", "Tortuosity", "", agg="median",
              note="Path length over net displacement. 1 is a straight line; "
                   "large values mean the animal covered ground without going "
                   "anywhere."),
    AC.Metric("net_displacement_bl", "Net displacement", "body lengths",
              agg="median",
              note="Normalised by that plate's mean worm length, so it survives "
                   "a magnification change that the pixel column does not."),
    AC.Metric("bpm", "Bend rate", "bends/min", agg="mean",
              note="Same head-swing counter as the motility assay, on a "
                   "crawling animal. The two are not interchangeable — a "
                   "crawling gait is not a swimming one."),
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
            write_log: Callable[[str], None]) -> list:
    """The half of every report that is identical: sheets, figures, explorer."""
    AX.write_readme(wb, assay,
                    list(readme_extra) + AX.readme_common(metrics, keep_note,
                                                          unit_note))
    AX.write_run_info(wb, AX.run_info_common(
        assay, out_dir, len(agg.per_condition), len(agg.per_plate),
        agg.n_items, agg.n_kept, run_info_extra))
    AX.write_aggregate_sheets(wb, agg)
    AX.write_distribution_sheets(wb, dist, dist_label, dist_unit)

    written = []
    csv_path = out_dir / f"{stem}_condition_summary.csv"
    n = AX.write_condition_csv(csv_path, agg)
    write_log(f"Wrote {csv_path} ({n} condition(s); the replication unit is the "
              "plate — see the workbook README)")
    written.append(csv_path)
    f = AF.fig_dose_response(out_dir / f"{stem}_dose_response.png", agg,
                             metrics, title, write_log)
    if f:
        written.append(f)
    strains = sorted({c["strain"] for c in agg.per_condition})
    doses = sorted({c["dose"] for c in agg.per_condition
                    if c.get("dose") is not None})
    f = AF.fig_distribution(out_dir / f"{stem}_distribution.png", dist,
                            dist_label, dist_unit, doses, strains,
                            AC.dose_unit_of(agg.per_condition),
                            dist_title, write_log)
    if f:
        written.append(f)

    payload = AE.build_payload(
        title=title, subtitle=subtitle, dr_caption=dr_caption, caveat=caveat,
        agg=agg, metrics=metrics, dist=dist, dist_title=dist_title,
        dist_caption=dist_caption, dist_label=dist_label, dist_unit=dist_unit,
        meta={"assay": assay})
    f = AE.write_explorer(out_dir / "explorer.html", payload, write_log)
    if f:
        written.append(f)
    return written


def _dist_by_strain_dose(items: Sequence[dict], key: str, log: bool,
                         keep, write_log) -> Optional[dict]:
    """Group per-item values as "<strain>|<dose>", the key the explorer and the
    distribution figure both index by. Conditions outside the dose grammar fall
    back to their own name so they are still drawn."""
    groups: dict[str, list] = {}
    for r in items:
        if not keep(r):
            continue
        info = AC.split_condition(str(r.get("condition", "")))
        gk = (f"{info['strain']}|{info['dose']}" if info["parsed"]
              else info["condition"])
        groups.setdefault(gk, []).append(r.get(key))
    return AC.distribution(groups, log=log, write_log=write_log)


def motility_report(wb, worm_rows: Sequence[dict], out_dir: Path,
                    write_log: Callable[[str], None], *,
                    long_threshold_s: Optional[float] = None,
                    bend_method: str = "head_angle_peaks_v2") -> list:
    """Motility: items are worms, the gate is is_long."""
    metrics = MOTILITY_METRICS

    def keep(r):
        return bool(r.get("is_long"))

    agg = AC.aggregate(worm_rows, metrics, keep=keep)
    dist = _dist_by_strain_dose(worm_rows, "bpm", log=False, keep=keep,
                                write_log=write_log)
    return _finish(
        wb=wb, agg=agg, dist=dist, metrics=metrics, out_dir=out_dir,
        stem="motility", assay="Motility (body bends, swimming in M9)",
        title="Motility — bend rate",
        subtitle=(f"{agg.n_kept:,} worms over the quality gate, of "
                  f"{agg.n_items:,} tracked, on {len(agg.per_plate)} plate(s) "
                  f"in {len(agg.per_condition)} condition(s)."),
        dr_caption="Marker is the condition mean across plates with ±1 SD; grey "
                   "dots are plates. The y-axis is shared across each row, so "
                   "columns compare directly.",
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
                    min_span_s: Optional[float] = None) -> list:
    """Crawling: items are worms, the gate is passed_filter."""
    metrics = CRAWLING_METRICS

    def keep(r):
        return bool(r.get("passed_filter"))

    agg = AC.aggregate(worm_rows, metrics, keep=keep)
    dist = _dist_by_strain_dose(worm_rows, "mean_speed_pxs", log=True,
                                keep=keep, write_log=write_log)
    return _finish(
        wb=wb, agg=agg, dist=dist, metrics=metrics, out_dir=out_dir,
        stem="crawling", assay="Crawling (locomotion on agar)",
        title="Crawling — population kinematics",
        subtitle=(f"{agg.n_kept:,} worms over the quality gate, of "
                  f"{agg.n_items:,} tracked, on {len(agg.per_plate)} plate(s) "
                  f"in {len(agg.per_condition)} condition(s)."),
        dr_caption="Marker is the condition mean across plates with ±1 SD; grey "
                   "dots are plates. The y-axis is shared across each row.",
        caveat="per_condition now aggregates PLATES, not worms. The previous "
               "worm-pooled numbers are kept as the per_condition_pooled sheet "
               "and as the *_pooled_median columns — they are not wrong, they "
               "answer a different question, with n = worms and a spread that "
               "is worm-to-worm rather than plate-to-plate.",
        dist_title="Speed distribution",
        dist_caption="Every worm that passed the gate. Log space, because speed "
                     "is multiplicative and a fixed bandwidth in linear space "
                     "over-smooths the slow peak.",
        dist_label="Mean speed", dist_unit="px/s",
        readme_extra=[
            ("This assay",
             "Crawling animals on agar, tracked by Tierpsy and regrouped by a "
             "linker that refuses ambiguous crossings rather than risk swapping "
             "two animals' identities. The per_worm sheet is unchanged and "
             "still carries all 47 columns."),
            ("Motility and crawling are not the same assay",
             "They share a tracker and a bend counter and nothing else: "
             "different Tierpsy parameters, a different linker, a different "
             "quality gate and a different output schema. A bend rate from one "
             "is not comparable with a bend rate from the other."),
        ],
        run_info_extra=[
            ("min_span_s", min_span_s if min_span_s else "(run default)"),
            ("quality_gate", "passed_filter — track span at or above the "
                             "minimum AND skeleton coverage at or above 0.70"),
        ],
        keep_note="A worm counts when passed_filter is true. Worms failing it "
                  "stay in the per_worm sheet with passed_filter false, so a "
                  "gate can be re-tuned without re-running the tracker. A worm "
                  "with no timeseries rows carries NaN, never 0, so unmeasured "
                  "animals cannot manufacture a dose-response.",
        unit_note="Speeds in pixels per second with body-length companions in "
                  "the per_worm sheet; distances in pixels or body lengths; "
                  "rates per observed minute; fractions 0–1. crawling_params."
                  "json sets microns_per_pixel to -1, so nothing is in microns.",
        write_log=write_log)


def counting_report(wb, plate_rows: Sequence[dict],
                    colony_rows: Sequence[dict], out_dir: Path,
                    write_log: Callable[[str], None], *,
                    options_note: str = "") -> list:
    """Colony survival: the measurement IS the plate, so plate rows go in
    directly. The distribution comes from the individual colonies."""
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
        subtitle=(f"{len(plate_rows)} well(s) in {len(agg.per_condition)} "
                  f"condition(s), {len(colony_rows):,} colonies measured"
                  + (f"; {n_confluent} well(s) flagged confluent."
                     if n_confluent else ".")),
        dr_caption="Marker is the condition mean across wells with ±1 SD; grey "
                   "dots are wells. Each well is one measurement, so the plate "
                   "IS the item here — there is no within-plate averaging step.",
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
        write_log=write_log)
