"""Tests for the shared assay layer: aggregation, sheets, CSV, explorer.

Run from launcher/:  python test_assay_reports.py

Dependency-light — no pytest, same style as test_survival_scale.py. Figures are
written and checked for existence only; nothing here looks at pixels.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import assay_common as AC          # noqa: E402
import assay_reports as AR         # noqa: E402

FAILURES: list[str] = []


def check(cond, what):
    print(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        FAILURES.append(what)


def near(a, b, tol=1e-6):
    return a is not None and abs(float(a) - float(b)) <= tol


# ---------------------------------------------------------------------------

def test_grammar():
    print("\ncondition grammar")
    check(AC.parse_condition("601 20J") == ("601", 20, "J/m²"),
          "\"601 20J\" parses to strain, dose, unit")
    check(AC.parse_condition("N2 100 uM") == ("N2", 100, "µM"),
          "a spaced µM dose parses")
    check(AC.parse_condition("control") is None,
          "a name outside the grammar does not parse")
    f = AC.split_condition("control")
    check(f["strain"] == "control" and f["dose"] is None and not f["parsed"],
          "an unparsed name becomes its own strain rather than being dropped")
    import survival
    check(survival.parse_condition is AC.parse_condition,
          "Development and the other assays share ONE grammar object")


def test_plate_first():
    print("\nthe replication unit is the plate")
    m = [AC.Metric("bpm", "Bend rate", "bends/min")]
    items = ([{"condition": "601 20J", "plate": "p1", "bpm": 10.0, "keep": True}] * 100
             + [{"condition": "601 20J", "plate": "p2", "bpm": 100.0, "keep": True}] * 2)
    agg = AC.aggregate(items, m, keep=lambda r: r["keep"])
    c = agg.per_condition[0]
    check(near(c["bpm_mean"], 55.0),
          f"a 100-worm plate does not outvote a 2-worm one (mean {c['bpm_mean']})")
    check(near(c["bpm_pooled_median"], 10.0),
          "the pooled median is kept alongside, and differs (10.0)")
    check(c["n_plates"] == 2 and c["n_kept"] == 102,
          "n_plates is 2 and n_kept is 102 — both are reported")
    check(near(c["bpm_sd"], 63.6396, 1e-3),
          "SD is across the two plate means, not across worms")

    one = AC.aggregate([{"condition": "N2 0J", "plate": "p1", "bpm": 5.0}], m)
    check(one.per_condition[0]["bpm_sd"] is None,
          "a single plate has no SD — blank, not 0.0")


def test_gate():
    print("\nquality gate")
    m = [AC.Metric("bpm", "Bend rate", "bends/min")]
    items = [{"condition": "N2 0J", "plate": "p1", "bpm": 10.0, "is_long": True},
             {"condition": "N2 0J", "plate": "p1", "bpm": 90.0, "is_long": False}]
    agg = AC.aggregate(items, m, keep=lambda r: r["is_long"])
    p = agg.per_plate[0]
    check(p["n_items"] == 2 and p["n_kept"] == 1,
          "a failing item is counted in n_items and excluded from n_kept")
    check(near(p["bpm"], 10.0), "…and excluded from the statistic")

    items = [{"condition": "N2 0J", "plate": "p1", "bpm": None},
             {"condition": "N2 0J", "plate": "p1", "bpm": float("nan")}]
    agg = AC.aggregate(items, m)
    check(agg.per_plate[0]["bpm"] is None and agg.per_plate[0]["n_bpm"] == 0,
          "all-non-finite gives blank and n=0, never 0.0")


def _worms(n_plates=3, n_worms=12):
    rows = []
    for strain, base in (("601", 40.0), ("N2", 90.0)):
        for dose in (0, 20, 40):
            for p in range(n_plates):
                for w in range(n_worms):
                    hit = base - (dose * 0.6 if strain == "601" else 0.05 * dose)
                    rows.append({
                        "condition": f"{strain} {dose}J", "plate": f"plate {p+1:02d}",
                        "bpm": max(0.0, hit + (w % 5) - 2 + p),
                        "bend_interval_cv": 0.2 + 0.01 * (w % 7),
                        "speed_median_abs": 30.0 + w,
                        "duration_s": 25.0 + w,
                        "is_long": w != 0,          # one short worm per plate
                        "mean_speed_pxs": 12.0 + w * 0.5,
                        "fraction_paused": 0.1 + 0.01 * (w % 5),
                        "reversal_rate_per_min": 2.0 + 0.1 * w,
                        "turn_rate_per_min": 1.0 + 0.05 * w,
                        "tortuosity": 1.2 + 0.02 * w,
                        "net_displacement_bl": 3.0 + 0.1 * w,
                        "passed_filter": w != 0,
                    })
    return rows


def _run(fn, tmp: Path, *args, **kw):
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    logs: list[str] = []
    out = fn(wb, *args, out_dir=tmp, write_log=logs.append, **kw) \
        if False else fn(wb, *args, tmp, logs.append, **kw)
    ws = wb.create_sheet("_keepalive") if not wb.sheetnames else None
    wb.save(tmp / "book.xlsx")
    return wb, out, logs


def test_motility(tmp: Path):
    print("\nmotility report")
    d = tmp / "mot"; d.mkdir()
    wb, written, logs = _run(AR.motility_report, d, _worms(), long_threshold_s=5.0)
    names = wb.sheetnames
    check("README" in names and "run_info" in names, "README and run_info written")
    check("plate_summary" in names and "condition_summary" in names
          and "qc" in names, "plate_summary, condition_summary and qc written")
    check("dist_histogram" in names and "dist_summary" in names,
          "the distribution sheets are written")
    check((d / "motility_condition_summary.csv").exists(),
          "a condition-summary CSV is written beside the workbook")
    check((d / "motility_dose_response.png").exists(),
          "the dose-response figure is written")
    check((d / "motility_distribution.png").exists(),
          "the distribution figure is written")
    check((d / "explorer.html").exists(), "explorer.html is written")

    with open(d / "motility_condition_summary.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    check(len(rows) == 6, f"one CSV row per condition (got {len(rows)})")
    check(rows[0]["n_plates"] == "3", "n_plates is on every row")
    c601_0 = next(r for r in rows if r["condition"] == "601 0J")
    c601_40 = next(r for r in rows if r["condition"] == "601 40J")
    check(float(c601_0["bpm_mean"]) > float(c601_40["bpm_mean"]),
          "the seeded dose effect survives aggregation")
    check(rows[0]["strain"] and rows[0]["dose"], "strain and dose are parsed out")

    html = (d / "explorer.html").read_text(encoding="utf-8")
    blob = re.search(r"const D = (\{.*?\});\n", html, re.S)
    check(blob is not None, "the payload is inlined in the explorer")
    if blob:
        D = json.loads(blob.group(1))
        check(D["has_dose"] and D["doses"] == [0, 20, 40],
              "the explorer knows its dose axis")
        check(len(D["cond"]) == 6 and len(D["plates"]) == 18,
              "every condition and plate is in the payload")
        check(D["dist"] and D["dist"]["log"] is False,
              "bend rate is drawn in linear space, not log")
        check(all("|" in k for k in D["dist"]["groups"]),
              "distribution groups are keyed strain|dose")
    check(any("explorer.html" in m for m in logs), "the explorer write is logged")


def test_crawling(tmp: Path):
    print("\ncrawling report")
    d = tmp / "crawl"; d.mkdir()
    wb, written, logs = _run(AR.crawling_report, d, _worms(), min_span_s=30.0)
    check("condition_summary" in wb.sheetnames,
          "condition_summary is added without touching per_worm/per_condition")
    check((d / "crawling_condition_summary.csv").exists()
          and (d / "explorer.html").exists(),
          "CSV and explorer are written")
    html = (d / "explorer.html").read_text(encoding="utf-8")
    D = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    check([m["key"] for m in D["metrics"]] ==
          ["mean_speed_pxs", "fraction_paused", "reversal_rate_per_min",
           "turn_rate_per_min", "tortuosity", "net_displacement_bl", "bpm"],
          "the agreed crawling panels, in order")
    check(D["dist"]["log"] is True, "speed is drawn in log space")
    check("plate" in (D.get("caveat") or "").lower(),
          "the change of replication unit is stated on the page")


def test_counting(tmp: Path):
    print("\ncounting report")
    d = tmp / "count"; d.mkdir()
    plates, colonies = [], []
    for dose in (0, 2, 4):
        for p in range(3):
            n = max(1, 60 - 14 * dose)
            plates.append({"condition": f"HeLa {dose}J", "plate": f"well {p+1}",
                           "colony_count": n + p, "mean_area_mm2": 0.8,
                           "median_area_mm2": 0.7,
                           "total_colony_area_mm2": 0.8 * (n + p),
                           "stained_fraction": 0.05 * (dose + 1),
                           "confluent": dose == 0 and p == 0})
            for k in range(n):
                colonies.append({"condition": f"HeLa {dose}J",
                                 "plate": f"well {p+1}",
                                 "equiv_diam_um": 300.0 + 8 * (k % 20)})
    wb, written, logs = _run(AR.counting_report, d, plates, colonies)
    check("condition_summary" in wb.sheetnames and "qc" in wb.sheetnames,
          "condition_summary and qc are added")
    check((d / "counting_condition_summary.csv").exists()
          and (d / "explorer.html").exists()
          and (d / "counting_dose_response.png").exists(),
          "CSV, explorer and figure are written")
    html = (d / "explorer.html").read_text(encoding="utf-8")
    D = json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))
    lo = next(c for c in D["cond"] if c["dose"] == 0)
    hi = next(c for c in D["cond"] if c["dose"] == 4)
    check(lo["stats"]["colony_count"]["mean"] > hi["stats"]["colony_count"]["mean"],
          "colony count falls with dose")
    check(lo["n_plates"] == 3, "each well is one plate row")
    check("confluent" in (D.get("caveat") or "").lower(),
          "the confluent wells are called out on the page")
    check(D["dist"]["log"] is True and D["dist"]["unit"] == "µm",
          "colony size is a log-space distribution in µm")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        test_grammar()
        test_plate_first()
        test_gate()
        test_motility(tmp)
        test_crawling(tmp)
        test_counting(tmp)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("all checks passed")
