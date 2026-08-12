"""Offline harness: synthesise a Development run and exercise every output.

No model, no vision venv — we feed `aggregate` the same record dicts that
run_inference would have produced, then run the workbook, the figures and the
explorer over it. Also computes an INDEPENDENT set of expected numbers so the
workbook cross-check has something to disagree with.
"""
import csv, json, math, random, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# Scratch lives beside the tests, never in the repo tree that ships.
WORK = HERE / "_scratch"
sys.path.insert(0, str(REPO / "launcher"))

import survival, survival_excel, survival_explorer, survival_figures, survival_size

OUT = WORK / "run"
if OUT.exists():
    shutil.rmtree(OUT)
(OUT / "t0").mkdir(parents=True)
(OUT / "t24").mkdir(parents=True)
OUTDIR = OUT / "_development_test"
OUTDIR.mkdir()

STAGES = ["L1", "L2", "L3", "L4", "adult"]
rng = random.Random(7)

STRAINS = ["N2", "601", "604"]
DOSES = [0, 20, 60]
QUADS = ["NE", "NW", "SE", "SW"]


def counts_for(strain, dose, tp):
    """Plausible-ish shape: dose delays, time advances."""
    centre = 1.4 + tp / 14.0 - dose * (0.030 if strain == "601" else 0.012)
    centre = max(1.0, min(5.0, centre))
    n = max(6, int(rng.gauss(70 - dose * 0.45, 8)))
    out = {s: 0 for s in STAGES}
    for _ in range(n):
        v = rng.gauss(centre, 0.75)
        i = int(round(max(1, min(5, v)))) - 1
        out[STAGES[i]] += 1
    return out


records = {0.0: [], 24.0: []}
for tp, folder in ((0.0, "t0"), (24.0, "t24")):
    for strain in STRAINS:
        for dose in DOSES:
            # 604 at 60 J is missing at 24 h -> a gap
            if strain == "604" and dose == 60 and tp == 24.0:
                continue
            # N2 at 60 J has a single plate -> quadrant replication path
            plates = [1] if (strain == "N2" and dose == 60) else [1, 2]
            for p in plates:
                for qi, q in enumerate(QUADS):
                    name = f"{strain}_Trial_{dose}J_p{p:02d}_{qi:04d}_{q}.png"
                    path = OUT / folder / name
                    path.write_bytes(b"")
                    records[tp].append(
                        {"path": str(path), "counts": counts_for(strain, dose, tp),
                         "w": 4056, "h": 3040})

# one errored image and one that carries no dose+plate token
err = OUT / "t0" / "601_Trial_20J_p01_9999_NE.png"
err.write_bytes(b"")
records[0.0].append({"path": str(err), "error": "decode failed"})
odd = OUT / "t24" / "stray_image_NE.png"
odd.write_bytes(b"")
records[24.0].append({"path": str(odd), "counts": {"L1": 3, "adult": 1},
                      "w": 4056, "h": 3040})

plans = [
    survival.FolderPlan(folder=OUT / "t0", hours=0.0, method="typed",
                        detail="typed by the user (0 h)", n_images=len(records[0.0])),
    survival.FolderPlan(folder=OUT / "t24", hours=24.0, method="typed",
                        detail="typed by the user (24 h)", n_images=len(records[24.0])),
]
folder_runs = [
    {"plan": plans[0], "records": records[0.0], "mode": "filename",
     "encoded_fraction": 0.99},
    {"plan": plans[1], "records": records[24.0], "mode": "filename",
     "encoded_fraction": 0.99},
]

cats, unmapped = survival.build_stage_categories(STAGES, survival.SURVIVAL_CONFIG)
agg = survival.aggregate(STAGES, folder_runs, cats)

# ---- soft scores CSV, sized so each stage sits where it should -------------
SIZE_MU = {"L1": 3.55, "L2": 3.76, "L3": 3.95, "L4": 4.20, "adult": 4.55}
soft = OUTDIR / survival._SOFT_SCORES_NAME
with open(soft, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["folder", "timepoint_h", "image", "det_index", "x1", "y1", "x2",
                "y2", "w_px", "h_px", "size_px", "hard_call", "hard_call_raw",
                "hard_score", "match_iou", "entropy"]
               + [f"raw_{s}" for s in STAGES] + [f"p_{s}" for s in STAGES])
    for run in folder_runs:
        tp = run["plan"].hours
        for rec in run["records"]:
            if "error" in rec:
                continue
            nm = Path(rec["path"]).name
            i = 0
            for st, c in rec["counts"].items():
                for _ in range(c):
                    size = math.exp(rng.gauss(SIZE_MU[st], 0.11))
                    w.writerow([run["plan"].folder.name, f"{tp:g}", nm, i,
                                0, 0, size, size, size, size, round(size, 2),
                                st, st, 0.7, 1.0, 0.5]
                               + [0.1] * len(STAGES) + [0.2] * len(STAGES))
                    i += 1

msgs = []
def wl(m):
    msgs.append(m)

size = survival_size.build_size_payload(soft, agg["per_image"], wl)

meta = {
    "names": STAGES, "class_conf": {s: 0.4 for s in STAGES},
    "conf": 0.25, "overlap": 0.35,
    "seam": {"margin_px": 12, "cover_frac": 0.6},
    "class_agnostic_iou": 0.7,
    "class_size_px": {"L1": [21, 56]},
    "rescore": {"alpha": 2.0, "refs": {"L1": 0.8075, "L2": 0.1418,
                                       "L3": 0.7944, "L4": 0.7199,
                                       "adult": 0.86}},
    "exclude_classes": ["egg"],
}
summary = {"mode": "filename", "encoded_fraction": 0.99,
           "n_conditions": agg["n_conditions"], "n_plates": agg["n_plates"],
           "n_images": agg["n_images_ok"] + 1, "n_unparsed": agg["n_unparsed"]}

survival_excel.index_map = dict(survival.STAGE_INDEX)
xlsx = OUTDIR / survival._RESULTS_NAME
expected = survival_excel.write_workbook(
    xlsx, agg, stage_names=STAGES, cats=cats, unmapped=unmapped, meta=meta,
    plans=plans, summary=summary, size=size, write_log=wl)

pngs = survival_figures.write_figures(OUTDIR, agg, size, wl)
survival_explorer.write_explorer(OUTDIR / "explorer.html", agg, size, meta,
                                 plans, summary, wl)
survival._console_summary(agg, wl)

import pickle
pickle.dump(expected, open(WORK / "expected.pkl", "wb"))
print("\n".join(msgs))
print("\nRESULT", len(agg["per_image"]), "images,", len(agg["per_plate"]),
      "plates,", len(agg["per_condition"]), "cond-cells,",
      len(agg["gaps"]), "gaps,", agg["n_error"], "errors,",
      agg["n_unparsed"], "unparsed")
print("expected cells:", len(expected))
