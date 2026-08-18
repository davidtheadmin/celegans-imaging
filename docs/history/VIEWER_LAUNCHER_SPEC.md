> **HISTORICAL — archived 2026-08-18.** A pre-build implementation spec. The
> feature shipped, including the parts marked optional (settings persistence);
> only the stdout-streaming progress nicety was skipped, and that is still an
> open backlog item. Written in the imperative future tense, so read it as an
> intent document. One factual error worth noting: it states Pillow is already in
> `launcher/requirements.txt`. It is not — it arrives only transitively via
> matplotlib and scikit-image, despite being imported directly by `widgets.py`.

# Spec: "Review" viewers in the WormScan launcher

Wire two existing, tested generator scripts into the launcher as a single new
top-level feature. This is a focused, additive change — no edits to capture,
sync, or analysis pipelines.

## What already exists (do not rewrite these)

Two standalone generators, validated and ready. Put them in the repo at:

```
launcher/viewers/make_image_viewer.py    # still images  -> strain x dose grid, loupe + pin/compare, multi-day
launcher/viewers/make_video_viewer.py    # crawling videos -> strain x dose grid of looping clips, video loupe, multi-day
```

Both share the same CLI contract:

```
python make_image_viewer.py  FOLDER [FOLDER ...] [--out PATH]
python make_video_viewer.py  FOLDER [FOLDER ...] [--out PATH] [--target-seconds 3.0]
```

Behavior, identical across both:
- ONE folder  -> single strain x dose grid, no day arrows.
- MANY folders -> same grid per day, with arrows + left/right keys; days ordered
  by the leading `YYMMDD` date prefix in the folder name.
- Each folder holds `<strain> <dose>J` condition subfolders; inside each, the
  HIGHEST-numbered `plateNN` subfolder's file is used (falls back to a file
  directly in the condition folder). Subfolders starting with `_` (e.g.
  `_crawling_analysis_*`) are ignored.
- Image viewer caches 480px thumbnails in `<folder>/.viewer_cache/`.
- Video viewer pre-transcodes each video into a short fast loop clip in
  `<folder>/.viewer_cache/` (this is the slow step; cached by source mtime).
- Output HTML is written to the parent of the first folder, named
  `<firstfolder>_viewer.html` (image) or `<firstfolder>_video_viewer.html`
  (video), or `..._<N>days_...` when multiple. `--out` overrides.
- Exit code 0 on success; non-zero with a message on no-match / bad dir.

Both depend only on the stdlib + Pillow (image) / ffmpeg+ffprobe (video).
Pillow and imageio-ffmpeg are already in `launcher/requirements.txt`; ffmpeg is
already required by the analysis pipeline (`ffmpeg_utils.run_preflight`).

## The new launcher feature

Add ONE new top-level action button to `MainWindow`'s `btn_frame` in
`launcher/ui.py` (alongside "Open Imaging UI", "Open Analysis", etc.):

```
text = "Review (Grid Viewer)"
command = self._open_review
```

`self._open_review` opens a new modal dialog, `ReviewDialog` (model it on the
existing `SettingsDialog` / `AnalysisDialog` structure in `ui.py`).

### ReviewDialog requirements

1. **Folder selection (one or many).**
   tkinter's `filedialog.askdirectory` only returns ONE folder, which does not
   cover the multi-day case. Implement an add/remove list instead:
   - A Listbox showing the chosen folders (top to bottom).
   - An "Add folder…" button -> `filedialog.askdirectory(initialdir=mirror_root)`
     appends the chosen path. The user clicks it once per day folder.
   - A "Remove selected" button removes the highlighted entry.
   - Default `initialdir` is `settings.mirror_root` (the synced mirror; that's
     where experiment/day folders live).
   - Require at least one folder before enabling Start.

2. **Type selection: Pictures vs Videos.**
   A radio pair: `( ) Pictures   ( ) Videos`, plus a third `(•) Auto-detect`
   default. Auto-detect rule: scan the FIRST selected folder's condition
   subfolders; if more `*.mp4/.avi/.mov/.mkv/.m4v` files are found than
   `*.jpg/.jpeg/.png/.tif/.tiff/.bmp/.webp`, pick Videos, else Pictures. Show
   the resolved choice next to the radio (e.g. "auto -> videos") so the user can
   override before starting. If auto-detect finds neither, show an inline error
   and keep Start disabled.

3. **Video-only option.**
   When Videos is the (resolved) type, show a small spinbox
   "Loop length (s)" (range 1.0-10.0, default 3.0) wired to `--target-seconds`.
   Hide it for Pictures (it's inert there), mirroring how `AnalysisDialog` hides
   the motility-only spinbox for crawling.

4. **Start.**
   - Resolve script path: `Path(__file__).parent / "viewers" / ("make_video_viewer.py" if video else "make_image_viewer.py")`.
   - Build argv: `[sys.executable, str(script), *folder_paths]` and, for video,
     append `["--target-seconds", str(loop_s)]`. Do NOT pass `--out`; the
     default naming is what we want.
   - Run via `subprocess.Popen` on a short-lived worker thread (the video
     transcode can take minutes on a full experiment — do NOT block the UI
     thread). Pass `creationflags=CREATE_NO_WINDOW` on Windows (the analysis
     code already defines this pattern; reuse it).
   - Show a small modeless progress window (reuse `AnalysisProgressDialog`'s
     look if cheap, or a simple "Building viewer…" label with an indeterminate
     bar). Stream the child's stdout into a small log box if easy; otherwise
     just spin until exit.
   - On exit code 0: parse the written HTML path from the script's final
     `Wrote <path>` stdout line (or recompute it with the same naming rule), then
     `webbrowser.open_new_tab(Path(html).as_uri())` — note `.as_uri()` gives the
     correct `file://` URL with proper escaping. Close the progress window.
   - On non-zero exit: show `messagebox.showerror` with the last ~1000 chars of
     stderr (same truncation the Tierpsy runner uses).

5. **Threading contract.**
   Follow the launcher's existing rule: worker thread does the subprocess and
   writes status to a thread-safe object; UI thread polls via `root.after(...)`
   and touches widgets only on the main thread. Do not call `webbrowser` or
   `messagebox` from the worker thread — hand the result back to the UI thread
   to act on. (Same model as `SyncAgent`/`MotilityAgent`.)

### Settings persistence (optional, nice-to-have)
Persist the last-used review type and loop length on `Settings`
(`review_type: str = "auto"`, `review_loop_s: float = 3.0`) so the dialog
restores them. If you add fields, remember `Settings.load()` filters unknown
keys, and update `_on_settings_saved` only if the dialog reads live settings
(it doesn't need to — it's modal and reads at open time).

## Acceptance checks
- Picking one folder of survival stills -> Pictures grid opens, no day arrows.
- Picking three day folders of stills -> multi-day grid, arrows + L/R keys,
  pins clear on day switch.
- Picking crawling-video day folders -> looping-clip grid; first run transcodes
  (slow), re-run is fast (cache hit).
- Auto-detect resolves correctly for a folder that is clearly stills vs clearly
  videos, and the resolved label is shown before Start.
- UI never freezes during a video build; closing the progress dialog doesn't
  orphan the child (let it finish or terminate it on cancel — your call, but be
  explicit).
- Conditions whose folder name doesn't match `<strain> <dose>J` are silently
  skipped (already handled by the generators).

## Out of scope
No changes to capture, sync, retention, or the motility/crawling analysis
pipelines. No new network calls. The viewers operate purely on local folders in
the synced mirror.
