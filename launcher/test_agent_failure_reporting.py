"""A crashed run must tell the user, in every assay.

Run from launcher/:  python test_agent_failure_reporting.py

The UI has always had a failure notice (ui.py, _poll_body: `if
result.get("failed")`). It only ever fired for Development, because only
SurvivalStatus had a mark_failed that put a result in the queue. The other three
agents set the status dot red and returned, which from the outside is exactly
what a run that quietly did nothing looks like. Reported 2026-08-19 as "at the
end of the run there was no message".

These tests pin the contract between the four status objects and the one place
in the UI that reads them.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "analysis"))

FAILURES: list[str] = []


def check(cond, what):
    print(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        FAILURES.append(what)


def _status_classes():
    from analysis.motility import MotilityStatus
    from analysis.crawling import CrawlingStatus
    from analysis.counting_agent import CountingStatus
    from survival import SurvivalStatus
    return [("Motility", MotilityStatus), ("Crawling", CrawlingStatus),
            ("Counting", CountingStatus), ("Development", SurvivalStatus)]


def test_every_agent_can_report_failure():
    print("\nevery assay can report a failure")
    for name, cls in _status_classes():
        st = cls()
        check(hasattr(st, "mark_failed"), f"{name} has mark_failed")
        if not hasattr(st, "mark_failed"):
            continue
        st.mark_failed("ValueError: I/O operation on closed file.",
                       Path("/tmp/run_dir"))
        r = st.pop_completed()
        # exactly the keys ui.py reads
        check(bool(r), f"{name} puts a result in the queue the UI polls")
        check(r and r.get("failed") is True, f"{name} marks it failed")
        check(r and "closed file" in str(r.get("error")),
              f"{name} carries the error text the notice shows")
        check(r and r.get("out_dir") is not None,
              f"{name} carries the run folder, so 'Open run folder' works")
        check(st.pop_completed() is None,
              f"{name} delivers the result once, not on every poll")


def test_success_is_explicitly_not_failed():
    print("\na successful run is explicitly not a failure")
    for name, cls in _status_classes():
        st = cls()
        st.mark_completed(3, 0, Path("/tmp/run_dir"))
        r = st.pop_completed()
        check(r and r.get("failed") is False,
              f"{name} sets failed=False rather than leaving it absent")
        check(r and r.get("n_ok") == 3, f"{name} still reports its counts")


def test_ui_reads_these_keys():
    print("\nthe UI contract")
    ui = (Path(__file__).parent / "ui.py").read_text(encoding="utf-8")
    for key in ('result.get("failed")', 'result.get("error")',
                'result.get("out_dir")', 'result["n_ok"]'):
        check(key in ui, f"ui.py reads {key}")


def test_crash_writes_a_traceback_into_the_run_log():
    print("\na crash leaves an explanation in the run's own log.txt")
    import re
    for f in ("analysis/motility.py", "analysis/crawling.py",
              "analysis/counting_agent.py"):
        src = (Path(__file__).parent / f).read_text(encoding="utf-8")
        m = re.search(r"except Exception as exc:\n\s+log\.exception\(\"\w+Agent "
                      r"crashed\"\)(.{0,900})", src, re.S)
        body = m.group(1) if m else ""
        check("traceback.format_exc()" in body,
              f"{f} appends the traceback to log.txt")
        check("mark_failed" in body, f"{f} calls mark_failed")
        check("ANALYSIS CRASHED" in body,
              f"{f} marks the crash so it is findable in a long log")


if __name__ == "__main__":
    test_every_agent_can_report_failure()
    test_success_is_explicitly_not_failed()
    test_ui_reads_these_keys()
    test_crash_writes_a_traceback_into_the_run_log()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("all checks passed")
