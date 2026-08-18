> **HISTORICAL — archived 2026-08-18.** A reproducibility audit taken 2026-07-09
> at commit `c7045bd`. Four of its seven "[blocks paper]" findings are now
> closed: `LICENSE` exists (AGPL-3.0), launcher dependencies are exact-pinned,
> automated tests exist (`dev/development_tests/`), and the README's camera and
> bend-method claims were corrected. Still open: the Tierpsy image is pinned to
> `:latest`, and analysis outputs remain in pixels. Its file lists in §3 and §4
> point at paths that no longer exist (`launcher/tools/` moved to `dev/tools/`).
> Superseded by the 2026-08-18 audit.

# AUDIT.md

Read-only audit generated 2026-07-09 from live `HEAD` (`c7045bd`). No source,
config, or data file was modified. Findings are tagged **[blocks paper]**
(would stop another lab reproducing or publishing results), **[cleanup]**
(hygiene, safe to defer), or **[defer]** (intentional / low-value / out of scope).

Working tree is clean. One unpushed commit (ahead of `origin/main` by 1). One
stash (`stash@{0}: polish files in progress` — doc phase-roadmap relabel +
`app.js` button-lockout/SELECT-hotkey polish; not evaluated as live code).

---

## 1. TODO / FIXME / XXX / HACK comments

**No `TODO`, `FIXME`, `XXX`, or `HACK` markers exist anywhere in the tracked
source.** A grep for those tokens returns only HTML `placeholder=` input
attributes and documentation "placeholder" notes (screenshots, icon), none of
which are code debt:

- `CLAUDE.md:48` — "placeholder app icon (replace later)" **[cleanup]**
- `launcher/INSTALL.md:16,36,48,61,78` — "[screenshot placeholder …]" (5×) **[defer]** (end-user doc, needs screenshots before distribution)
- `launcher/INSTALL.md:121` — "**Contact**: [David's email / Slack — placeholder]" **[cleanup]**
- `launcher/INSTALL.md:127` — "The current icon is a placeholder." **[defer]**
- `motility_analysis_spec.md:18` — "Counting (**placeholder** for …)" — stale; Counting is now built **[cleanup]**
- Various `capture/app/static/*.{html,js,css}` — `placeholder=` form attributes, not debt **[defer]**

---

## 2. Stubs, dead code, commented-out blocks

- `launcher/analysis/analysis_csv.py` — **dead function-signature defaults**: `head_angle_prominence=0.30` throughout (`compute_head_angle_signal`, `read_fragments`, `compute_crawling_metrics`, plot/render helpers). The real value (0.50) always flows from the JSON; the 0.30 defaults are never the operative value and mislead readers. **[cleanup]**
- `launcher/analysis/motility.py` / `analysis_csv.py` — **`is_full_track` is computed but never read and never exported** (dead field). The per-worm-trace render loop variable is named `full_track_rows` but filters on `is_long`. **[cleanup]**
- `launcher/analysis/analysis_csv.py` — **stale/contradictory comment**: block headed "Bend counter — UNCHANGED from v1 … Do not modify" while every row is stamped `bend_method = "head_angle_peaks_v2"`. Comment lies about what the code does. **[cleanup]**
- `launcher/analysis/crawling.py` — **stale module docstring** calling crawling "a near-exact copy … same Tierpsy parameters … same output format." False on all three counts. **[cleanup]**
- `launcher/analysis/crawling_metrics.py` — **provisional dual reversal metrics**: `ARROW_COLS` (velocity-arrow reversal/turn) ship *alongside* the `motion_mode`-based `reversal_count`, per an in-code comment, "for at least one analysis cycle of side-by-side comparison." One is expected to be retired. Not dead yet, but flagged transitional. **[defer]**
- `launcher/_widget_gallery.py` — **dev-only harness** (renders the CTk widget catalogue); not imported by the app, hardcoded sample paths. Effectively dead in production. **[cleanup]**
- No commented-out logic blocks of note were found in the analysis or capture code.

---

## 3. Orphaned / unused files (tracked but not imported by the app)

- `check_skel_flag.py`, `check_skeletons.py`, `inspect_filter_decisions.py`, `inspect_filter_decisions2.py`, `inspect_filter_decisions3.py` (repo root) — one-off debug scripts, hardcoded `C:\Users\Isabe\Desktop\Tierpsyclips\…` paths, not imported anywhere. **[cleanup]**
- `launcher/tools/` — all eight are ad-hoc diagnostics, none imported by the app: `tierpsy_param_sweep.py`, `inspect_skeleton_failures.py`, `inspect_head_angle_spectrum.py`, `compute_shape_metrics.py`, `contrast_analysis.py`, `cut_clip.py`, `worm_stage_preview.py`, plus data artifact `contrast.csv`. **[cleanup]**
- `launcher/bend_calibration.py` — near-duplicate of `docs/calibration/bend_calibration.py`; the launcher copy carries hardcoded manual-count paths. Two tracked copies of the same calibration script. **[cleanup]**
- `launcher/_widget_gallery.py` — see §2. **[cleanup]**

None of these are wired into `main.py` / the agents, so removing or relocating
them (e.g. to a `dev/` or `scratch/` dir) would not affect runtime. Left in place
per the "do not change anything" scope.

---

## 4. Hardcoded absolute paths (file:line)

**In non-doc code (the ones that matter for portability):**

- `capture/app/config.py:8` — `DATA_ROOT = "/home/pi/celegans-data"` (a *default*; overridable via `CELEGANS_DATA_ROOT`). **[defer]** (intended default)
- `launcher/widgets.py:385` — `_SEGOE_FLUENT = r"C:\Windows\Fonts\SegoeIcons.ttf"` **[defer]** (Windows-only app; has fallback handling)
- `launcher/widgets.py:386` — `_SEGOE_MDL2 = r"C:\Windows\Fonts\segmdl2.ttf"` **[defer]** (same)
- `check_skel_flag.py:5` — `C:\Users\Isabe\Desktop\Tierpsyclips\…\sick_worm_clip_featuresN.hdf5` **[cleanup]**
- `check_skeletons.py:4` — same path **[cleanup]**
- `inspect_filter_decisions.py:5` — same path **[cleanup]**
- `inspect_filter_decisions2.py:5` — same path **[cleanup]**
- `inspect_filter_decisions3.py:5` — same path **[cleanup]**
- `launcher/tools/compute_shape_metrics.py:13` — `EXPERIMENT_ROOT = Path(r"C:\Users\Isabe\Documents\WormScan\experiments\260521_Motility")` **[cleanup]**
- `launcher/tools/inspect_head_angle_spectrum.py:9,40,44` — `C:\Users\Isabe\Documents\WormScan\test` (docstring + `OUT_DIR` + `_CACHE`) **[cleanup]**
- `launcher/tools/worm_stage_preview.py:37` — `INPUT_DIR = r"C:\Users\Isabe\Documents\WormScan\Counting_test_images"` **[cleanup]**
- `launcher/bend_calibration.py:17–24` — eight `C:\Users\Isabe\Documents\WormScan\experiments\calibration test\…` featuresN.hdf5 paths (manual-count regression fixtures) **[cleanup]**
- `launcher/_widget_gallery.py:146–148` — three `C:\Users\Isabe\Documents\WormScan\…` sample paths **[cleanup]**

**In scripts / config (intended, env-overridable):**

- `scripts/deploy.sh:16` — `/home/pi/celegans-imaging/deploy/*.service|*.timer` **[defer]**
- `scripts/move_videos_out_of_pictures.sh:11`, `scripts/rename_data_folders.sh:14`, `scripts/wipe_data.sh:12` — `DATA_ROOT="${CELEGANS_DATA_ROOT:-/home/pi/celegans-data}"` **[defer]** (env-overridable default)
- `CLAUDE.md:18` and various docs — `/home/pi/` references **[defer]** (documentation)

---

## 5. Reproducibility gaps

**README present?** Yes (`README.md`, tracked). **Current?** Partially — it
repeats two `CLAUDE.md` errors that will actively mislead a new lab:

- **[blocks paper]** `README.md` states the camera is a "Raspberry Pi Camera Module 3 (IMX708)". The hardware is an **IMX477 HQ Camera** (4056×3040). A reproducing lab that buys the wrong sensor gets a different FoV / pixel scale.
- **[blocks paper]** `README.md` §"Motility analysis pipeline" step 4 says BPM comes "from midbody curvature zero-crossings." The live algorithm counts **head-swing-angle peaks** (skeleton points 0/5 vs 20/30, prominence 0.50 rad). The methods description is wrong for any paper drawing on it.
- **[cleanup]** `README.md` on-disk layout still shows `sessions/` + `freecapture/`; real names are `experiments/`/`pictures/`/`videos/`.
- **[cleanup]** `README.md` phase table lists analysis/counting as "Next"; they are built.

**Dependencies pinned?**

- **[blocks paper]** `launcher/requirements.txt` uses `>=` lower bounds only (e.g. `scipy>=1.11.0`, `scikit-image>=0.24.0`, `opencv-python>=4.8.0`, `customtkinter>=5.2.0`). No upper bounds / no lockfile — a fresh `pip install` months later can pull incompatible majors and silently change numeric output (peak detection, watershed, skeleton features).
- **[blocks paper]** The Tierpsy Docker image is pinned to **`tierpsy/tierpsy-tracker:latest`** (`config.py` default `tierpsy_image_tag = "latest"`). The single most output-determining dependency is unversioned; results are not reproducible across Tierpsy releases. Pin to a digest or tagged version.
- **[cleanup]** `capture/requirements.txt` pins everything with `==` **except `Pillow`** (bare, unpinned).
- **[defer]** `capture/requirements.txt` omits `numpy` and `picamera2`; these come from the Pi's system site-packages (venv created `--system-site-packages`). Intended, but not obvious to an external cloner — worth a one-line note.

**Other blockers to clone-and-run elsewhere:**

- **[blocks paper]** No `LICENSE` file. Nothing grants reuse rights.
- **[blocks paper]** No automated tests anywhere in the repo (no `tests/`, no `pytest` config). Every claim about numeric correctness (bend counts, colony counts, sync integrity) rests on manual validation recorded in `STATUS.md`. `crop_wells`/`counting` are explicitly treated as validated black boxes with no regression guard.
- **[blocks paper]** `microns_per_pixel = -1.0` in both Tierpsy JSONs → motility/crawling distance & speed outputs are in **pixels / px·s⁻¹**, not physical units, despite column names that don't say "px". Counting derives µm/px from the detected well radius, so only colony sizes are physical.
- **[cleanup]** Flat-field directory mismatch (CURRENT_STATE §6.4): the service reads `DATA_ROOT/flatfield` but its own error message points users at `capture.py --capture-flat`, which writes to `<repo>/data/flatfield`. Following the instructions produces a flat the service can't find. Dormant (flat-field is opt-in).
- **[defer]** No top-level / whole-project requirements file or environment manifest; deps are split across `capture/` and `launcher/`. Fine given the two run on different machines, but there is no single "here's how to stand up the whole system" doc beyond `CLAUDE.md` (which is stale).

---

## 6. Manuscript `\todo{}` markers

**N/A — there is no manuscript in this repository.** No `.tex`, `.bib`, `.rmd`,
`.ipynb`, or `.docx` files are tracked, and a repo-wide grep for `\todo` returns
nothing. The spec/narrative docs present are `motility_analysis_spec.md`,
`VIEWER_LAUNCHER_SPEC.md`, `STATUS.md`, and `BACKLOG.md`; none is a paper draft.
If the manuscript lives in another repo, it is out of scope for this audit.

---

## 7. Triage summary

**[blocks paper] (address before external reproduction / submission):**

1. `README.md` camera identity wrong (IMX708 → IMX477).
2. `README.md` motility methods wrong (midbody-curvature → head-angle peaks).
3. Tierpsy image pinned to `:latest` — pin a version/digest.
4. `launcher/requirements.txt` unbounded `>=` deps — add upper bounds / lockfile.
5. No `LICENSE`.
6. No automated tests / regression guards (esp. counting & bend-counting).
7. Analysis outputs in pixels (`microns_per_pixel = -1.0`), not physical units.

**[cleanup] (hygiene, safe anytime):**

- Dead `0.30` prominence defaults; dead `is_full_track`; `full_track_rows` misnomer.
- Stale comments/docstrings: "Bend counter UNCHANGED/Do not modify" vs `v2` label; `crawling.py` "near-exact copy" docstring; `CLAUDE.md` camera/layout/roadmap/deps; `motility_analysis_spec.md` "Counting placeholder".
- Orphaned tracked debug scripts (root `check_*`/`inspect_filter_decisions*`, all of `launcher/tools/`, `contrast.csv`, `_widget_gallery.py`) + their hardcoded paths (§4).
- Duplicate `bend_calibration.py` (two tracked copies).
- Unpinned `Pillow` in `capture/requirements.txt`.
- Flat-field directory mismatch (§5).

**[defer] (intentional / transitional / out of scope):**

- Divergent `motility_params.json` vs `crawling_params.json` — **intentional, leave as-is** (confirmed by user).
- Provisional dual reversal metrics (`ARROW_COLS` alongside `reversal_count`) — awaiting the side-by-side comparison cycle.
- Windows-only font paths in `widgets.py`; `DATA_ROOT` default in `config.py`; env-overridable paths in `scripts/`.
- `INSTALL.md` screenshot/contact placeholders (needed before end-user distribution).
- `capture/requirements.txt` relying on system site-packages for numpy/picamera2.
