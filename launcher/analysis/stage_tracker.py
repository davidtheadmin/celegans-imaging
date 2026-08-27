"""What a run is doing RIGHT NOW, for the progress dialog.

WHY THIS EXISTS, AND WHY IT IS NOT THE LOG. Every pipeline already writes a
detailed log, and every pipeline buffers it: the worker collects lines and the
collecting thread flushes them when an item finishes. That is right for a log —
one video's lines stay contiguous instead of interleaving with seven others' —
and it is useless for a progress window, because on a 25-minute video it means
25 minutes of nothing while the only visible number ("12/38 done") does not
move. Somebody watching cannot tell a working run from a hung one, which on a
20-hour reanalysis is the only question they have.

This is the other channel: one short phrase per worker, overwritten in place,
never queued. It is lossy on purpose — if two phases pass between two UI polls,
the intermediate one is simply never shown, and that is fine. Nothing here is
a record; the log is the record.

THE COUNTS ARE THE POINT. Eight workers produce eight lines of the same three
phrases, which is not a status display. Grouping them is: "5x Calculating
skeletons" says everything about a pool in one clause, and a phase that is
stuck on one worker while the others move on is visible as the clause that
never changes.

THREADING. The tracker owns its own lock and nothing else, so worker threads
can write to it without touching the Status object's lock and without any
ordering rules against it. Writes are worker-thread; summary() is UI-thread.
"""
from __future__ import annotations

import re
import threading

# Tierpsy announces each checkpoint on its own stdout line, as
#   "<video_stem> Calculating skeletons. Total time = 0:04:02, fps = 22.3"
# and repeats it every few hundred frames. That sentence is a better answer to
# "what is it doing" than anything we could infer from outside the container,
# so it is parsed rather than guessed at. Everything else on the stream — the
# progress counters, the task tallies, the TERM warnings — is noise here and is
# left to the log.
_TIERPSY_PHASE_RE = re.compile(r"^(\S+)\s+([A-Z][A-Za-z][^.]{2,58})\.")
_TIERPSY_PHASE_SKIP = ("Total time", "Tasks:", "Checking file", "Finished to")

# How many distinct phases to name before collapsing the rest into "+N more".
_MAX_GROUPS = 3

# Prefixes worth factoring out of the whole line when every clause carries them
# (see summary). Longest first, so a more specific prefix wins.
_SHARED_PREFIXES = ("Tierpsy: ",)


def tierpsy_phase(line: str) -> str:
    """The checkpoint name on this Tierpsy output line, or "" if there is none.

    Shared by the crawling and motility pipelines so the two cannot drift into
    describing the same tracker in two different vocabularies.
    """
    m = _TIERPSY_PHASE_RE.match(line.strip())
    if not m:
        return ""
    # The leading token is the video stem. Tierpsy's end-of-run summary block
    # has the same shape with a COUNT in front — "1\tUnprocessed files.",
    # "0\tFiles whose analysis is incompleted." — and those would otherwise be
    # reported as phases at the moment the run finishes, which is exactly when
    # a wrong phase is most misleading. A video stem is never all digits.
    if m.group(1).isdigit():
        return ""
    phrase = m.group(2).strip()
    if any(phrase.startswith(p) for p in _TIERPSY_PHASE_SKIP):
        return ""
    return phrase


class StageTracker:
    """Per-item phases, summarised into one line for the progress dialog."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, str] = {}

    def set(self, key: str, stage: str) -> None:
        """Worker thread: this item is now in `stage`; "" removes it."""
        with self._lock:
            if stage:
                self._stages[key] = stage
            else:
                self._stages.pop(key, None)

    def reporter(self, key: str):
        """A one-item stage channel to hand a worker.

        Bound per item rather than shared, so eight workers cannot overwrite
        each other's phase and a worker that dies leaves one stale key rather
        than a wrong global string. Swallows its own errors: a status update is
        never worth failing an analysis over.
        """
        def report(stage: str) -> None:
            try:
                self.set(key, stage)
            except Exception:                                      # noqa: BLE001
                pass
        return report

    def clear(self) -> None:
        with self._lock:
            self._stages.clear()

    def summary(self) -> str:
        """UI thread: one line, identical phases grouped and counted."""
        with self._lock:
            stages = list(self._stages.values())
        if not stages:
            return ""
        counts: dict[str, int] = {}
        for s in stages:
            counts[s] = counts.get(s, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

        # Once the pool is all inside Tierpsy, every clause starts "Tierpsy: "
        # and the repetition eats the width that the phase names need. Say it
        # once at the front instead — and only when it is true of every clause,
        # so a pool that is half transcoding is never described as being
        # entirely in Tierpsy.
        lead = ""
        for prefix in _SHARED_PREFIXES:
            if all(name.startswith(prefix) for name, _ in ordered):
                lead = prefix.rstrip(": ") + " — "
                ordered = [(name[len(prefix):], n) for name, n in ordered]
                break

        parts = [(f"{n}x {name}" if n > 1 else name)
                 for name, n in ordered[:_MAX_GROUPS]]
        if len(ordered) > _MAX_GROUPS:
            parts.append(f"+{len(ordered) - _MAX_GROUPS} more")
        return lead + "  ·  ".join(parts)
