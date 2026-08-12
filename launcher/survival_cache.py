"""
Detection cache for Development runs.

The workflow this exists for: image a timepoint, analyse it, image the next one,
analyse it, and at the end run all of them together to get the figures. Without
a cache that last step re-runs the model over every image of every timepoint —
the slowest part of the pipeline, repeated for no new information.

What is cached, and why that is safe
------------------------------------
A finished run already writes ``soft_stage_scores.csv``: one row per surviving
detection, carrying its box, its size, the label the pipeline counted, the label
before rescoring, and the FULL per-class score vector. Everything downstream of
inference — counts, plate and condition aggregation, stage index, composition,
body size, the workbook, the figures, the explorer — is derivable from that.
This module adds the one thing the CSV was missing, a sidecar manifest
(``analysis_cache.json``) recording WHICH images were analysed and under WHAT
settings, so a later run can tell "0 worms on this plate" from "never looked at
this plate", and can tell whether the cached detections are still valid.

What invalidates it
-------------------
Anything that changes which BOXES exist. The per-class confidence floors and the
size gate are applied to candidate boxes BEFORE the merge, so changing either
changes the candidate set and therefore every NMS, nested and seam decision
after it. Same for the tiling and merge parameters, and for the model file. All
of that is folded into one digest; if it moves, the cache is not used and the
log says which part moved.

What does NOT invalidate it
---------------------------
The rescoring alpha. Rescoring is the LAST pass and is a pure arg-max over the
per-class vector — ``argmax(raw_c / ref_c ** alpha)`` — and that vector is in the
CSV for exactly the boxes that survived. So a different alpha is recomputed here
from the cached rows and reproduces what a fresh run would have produced,
without the model. That makes "what would alpha 1.5 have given?" a file read
rather than an afternoon.

Granularity is the image, not the folder: images whose size and mtime match the
manifest are reused, anything new or changed is inferred. Adding two plates to a
folder re-analyses two plates.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

MANIFEST_NAME = "analysis_cache.json"
_SCHEMA = 1
# Output-folder prefixes worth looking inside. Older runs wrote _survival_*
# and have no manifest at all, so they are simply never found — which is the
# correct behaviour, not a bug to work around.
_RUN_GLOB = "_development_*"


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def _stat_of(path: Path) -> dict:
    try:
        st = path.stat()
        return {"size": int(st.st_size), "mtime": round(st.st_mtime, 3)}
    except OSError:
        return {"size": -1, "mtime": -1.0}


def _strip_private(obj):
    """Drop every key starting with '_' — the _README / _comment prose.

    Editing a comment in stage_conf.json should not throw away a day of
    inference.
    """
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items()
                if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_private(v) for v in obj]
    return obj


def settings_digest(stage_conf: dict, class_conf: dict,
                    exclude_classes: Optional[list], model_path: Path) -> str:
    """One hash over everything that decides which boxes exist.

    Deliberately hashes the WHOLE of stage_conf.json (minus comments and minus
    the rescore block) rather than an enumerated list of keys. Enumerating means
    that the day someone adds a new suppression parameter, the cache silently
    keeps serving results produced without it. Hashing the file means a new
    parameter invalidates the cache by construction — the safe direction.

    The rescore block is excluded on purpose: alpha is recomputable from the
    cached score vectors, so changing it must not force a re-run.
    """
    payload = {
        "stage_conf": _strip_private(
            {k: v for k, v in (stage_conf or {}).items() if k != "rescore"}),
        "class_conf": {str(k): round(float(v), 4)
                       for k, v in sorted((class_conf or {}).items())},
        "exclude_classes": sorted(str(c).strip().lower()
                                  for c in (exclude_classes or [])),
        "model": _stat_of(model_path),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def image_fingerprints(folder: Path, images: list[Path]) -> list[dict]:
    out = []
    for p in images:
        try:
            rel = str(p.relative_to(folder))
        except ValueError:
            rel = p.name
        d = _stat_of(p)
        d["rel"] = rel
        d["name"] = p.name
        out.append(d)
    return out


def _same_file(a: dict, b: dict) -> bool:
    return (a.get("size") == b.get("size")
            and a.get("size", -1) >= 0
            and abs(float(a.get("mtime", -1)) - float(b.get("mtime", -2))) < 0.002)


# ---------------------------------------------------------------------------
# Writing the manifest
# ---------------------------------------------------------------------------

def write_manifest(out_dir: Path, *, digest: str, meta: dict,
                   stage_names: list[str], previews: bool,
                   folders: list[dict], write_log: Callable[[str], None]) -> None:
    """Record what this run analysed, so a later run can reuse it.

    ``folders`` is one dict per folder: {"folder": Path, "timepoint_h": float,
    "images": [Path], "errors": [basename], "n_rows": int}.
    """
    entries = []
    seen_keys: set[tuple] = set()
    for f in folders:
        folder = Path(f["folder"])
        fps = image_fingerprints(folder, list(f["images"]))
        names = [d["name"] for d in fps]
        reusable, reason = True, ""
        if len(set(names)) != len(names):
            # Rows in the CSV are keyed by basename. Two images sharing one
            # inside a single folder would make a cached row ambiguous, and a
            # detection attributed to the wrong plate is worse than a re-run.
            reusable = False
            reason = "two or more images share a filename inside this folder"
        key = (folder.name, f"{float(f['timepoint_h']):g}")
        if key in seen_keys:
            reusable = False
            reason = ("another folder in the same run has the same name and "
                      "timepoint, so its rows cannot be told apart")
        seen_keys.add(key)
        entries.append({
            "path": str(folder),
            "name": folder.name,
            "timepoint_h": float(f["timepoint_h"]),
            "reusable": reusable,
            "reason": reason,
            "images": fps,
            "errors": sorted(set(f.get("errors") or [])),
            "n_rows": int(f.get("n_rows") or 0),
        })

    manifest = {
        "schema": _SCHEMA,
        "written": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(out_dir),
        "soft_csv": "soft_stage_scores.csv",
        "digest": digest,
        "previews": bool(previews),
        "stage_names": list(stage_names),
        "meta": meta,
        "folders": entries,
    }
    try:
        (out_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=1), encoding="utf-8")
        write_log(f"Wrote {out_dir / MANIFEST_NAME} — a later run over these "
                  "folders can reuse these detections instead of re-analysing "
                  "the images.")
    except OSError as exc:
        write_log(f"Could not write {MANIFEST_NAME} ({exc}); this run's results "
                  "are unaffected, but a later run will have to re-analyse.")


# ---------------------------------------------------------------------------
# Finding manifests
# ---------------------------------------------------------------------------

def discover(folders: list[Path],
             write_log: Optional[Callable[[str], None]] = None) -> list[dict]:
    """Every readable manifest under any of ``folders``, newest first.

    Searches all of them, not just the folder being resolved: a combined run
    writes its output under the FIRST folder, and its manifest describes every
    folder in that run. So re-running the same combination finds one manifest
    that covers the lot.
    """
    found: list[tuple[float, dict]] = []
    for folder in folders:
        try:
            candidates = sorted(Path(folder).glob(_RUN_GLOB))
        except OSError:
            continue
        for run_dir in candidates:
            path = run_dir / MANIFEST_NAME
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
                continue
            data["_manifest_path"] = str(path)
            data["_run_dir"] = str(run_dir)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            found.append((mtime, data))
    found.sort(key=lambda t: t[0], reverse=True)
    if write_log and found:
        write_log(f"Found {len(found)} previous run(s) with reusable "
                  "detections.")
    return [d for _, d in found]


# ---------------------------------------------------------------------------
# Planning one folder
# ---------------------------------------------------------------------------

@dataclass
class FolderCache:
    """What can be reused for one folder, and what still has to be inferred."""
    folder: Path
    to_infer: list[Path] = field(default_factory=list)
    reused: list[Path] = field(default_factory=list)
    manifest: Optional[dict] = None
    entry: Optional[dict] = None
    reason: str = ""

    @property
    def n_reused(self) -> int:
        return len(self.reused)

    @property
    def hit(self) -> bool:
        return bool(self.reused)


def plan_folder(folder: Path, images: list[Path], manifests: list[dict],
                digest: str, previews: bool) -> FolderCache:
    """Decide, image by image, what can come from a cache.

    Never raises and never partially commits: if anything about a candidate
    manifest does not line up, it is skipped with a reason and the next one is
    tried, ending at "infer everything", which is always correct.
    """
    plan = FolderCache(folder=folder, to_infer=list(images))
    if not images:
        plan.reason = "no images"
        return plan
    if previews:
        plan.reason = ("preview PNGs were requested — those are drawn during "
                       "inference and cannot come from a cache")
        return plan

    target = str(Path(folder).resolve()).lower()
    reasons: list[str] = []
    for man in manifests:
        if man.get("digest") != digest:
            reasons.append(f"{Path(man['_run_dir']).name}: detection settings "
                           "or the model have changed since it ran")
            continue
        entry = None
        for e in man.get("folders", []):
            try:
                if str(Path(e.get("path", "")).resolve()).lower() == target:
                    entry = e
                    break
            except OSError:
                continue
        if entry is None:
            continue
        if not entry.get("reusable", False):
            reasons.append(f"{Path(man['_run_dir']).name}: "
                           + (entry.get("reason") or "marked not reusable"))
            continue
        csv_path = Path(man["_run_dir"]) / man.get("soft_csv",
                                                   "soft_stage_scores.csv")
        if not csv_path.is_file():
            reasons.append(f"{Path(man['_run_dir']).name}: its "
                           "soft_stage_scores.csv is missing")
            continue

        by_rel = {d["rel"]: d for d in entry.get("images", [])}
        errored = set(entry.get("errors") or [])
        reused, to_infer = [], []
        for p in images:
            try:
                rel = str(p.relative_to(folder))
            except ValueError:
                rel = p.name
            cached = by_rel.get(rel)
            # An image that errored last time is re-tried: errors are often
            # transient, they are few, and a retry that succeeds is strictly
            # better than inheriting a failure.
            if (cached is not None and p.name not in errored
                    and _same_file(cached, _stat_of(p))):
                reused.append(p)
            else:
                to_infer.append(p)
        if not reused:
            reasons.append(f"{Path(man['_run_dir']).name}: none of the images "
                           "match (they have changed since it ran)")
            continue
        plan.manifest = man
        plan.entry = entry
        plan.reused = reused
        plan.to_infer = to_infer
        return plan

    plan.reason = "; ".join(reasons) if reasons else "no previous run found"
    return plan


# ---------------------------------------------------------------------------
# Reading cached detections back
# ---------------------------------------------------------------------------

def _norm(s) -> str:
    return str(s).strip().lower()


def _resolve_refs(refs: dict, stage_names: list[str]) -> dict:
    """{class name: reference score}, matched like tiled_infer.resolve_refs."""
    src = {_norm(k): float(v) for k, v in (refs or {}).items()
           if not str(k).startswith("_")}
    out = {}
    for name in stage_names:
        v = src.get(_norm(name))
        if v is not None and v > 0:
            out[str(name)] = float(v)
    return out


@dataclass
class CachedRows:
    header: list[str]
    rows: list[list]
    counts_by_image: dict[str, dict]
    n_relabelled: int = 0
    n_no_vector: int = 0


def load_cached_rows(plan: FolderCache, wanted: set[str], stage_names: list[str],
                     alpha: float, refs: dict,
                     excluded: Optional[list[str]] = None) -> Optional[CachedRows]:
    """Read this folder's rows out of a previous run's soft CSV.

    Relabels them for ``alpha`` when it differs from the alpha they were written
    under. That is not an approximation: rescoring is a pure arg-max over the
    per-class vector, applied after every suppression pass, and the vector is in
    the file — so the labels this produces are the labels a fresh run at that
    alpha would have produced.

    Returns None if the file cannot be read; the caller then infers the folder.
    """
    man, entry = plan.manifest, plan.entry
    if man is None or entry is None:
        return None
    csv_path = Path(man["_run_dir"]) / man.get("soft_csv",
                                               "soft_stage_scores.csv")
    tp_key = f"{float(entry['timepoint_h']):g}"
    folder_name = entry["name"]

    cached_alpha = float(((man.get("meta") or {}).get("rescore") or {})
                         .get("alpha") or 0.0)
    skip = {_norm(c) for c in (excluded or [])}
    eligible = [s for s in stage_names if _norm(s) not in skip]
    ref = _resolve_refs(refs, stage_names)
    denom = {s: (ref.get(s, 1.0) ** float(alpha)) for s in stage_names}
    relabel = abs(cached_alpha - float(alpha)) > 1e-9

    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rd = csv.reader(fh)
            header = next(rd)
            idx = {name: i for i, name in enumerate(header)}
            need = ("folder", "timepoint_h", "image", "hard_call")
            if any(k not in idx for k in need):
                return None
            raw_cols = {s: idx.get(f"raw_{s}") for s in stage_names}
            i_folder, i_tp = idx["folder"], idx["timepoint_h"]
            i_image, i_call = idx["image"], idx["hard_call"]
            i_raw_call = idx.get("hard_call_raw")
            i_score = idx.get("hard_score")

            out_rows: list[list] = []
            counts: dict[str, dict] = {name: {} for name in wanted}
            n_relabelled = 0
            n_no_vector = 0
            for row in rd:
                if len(row) < len(header):
                    continue
                if row[i_folder] != folder_name or row[i_tp] != tp_key:
                    continue
                image = row[i_image]
                if image not in wanted:
                    continue
                if relabel:
                    vec = {}
                    ok = True
                    for s in stage_names:
                        j = raw_cols.get(s)
                        if j is None or not row[j]:
                            ok = False
                            break
                        try:
                            vec[s] = float(row[j])
                        except ValueError:
                            ok = False
                            break
                    if not ok:
                        n_no_vector += 1
                    elif alpha == 0 and i_raw_call is not None and row[i_raw_call]:
                        # alpha 0 IS arg-max on the raw scores, and the label
                        # before rescoring is already recorded — use it rather
                        # than recomputing an arg-max that could tie differently.
                        if row[i_call] != row[i_raw_call]:
                            row = list(row)
                            row[i_call] = row[i_raw_call]
                            n_relabelled += 1
                    else:
                        best = max(eligible, key=lambda s: vec[s] / denom[s]) \
                            if eligible else None
                        if best is not None and best != row[i_call]:
                            row = list(row)
                            row[i_call] = best
                            if i_score is not None:
                                row[i_score] = f"{vec[best]:.5f}"
                            n_relabelled += 1
                call = row[i_call]
                bucket = counts.setdefault(image, {})
                bucket[call] = bucket.get(call, 0) + 1
                out_rows.append(list(row))
    except (OSError, StopIteration, csv.Error, ValueError):
        log.warning("cache read failed for %s", csv_path, exc_info=True)
        return None

    return CachedRows(header=header, rows=out_rows, counts_by_image=counts,
                      n_relabelled=n_relabelled, n_no_vector=n_no_vector)


def records_from_counts(images: list[Path], counts_by_image: dict[str, dict],
                        errored: set[str]) -> list[dict]:
    """Rebuild the per-image record dicts `aggregate()` expects.

    An image that was analysed and found nothing gets an empty counts dict, not
    a missing record — "no worms on this plate" and "this plate was never
    looked at" are different claims and the manifest is what lets us tell them
    apart.
    """
    out = []
    for p in images:
        if p.name in errored:
            out.append({"path": str(p), "error": "cached: image errored"})
            continue
        out.append({"path": str(p),
                    "counts": dict(counts_by_image.get(p.name, {}))})
    return out
