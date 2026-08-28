"""Run motility and crawling back to back, headless, with no launcher window.

WHY THIS EXISTS
---------------
Both pipelines are driven from the launcher GUI, one run at a time, and a run
you cannot start without clicking is a run you cannot queue overnight. The
agents themselves are plain threads with a status object — nothing about them
needs tkinter — so this drives them directly and waits.

It is a RUNNER, not a second implementation: it calls the same
`start_analysis()` the buttons call, with the same settings object from
config.load(). Nothing about the analysis differs from a GUI run.

WHAT IT DOES NOT DO
-------------------
Renders are off unless asked; `--renders tracked` gives just the overlay you
evaluate a filter with, `--renders all` everything each pipeline can draw.
`clear_cache` is False and there is no flag for it:
the whole point of an unattended run is that you decided what the cache holds
BEFORE going to bed, and a stray --clear-cache would silently turn a 40-minute
job into a 16-hour one. Clear it by hand first if that is what you want.

USAGE
-----
    launcher\\.venv\\Scripts\\python.exe dev\\run_overnight.py ^
        --motility "E:\\Wormdata\\260521_Motility" ^
        --motility-threshold-s 5 ^
        --crawling "E:\\Wormdata\\Crawling\\260529_Crawling_day0=0" ^
        --crawling "E:\\Wormdata\\Crawling\\260530_Crawling_day1=24" ^
        --crawling "E:\\Wormdata\\Crawling\\260531_Crawling_day2=48" ^
        --crawling "E:\\Wormdata\\Crawling\\260601_Crawling_day3=72" ^
        --crawling "E:\\Wormdata\\Crawling\\260602_Crawling_day_4=96" ^
        --crawling-min-span-s 20 ^
        --log E:\\Wormdata\\overnight.log

Each --crawling is FOLDER=HOURS. Order on the command line is the order the
timecourse gets. Motility runs first because it is the shorter of the two, so a
failure there is visible sooner.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "launcher"))

import config                                                     # noqa: E402
from survival import FolderPlan                                   # noqa: E402
from analysis.motility import MotilityAgent, MotilityStatus       # noqa: E402
from analysis.crawling import CrawlingAgent, CrawlingStatus       # noqa: E402

_log_fh = None


def say(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def wait_for(agent, status, label: str, heartbeat: int) -> dict | None:
    """Block until the agent finishes, reporting what it is doing as it goes."""
    last = 0.0
    # The agent sets running=True inside start_analysis, but give the thread a
    # moment to pick up the wake event before treating "not running" as "done".
    time.sleep(2.0)
    while status.is_running():
        now = time.time()
        if now - last >= heartbeat:
            last = now
            s = status.snapshot()
            detail = (getattr(s, "stage_detail", "") or "").strip()
            say(f"  {label}: {s.current_index}/{s.total} "
                f"{s.current_stage or ''}".rstrip()
                + (f" | {detail}" if detail else ""))
        time.sleep(1.0)
    return status.pop_completed()


def report(label: str, result: dict | None) -> bool:
    """True only if the job really succeeded.

    The status object reports a CRASH as completed with n_ok=0, n_fail=0 and
    failed=True. Reading only the counts turns "the agent died" into "done —
    ok=0 failed=0", which is exactly the line you do not want to find in an
    overnight log. `failed` is checked first.
    """
    if not result:
        say(f"{label}: finished but reported no result — check its log.txt")
        return False
    if result.get("failed"):
        say(f"{label}: FAILED — {result.get('error') or 'no error recorded'}")
        say(f"{label}: output dir was {result.get('out_dir','?')}")
        return False
    ok = result.get("n_ok", 0)
    bad = result.get("n_fail", 0)
    say(f"{label}: done — ok={ok} failed={bad} out={result.get('out_dir','?')}")
    if ok == 0:
        say(f"{label}: WARNING — zero videos succeeded")
        return False
    return not bad


def main() -> int:
    global _log_fh
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--motility", type=Path, help="motility folder (one)")
    p.add_argument("--motility-threshold-s", type=float, default=5.0,
                   help="min fragment length for is_long (default 5)")
    p.add_argument("--crawling", action="append", default=[], metavar="FOLDER=HOURS",
                   help="repeat once per day folder")
    p.add_argument("--crawling-min-span-s", type=float, default=20.0)
    p.add_argument("--crawling-threshold-s", type=float, default=5.0)
    p.add_argument("--renders", choices=("none", "tracked", "all"), default="none",
                   help="none (default); tracked = the tracked overlay only, "
                        "which is the one you evaluate a filter with; all = "
                        "every artefact each pipeline can draw, including "
                        "motility's per-worm traces, which are the slow part")
    p.add_argument("--heartbeat", type=int, default=60, help="s between progress lines")
    p.add_argument("--log", type=Path, help="also write this transcript to a file")
    p.add_argument("--crawling-first", action="store_true")
    args = p.parse_args()

    if not args.motility and not args.crawling:
        p.error("nothing to do — give --motility and/or --crawling")

    plans = []
    for spec in args.crawling:
        if "=" not in spec:
            p.error(f"--crawling wants FOLDER=HOURS, got {spec!r}")
        folder, _, hours = spec.rpartition("=")
        f = Path(folder)
        if not f.is_dir():
            p.error(f"not a folder: {f}")
        plans.append(FolderPlan(folder=f, hours=float(hours),
                                method="typed", detail="typed by run_overnight"))
    if args.motility and not args.motility.is_dir():
        p.error(f"not a folder: {args.motility}")

    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        _log_fh = open(args.log, "a", encoding="utf-8")

    settings = config.load()
    t0 = time.time()
    tracked = args.renders in ("tracked", "all")
    every = args.renders == "all"
    say(f"start — motility={args.motility or 'skipped'} "
        f"crawling={len(plans)} folder(s) renders={args.renders}")

    def do_motility() -> bool:
        if not args.motility:
            return True
        st = MotilityStatus()
        ag = MotilityAgent(settings, st)
        ag.start()
        say(f"motility: {args.motility}  threshold_s={args.motility_threshold_s:g}")
        ag.start_analysis(
            [FolderPlan(folder=args.motility, hours=0.0,
                        method="single folder", detail="single folder")],
            threshold_s=args.motility_threshold_s,
            clear_cache=False,
            want_tracked=tracked,
            want_curvature=every,
            want_sidebyside=every,
            want_per_worm_traces=every,
        )
        return report("motility", wait_for(ag, st, "motility", args.heartbeat))

    def do_crawling() -> bool:
        if not plans:
            return True
        st = CrawlingStatus()
        ag = CrawlingAgent(settings, st)
        ag.start()
        say(f"crawling: {len(plans)} folder(s) at "
            f"{', '.join(f'{q.hours:g}h' for q in plans)}  "
            f"min_span_s={args.crawling_min_span_s:g}")
        ag.start_analysis(
            plans,
            threshold_s=args.crawling_threshold_s,
            clear_cache=False,
            want_tracked=tracked,
            want_sidebyside=every,
            want_path_traces=every,
            min_span_s=args.crawling_min_span_s,
        )
        return report("crawling", wait_for(ag, st, "crawling", args.heartbeat))

    order = ([do_crawling, do_motility] if args.crawling_first
             else [do_motility, do_crawling])
    # Both run whatever the first one did. An overnight queue that abandons the
    # second job because the first hit one bad video wastes the night.
    results = []
    for fn in order:
        try:
            results.append(fn())
        except Exception as exc:                                   # noqa: BLE001
            say(f"FAILED: {type(exc).__name__}: {exc}")
            results.append(False)

    say(f"all done in {(time.time()-t0)/60:.1f} min")
    if _log_fh:
        _log_fh.close()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
