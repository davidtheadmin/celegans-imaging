"""Checks that the run-cache digest covers everything that moves the numbers.

Run it directly: `python test_run_cache_digest.py`. Not a pytest suite — the
other test modules in this folder are scripts and this one matches them.

WHY THIS FILE EXISTS. On 27 Aug the crawling re-run that was supposed to
measure MIN_FRAGMENT_SKELETON_COVERAGE reported that the floor changed
nothing. It had not: the floor was not part of `settings_digest`, so
`plan_folder` matched the previous run's manifest, every folder was reused,
and the tracker never ran. The same reuse handed back per-worm rows written
before `directionality`, `is_immobile` and `reversal_rate_moving_per_min`
existed, and those three columns came out EMPTY for all 3173 worms — two of
the four headline metrics blank in the workbook, the figures and the explorer,
with nothing in the log to say so.

Neither failure raises. Both look exactly like a run that had nothing to do.
So what is checked here is the property that makes them impossible: a change
to anything that moves the per-worm rows must change the digest.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from analysis import crawling_metrics as cm                      # noqa: E402
from analysis.run_cache import settings_digest                   # noqa: E402

FAILURES: list[str] = []

PARAMS = {"traj_min_area": 500, "expected_fps": 30}


def check(ok: bool, what: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + what)
    if not ok:
        FAILURES.append(what)


def digest(min_span_s: float = 20.0, threshold_s: float = 2.0,
           params: dict | None = None, flat_field: bool = True) -> str:
    return settings_digest("crawling", PARAMS if params is None else params,
                           flat_field,
                           cm.reuse_post_settings(min_span_s, threshold_s))


def test_gates() -> None:
    print("\nthe two track gates")
    base = digest()
    check(base == digest(), "the same settings give the same digest")
    check(digest(min_span_s=10.0) != base, "min_span_s changes it")
    check(digest(threshold_s=3.0) != base, "threshold_s changes it")
    check(digest(flat_field=False) != base, "the flat-field flag changes it")
    check(digest(params={"traj_min_area": 250, "expected_fps": 30}) != base,
          "a Tierpsy parameter changes it")
    check(digest(params={"traj_min_area": 500, "expected_fps": 10}) == base,
          "expected_fps alone does not — it is excluded on purpose")


def test_skeleton_floor() -> None:
    print("\nthe skeleton floor (the 27 Aug failure)")
    base = digest()
    old = cm.MIN_FRAGMENT_SKELETON_COVERAGE
    try:
        cm.MIN_FRAGMENT_SKELETON_COVERAGE = 0.0
        off = digest()
        cm.MIN_FRAGMENT_SKELETON_COVERAGE = 0.5
        high = digest()
    finally:
        cm.MIN_FRAGMENT_SKELETON_COVERAGE = old
    check(off != base, "turning the floor off changes the digest")
    check(high != base and high != off, "so does moving it")
    check(digest() == base, "and restoring it restores the digest")


def test_column_schema() -> None:
    print("\nthe per-worm column set (the empty-column failure)")
    base = digest()
    old = list(cm.PER_WORM_COLS)
    try:
        cm.PER_WORM_COLS = old + ["some_new_metric"]
        added = digest()
        cm.PER_WORM_COLS = old[:-1]
        removed = digest()
        cm.PER_WORM_COLS = list(reversed(old))
        reordered = digest()
    finally:
        cm.PER_WORM_COLS = old
    check(added != base, "adding a metric column changes the digest")
    check(removed != base, "removing one changes it")
    check(reordered != base, "so does reordering — the CSV header is ordered")
    check(digest() == base, "and restoring the columns restores the digest")


def test_headline_metrics_are_covered() -> None:
    print("\nthe three columns that came back empty")
    for col in ("directionality", "is_immobile",
                "reversal_rate_moving_per_min"):
        check(col in cm.PER_WORM_COLS,
              f"{col} is in PER_WORM_COLS, so the schema hash covers it")


def test_post_settings_shape() -> None:
    print("\nreuse_post_settings is the one spelling")
    post = cm.reuse_post_settings(20.0, 2.0)
    check(set(post) == {"min_span_s", "threshold_s",
                        "min_fragment_skeleton_coverage", "per_worm_schema"},
          "it carries the two gates, the floor and the schema and nothing else")
    check(post["min_fragment_skeleton_coverage"]
          == cm.MIN_FRAGMENT_SKELETON_COVERAGE,
          "the floor is read from the module, not passed in by the caller")
    check(all(isinstance(v, (float, str)) for v in post.values()),
          "every value is JSON-stable, so the hash is stable across runs")


if __name__ == "__main__":
    test_gates()
    test_skeleton_floor()
    test_column_schema()
    test_headline_metrics_are_covered()
    test_post_settings_shape()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("all checks passed")
