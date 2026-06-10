# Polish backlog

Small items deferred from main work. Pick up when convenient.

## Storage (done 2026-05-28)

- `.trash` no longer leaks: reclamation deletes acked files directly (not move-to-trash), the recycle bin auto-purges after `CELEGANS_RETENTION_TRASH_MAX_AGE_DAYS`, and capture refuses on a full card with HTTP 507.

## UI

- **Soft-deleted thumbnails don't vanish from timeline.** When a file is
  deleted in the timeline, the thumbnail is correctly marked "deleted" but
  remains visible. Should fade out / be removed from the strip after delete.

- **Motility assay nomenclature.** Subgroups within a motility session are
  currently labeled "plates" (inherited from the survival assay schema).
  Should be "videos" or "captures" — plates make no sense for motility.
  Check both the frontend labels and any session.json schema fields that
  surface to the user.

## Launcher mirror folder structure

Current mirror layout uses Pi-internal names that are fine for code but
unfriendly when browsing in Explorer:
Documents\WormScan
├── freecapture
└── sessions<session_id_timestamp>\plates<folder_name>\

Desired layout, mapped at the launcher (Pi stays as-is):
Documents\WormScan
├── Free captures
└── <user-given session name>
├── WT 0J
├── WT 10J
└── ...

Means:
- Rename `freecapture` → `Free captures` (or similar) at mirror time.
- Rename `sessions` top-level away (move user sessions to top level).
- Use the user-given session name (from session.json) instead of the
  generated session_id in the path.
- Use the condition name as the second-level folder (drop the
  intermediate `plates/` segment).

Note: the launcher's recovery logic relies on Pi-relative paths matching
the mirror structure. Renaming requires a stable mapping table held in
the launcher, OR the launcher writes a `.wormscan-meta` marker per
folder recording the original Pi path. Decide which when we pick this up.

## Launcher mirror: rename-orphan limitation

When an experiment is renamed in the browser, the existing mirror folder
is **not** moved. New files captured after the rename appear in a new
folder named after the new experiment name; files already mirrored stay
in the old folder. This is accepted as a minor papercut — the old folder
is harmless and can be deleted manually.

The root cause: the launcher has no stable mapping from session_id to
previous friendly folder names. A future fix would write a
`.wormscan-meta` marker per folder recording the original session_id,
allowing the launcher to detect renames and move the folder.

- On rename, offer to clean orphaned mirror folders from the launcher side (currently they stay).

## UI: delete sessions and conditions, not just plates

Currently the timeline only supports deleting individual plates (and free
captures). Need bulk-delete operations:

- Delete entire session: removes all plates + session.json. Soft-delete
  to .trash/sessions/<sid>/ for recoverability.
- Delete a condition within a session: removes all plates assigned to
  that condition, but leaves the rest of the session intact.

Both need confirmation dialogs ("Delete session 'UV survival run 3' and
all 60 plates?"). Soft-delete semantics should match the existing
per-plate delete so retention can clean up later.

- [ ] Production motility plots (`make_video_summary_png`, `make_per_worm_trace_png`) draw straight lines across skeleton-failure gaps, which can be misleading. Insert NaN values into time/angle arrays at gap boundaries so matplotlib renders real visual gaps. Discovered during 2026-05-05 calibration revisit; was responsible for the apparent "step pattern" on worm 4 that turned out to be ~50% missing skeleton frames.
- [ ] Add `signal_coverage_pct` distinct from trajectory `coverage_pct` to motility CSV outputs. Current `coverage_pct` measures trajectory continuity (frames Tierpsy tracked the worm) but not head-angle signal validity (frames where skeleton was finite). Worm 4 reports 100% trajectory coverage but only ~48% signal coverage. Filter on `signal_coverage_pct >= X` would be more meaningful for analysis quality.

## Review (grid viewer)

- **Stream per-clip progress in the Review build dialog.** The build currently
  shows a single indeterminate "Building viewer…" spinner. The video generator
  prints one line per condition as it transcodes (the slow step); piping the
  child's stdout into the dialog and showing "clip N of M" would give real
  feedback on long first-run builds. Deferred from the initial Review feature.

## Crawling & analysis

- **Crawling under-count — Tierpsy segmentation fragmentation.** Worms visible
  all 180s are tracked by Tierpsy in only ~16–60s pieces, even at gap25/dist30.
  Diagnosed (this session) as Tierpsy-level: detection sees ~8 blobs/frame but
  the linker breaks IDs on brief dropouts; post-processing levers
  (`traj_max_frames_gap`, grouping distance, run-gap-bridge) are exhausted. The
  engine drops almost nothing (5 too_short, 0 debris/flicker). The 30s gate is
  the current pragmatic baseline (7 kept on 601 0J day-0). Deferred work: sweep
  segmentation params (`mask_min_area`, `thresh_C`, `thresh_block_size`,
  `worm_bw_thresh_factor`) on one video to hold worms tracked continuously — the
  only root-cause lever left. Note: raising `traj_max_allowed_dist` to 175
  REGRESSED (119 fragments) — looser link distance worsens associations, don't
  retry that. Smaller worms in newer crawling videos may be near the
  `mask_min_area=500` floor — prime sweep candidate.

- **Analysis cache doesn't invalidate on param change.** `_wormscan_cache` hits
  on the existence of `Results/<stem>_featuresN.hdf5`, ignoring whether the
  cached result was produced with the current params. Changing
  `crawling_params`/`motility_params` and re-running silently serves the stale
  result. Bit us this session (dist=175 output cached under dist=30 params).
  Fix: include a hash of the effective Tierpsy params in the cache key (or write
  a params fingerprint next to the cache and invalidate on mismatch).

- **Validate the 30s crawling gate across UV doses.** The 30s min-track gate was
  validated on ONE video (601 0J, day-0). Higher-dose worms (10J) move less /
  may fragment differently. When running the full batch, confirm 30s stays
  sensible per-dose; may warrant per-condition review. The gate re-applies at
  aggregation (no re-run needed to re-tune).

- **Docker Desktop CPU allocation limits parallel speedup.** The parallel
  pipelines autosize workers from `docker info` NCPU/MemTotal. Docker Desktop's
  Linux VM caps CPUs below the host (capped at 4 on the dev box → auto picks 2 →
  1.76×). Before the full UV batch on the 8C/16GB analysis machine, raise Docker
  Desktop → Settings → Resources CPU/RAM so auto scales to ~4 workers. Settings
  change, not code.
