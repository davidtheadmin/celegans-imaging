# Development-mode verification harness

Five scripts that prove the Development pipeline still does what it claims,
without a GPU, the vision venv, or a single real plate image. Run them in this
order after touching anything in `launcher/survival*.py`:

```bash
cd dev/development_tests
python harness.py         # synthesise a run -> workbook, 4 PNGs, explorer
python verify.py          # recalculate the workbook, cross-check every cell
python check_explorer.py  # headless render, both colour schemes, all controls
python test_units.py      # timepoints, stage index, the rescore switch
python test_analyze.py    # analyze() end to end with inference stubbed
python test_cache.py      # the detection cache: reuse, invalidation, relabelling
python test_agent.py      # the UI <-> agent seam, and failure reporting
```

Everything they write goes in `_scratch/` and `_scratch_cache/` (gitignored).
Nothing touches real data.

## What each one is actually guarding

**harness.py** builds a fake two-timepoint, three-strain, three-dose run and
pushes it through the real `aggregate` / `survival_excel` / `survival_figures` /
`survival_explorer` code. It deliberately includes the awkward cases: a
condition with a single plate (so the replication unit falls back to quadrant
images), a condition missing at one timepoint (a gap), an image that errored,
and an image with no dose+plate token. If you change the aggregation, look at
the PNGs it writes — they are the fastest way to see that something moved.

**verify.py** is the important one. It recalculates the workbook with
LibreOffice and then compares **every computed cell** against the value Python
already had. A clean recalc only proves the formulas *evaluate*; an off-by-one
row range recalculates perfectly and reports the wrong number. During this
build the qc sheet was dividing a plate count by a quadrant count — 4x out, no
error anywhere — and this script is the only thing that noticed. Do not delete
it, and do not accept "recalc was clean" as verification on its own.

**check_explorer.py** renders `explorer.html` headless in light and dark,
clicks every control in every panel, hovers a mark, and fails on any console
message. It also asserts that curves carry a literal `stroke` colour: chart code
that reads colours from CSS custom properties at draw time gets `''` in a
sandboxed frame, which becomes `fill: black` (bars survive, looking ugly) and
`stroke: none` (curves vanish, looking like missing data). That is why the
template hard-codes its palette in JS. Keep it that way.

**test_units.py** covers timepoint resolution (typed, derived, mixed, refused),
quadrant and capture-stamp parsing, the stage-index mapping, and the rescoring
switch — including a check that no alpha *value* is hardcoded anywhere in the
launcher. `vision/stage_conf.json` is the single source of truth for alpha; the
checkbox only chooses between "that file's value" and "0".

**test_analyze.py** runs the whole orchestrator with `run_inference` stubbed:
two folders, per-folder soft CSVs merged into one, exactly four PNGs and no
survival curve, and a workbook that recalculates clean.

**test_cache.py** protects the reuse path, which is the one place where being
wrong is silent and expensive — serving stale detections looks exactly like
serving fresh ones. It asserts that a second run over the same folder calls the
model ZERO times and gets identical counts; that analysing two timepoints
separately and then combining them does no inference at all; that adding one
plate analyses one plate; that editing a single image re-analyses only that
image; that moving a confidence floor throws the whole cache away; and that
flipping the class-confidence correction is recomputed from the saved score
vectors rather than re-analysed — checked by comparing it against a forced
fresh run at the same alpha, which must agree exactly.

**test_agent.py** guards the seam between the dialog and the background thread,
and it exists because of a bug that survived four rounds of debugging. The
dialog called `start_analysis(..., force_reanalyze=...)` while the agent had no
such parameter, so every Development run raised `TypeError` inside a Tk
callback — and `pythonw.exe` sends stderr nowhere, so nothing was printed
anywhere. The visible symptom was a window flashing up empty and no completion
message; the cause was invisible, and four increasingly elaborate fixes to the
*message* changed nothing because the run was never starting.

Every other script here calls `analyze()` directly, so all of them passed
throughout. This one parses `ui.py`, finds each `start_analysis` call and binds
its keywords against the real signature, then actually runs the agent thread
end to end — success, reuse, `force_reanalyze`, and a deliberately exploded run
that must still report a failure with its log. Reintroduce the missing
parameter and it fails on the first check.

Two lessons worth keeping: a test that never crosses a layer boundary cannot
find a bug that lives on one, and a background worker must report failure
through the same channel as success or the failure is indistinguishable from
doing nothing.

## Dependencies

`numpy`, `pandas`, `openpyxl`, `matplotlib` (already in
`launcher/requirements.txt`), plus `playwright` and LibreOffice for the last
two checks. `recalc.py` and `office/` are Anthropic's xlsx-skill helpers,
vendored here so `verify.py` runs without them installed.
