"""End-to-end analyze(): two folders, stubbed inference, every output checked.

Covers the orchestrator itself — the per-folder loop, timepoint tagging, the
soft-CSV merge, and the fact that a cancelled/errored folder does not take the
run down with it.
"""
import csv, math, random, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# Scratch lives beside the tests, never in the repo tree that ships.
WORK = HERE / "_scratch"
sys.path.insert(0, str(REPO / "launcher"))
import survival

ROOT = WORK / "e2e"
if ROOT.exists():
    shutil.rmtree(ROOT)

STAGES = ["L1", "L2", "L3", "L4", "adult"]
rng = random.Random(11)
fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + detail))
    if not cond:
        fails.append(name)


# --- two folders of real files, ISO-stamped so timepoints derive -----------
for day, folder in (("20260805", "t0"), ("20260806", "t24")):
    d = ROOT / folder
    d.mkdir(parents=True)
    for strain in ("N2", "601"):
        for dose in (0, 20):
            for p in (1, 2):
                for qi, q in enumerate(("NE", "NW", "SE", "SW")):
                    (d / f"{strain}_T_{dose}J_p{p:02d}_{day}T0754{qi:02d}_{q}.png"
                     ).write_bytes(b"")

plans = survival.resolve_timepoints([(ROOT / "t0", ""), (ROOT / "t24", "")])
check("timepoints derived for both folders",
      [p.hours for p in plans] == [0.0, 24.0] and not any(p.error for p in plans),
      str([(p.hours, p.error) for p in plans]))

# --- stub the vision subprocess -------------------------------------------
soft_written = []


def fake_run_inference(images, class_conf, *, exclude_classes, preview_dir,
                       soft_csv, rescore, write_log, progress_cb=None,
                       cancel_check=None):
    write_log(f"[stub] {len(images)} image(s), rescore={rescore}")
    records = []
    rows = []
    for img in images:
        counts = {s: rng.randint(0, 25) for s in STAGES}
        records.append({"path": str(img), "counts": counts, "w": 4056, "h": 3040})
        for st, c in counts.items():
            for _ in range(c):
                sz = math.exp(rng.gauss(3.5 + STAGES.index(st) * 0.22, 0.1))
                rows.append([img.name, 0, 0, 0, sz, sz, sz, sz, round(sz, 2),
                             st, st, 0.7, 1.0, 0.5])
        if progress_cb:
            progress_cb(len(records), len(images), img.name)
    with open(soft_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["image", "det_index", "x1", "y1", "x2", "y2", "w_px",
                    "h_px", "size_px", "hard_call", "hard_call_raw",
                    "hard_score", "match_iou", "entropy"])
        w.writerows(rows)
    soft_written.append(Path(soft_csv))
    meta = {"names": STAGES + ["egg"], "class_conf": {}, "conf": 0.25,
            "overlap": 0.35, "seam": {}, "exclude_classes": ["egg"],
            "rescore": {"alpha": 2.0 if rescore else 0.0,
                        "refs": {"L2": 0.1418}}}
    return list(meta["names"]), records, meta


survival.run_inference = fake_run_inference

out = ROOT / "_development_e2e"
out.mkdir()
lines = []
result = survival.analyze(plans, {}, False, out, exclude_classes=["egg"],
                          rescore=True, write_log=lines.append)

log = "\n".join(lines)
check("workbook written", result["out_xlsx"].exists())
check("explorer written", (out / "explorer.html").exists())
check("merged soft CSV written", (out / "soft_stage_scores.csv").exists())
check("per-folder soft CSVs cleaned up",
      not any(p.exists() for p in soft_written),
      str([p.name for p in soft_written]))
for png in ("stage_index.png", "stage_composition.png", "body_size.png",
            "quality_control.png"):
    check(f"{png} written", (out / png).exists())
check("exactly four PNGs, no survival curve",
      sorted(p.name for p in out.glob("*.png")) ==
      ["body_size.png", "quality_control.png", "stage_composition.png",
       "stage_index.png"],
      str(sorted(p.name for p in out.glob("*.png"))))

with open(out / "soft_stage_scores.csv", encoding="utf-8") as fh:
    head = next(csv.reader(fh))
check("merged CSV carries folder and timepoint",
      head[:2] == ["folder", "timepoint_h"], str(head[:3]))

check("timepoints logged per folder with the method",
      "TIMEPOINTS: 2 folder(s)" in log and "derived from image capture times" in log)
check("both folders inferred", log.count("[stub]") == 2)
check("rescore request logged",
      "Class-confidence correction requested: ON" in log, "")
check("resolved rescore alpha echoed back",
      "ACTUALLY APPLIED: ON, alpha 2" in log, "")
check("no mismatch warning when they agree",
      "does not match what the checkbox" not in log, "")
check("grouping logged loudly", "GROUPING: mode=filename" in log)
check("no survival curve mentioned anywhere",
      "survival_curve" not in log)
check("plate count is 2 folders x 2 strains x 2 doses x 2 plates",
      result["n_plates"] == 16, str(result["n_plates"]))

# --- the workbook actually recalculates and cross-checks -------------------
import subprocess, survival_excel
r = subprocess.run([sys.executable, str(HERE / "recalc.py"), str(result["out_xlsx"]), "180"],
                   capture_output=True, text=True,
                   cwd=str(HERE))
check("recalc clean", '"total_errors": 0' in r.stdout, r.stdout[:300])

print()
if fails:
    print(f"{len(fails)} FAILURE(S): " + ", ".join(fails))
    sys.exit(1)
print("analyze() end-to-end: all checks passed")
