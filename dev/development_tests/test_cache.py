"""The detection cache: does a second run really skip the model, and does it
get the same numbers when it does?

The workflow being protected: analyse timepoint 0, analyse timepoint 24, then
run both together for the figures. That third run must do no inference at all
and must produce the same aggregate as a single run over both folders.
"""
import csv, json, math, random, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WORK = HERE / "_scratch_cache"
sys.path.insert(0, str(REPO / "launcher"))

import survival, survival_cache

if WORK.exists():
    shutil.rmtree(WORK)
ROOT = WORK / "data"

STAGES = ["L1", "L2", "L3", "L4", "adult"]
MODEL_CLASSES = STAGES + ["egg"]
fails = []
inferred_batches = []          # one entry per run_inference call


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + detail))
    if not cond:
        fails.append(name)


# --- two folders of ISO-stamped images -------------------------------------
for day, folder in (("20260805", "t0"), ("20260806", "t24")):
    d = ROOT / folder
    d.mkdir(parents=True)
    for strain in ("N2", "601"):
        for dose in (0, 20):
            for p in (1, 2):
                for qi, q in enumerate(("NE", "NW", "SE", "SW")):
                    (d / f"{strain}_T_{dose}J_p{p:02d}_{day}T0754{qi:02d}_{q}.png"
                     ).write_bytes(b"x")

# --- a deterministic stub for the vision subprocess ------------------------
# Detections are a pure function of the filename, so the same image always
# yields the same rows however many times it is analysed. That is what makes
# "cached and fresh agree" a meaningful assertion rather than a coincidence.
REF = {"L1": 0.8075, "L2": 0.1418, "L3": 0.7944, "L4": 0.7199, "adult": 0.86}


def _vec_for(name, k):
    rng = random.Random(f"{name}|{k}")
    return {s: round(rng.uniform(0.05, 0.95), 6) for s in MODEL_CLASSES}


def _label(vec, alpha, eligible):
    return max(eligible, key=lambda s: vec[s] / (REF.get(s, 1.0) ** alpha))


def fake_run_inference(images, class_conf, *, exclude_classes, preview_dir,
                       soft_csv, rescore, write_log, progress_cb=None,
                       cancel_check=None):
    inferred_batches.append([Path(i).name for i in images])
    write_log(f"[stub] analysing {len(images)} image(s)")
    alpha = 2.0 if rescore else 0.0
    eligible = [s for s in MODEL_CLASSES
                if s not in {c.lower() for c in (exclude_classes or [])}]
    records, rows = [], []
    for img in images:
        counts = {}
        n = 6 + (hash(img.name) % 9)
        for k in range(n):
            vec = _vec_for(img.name, k)
            raw = _label(vec, 0.0, eligible)
            hard = _label(vec, alpha, eligible)
            counts[hard] = counts.get(hard, 0) + 1
            size = math.exp(3.4 + 0.2 * STAGES.index(hard if hard in STAGES
                                                     else "L1"))
            rows.append([img.name, k, 0, 0, size, size, size, size,
                         round(size, 2), hard, raw, round(vec[hard], 5),
                         1.0, 0.5]
                        + [vec[s] for s in MODEL_CLASSES]
                        + [round(vec[s] / sum(vec.values()), 6)
                           for s in MODEL_CLASSES])
        records.append({"path": str(img), "counts": counts,
                        "w": 4056, "h": 3040})
        if progress_cb:
            progress_cb(len(records), len(images), img.name)
    with open(soft_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["image", "det_index", "x1", "y1", "x2", "y2", "w_px",
                    "h_px", "size_px", "hard_call", "hard_call_raw",
                    "hard_score", "match_iou", "entropy"]
                   + [f"raw_{s}" for s in MODEL_CLASSES]
                   + [f"p_{s}" for s in MODEL_CLASSES])
        w.writerows(rows)
    meta = {"names": MODEL_CLASSES, "class_conf": {}, "conf": 0.25,
            "overlap": 0.35, "seam": {}, "exclude_classes": ["egg"],
            "rescore": {"alpha": alpha, "refs": dict(REF)}}
    return list(MODEL_CLASSES), records, meta


survival.run_inference = fake_run_inference


def run(folders, tag, **kw):
    plans = survival.resolve_timepoints([(ROOT / f, "") for f in folders])
    # Output goes INSIDE the first folder, exactly as the agent does it —
    # which is also where the cache looks for previous runs.
    out = ROOT / folders[0] / f"_development_{tag}"
    out.mkdir(parents=True)
    lines = []
    res = survival.analyze(plans, {"L1": 0.4}, False, out,
                           exclude_classes=["egg"], write_log=lines.append, **kw)
    return res, "\n".join(lines), out


def totals(out_dir):
    """Per-condition stage counts, summed from per_image.

    Read off per_image because those cells are the raw measurement and hold
    literals; everything downstream is a formula with no cached value until
    Excel opens the file. Includes the per-stage split, not just the total —
    relabelling changes which stage an animal is counted as and NEVER how many
    there are, so a total-only fingerprint could not tell alpha 0 from alpha 2.
    """
    import collections, openpyxl
    wb = openpyxl.load_workbook(out_dir / "development_results.xlsx")
    ws = wb["per_image"]
    hdr = [c.value for c in ws[1]]
    i_tp, i_cond = hdr.index("timepoint_h"), hdr.index("condition")
    stage_idx = {s: hdr.index(s) for s in STAGES if s in hdr}
    acc = collections.defaultdict(collections.Counter)
    for row in ws.iter_rows(min_row=2, values_only=True):
        key = (row[i_tp], row[i_cond])
        for s, i in stage_idx.items():
            acc[key][s] += int(row[i] or 0)
    wb.close()
    return sorted((k[0], k[1], tuple(sorted(v.items()))) for k, v in acc.items())


print("1. first run over t0 — everything is new")
inferred_batches.clear()
r1, log1, out1 = run(["t0"], "t0")
n_first = sum(len(b) for b in inferred_batches)
check("every image analysed", n_first == 32, str(n_first))
check("a manifest was written",
      (out1 / survival_cache.MANIFEST_NAME).is_file())
check("no reuse claimed", "REUSE: 0 image(s)" in log1)
check("no note on the completion dialog", not r1["reuse_note"], r1["reuse_note"])

print("\n2. same folder again — nothing should reach the model")
inferred_batches.clear()
r2, log2, out2 = run(["t0"], "t0_again")
check("the model was not run at all", sum(len(b) for b in inferred_batches) == 0,
      str(inferred_batches))
check("reuse logged loudly", "REUSE: 32 image(s) taken from previous runs" in log2)
check("log says the model was not run",
      "the model was not run at all" in log2)
check("the user is told", "No images were analysed" in (r2["reuse_note"] or ""),
      r2["reuse_note"])
check("same numbers as the fresh run", totals(out1) == totals(out2))

print("\n3. second timepoint, then both together")
inferred_batches.clear()
r3, log3, out3 = run(["t24"], "t24")
check("t24 analysed fresh", sum(len(b) for b in inferred_batches) == 32)
inferred_batches.clear()
r4, log4, out4 = run(["t0", "t24"], "both")
check("combining runs no inference at all",
      sum(len(b) for b in inferred_batches) == 0, str(inferred_batches))
check("both folders reported as reused",
      "REUSE: 64 image(s) taken from previous runs" in log4)
check("two timepoints in the combined run",
      len({t for t, _, _ in totals(out4)}) == 2)
# Compared without the timepoint: a folder analysed on its own has no second
# folder to be relative to, so it is 0 h there and 24 h in the combined run.
# The counts must be identical either way — that is the point of the cache.
_strip = lambda rows: sorted((c, n) for _, c, n in rows)
check("combined counts are the union of the two solo runs",
      _strip(totals(out4)) == sorted(_strip(totals(out1)) + _strip(totals(out3))),
      "")

print("\n4. a new plate dropped into an analysed folder")
extra = ROOT / "t0" / "N2_T_60J_p01_20260805T075499_NE.png"
extra.write_bytes(b"x")
inferred_batches.clear()
r5, log5, out5 = run(["t0"], "t0_plus")
check("only the new image is analysed",
      [b for b in inferred_batches] == [[extra.name]], str(inferred_batches))
check("the other 32 are reused", "REUSE: 32 image(s)" in log5)
check("the note counts both", "Reused 32 of 33" in (r5["reuse_note"] or ""),
      r5["reuse_note"])

print("\n5. an edited image invalidates just itself")
one = sorted((ROOT / "t24").glob("*.png"))[0]
one.write_bytes(b"xy")          # size changes -> fingerprint changes
inferred_batches.clear()
r6, log6, out6 = run(["t24"], "t24_edit")
check("only the edited image is re-analysed",
      inferred_batches == [[one.name]], str(inferred_batches))

print("\n6. changing a detection setting drops the cache entirely")
inferred_batches.clear()
plans = survival.resolve_timepoints([(ROOT / "t24", "")])
out = ROOT / "t24" / "_development_conf"
out.mkdir()
lines = []
survival.analyze(plans, {"L1": 0.55}, False, out, exclude_classes=["egg"],
                 write_log=lines.append)
check("everything re-analysed when a confidence floor moves",
      sum(len(b) for b in inferred_batches) == 32,
      str(sum(len(b) for b in inferred_batches)))
check("and it says why",
      "detection settings or the model have changed" in "\n".join(lines))

print("\n7. force re-analyse ignores the cache")
inferred_batches.clear()
r7, log7, out7 = run(["t0"], "forced", force_reanalyze=True)
check("every image analysed again",
      sum(len(b) for b in inferred_batches) == 33,
      str(sum(len(b) for b in inferred_batches)))
check("and it says so", "Re-analyse requested" in log7)

print("\n8. flipping the rescoring switch is recomputed, NOT re-analysed")
inferred_batches.clear()
r8, log8, out8 = run(["t24"], "alpha0", rescore=False)
check("still no inference", sum(len(b) for b in inferred_batches) == 0,
      str(inferred_batches))
check("cached detections were relabelled", "relabelled" in log8)
check("alpha 0 reported as applied",
      "ACTUALLY APPLIED: OFF (alpha 0)" in log8)
check("the numbers actually changed", totals(out8) != totals(out6),
      "alpha 0 gave the same per-condition totals as alpha 2")
# and the relabelling must match what a fresh alpha-0 run produces
inferred_batches.clear()
r9, log9, out9 = run(["t24"], "alpha0_fresh", rescore=False,
                     force_reanalyze=True)
check("relabelled-from-cache == freshly analysed at alpha 0",
      totals(out8) == totals(out9),
      f"{totals(out8)[:2]} vs {totals(out9)[:2]}")

print("\n9. the merged CSV is consistent with the counts")
with open(out8 / "soft_stage_scores.csv", newline="", encoding="utf-8") as fh:
    rd = csv.DictReader(fh)
    rows = list(rd)
check("CSV rows carry this run's timepoint",
      {r["timepoint_h"] for r in rows} == {"0"}, str({r["timepoint_h"] for r in rows}))
import collections
csv_counts = collections.Counter(r["hard_call"] for r in rows)
sheet_counts = collections.Counter()
for _, _, pairs in totals(out8):
    for stage, n in pairs:
        sheet_counts[stage] += n
check("CSV labels match the workbook's counts",
      all(csv_counts[s] == sheet_counts[s] for s in STAGES),
      f"csv={dict(csv_counts)} sheet={dict(sheet_counts)}")

print("\n10. five timepoints analysed one at a time, then combined")
# The workflow this whole feature exists for. Each folder gets analysed on the
# day it was imaged; at the end all five are run together for the figures.
FIVE = WORK / "five"
DAYS = ["20260901", "20260902", "20260903", "20260904", "20260905"]
for di, day in enumerate(DAYS):
    d = FIVE / f"tp{di}"
    d.mkdir(parents=True)
    for strain in ("N2", "601"):
        for dose in (0, 20):
            for qi, q in enumerate(("NE", "NW", "SE", "SW")):
                (d / f"{strain}_T_{dose}J_p01_{day}T1200{qi:02d}_{q}.png"
                 ).write_bytes(b"x")

solo_out = []
for di in range(5):
    inferred_batches.clear()
    plans = survival.resolve_timepoints([(FIVE / f"tp{di}", "")])
    out = FIVE / f"tp{di}" / "_development_solo"
    out.mkdir(parents=True)
    lines = []
    survival.analyze(plans, {"L1": 0.4}, False, out, exclude_classes=["egg"],
                     write_log=lines.append)
    solo_out.append(out)
    if sum(len(b) for b in inferred_batches) != 16:
        check(f"tp{di} analysed fresh", False,
              str(sum(len(b) for b in inferred_batches)))
check("all five analysed individually", len(solo_out) == 5)

# Now combine them. Timepoints derive from the capture stamps: 0, 24, 48, ...
inferred_batches.clear()
plans = survival.resolve_timepoints([(FIVE / f"tp{di}", "") for di in range(5)])
check("five timepoints derived",
      [p.hours for p in plans] == [0.0, 24.0, 48.0, 72.0, 96.0],
      str([p.hours for p in plans]))
combined = FIVE / "tp0" / "_development_combined"
combined.mkdir(parents=True)
lines = []
res = survival.analyze(plans, {"L1": 0.4}, False, combined,
                       exclude_classes=["egg"], write_log=lines.append)
log10 = "\n".join(lines)
check("combining five folders runs the model zero times",
      sum(len(b) for b in inferred_batches) == 0, str(inferred_batches))
check("all 80 images reported as reused",
      "REUSE: 80 image(s) taken from previous runs" in log10)
check("the message says nothing was analysed",
      "No images were analysed" in (res["reuse_note"] or ""), res["reuse_note"])

check("workbook written for the combined run",
      (combined / "development_results.xlsx").is_file())
check("explorer written for the combined run",
      (combined / "explorer.html").is_file())
check("all four figures written for the combined run",
      sorted(p.name for p in combined.glob("*.png")) ==
      ["body_size.png", "quality_control.png", "stage_composition.png",
       "stage_index.png"],
      str(sorted(p.name for p in combined.glob("*.png"))))
check("five timepoints in the combined workbook",
      sorted({t for t, _, _ in totals(combined)}) == [0, 24, 48, 72, 96],
      str(sorted({t for t, _, _ in totals(combined)})))

with open(combined / "soft_stage_scores.csv", newline="", encoding="utf-8") as fh:
    rows10 = list(csv.DictReader(fh))
check("the merged CSV spans all five timepoints",
      {r["timepoint_h"] for r in rows10} == {"0", "24", "48", "72", "96"},
      str(sorted({r["timepoint_h"] for r in rows10})))
check("every folder contributed rows",
      {r["folder"] for r in rows10} == {f"tp{i}" for i in range(5)},
      str(sorted({r["folder"] for r in rows10})))

# the counts must match the five solo runs, folder for folder
solo_counts = sorted(x for out in solo_out for x in _strip(totals(out)))
check("combined counts equal the five solo runs put together",
      sorted(_strip(totals(combined))) == solo_counts)

print("\n11. the dialog's preview matches what the run does")
preview = survival.plan_reuse(plans, {"L1": 0.4}, exclude_classes=["egg"])
check("preview says everything is cached", preview.all_cached, "")
check("preview counts match", (preview.n_images, preview.n_reused,
                               preview.n_fresh) == (80, 80, 0),
      f"{preview.n_images}/{preview.n_reused}/{preview.n_fresh}")
check("preview lists every folder", len(preview.folder_lines()) == 5)
check("preview lines read sensibly",
      all("all 16 already done" in line for line in preview.folder_lines()),
      str(preview.folder_lines()))

print()
if fails:
    print(f"{len(fails)} FAILURE(S): " + ", ".join(fails))
    sys.exit(1)
print("cache: all checks passed")
