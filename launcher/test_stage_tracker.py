"""Checks for the live progress channel (analysis/stage_tracker.py).

Run it directly: `python test_stage_tracker.py`. Not a pytest suite — the other
test modules in this folder are scripts with positional fixtures and this one
matches them.

WHAT IS WORTH TESTING HERE. The tracker itself is twenty lines and could not go
far wrong. What can go wrong, and what these checks are actually for:

  * the Tierpsy phrase parser silently matching noise — every "Total time"
    line would become a phase and the dialog would flicker meaninglessly;
  * a reporter raising into a worker thread — a status update must never be
    able to fail a video that otherwise succeeded;
  * a finished or failed worker leaving its phase behind, which makes a
    stalled run and a working one look identical;
  * the shared-prefix factoring firing on a mixed pool, which would describe
    workers as being inside Tierpsy when they are transcoding.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from analysis.stage_tracker import StageTracker, tierpsy_phase   # noqa: E402

FAILURES: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + what)
    if not ok:
        FAILURES.append(what)


def test_phrase_parser() -> None:
    print("\nTierpsy phrase parser")
    real = "20260529T135212_video Calculating skeletons. Total time = 0:04:02, fps = 22.3"
    check(tierpsy_phase(real) == "Calculating skeletons",
          "a checkpoint line yields the checkpoint name, without the timings")
    check(tierpsy_phase("vid Filter Skeletons: Calculating outliers.")
          == "Filter Skeletons: Calculating outliers",
          "a phase containing a colon survives intact")
    for noise in ("TERM environment variable not set.",
                  "Tasks: 0 finished, 1 remaining. Total_time 0:00:12.",
                  "Checking file 1 of 1. Total time: 0:00:00",
                  "*********************************************",
                  "vid Total time = 0:00:00, fps = 46603.",
                  "",
                  "1\tUnprocessed files.",
                  "0\tFiles whose analysis is incompleted.",
                  "1\tTotal files to be processed."):
        check(tierpsy_phase(noise) == "",
              f"noise is not a phase: {noise[:44]!r}")


def test_grouping() -> None:
    print("\ngrouping and counting")
    t = StageTracker()
    check(t.summary() == "", "an idle tracker renders as an empty line")
    for k in ("a", "b", "c"):
        t.set(k, "Calculating skeletons")
    t.set("d", "Compressing video")
    check("3x Calculating skeletons" in t.summary(),
          "identical phases are grouped and counted")
    check(t.summary().index("3x") < t.summary().index("Compressing"),
          "the commonest phase is named first")
    for i, k in enumerate("efghij"):
        t.set(k, f"Phase {i}")
    check("+" in t.summary() and "more" in t.summary(),
          "past three distinct phases the rest collapse into '+N more'")


def test_prefix_factoring() -> None:
    print("\nshared-prefix factoring")
    t = StageTracker()
    t.set("a", "Tierpsy: Calculating skeletons")
    t.set("b", "Tierpsy: Compressing video")
    s = t.summary()
    check(s.startswith("Tierpsy — ") and "Tierpsy:" not in s,
          "an all-Tierpsy pool says Tierpsy once, at the front")
    t.set("c", "Flat-field + transcode")
    s = t.summary()
    check(not s.startswith("Tierpsy — ") and "Tierpsy: " in s,
          "a MIXED pool keeps the prefix per clause — half a pool transcoding "
          "must never read as all of it being inside Tierpsy")


def test_lifecycle() -> None:
    print("\nlifecycle")
    t = StageTracker()
    t.set("v1", "Calculating skeletons")
    t.set("v2", "Compressing video")
    t.set("v1", "")
    check("Calculating skeletons" not in t.summary(),
          "a finished item's phase leaves the line")
    t.clear()
    check(t.summary() == "", "clear() empties it — a completed run shows nothing")

    rep = t.reporter("v9")
    rep("Measuring")
    check("Measuring" in t.summary(), "a reporter writes under its own key")
    rep("")
    check(t.summary() == "", "and clears it")

    class Broken(StageTracker):
        def set(self, key, stage):
            raise RuntimeError("status backend exploded")

    try:
        Broken().reporter("v")("anything")
        check(True, "a reporter swallows its own errors — a status update "
                    "cannot fail the video it is reporting on")
    except Exception as exc:                                       # noqa: BLE001
        check(False, f"reporter raised into the worker: {exc}")


def test_threading() -> None:
    print("\nthreading")
    t = StageTracker()
    errors: list[str] = []

    def worker(n: int) -> None:
        try:
            r = t.reporter(f"v{n}")
            for i in range(400):
                r(f"Phase {i % 5}")
                t.summary()
            r("")
        except Exception as exc:                                   # noqa: BLE001
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    check(not errors, "eight writers and a concurrent reader raise nothing")
    check(t.summary() == "", "and every worker's key is gone at the end")


if __name__ == "__main__":
    test_phrase_parser()
    test_grouping()
    test_prefix_factoring()
    test_lifecycle()
    test_threading()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("all checks passed")
