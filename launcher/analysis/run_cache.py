"""
Folder-level result reuse for the motility and crawling pipelines.

The problem this solves
-----------------------
A timecourse is analysed as several folders in one run. Re-running it after
adding one more day should not re-analyse the days that have not changed —
which is how Development already behaves (`survival_cache.py`) and what this
brings to the video assays.

Granularity is the FOLDER, not the video, and deliberately so. The expensive
step in these pipelines is Tierpsy, and that is already cached per video in
`_wormscan_cache/<stem>/` keyed by a parameter fingerprint. What this layer
saves is the rest: transcoding, metrics, renders and the per-video plots for a
folder that has already been analysed under identical settings. Anything finer
would duplicate the Tierpsy cache while adding a second thing to get wrong.

What makes a folder reusable
----------------------------
1. A previous run's manifest lists it,
2. under the same `digest` — every setting that changes the numbers: the
   Tierpsy parameters, the flat-field flag, the post-processing thresholds
   (crawling's are min_span_s, threshold_s and
   MIN_FRAGMENT_SKELETON_COVERAGE; motility's are threshold_s and the
   tuning block at the top of analysis_csv.py), and, for crawling, a hash
   of the per-worm column set, so that adding a metric cannot be reused
   away into an empty column,
3. with the same set of videos, by name, size and mtime,
4. and that run's per-worm CSV is still on disk and still contains its rows.

Fail any of those and the folder is re-analysed, with the reason recorded.
Reuse is never silent: the reason a folder was or was not reused goes in the
log and into `run_info`.

Renders are the one asymmetry. A reused folder produces no new render, so when
renders are requested the reuse of a folder that has none is refused — see
`plan_folder`. Otherwise ticking a render box would appear to do nothing.

The timepoint is NOT part of the digest. Re-running the same folders with a
different timepoint assignment must reuse the detections and simply re-stamp
them, exactly as `survival_size.write_merged_soft_csv` re-stamps Development's.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

MANIFEST_NAME = "analysis_cache.json"
ROWS_NAME = "per_worm_rows.csv"
_SCHEMA = 1


def settings_digest(pipeline: str, params_template: dict, flat_field: bool,
                    post: dict) -> str:
    """One hash over everything that changes the per-worm rows.

    `post` is the pipeline's post-Tierpsy settings (thresholds, prominence).
    The timepoint is excluded on purpose: it labels rows, it does not produce
    them.
    """
    payload = {
        "pipeline": pipeline,
        "params": {k: v for k, v in sorted((params_template or {}).items())
                   if k != "expected_fps"},
        "flat_field": bool(flat_field),
        "post": {k: post[k] for k in sorted(post or {})},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def video_fingerprints(videos) -> list[dict]:
    """[{name, size, mtime}] for a folder's videos, sorted by name."""
    out = []
    for v in videos:
        try:
            st = v.stat()
            out.append({"name": v.name, "size": int(st.st_size),
                        "mtime": round(float(st.st_mtime), 3)})
        except OSError:
            out.append({"name": v.name, "size": -1, "mtime": -1.0})
    return sorted(out, key=lambda d: d["name"])


def _same_videos(a: list[dict], b: list[dict]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(sorted(a, key=lambda d: d["name"]),
                    sorted(b, key=lambda d: d["name"])):
        if x["name"] != y["name"] or x["size"] != y["size"]:
            return False
        if abs(float(x["mtime"]) - float(y["mtime"])) > 1.0:
            return False
    return True


def write_manifest(out_dir: Path, *, pipeline: str, digest: str,
                   folders: list[dict], has_renders: bool,
                   write_log: Optional[Callable[[str], None]] = None) -> None:
    """Record this run so the next one can reuse its folders.

    `folders` is one dict per folder analysed or reused:
        {"folder": str, "timepoint_h": float|None, "videos": [...],
         "n_rows": int, "n_videos_ok": int, "n_videos_failed": int}

    A folder with any failed video is marked not reusable: the missing worms
    would otherwise be silently inherited by every future run.
    """
    entries = []
    for f in folders:
        reusable, reason = True, ""
        if int(f.get("n_videos_failed", 0)):
            reusable, reason = False, (
                f"{f['n_videos_failed']} video(s) failed in that run")
        elif not f.get("videos"):
            reusable, reason = False, "no videos recorded"
        entries.append({**f, "reusable": reusable, "reason": reason})

    doc = {"schema": _SCHEMA, "pipeline": pipeline, "digest": digest,
           "rows_csv": ROWS_NAME, "has_renders": bool(has_renders),
           "folders": entries}
    try:
        (out_dir / MANIFEST_NAME).write_text(
            json.dumps(doc, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        if write_log:
            write_log(f"Could not write {MANIFEST_NAME}: {exc}")
        log.warning("could not write manifest to %s", out_dir, exc_info=True)


def discover(folders, prefix: str,
             write_log: Optional[Callable[[str], None]] = None) -> list[dict]:
    """Every readable manifest under any folder of this run, newest first.

    A combined run writes its output under the FIRST folder and its manifest
    describes every folder in the run, so re-running the same combination finds
    one manifest covering the lot — the same trick Development uses.
    """
    found = []
    for folder in folders:
        try:
            runs = sorted(Path(folder).glob(f"{prefix}_*"))
        except OSError:
            continue
        for run in runs:
            mf = run / MANIFEST_NAME
            if not mf.exists():
                continue
            try:
                doc = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if doc.get("schema") != _SCHEMA:
                continue
            doc["_dir"] = run
            try:
                doc["_mtime"] = mf.stat().st_mtime
            except OSError:
                doc["_mtime"] = 0.0
            found.append(doc)
    found.sort(key=lambda d: d["_mtime"], reverse=True)
    if write_log and found:
        write_log(f"Found {len(found)} previous run(s) that could be reused.")
    return found


@dataclass
class FolderCache:
    folder: Path
    hit: bool = False
    rows_csv: Optional[Path] = None
    n_rows: int = 0
    reason: str = ""
    source_dir: Optional[Path] = None


def plan_folder(folder: Path, videos, manifests: list[dict], digest: str,
                *, pipeline: str, want_renders: bool,
                force: bool = False) -> FolderCache:
    """Can `folder` be reused? Returns the decision and why."""
    if force:
        return FolderCache(folder=folder, reason="re-analysis forced")
    if not videos:
        return FolderCache(folder=folder, reason="no videos found")

    fps = video_fingerprints(videos)
    key = str(Path(folder).resolve()).lower()
    for doc in manifests:
        if doc.get("pipeline") != pipeline:
            continue
        if doc.get("digest") != digest:
            continue
        for e in doc.get("folders", []):
            try:
                if str(Path(e.get("folder", "")).resolve()).lower() != key:
                    continue
            except OSError:
                continue
            if not e.get("reusable", False):
                return FolderCache(folder=folder,
                                   reason=f"previous run unusable: {e.get('reason', '')}")
            if not _same_videos(fps, e.get("videos", [])):
                return FolderCache(folder=folder,
                                   reason="the videos in this folder have changed")
            if want_renders and not doc.get("has_renders", False):
                return FolderCache(
                    folder=folder,
                    reason="renders were requested and the cached run has none")
            csv_path = Path(doc["_dir"]) / doc.get("rows_csv", ROWS_NAME)
            if not csv_path.exists():
                return FolderCache(folder=folder,
                                   reason="the cached per-worm CSV is gone")
            return FolderCache(folder=folder, hit=True, rows_csv=csv_path,
                               n_rows=int(e.get("n_rows", 0)),
                               reason=f"reused from {Path(doc['_dir']).name}",
                               source_dir=Path(doc["_dir"]))
    return FolderCache(folder=folder, reason="not analysed before with these settings")


def write_rows(path: Path, rows: list[dict], columns: list[str],
               write_log: Optional[Callable[[str], None]] = None) -> None:
    """The reusable artifact: every per-worm row, all columns, one CSV.

    Written for every run, not only multi-folder ones — a single-folder run
    today is the folder a timecourse reuses tomorrow.

    WRITTEN VIA A TEMPORARY FILE AND THEN RENAMED. Opening the destination
    with "w" truncates it first, so anything that stops the write half way —
    a cancelled run, the process going away, a full disk — leaves a 0-byte
    file exactly where hours of correct analysis used to be, and it is
    indistinguishable from a run that produced nothing. Writing beside it and
    renaming means the destination only ever holds a complete file: either the
    new one or, if the write fails, the previous one untouched.
    """
    cols = list(columns)
    for extra in ("source_folder", "timepoint_h"):
        if extra not in cols:
            cols.append(extra)
    tmp = path.with_name(path.name + ".partial")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)          # atomic on Windows and POSIX alike
    except OSError as exc:
        if write_log:
            write_log(f"Could not write {path.name}: {exc}. The previous "
                      f"{path.name}, if there was one, is untouched.")
        log.warning("could not write rows csv %s", path, exc_info=True)
        try:
            tmp.unlink()
        except OSError:
            pass


def read_rows(path: Path, folder: Path,
              numeric_hint: Optional[dict] = None) -> list[dict]:
    """Read back one folder's rows from a cached run's CSV.

    Values come back as strings, so anything that parses as a float is
    converted; empty stays None (not 0.0 — see the zero-defaults note in
    crawling_metrics, an empty cell means "not measured").
    """
    key = str(Path(folder).resolve()).lower()
    out: list[dict] = []
    try:
        with path.open("r", newline="", encoding="utf-8") as fh:
            for raw in csv.DictReader(fh):
                src = (raw.get("source_folder") or "").strip()
                try:
                    if src and str(Path(src).resolve()).lower() != key:
                        continue
                except OSError:
                    continue
                row: dict = {}
                for k, v in raw.items():
                    if v is None or v == "":
                        row[k] = None
                        continue
                    if v in ("True", "False"):
                        row[k] = (v == "True")
                        continue
                    try:
                        row[k] = float(v)
                        if row[k].is_integer() and ("." not in v) and ("e" not in v.lower()):
                            row[k] = int(row[k])
                    except ValueError:
                        row[k] = v
                out.append(row)
    except OSError:
        log.warning("could not read cached rows from %s", path, exc_info=True)
        return []
    return out


@dataclass
class ReusePlan:
    """What a run will reuse and what it will analyse. Cheap to build."""
    digest: str = ""
    caches: dict = field(default_factory=dict)      # folder -> FolderCache
    n_folders: int = 0
    n_reused: int = 0

    @property
    def all_cached(self) -> bool:
        return self.n_folders > 0 and self.n_reused == self.n_folders

    @property
    def any_cached(self) -> bool:
        return self.n_reused > 0

    def lines(self) -> list[str]:
        out = []
        for folder, c in self.caches.items():
            mark = "reuse " if c.hit else "run   "
            out.append(f"  {mark} {Path(folder).name}  ({c.reason})")
        return out


def plan_reuse(folders, videos_by_folder: dict, digest: str, *, pipeline: str,
               prefix: str, want_renders: bool, force: bool = False,
               write_log: Optional[Callable[[str], None]] = None) -> ReusePlan:
    """Decide reuse for every folder. Safe to call from the Tk thread — a
    directory glob, a few small JSON reads and one stat() per video."""
    manifests = discover(folders, prefix, write_log)
    plan = ReusePlan(digest=digest, n_folders=len(folders))
    for folder in folders:
        c = plan_folder(Path(folder), videos_by_folder.get(folder, []),
                        manifests, digest, pipeline=pipeline,
                        want_renders=want_renders, force=force)
        plan.caches[folder] = c
        if c.hit:
            plan.n_reused += 1
    return plan
