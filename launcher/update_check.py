"""
Tell the user when a newer WormScan has been released. Nothing more.

Deliberately the smallest thing that closes the loop:

  * one HTTPS GET to the GitHub Releases API, on a background thread
  * compares the tag to this build's own version (launcher/paths.py)
  * if newer, the launcher shows a line the user can click, which opens the
    release page in their browser

It does NOT download anything, does not install anything, and never asks for
administrator rights. An auto-updater that can go wrong halfway through is a
liability in a tool people run analyses with; a notification cannot be.

Three properties matter more than the feature itself:

  1. **It is silent when it fails.** Offline, rate-limited, DNS blocked,
     malformed tag, GitHub down - every one of those means "say nothing".
     A user must never have to think about, or act on, an update check.
  2. **It never blocks.** Daemon thread, short timeout, started after the
     window is already up. Nothing waits on it.
  3. **It says nothing on a dev build.** Running from source reports its
     version as "dev", which is not comparable to a release tag, so the check
     exits immediately rather than nagging on every `python launcher/main.py`.

The result is cached in the user data dir for a day. GitHub allows 60
unauthenticated API calls per hour *per IP*, which is plentiful for one
machine and less so for a lab of them behind one NAT. Note the cache stores
the answer, not just the timestamp, so a pending update keeps being shown for
the whole day rather than appearing once and vanishing.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import paths

log = logging.getLogger(__name__)

# The repository releases are published from.
GITHUB_REPO = "davidtheadmin/celegans-imaging"

_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"

_TIMEOUT_S = 5.0
_CACHE_TTL_S = 24 * 60 * 60
_STARTUP_DELAY_S = 5.0   # let the window finish drawing first

_CACHE_FILE = paths.user_data_dir() / "update-check.json"


@dataclass(frozen=True)
class UpdateInfo:
    """What the UI needs to draw the notice."""
    latest: str        # the release tag, as published
    current: str       # this build's version
    url: str           # where to send the user


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

def parse_version(text: str) -> Optional[tuple[int, ...]]:
    """
    Turn a version or tag into a comparable tuple, or None if it is not one.

    Handles the shapes this project actually produces and the ones a human
    might type into a tag:

        "2026.08.12+52d31b1"  -> (2026, 8, 12)     build_info version
        "v2026.09.01"         -> (2026, 9, 1)      a release tag
        "1.2.3-beta"          -> (1, 2, 3)         belt and braces
        "dev"                 -> None              a source checkout
        ""                    -> None

    Everything from the first '+' or '-' is dropped: build metadata and
    prerelease markers do not order releases here. A component that is not a
    plain integer ends the parse rather than failing it, so a stray suffix
    degrades to a shorter tuple instead of to nothing.
    """
    if not text:
        return None
    s = str(text).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    for sep in ("+", "-", " "):
        s = s.split(sep, 1)[0]
    parts: list[int] = []
    for chunk in s.split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts) if parts else None


def is_newer(latest: str, current: str) -> bool:
    """True only when `latest` is a real version strictly greater than `current`."""
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    # Pad so (2026, 9) and (2026, 9, 0) compare equal rather than by length.
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# ---------------------------------------------------------------------------
# Status object - same contract as the analysis agents:
# the worker thread writes, the UI thread reads.
# ---------------------------------------------------------------------------

class UpdateStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._info: Optional[UpdateInfo] = None

    def set(self, info: Optional[UpdateInfo]) -> None:
        with self._lock:
            self._info = info

    def snapshot(self) -> Optional[UpdateInfo]:
        with self._lock:
            return self._info


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _read_cache() -> Optional[dict]:
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if time.time() - float(data.get("checked_at", 0)) > _CACHE_TTL_S:
            return None
    except (TypeError, ValueError):
        return None
    return data


def _write_cache(latest: str, url: str) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({"checked_at": time.time(), "latest": latest, "url": url}),
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("could not write the update cache: %s", exc)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _fetch_latest() -> Optional[tuple[str, str]]:
    """(tag, html_url) for the newest release, or None. Never raises."""
    try:
        import requests  # already a launcher dependency
        resp = requests.get(
            _API_URL,
            timeout=_TIMEOUT_S,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"WormScan/{paths.version_string()}",
            },
        )
        if resp.status_code != 200:
            # 403 is the rate limit, 404 means no releases published yet.
            log.debug("update check: HTTP %s", resp.status_code)
            return None
        data = resp.json()
        tag = str(data.get("tag_name") or "").strip()
        if not tag:
            return None
        return tag, str(data.get("html_url") or _RELEASES_URL)
    except Exception as exc:
        # Deliberately broad. Offline, DNS blocked, TLS intercepted by a
        # corporate proxy, JSON malformed - none of it is the user's problem.
        log.debug("update check failed: %s", exc)
        return None


def check_now(current: Optional[str] = None, use_cache: bool = True) -> Optional[UpdateInfo]:
    """
    Run the check synchronously. Returns None when there is nothing to say.

    Exposed separately from the thread so it can be tested, and so a future
    "check now" button has something to call.
    """
    current = current if current is not None else paths.version_string()
    if parse_version(current) is None:
        log.debug("update check skipped: running version %r is not a release", current)
        return None

    cached = _read_cache() if use_cache else None
    if cached is not None:
        latest = str(cached.get("latest") or "")
        url = str(cached.get("url") or _RELEASES_URL)
        log.debug("update check: using cached result %r", latest)
    else:
        got = _fetch_latest()
        if got is None:
            return None
        latest, url = got
        _write_cache(latest, url)

    if is_newer(latest, current):
        log.info("update available: %s (running %s)", latest, current)
        return UpdateInfo(latest=latest, current=current, url=url)

    log.debug("up to date: latest %r, running %r", latest, current)
    return None


class UpdateChecker(threading.Thread):
    """
    One-shot background check, with the start/stop/join shape of the other
    agents so main.py can treat it the same way.

    It runs once and exits: a launcher session is not long enough for a second
    look to be worth anything, and a thread that has finished cannot misbehave.
    """

    def __init__(self, settings: object, status: UpdateStatus) -> None:
        super().__init__(daemon=True, name="update-check")
        self._settings = settings
        self._status = status
        self._stop_evt = threading.Event()

    def run(self) -> None:
        if not getattr(self._settings, "check_for_updates", True):
            log.info("update check disabled in settings")
            return
        # Wait, interruptibly, so a user who closes the window immediately does
        # not leave a socket open behind them.
        if self._stop_evt.wait(_STARTUP_DELAY_S):
            return
        try:
            self._status.set(check_now())
        except Exception as exc:
            log.debug("update check thread failed: %s", exc)

    def stop(self) -> None:
        self._stop_evt.set()

    def update_settings(self, settings: object) -> None:
        self._settings = settings
