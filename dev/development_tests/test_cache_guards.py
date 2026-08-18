"""Cache guards added 2026-08-18 — the two silent-staleness holes.

Run:  python dev/development_tests/test_cache_guards.py

1. Rescore REFS must invalidate the detection cache while rescore ALPHA must
   not. Alpha is recomputable from the stored per-class vectors, so changing it
   relabels cached rows; refs are not recomputable, and stage_conf.json tells
   you to re-measure them after a retrain — so leaving them out of the digest
   meant that supported workflow silently served the previous refs' counts
   while run_info reported the new ones.

2. A run that did not finish must not be reusable. The soft CSV is
   per-detection, so once a run is over an image with no rows is
   indistinguishable from an image that was never analysed. Before this, a run
   that died at image 3 of 32 wrote a manifest claiming all 32 were analysed,
   and the next run reported 29 plates as having zero animals.

Manifests written before the completeness record existed cannot be checked, so
they are refused rather than trusted — a one-time re-analysis.
"""
import sys, json, tempfile
from pathlib import Path
sys.path.insert(0, str(Path("launcher").resolve()))
import survival_cache as sc

ok = fail = 0
def check(label, cond):
    global ok, fail
    print(("  ok   " if cond else "  FAIL ") + label)
    ok, fail = (ok + (1 if cond else 0), fail + (0 if cond else 1))

STAGE = {"class_conf": {"L2": 0.25}, "rescore": {"alpha": 2.0,
         "refs": {"L2": 0.1418, "L3": 0.7944}}, "merge": {"class_agnostic_iou": 0.7}}
model = Path(tempfile.mkstemp(suffix=".pt")[1]); model.write_bytes(b"x" * 10)

print("\n1. rescore refs now invalidate the cache; alpha still does not")
base = sc.settings_digest(STAGE, {"L2": 0.25}, ["egg"], model)
alpha_only = json.loads(json.dumps(STAGE)); alpha_only["rescore"]["alpha"] = 1.5
check("changing ONLY alpha keeps the digest (relabel, not re-run)",
      sc.settings_digest(alpha_only, {"L2": 0.25}, ["egg"], model) == base)
refs_changed = json.loads(json.dumps(STAGE)); refs_changed["rescore"]["refs"]["L2"] = 0.60
check("changing refs CHANGES the digest (was the silent-stale bug)",
      sc.settings_digest(refs_changed, {"L2": 0.25}, ["egg"], model) != base)
other = json.loads(json.dumps(STAGE)); other["merge"]["class_agnostic_iou"] = 0.55
check("changing an unrelated merge param still changes the digest",
      sc.settings_digest(other, {"L2": 0.25}, ["egg"], model) != base)

print("\n2. a run that did not finish is not reusable")
out = Path(tempfile.mkdtemp()); folder = Path(tempfile.mkdtemp())
imgs = []
for i in range(5):
    q = folder / f"N2_20J_p01_{i:04d}.png"; q.write_bytes(b"img" + bytes([i])); imgs.append(q)
logged = []
def mk(covered):
    sc.write_manifest(out, digest=base, meta={}, stage_names=["L1"], previews=False,
        folders=[{"folder": folder, "timepoint_h": 0.0, "images": imgs,
                  "errors": [], "n_rows": len(covered), "covered": covered}],
        write_log=logged.append)
    return json.loads((out / "analysis_cache.json").read_text())["folders"][0]

full = mk([p.name for p in imgs])
check("a complete run IS reusable", full["reusable"] is True)
check("and records what it covered", len(full["covered"]) == 5)
partial = mk([p.name for p in imgs[:3]])
check("a run that died at image 3 of 5 is NOT reusable", partial["reusable"] is False)
check("and the reason says so plainly", "never actually analysed" in partial["reason"])

print("\n3. a pre-existing manifest without the completeness record is refused")
man = json.loads((out / "analysis_cache.json").read_text())
man["_run_dir"] = str(out); man["folders"][0]["reusable"] = True
man["folders"][0].pop("covered")
(out / "soft_stage_scores.csv").write_text("folder,timepoint_h,image,hard_call\n")
plan = sc.plan_folder(folder, imgs, [man], base, False)
check("legacy manifest is not reused", plan.hit is False)
check("and it says why", "completeness" in (plan.reason or ""))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
