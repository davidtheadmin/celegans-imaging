"""
WormScan Launcher UI — Tkinter main window and settings dialog.

Thread boundary: this module runs entirely on the main (Tk) thread.
It reads sync state only through SyncStatus.snapshot() via root.after().
No widget method is ever called from the sync thread.
"""
import logging
import os
import random
import re
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import MISSING as _MISSING, fields as _dc_fields, replace
from urllib.parse import quote
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

import customtkinter as ctk

import config as cfg
import paths
import theme
import update_check
import widgets
from analysis.docker_utils import run_preflight
from analysis.motility import MotilityAgent, MotilityStatus
from analysis.crawling import CrawlingAgent, CrawlingStatus
from analysis.counting_agent import (
    CountingAgent, CountingStatus, counting_preflight,
)
from survival import (
    SurvivalAgent,
    SurvivalStatus,
    default_class_conf,
    default_exclude_classes,
    plan_reuse,
    resolve_timepoints,
    survival_preflight,
)
from sync import SyncAgent, SyncStatus

_log = logging.getLogger(__name__)

_DOT_COLORS: dict[str, str] = {
    "green": "#4caf50",
    "yellow": "#ffb300",
    "red":    "#f44336",
    "gray":   "#9e9e9e",
}

_POLL_MS = 2000        # main window refresh interval
_PROGRESS_POLL_MS = 200  # progress dialog refresh interval
# Flavour-line dwell, in poll ticks. 15 ticks (3 s) read as a progress
# indicator and made the window feel busier than the run was; 45 (9 s) is
# long enough to read a line, look away, and not catch it changing.
_FLAVOUR_TICKS = 45

# ---------------------------------------------------------------------------
# Window placement
#
# The launcher sits hard against the left edge at full working height, and each
# window it opens is placed immediately to its right at the same height. Two
# windows that tile deterministically beat two windows that land wherever the
# window manager felt like putting them — which, with a fixed-width launcher,
# meant the Analyze dialog regularly opened on top of the thing it was about
# to report on.
#
# "Full height" means the WORK AREA, not the screen: on Windows the taskbar
# owns the bottom strip, and a window sized to the raw screen height has its
# footer — the Start button — underneath it.
# ---------------------------------------------------------------------------

_WINDOW_GAP = 8          # px between tiled windows
_WINDOW_MARGIN_Y = 0     # px from the top of the work area
_WINDOW_MARGIN_BOTTOM = 8
# How much of the work area the window frame is assumed to eat before we
# measure it for real. A Windows title bar is ~31 px at 100% scaling and ~46 px
# at 150%; 56 is comfortably over both, and the correction loop gives back
# whatever was over-reserved.
_FRAME_RESERVE = 56
# _fit_height calls update(), which pumps the event loop and can therefore
# re-enter through a timer or a click. One fit at a time.
_fitting = False


def _window_scaling(win: tk.Misc) -> float:
    """CustomTkinter's widget-scaling factor for this window.

    This matters more than it looks. CTk overrides .geometry() and multiplies
    the width and height it is given by this factor, while every winfo_* query
    answers in real pixels. Feed a measured pixel height straight back into
    geometry() on a 150% display and you get a window half again too tall —
    which is exactly how the Start button ended up below the bottom of the
    screen.
    """
    try:
        return float(ctk.ScalingTracker.get_window_scaling(win)) or 1.0
    except Exception:
        return 1.0


def _work_area_win32() -> Optional[tuple[int, int]]:
    """The Windows work area (screen minus taskbar) from the OS itself.

    wm_maxsize() was the obvious source and it is not reliable: for a Toplevel
    it can come back as the full screen, which puts the bottom of a "full
    height" window — and the Start button pinned to it — behind the taskbar.
    SystemParametersInfo answers the actual question.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        if not ok:
            return None
        w = int(rect.right - rect.left)
        h = int(rect.bottom - rect.top)
        if w > 200 and h > 200:
            return w, h
    except Exception:
        _log.debug("SPI_GETWORKAREA failed", exc_info=True)
    return None


def work_area(win: tk.Misc) -> tuple[int, int]:
    """(width, height) of the usable desktop in real pixels, taskbar excluded."""
    native = _work_area_win32()
    if native is not None:
        return native
    try:
        w, h = win.wm_maxsize()
        if w > 200 and h > 200:
            return int(w), int(h)
    except tk.TclError:
        pass
    return win.winfo_screenwidth(), win.winfo_screenheight()


def _fit_height(win: tk.Misc, width: int, x: int, top: int) -> None:
    """Size `win` to the work area below `top` — conservatively, then exactly.

    ``width`` is in CTk units (unscaled), matching every other geometry call in
    this file. The height cannot be: it comes from the work area, which is real
    pixels, so it is converted through the widget-scaling factor.

    Two things about this are hard-won.

    First, the WINDOW FRAME is not part of the geometry you ask for. A window
    placed at y=0 with height H actually occupies H plus a title bar, so asking
    for the full work-area height puts the bottom of the window — and anything
    pinned to it — under the taskbar. We therefore ask for less than we want by
    ``_FRAME_RESERVE`` and let the correction loop below grow it back.

    Second, update_idletasks() is NOT enough to read the result back. The
    window manager applies a geometry change asynchronously, and idle tasks do
    not wait for the ConfigureNotify that reports it. Measuring after
    update_idletasks() returns the size the window had BEFORE the request —
    which on the first call is the natural requested size, so the loop sees no
    overflow, congratulates itself and returns without ever correcting
    anything. That is exactly what it did. update() waits.
    """
    global _fitting
    if _fitting:
        return
    _, work_h = work_area(win)
    limit = work_h - _WINDOW_MARGIN_BOTTOM
    scale = _window_scaling(win)
    # Deliberately short of the target: too small is a cosmetic gap, too tall
    # hides a button. The loop closes the gap from the safe side.
    h = max(300.0, (limit - top - _FRAME_RESERVE) / scale)
    _fitting = True
    try:
        win.geometry(f"{width}x{round(h)}+{int(x)}+{int(top)}")
        for _ in range(4):
            try:
                win.update()
                bottom = win.winfo_rooty() + win.winfo_height()
            except tk.TclError:
                return
            delta = bottom - limit
            if abs(delta) <= 2:
                return
            h = max(300.0, h - delta / scale)
            win.geometry(f"{width}x{round(h)}+{int(x)}+{int(top)}")
    finally:
        _fitting = False


def place_left_full_height(win: tk.Misc, width: int) -> None:
    """Pin `win` to the left edge, filling the work area vertically."""
    _fit_height(win, width, 0, _WINDOW_MARGIN_Y)


def place_beside(win: tk.Misc, parent: tk.Misc, width: Optional[int] = None,
                 full_height: bool = True) -> None:
    """Place `win` immediately right of `parent`, same top edge.

    ``width`` is in CTk units and is only applied when ``full_height`` is set;
    otherwise the window keeps whatever size it asked for and only moves, which
    avoids scaling a measured pixel width back through CTk's geometry override
    for no reason.

    If it would run off the right edge of the desktop it is pushed back on
    screen rather than opening half-invisible; if it still does not fit beside
    the parent it overlaps, which is the least-bad option on a small display.
    """
    try:
        parent.update_idletasks()
        win.update_idletasks()
        sw, _ = work_area(win)
        x = parent.winfo_rootx() + parent.winfo_width() + _WINDOW_GAP
        own_w = win.winfo_width() if width is None else width * _window_scaling(win)
        if x + own_w > sw:
            x = max(0, int(sw - own_w))
        if full_height and width is not None:
            _fit_height(win, width, x, _WINDOW_MARGIN_Y)
        else:
            win.geometry(f"+{int(x)}+{int(parent.winfo_rooty())}")
    except tk.TclError:
        pass


# Prefixes analysis pipelines write their output folders under. Used only to
# list recent runs on the launcher's front page — an unknown prefix simply
# does not show up, it never breaks anything.
_RESULT_PREFIXES = ("_development", "_survival", "_counting", "_motility",
                    "_crawling", "_analysis")
_RESULT_SCAN_MAX_DIRS = 4000   # hard cap so a huge mirror cannot stall the UI
_RESULT_SCAN_DEPTH = 3

# Auto-detect file-type classification for the Review dialog. Mirrors the
# extension sets the two generator scripts accept.
_REVIEW_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".m4v"}
_REVIEW_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
_REVIEW_CACHE_DIRNAME = ".viewer_cache"
_REVIEW_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_REVIEW_DATE_RE = re.compile(r"^(\d{6})")

_FLAVOUR_TEXTS: list[str] = [
    "Counting wiggles…",
    "Asking the worms to hold still — they refuse.",
    "Untangling skeletons…",
    "Watching 49-point polylines dance.",
    "Smoothing the curvature signal…",
    "Consulting the worm literature…",
    "Detrending the baseline (it was trending).",
    "Identifying long fragments…",
    "Teaching Docker about nematodes.",
    "Looking for the head.",
    "Measuring bend amplitude…",
    "Cross-referencing the skeleton map.",
    "Applying the 2-second rolling mean.",
    "Calibrating the wiggle detector.",
    "Counting mean-crossings…",
    "The curvature is non-trivial.",
    "Tierpsy is thinking deeply.",
    "Remapping skeleton indices…",
    "Running feature extraction…",
    "Synchronising frame numbers.",
    "BGR24 frames incoming.",
    "ffmpeg is very happy right now.",
    "Almost there (probably).",
    "Your patience is greatly appreciated.",
    "This worm has a lot of opinions.",
    # Phase 3a additions ("Counting wiggles…" is dropped — exact dup of entry 0).
    "Asking the worms to hold still…",
    "Negotiating with Tierpsy…",
    "Measuring bends per minute…",
    "Politely herding nematodes…",
    "Waiting for Docker to find itself…",
    "Skeletonizing, 49 points at a time…",
    "Bribing worm #47 to crawl straighter…",
    "Untangling a collision…",
    "Deciding if that's a worm or a scratch…",
    "Detrending the head-swing…",
    "Normalizing to body lengths…",
    "Locating the head (harder than it sounds)…",
    "Ignoring the flickering blobs…",
    "Transcoding to something Tierpsy respects…",
    "Re-running it just to be sure…",
    "Telling the worms apart from the dust…",
    "Spooling up the agar treadmill…",
    "Consulting the petri oracle…",
    # 2026-08-27: the pool doubled and the shuffle slowed. A line that changed
    # every three seconds read as a progress indicator, which it is not — the
    # line above it is. These are meant to be noticed once and then ignored.
    "Counting the ones that hold still.",
    "Nothing has gone wrong yet.",
    "Every worm is a 49-point polyline with opinions.",
    "Subtracting the illumination gradient…",
    "The rim of the plate is darker. We know. It's handled.",
    "Refusing an ambiguous crossing rather than swapping two animals.",
    "Two worms met. Neither will be renamed.",
    "Measuring in body lengths, because pixels lie across days.",
    "Deciding whether that stopped or just paused.",
    "A blank is not a zero.",
    "Reading the head-swing angle…",
    "Half-peaks count for half.",
    "Bridging a one-frame skeleton hiccup…",
    "That was an egg.",
    "That was definitely a scratch.",
    "Worm #12 has gone under the lawn.",
    "Following a track that would rather not be followed.",
    "This plate has more debris than worms. Filtering.",
    "Checking whether the fragment came back.",
    "Merging two fragments that were one animal.",
    "Splitting one fragment that was two animals.",
    "Docker is fine. Docker is always fine.",
    "22 GB of RAM, eight workers, one very slow worm.",
    "The container is thinking. Let it think.",
    "Reading HDF5 the long way round…",
    "Sorting by frame number, again.",
    "Nobody knows why this stage is the slow one.",
    "Intensity maps: expensive, occasionally worth it.",
    "Smoothing skeletons, which is not the same as smoothing data.",
    "Normalising by the plate, not by the worm.",
    "The median is doing the heavy lifting here.",
    "Averaging plates, not animals. It matters.",
    "One plate of 200 does not outvote one of 12.",
    "Computing something you will look at for four seconds.",
    "Building a figure nobody asked for but everybody wants.",
    "Preparing an explorer with more toggles than last time.",
    "Rounding to four places, reluctantly.",
    "Writing the CSV before the renders, having learned.",
    "Not overwriting yesterday's file.",
    "Still going. Genuinely.",
    "This is the part where you make tea.",
    "Long runs are the price of not guessing.",
    "You could be doing this by eye. You are not. Good.",
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _add_tooltip(widget: tk.Widget, text: str) -> None:
    tip: Optional[tk.Toplevel] = None

    def show(event: tk.Event) -> None:
        nonlocal tip
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 12}")
        tk.Label(
            tip, text=text, background="#ffffe0",
            relief="solid", borderwidth=1, font="TkSmallCaptionFont",
        ).pack()

    def hide(event: tk.Event) -> None:
        nonlocal tip
        if tip:
            tip.destroy()
            tip = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(ctk.CTkToplevel):
    def __init__(
        self,
        parent: tk.Tk,
        settings: cfg.Settings,
        on_save: Callable[[cfg.Settings], None],
    ) -> None:
        super().__init__(parent)
        self.title("WormScan Settings")
        self.configure(fg_color=theme.BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._current = settings
        self._on_save = on_save
        self._build(settings)
        self.update_idletasks()
        place_beside(self, parent, full_height=False)

    def _entry(self, parent: tk.Widget, **kw) -> ctk.CTkEntry:
        return ctk.CTkEntry(
            parent, fg_color=theme.BG, text_color=theme.TEXT,
            border_color=theme.HAIRLINE, border_width=1,
            corner_radius=theme.BTN_RADIUS, font=theme.body(), **kw
        )

    def _field_label(self, parent: tk.Widget, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text, font=theme.body(), text_color=theme.TEXT, anchor="w",
        ).pack(fill="x", pady=(0, 2))

    def _build(self, s: cfg.Settings) -> None:
        card = widgets.Card(self)
        card.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        body = card.content

        # Pi URL
        self._field_label(body, "Pi URL")
        self._pi_url = self._entry(body)
        self._pi_url.insert(0, s.pi_url)
        self._pi_url.pack(fill="x", pady=(0, 10))
        # Paste a full connection link here and both fields fill in. Handled on
        # FocusOut as well as on Save so the split is visible immediately -- a
        # silent transformation at save time looks like the paste was ignored.
        self._pi_url.bind("<FocusOut>", lambda _e: self._absorb_link())
        widgets.Tooltip(
            self._pi_url,
            "Base URL of the Pi capture service, e.g. http://192.168.50.2:8000\n"
            "You can also paste a full connection link containing the token.",
        )

        # Token (masked)
        self._field_label(body, "Token")
        self._token = self._entry(body, show="*")
        self._token.insert(0, s.token)
        self._token.pack(fill="x", pady=(0, 10))
        widgets.Tooltip(self._token, "Shared auth token, must match the Pi's .env")

        # Mirror folder — entry + "…" picker
        self._field_label(body, "Mirror folder")
        mirror_row = ctk.CTkFrame(body, fg_color="transparent")
        mirror_row.pack(fill="x", pady=(0, 10))
        self._mirror = self._entry(mirror_row)
        self._mirror.insert(0, s.mirror_root)
        self._mirror.pack(side="left", fill="x", expand=True)
        browse_btn = widgets.secondary_button(mirror_row, "…", self._browse)
        browse_btn.configure(width=36)
        browse_btn.pack(side="left", padx=(6, 0))
        widgets.Tooltip(self._mirror, "Local folder where synced Pi data is mirrored")

        # Poll interval
        self._field_label(body, "Poll interval (s)")
        self._poll = self._entry(body, width=90)
        self._poll.insert(0, str(s.poll_interval_s))
        self._poll.pack(anchor="w", pady=(0, 10))
        widgets.Tooltip(
            self._poll, "Seconds between automatic sync checks (minimum 10)"
        )

        # Check for updates
        self._check_updates = tk.BooleanVar(value=bool(
            getattr(s, "check_for_updates", True)))
        ctk.CTkCheckBox(
            body, text="Check for updates on startup",
            variable=self._check_updates,
            font=theme.body(), text_color=theme.TEXT,
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        ).pack(anchor="w", pady=(0, 10))

        # Read-only log-path caption
        log_path = cfg.APP_DATA / "launcher.log"
        ctk.CTkLabel(
            body, text=f"Log: {log_path}", font=theme.caption(),
            text_color=theme.TEXT_2, anchor="w", justify="left",
        ).pack(fill="x")

        # Licence + source. Free software should say so somewhere a user can
        # see it, and the source URL is what makes the AGPL work in practice.
        lic = ctk.CTkLabel(
            body,
            text="WormScan " + paths.version_string()
                 + "  \u00b7  \u00a9 2026 Erasmus MC  \u00b7  GNU AGPL v3",
            font=theme.caption(), text_color=theme.TEXT_2,
            anchor="w", justify="left", cursor="hand2",
        )
        lic.pack(fill="x", pady=(6, 0))
        lic.bind("<Button-1>", lambda _e: webbrowser.open_new_tab(
            "https://github.com/davidtheadmin/celegans-imaging"))
        widgets.Tooltip(lic, "Free software under the GNU AGPL v3.\n"
                             "Click to open the source code on GitHub.")

        # Footer — Save (primary) / Cancel (secondary)
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=16, pady=(0, 16))
        widgets.primary_button(footer, "Save", self._save).pack(
            side="right", padx=(8, 0)
        )
        widgets.secondary_button(footer, "Cancel", self.destroy).pack(side="right")
        copy_btn = widgets.secondary_button(footer, "Copy link", self._copy_link)
        copy_btn.pack(side="left")
        widgets.Tooltip(
            copy_btn,
            "Copy a single link containing the Pi address AND the token.\n"
            "Send it to someone setting up WormScan: they paste it into the\n"
            "Pi URL box and both fields fill in. Treat it like a password.",
        )

    def _absorb_link(self) -> None:
        """If the Pi URL box holds a full link, split it across both fields."""
        url, token = cfg.parse_connection(self._pi_url.get())
        if not url:
            return
        if url != self._pi_url.get().strip():
            self._pi_url.delete(0, tk.END)
            self._pi_url.insert(0, url)
        if token:
            self._token.delete(0, tk.END)
            self._token.insert(0, token)

    def _copy_link(self) -> None:
        """Put a one-string connection link on the clipboard."""
        url, token_in_url = cfg.parse_connection(self._pi_url.get())
        token = (token_in_url or self._token.get()).strip()
        if not url or not token:
            messagebox.showinfo(
                "Nothing to copy",
                "Fill in the Pi URL and the token first.",
                parent=self,
            )
            return
        # quote(): a token is a random string and may contain '+', '/' or '='
        # (base64 alphabet). Unencoded, '+' decodes back as a SPACE, so the
        # link would hand over a subtly different token and auth would fail
        # with no clue why.
        self.clipboard_clear()
        self.clipboard_append(f"{url}/?token={quote(token, safe='')}")
        self.update()
        messagebox.showinfo(
            "Connection link copied",
            "Send this link to whoever is setting up WormScan. They paste it "
            "into the Pi URL box in Settings and both fields fill in.\n\n"
            "It contains the access token, so treat it like a password - "
            "anyone holding it has full access to the imaging station.",
            parent=self,
        )

    def _browse(self) -> None:
        path = filedialog.askdirectory(initialdir=self._mirror.get() or "~")
        if path:
            self._mirror.delete(0, tk.END)
            self._mirror.insert(0, path)

    def _save(self) -> None:
        try:
            poll = int(self._poll.get())
            if poll < 10:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid input", "Poll interval must be an integer ≥ 10.", parent=self
            )
            return
        # Parse again here: the user may paste and click Save without the
        # field ever losing focus, in which case _absorb_link never ran.
        pi_url, token_in_url = cfg.parse_connection(self._pi_url.get())
        new = replace(
            self._current,
            pi_url=pi_url or self._pi_url.get().rstrip("/"),
            token=(token_in_url or self._token.get()).strip(),
            mirror_root=self._mirror.get(),
            poll_interval_s=poll,
            check_for_updates=bool(self._check_updates.get()),
        )
        self._on_save(new)
        self.destroy()


# ---------------------------------------------------------------------------
# Analysis progress dialog (modeless)
# ---------------------------------------------------------------------------

class AnalysisProgressDialog(ctk.CTkToplevel):
    """
    Modeless progress window that tracks an analysis run in real time.
    Polls the status object at 200ms. Auto-closes when running becomes False.
    Works with either the motility or crawling agent/status (identical interface).
    """

    def __init__(
        self,
        parent: tk.Tk,
        agent: "MotilityAgent | CrawlingAgent | CountingAgent | SurvivalAgent",
        status: "MotilityStatus | CrawlingStatus | CountingStatus | SurvivalStatus",
        title: str = "WormScan Analysis",
        noun: str = "video",
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        # Not modal — no grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._agent = agent
        self._status = status
        self._noun = noun
        # Shuffled once per dialog rather than walked in file order: the list
        # is authored, so in order it plays the same opening three lines at the
        # start of every run and the later two thirds are never seen on a short
        # one.
        self._flavour_order = list(range(len(_FLAVOUR_TEXTS)))
        random.shuffle(self._flavour_order)
        self._flavour_pos = 0
        self._flavour_tick = 0
        self._build()
        # Beside the launcher, not full height: this window is six lines tall
        # and stretching it to the screen would be absurd.
        self.update_idletasks()
        place_beside(self, parent, full_height=False)
        self.after(_PROGRESS_POLL_MS, self._poll)

    def _build(self) -> None:
        self.configure(fg_color=theme.BG)
        card = widgets.Card(self)
        card.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        body = card.content

        self._video_lbl = ctk.CTkLabel(
            body, text="Starting…", font=theme.body(), text_color=theme.TEXT,
            anchor="w", width=380, justify="left",
        )
        self._video_lbl.pack(fill="x", pady=(0, 8))
        self._video_tip = widgets.Tooltip(self._video_lbl, "")

        self._bar = widgets.ProgressBar(body, mode="determinate")
        self._bar.pack(fill="x", pady=(0, 8))

        self._stage_lbl = ctk.CTkLabel(
            body, text="", font=theme.caption(), text_color=theme.TEXT_2,
            anchor="w", justify="left",
        )
        self._stage_lbl.pack(fill="x")

        # What the run is DOING, live — Tierpsy's own checkpoint names, counted
        # across the worker pool. This is the line to read; the flavour line
        # below it is decoration and should not be mistaken for progress, which
        # is why it sits lower, dimmer and changes slowly.
        self._detail_lbl = ctk.CTkLabel(
            body, text="", font=theme.caption(), text_color=theme.TEXT,
            anchor="w", justify="left",
        )
        self._detail_lbl.pack(fill="x", pady=(1, 0))

        self._flavour_lbl = ctk.CTkLabel(
            body, text=_FLAVOUR_TEXTS[self._flavour_order[0]],
            font=theme.caption(),
            text_color=theme.TEXT_2, anchor="w", justify="left",
        )
        self._flavour_lbl.pack(fill="x", pady=(6, 0))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=16, pady=(0, 16))
        widgets.secondary_button(footer, "Cancel", self._on_cancel).pack(side="right")

    def _poll(self) -> None:
        snap = self._status.snapshot()

        if not snap.running:
            self.destroy()
            return

        total = snap.total
        # ProgressBar.update handles the edge cases the old ttk code did:
        # total==0 → empty, current==total → full.
        self._bar.update(snap.current_index, total)
        if total > 0:
            if snap.current_index < total:
                base = widgets.middle_truncate(snap.current_basename, 36)
                self._video_lbl.configure(
                    text=f"{self._noun.capitalize()} {snap.current_index + 1} of {total}: {base}"
                )
                self._video_tip.set_text(snap.current_basename)
            else:
                self._video_lbl.configure(text=f"Finishing… ({total} of {total} done)")
                self._video_tip.set_text("")

        self._stage_lbl.configure(text=snap.current_stage)
        # Pipelines that do not report per-worker phases simply leave this
        # empty; getattr keeps this dialog working for all four assays off one
        # snapshot shape.
        self._detail_lbl.configure(text=getattr(snap, "stage_detail", "") or "")

        self._flavour_tick += 1
        if self._flavour_tick >= _FLAVOUR_TICKS:
            self._flavour_tick = 0
            self._flavour_pos += 1
            if self._flavour_pos >= len(self._flavour_order):
                # Reshuffle rather than loop: on a 20-hour run the same order
                # twice is more noticeable than no order at all.
                random.shuffle(self._flavour_order)
                self._flavour_pos = 0
            self._flavour_lbl.configure(
                text=_FLAVOUR_TEXTS[self._flavour_order[self._flavour_pos]])

        self.after(_PROGRESS_POLL_MS, self._poll)

    def _on_cancel(self) -> None:
        self._agent.cancel()
        self.destroy()


# ---------------------------------------------------------------------------
# Analysis setup dialog
# ---------------------------------------------------------------------------

class AnalysisDialog(ctk.CTkToplevel):
    _WIDTH = 520
    # Segment labels are capitalised for display; _on_segment maps each back to
    # the canonical mode string the agents/_start expect.
    # Order here IS the left-to-right order of the segmented button. The three
    # worm assays come first because they are what a run is usually about;
    # Colony Survival is a mammalian-cell assay and sits last.
    _MODE_LABELS = {
        "Motility": "motility",
        "Crawling": "crawling",
        # User-facing rename only. The canonical mode string, the agent class
        # and the config fields all still say "survival" — see survival.py.
        "Development": "survival",
        "Colony Survival": "counting",
    }

    def __init__(
        self,
        parent: tk.Tk,
        settings: cfg.Settings,
        motility_agent: MotilityAgent,
        motility_status: MotilityStatus,
        crawling_agent: CrawlingAgent,
        crawling_status: CrawlingStatus,
        counting_agent: CountingAgent,
        counting_status: CountingStatus,
        survival_agent: SurvivalAgent,
        survival_status: SurvivalStatus,
        on_settings_update: Callable[[cfg.Settings], None],
    ) -> None:
        super().__init__(parent)
        self.title("WormScan Analysis")
        self.configure(fg_color=theme.BG)
        # Width fixed, height ours. The options for one mode are taller than a
        # laptop screen, so the body scrolls and the Start/Cancel footer is
        # pinned — previously the button that runs the analysis could sit below
        # the bottom of the display with no way to reach it.
        self.resizable(False, True)
        self.transient(parent)
        self.grab_set()
        self._parent = parent
        self._settings = settings
        self._agent = motility_agent
        self._status = motility_status
        self._crawling_agent = crawling_agent
        self._crawling_status = crawling_status
        self._counting_agent = counting_agent
        self._counting_status = counting_status
        self._survival_agent = survival_agent
        self._survival_status = survival_status
        self._on_settings_update = on_settings_update
        self._build()
        self.update_idletasks()
        place_beside(self, parent, self._WIDTH)
        # Re-apply once the window manager has actually mapped the window.
        # Some WMs (and CustomTkinter's own deferred title-bar work) resize a
        # Toplevel just after it appears, which is enough to push a pinned
        # footer back under the taskbar.
        self.after(150, lambda: place_beside(self, parent, self._WIDTH))

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}

        # Footer first, packed to the bottom, so it owns its strip of the window
        # no matter how tall the options above it get.
        footer = ctk.CTkFrame(self, fg_color=theme.BG)
        footer.pack(side="bottom", fill="x")
        widgets.HairlineSeparator(footer).pack(fill="x")
        # Messages about the run appear HERE, in this window, rather than in a
        # pop-up. Two attempts at a pop-up flashed and vanished; a frame cannot.
        self._notice = widgets.InlineNotice(footer)
        self._footer_buttons = ctk.CTkFrame(footer, fg_color="transparent")
        btn_frame = self._footer_buttons
        btn_frame.pack(fill="x", pady=(8, 12))
        widgets.primary_button(btn_frame, "Start", self._start).pack(
            side="right", padx=(6, 14)
        )
        widgets.secondary_button(btn_frame, "Cancel", self.destroy).pack(
            side="right", padx=6
        )

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(side="top", fill="both", expand=True)
        form = ctk.CTkFrame(scroll, fg_color="transparent")
        form.pack(fill="both", expand=True)
        form.grid_columnconfigure(1, weight=1)
        self._form = form

        chk_kw = dict(
            font=theme.body(), text_color=theme.TEXT,
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        )

        # Row 0 — analysis type (segmented control; _mode keeps canonical values)
        ctk.CTkLabel(
            form, text="Analysis type", font=theme.body_bold(),
            text_color=theme.TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="w", **pad)
        self._mode = tk.StringVar(value="motility")
        self._mode_seg = ctk.CTkSegmentedButton(
            form, values=list(self._MODE_LABELS.keys()),
            command=self._on_segment, font=theme.body(),
            fg_color=theme.CARD, text_color=theme.TEXT,
            selected_color=theme.ACCENT, selected_hover_color=theme.ACCENT_HOVER,
            unselected_color=theme.CARD, unselected_hover_color=theme.HAIRLINE,
            corner_radius=theme.BTN_RADIUS,
        )
        self._mode_seg.set("Motility")
        self._mode_seg.grid(row=0, column=1, columnspan=2, sticky="w", **pad)

        # Row 1 — single folder picker. Every mode but Development uses it;
        # Development hides it and uses the folder LIST inside its own card,
        # because one run there spans several folders (one per timepoint).
        self._folder_label = ctk.CTkLabel(
            form, text="Video folder", font=theme.body_bold(),
            text_color=theme.TEXT, anchor="w",
        )
        self._folder_label.grid(row=1, column=0, sticky="w", **pad)
        self._folder_var = tk.StringVar(value=self._settings.mirror_root)
        self._folder_entry = ctk.CTkEntry(
            form, textvariable=self._folder_var, width=300,
            fg_color=theme.CARD, text_color=theme.TEXT,
            border_color=theme.HAIRLINE, border_width=1,
            corner_radius=theme.BTN_RADIUS, font=theme.body(),
        )
        self._folder_entry.grid(row=1, column=1, sticky="ew", **pad)
        self._browse_btn = widgets.secondary_button(form, "…", self._browse)
        self._browse_btn.configure(width=36)
        self._browse_btn.grid(row=1, column=2, **pad)

        # Row 1 (alternate) — folder LIST for the two video assays. Several
        # folders make a timecourse: each folder is one timepoint, every worm
        # row is stamped with it, and the figures gain a time axis. One folder
        # is a list of one and behaves exactly as the single picker did.
        # Colony Survival keeps the single picker above.
        self._video_folder_box = ctk.CTkFrame(form, fg_color="transparent")
        ctk.CTkLabel(
            self._video_folder_box,
            text="Video folders — one per timepoint",
            font=theme.body_bold(), text_color=theme.TEXT, anchor="w",
        ).pack(anchor="w", pady=(0, 4))
        self._video_folders = widgets.FolderTimeList(
            self._video_folder_box, max_rows=6)
        self._video_folders.pack(fill="x", pady=(0, 4))
        _vbtns = ctk.CTkFrame(self._video_folder_box, fg_color="transparent")
        _vbtns.pack(anchor="w", fill="x", pady=(0, 2))
        widgets.secondary_button(
            _vbtns, "Add folder…", self._video_add_folder).pack(side="left")
        widgets.secondary_button(
            _vbtns, "Remove selected", self._video_remove_folder,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            self._video_folder_box,
            text=("Leave the timepoint blank to read it from the video "
                  "filenames. Results are written into the FIRST folder. "
                  "Folders already analysed with the same settings are reused, "
                  "not re-analysed."),
            font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
            justify="left", wraplength=440,
        ).pack(anchor="w")

        # Min fragment length (s) is built inside the motility Card (see below)
        # so it shows/hides with that card. _start still reads _threshold_var.
        self._threshold_var = tk.StringVar(
            value=str(self._settings.motility_long_threshold_s)
        )

        # Row 4 — clear-cache checkbox
        self._clear_cache_var = tk.BooleanVar(value=False)
        self._clear_cache_check = ctk.CTkCheckBox(
            form, text="Clear cache before run",
            variable=self._clear_cache_var, **chk_kw,
        )
        self._clear_cache_check.grid(
            row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4)
        )

        # Row 5 — render options (motility): unchanged motility binding
        self._motility_render_frame = widgets.Card(form, title="Video render options")
        render_frame = self._motility_render_frame.content
        self._want_tracked = tk.BooleanVar(value=False)
        self._want_curvature = tk.BooleanVar(value=False)
        self._want_sidebyside = tk.BooleanVar(value=False)
        self._want_per_worm_traces = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            render_frame, text="Tracked (skeleton + worm IDs)",
            variable=self._want_tracked, **chk_kw,
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(
            render_frame, text="Curvature (red = positive, blue = negative)",
            variable=self._want_curvature, **chk_kw,
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(
            render_frame, text="Side-by-side (original | masked + tracked)",
            variable=self._want_sidebyside, **chk_kw,
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(
            render_frame,
            text="Per-worm curvature traces (PNG + MP4 per fully-tracked worm)",
            variable=self._want_per_worm_traces, **chk_kw,
        ).pack(anchor="w", pady=2)
        ctk.CTkLabel(
            render_frame,
            text="Adds 30–90 s render time per video per option.",
            font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        # Min fragment length — motility quality filter; mirrors the Crawling
        # card's 'Min track span'. Lives in the card so it shows/hides with it.
        _min_frag_row = ctk.CTkFrame(render_frame, fg_color="transparent")
        _min_frag_row.pack(anchor="w", fill="x", pady=(6, 0))
        self._threshold_label = ctk.CTkLabel(
            _min_frag_row, text="Min fragment length (s)", font=theme.body(),
            text_color=theme.TEXT, anchor="w",
        )
        self._threshold_label.pack(side="left")
        widgets.Tooltip(
            self._threshold_label,
            "Worm tracks shorter than this don't count toward the summary statistics.",
        )
        self._threshold_spin = widgets.Spin(
            _min_frag_row, self._threshold_var,
            from_=1.0, to=30.0, increment=0.5, fmt="%.1f",
        )
        self._threshold_spin.pack(side="left", padx=(6, 0))
        self._threshold_help = ctk.CTkLabel(
            render_frame,
            text="Recommended: 5–10 s.  Higher = stricter but biases toward slower worms.",
            font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
            justify="left", wraplength=360,
        )
        self._threshold_help.pack(anchor="w", pady=(2, 0))
        self._reset_button(render_frame, self._reset_motility)

        # Row 5 — render options (crawling): tracking, side-by-side, path traces
        self._crawling_render_frame = widgets.Card(form, title="Video render options")
        crawl_frame = self._crawling_render_frame.content
        self._crawl_tracked = tk.BooleanVar(value=False)
        self._crawl_sidebyside = tk.BooleanVar(value=False)
        self._crawl_path_traces = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            crawl_frame, text="Tracked (skeleton + worm IDs)",
            variable=self._crawl_tracked, **chk_kw,
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(
            crawl_frame, text="Side-by-side (original | masked + tracked)",
            variable=self._crawl_sidebyside, **chk_kw,
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(
            crawl_frame, text="Path traces (fading centroid trails)",
            variable=self._crawl_path_traces, **chk_kw,
        ).pack(anchor="w", pady=2)
        ctk.CTkLabel(
            crawl_frame,
            text="Adds 1–3 min render time per video per option.",
            font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        # Min track span — quality filter; renders show only passing worms.
        _min_track_row = ctk.CTkFrame(crawl_frame, fg_color="transparent")
        _min_track_row.pack(anchor="w", fill="x", pady=(6, 0))
        _min_track_lbl = ctk.CTkLabel(
            _min_track_row, text="Min track length (s)", font=theme.body(),
            text_color=theme.TEXT, anchor="w",
        )
        _min_track_lbl.pack(side="left")
        widgets.Tooltip(
            _min_track_lbl,
            "Tracks shorter than this are dropped from aggregation. This is now "
            "the ONLY quality gate — the old hidden 70% skeleton-coverage "
            "requirement has been removed, and coverage is reported per track "
            "in the per_worm sheet instead. One animal may yield several tracks "
            "when it passes another worm, which is intended: n counts tracks, "
            "not animals.",
        )
        self._crawl_min_track = tk.StringVar(
            value=str(int(getattr(self._settings, "crawling_min_track_s", 10)))
        )
        widgets.Spin(
            _min_track_row, self._crawl_min_track,
            from_=1, to=600, increment=5, fmt="%.0f",
        ).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            crawl_frame,
            text="Tracks shorter than this are dropped from the aggregate and not drawn. Coverage is reported, not gated.",
            font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
            justify="left", wraplength=360,
        ).pack(anchor="w", pady=(2, 0))
        self._reset_button(crawl_frame, self._reset_crawling)

        # Row 5 — counting options: the two prominent tuning knobs. Everything
        # else uses counting.py defaults.
        self._counting_frame = widgets.Card(form, title="Colony Survival options")
        count_frame = self._counting_frame.content
        _split_row = ctk.CTkFrame(count_frame, fg_color="transparent")
        _split_row.pack(anchor="w", fill="x")
        ctk.CTkLabel(
            _split_row, text="Split sensitivity", font=theme.body_bold(),
            text_color=theme.TEXT, anchor="w",
        ).pack(side="left")
        self._count_split = tk.StringVar(
            value=f"{float(getattr(self._settings, 'counting_split_sensitivity', 3.0)):.1f}"
        )
        widgets.Spin(
            _split_row, self._count_split,
            from_=0.5, to=20.0, increment=0.5, fmt="%.1f",
        ).pack(side="left", padx=(6, 0))

        _mincol_row = ctk.CTkFrame(count_frame, fg_color="transparent")
        _mincol_row.pack(anchor="w", fill="x", pady=(6, 0))
        ctk.CTkLabel(
            _mincol_row, text="Min colony diameter (µm)", font=theme.body_bold(),
            text_color=theme.TEXT, anchor="w",
        ).pack(side="left")
        self._count_min_um = tk.StringVar(
            value=f"{float(getattr(self._settings, 'counting_min_colony_um', 200.0)):.0f}"
        )
        widgets.Spin(
            _mincol_row, self._count_min_um,
            from_=0.0, to=2000.0, increment=50.0, fmt="%.0f",
        ).pack(side="left", padx=(6, 0))

        # Detection sensitivity: the automatic (Otsu) threshold adapts to each
        # plate, but on a sparse plate it still lands above the faint colonies
        # and only their dense centres survive. This dial scales that threshold
        # — 5 leaves it exactly where it has always been, so an untouched
        # install counts as it did before this slider existed.
        _sens_row = ctk.CTkFrame(count_frame, fg_color="transparent")
        _sens_row.pack(anchor="w", fill="x", pady=(8, 0))
        ctk.CTkLabel(
            _sens_row, text="Detection sensitivity", font=theme.body_bold(),
            text_color=theme.TEXT, anchor="w",
        ).pack(side="left")
        self._count_sens = tk.DoubleVar(
            value=float(getattr(self._settings, "counting_sensitivity", 5.0))
        )
        self._count_sens_label = ctk.CTkLabel(
            _sens_row, text=f"{self._count_sens.get():.1f}", font=theme.body(),
            text_color=theme.TEXT_2, width=32,
        )
        ctk.CTkSlider(
            _sens_row, from_=0.0, to=10.0, number_of_steps=20,
            variable=self._count_sens,
            command=lambda v: self._count_sens_label.configure(
                text=f"{float(v):.1f}"),
            fg_color=theme.CARD, progress_color=theme.ACCENT,
            button_color=theme.ACCENT, button_hover_color=theme.ACCENT_HOVER,
            width=150,
        ).pack(side="left", padx=(8, 6))
        self._count_sens_label.pack(side="left")

        # Colony smoothing: colonies that grow as loose, feathery clusters are
        # not solid discs. Thresholding that texture at full resolution
        # splinters one colony into dozens of fragments. Blurring the detection
        # map first at roughly one colony-feature width puts it back together.
        # 0 = off, which is what the pipeline did before this knob.
        _smooth_row = ctk.CTkFrame(count_frame, fg_color="transparent")
        _smooth_row.pack(anchor="w", fill="x", pady=(6, 0))
        ctk.CTkLabel(
            _smooth_row, text="Colony smoothing (µm)", font=theme.body_bold(),
            text_color=theme.TEXT, anchor="w",
        ).pack(side="left")
        self._count_smooth = tk.StringVar(
            value=f"{float(getattr(self._settings, 'counting_smooth_um', 0.0)):.0f}"
        )
        widgets.Spin(
            _smooth_row, self._count_smooth,
            from_=0.0, to=1000.0, increment=25.0, fmt="%.0f",
        ).pack(side="left", padx=(6, 0))

        # Fixed threshold. The automatic (Otsu) threshold is derived from each
        # plate separately, which makes every plate its own reference — fine for
        # reading one image, wrong for a dose-response, where the entire question
        # is how plates compare. Ticking this applies ONE optical-density level
        # to every plate in the run, so the numbers are comparable across
        # conditions. Off by default: existing runs keep their behaviour.
        self._count_fixed = tk.BooleanVar(
            value=(getattr(self._settings, "counting_threshold_mode", "otsu")
                   == "fixed")
        )
        ctk.CTkCheckBox(
            count_frame,
            text="Same threshold for every plate (for dose series)",
            variable=self._count_fixed, command=lambda: self._sync_counting_mode(),
            **chk_kw,
        ).pack(anchor="w", pady=(8, 0))

        self._count_od_row = ctk.CTkFrame(count_frame, fg_color="transparent")
        self._count_od_row.pack(anchor="w", fill="x", pady=(4, 0))
        ctk.CTkLabel(
            self._count_od_row, text="Stain threshold (OD)", font=theme.body_bold(),
            text_color=theme.TEXT, anchor="w",
        ).pack(side="left")
        self._count_od = tk.StringVar(
            value=f"{float(getattr(self._settings, 'counting_od_threshold', 0.05)):.3f}"
        )
        widgets.Spin(
            self._count_od_row, self._count_od,
            from_=0.005, to=1.0, increment=0.01, fmt="%.3f",
        ).pack(side="left", padx=(6, 0))

        # Where the well comes from. The colony screen in the capture UI draws
        # an aim circle and the operator frames the well to it, so by analysis
        # time the well's position is already known: centred, radius 0.35 x the
        # short side. Detecting it again from a stained image is re-deriving
        # something we know from the least reliable part of the picture, and a
        # slipped fit corrupts both the mask and the micrometre scale at once.
        self._count_autowell = tk.BooleanVar(
            value=(getattr(self._settings, "counting_well_mode", "aim")
                   == "auto")
        )
        ctk.CTkCheckBox(
            count_frame,
            text="Find the well in the image instead (older behaviour)",
            variable=self._count_autowell,
            **chk_kw,
        ).pack(anchor="w", pady=(8, 0))

        widgets.HairlineSeparator(count_frame).pack(fill="x", pady=(10, 8))
        widgets.HelpBlock(count_frame, [
            ("Split sensitivity",
             "How eagerly a touching clump is cut into separate colonies. "
             "HIGHER = fewer splits, so big colonies stay whole. Lower it if "
             "one colony is being counted as several."),
            ("Min colony diameter",
             "Anything smaller than this is treated as a speck and not "
             "counted. Raise it if dust and scratches are appearing as "
             "colonies."),
            ("Detection sensitivity",
             "5 leaves the automatic threshold exactly where it has always "
             "been. Raise it when a sparse or faint plate comes out empty, or "
             "when only the dense centres of colonies get outlined. It has no "
             "effect at all when \"Same threshold for every plate\" is ticked."),
            ("Colony smoothing",
             "Blurs the detection map before thresholding, so a feathery, "
             "loose colony counts as one object instead of dozens of "
             "fragments. 0 is off; try about 100 µm for loose mammalian "
             "colonies."),
            ("Same threshold for every plate",
             "Tick this whenever you are COMPARING conditions. The automatic "
             "threshold is computed per plate, which makes every plate its own "
             "reference — fine for reading one image, wrong for a dose series, "
             "because a sparse plate and a dense one are then measured against "
             "different cuts and their numbers do not compare."),
            ("Find the well in the image instead",
             "Leave this OFF for plates captured through the colony screen. "
             "The aim circle you framed the well to IS the well — centred, and "
             "the same size on every plate — so the mask and the µm scale "
             "cannot drift between plates. Tick it only for stills that were "
             "not captured that way; then the rim is fitted per image, and a "
             "plate whose rim cannot be found is skipped entirely."),
            (None,
             "After any run, open overlays/ and check that what was outlined "
             "is what you would have counted."),
        ], wraplength=440).pack(anchor="w", fill="x")
        self._reset_button(count_frame, self._reset_counting)
        self._sync_counting_mode()

        # Row 5 — Development options: the folder list, one confidence
        # slider PER STAGE CLASS, the class-confidence correction,
        # plus save-previews. Staging inference runs in the vision venv; no
        # cache/render/threshold.
        #
        # Why per class: the survivor cutoff sits on the L2/L3 boundary, which
        # is exactly where the model is weakest, so a single global threshold
        # cannot be tightened on the confusable classes without also throwing
        # away confident calls on the easy ones.
        #
        # Defaults come from launcher/vision/stage_conf.json — the same file
        # infer_stage.py reads when nothing is passed — so these sliders start
        # on the values the "Analyze on laptop" button already uses. The class
        # list comes from that file too (this venv cannot load the model).
        self._survival_frame = widgets.Card(form, title="Development options")
        surv_frame = self._survival_frame.content
        _WRAP = 440

        # --- folder list (one folder per timepoint) ------------------------
        ctk.CTkLabel(
            surv_frame, text="Image folders — one per timepoint",
            font=theme.body_bold(), text_color=theme.TEXT, anchor="w",
        ).pack(anchor="w", pady=(0, 4))
        self._surv_folders = widgets.FolderTimeList(surv_frame, max_rows=4)
        self._surv_folders.pack(fill="x", pady=(0, 4))
        _fbtns = ctk.CTkFrame(surv_frame, fg_color="transparent")
        _fbtns.pack(anchor="w", fill="x", pady=(0, 2))
        widgets.secondary_button(
            _fbtns, "Add folder…", self._surv_add_folder).pack(side="left")
        widgets.secondary_button(
            _fbtns, "Remove selected", self._surv_remove_folder,
        ).pack(side="left", padx=(8, 0))
        widgets.HelpBlock(surv_frame, [
            ("Timepoint (h)",
             "Type the elapsed hours beside a folder, or leave it blank to "
             "read the capture times out of the image filenames. A folder "
             "with neither stops the run rather than quietly landing at 0 h."),
            (None,
             "One folder is a list of one. Results are written into the FIRST "
             "folder in the list."),
        ], wraplength=_WRAP).pack(anchor="w", fill="x", pady=(2, 6))

        widgets.HairlineSeparator(surv_frame).pack(fill="x", pady=(4, 6))

        # --- per-stage confidence, folded away by default ------------------
        #
        # Six sliders is the tallest block in this dialog and the one people
        # touch least; open it when you mean to, not every time you open the
        # window.
        self._surv_defaults = default_class_conf()
        saved = dict(getattr(self._settings, "survival_class_conf", None) or {})
        self._surv_conf_vars: dict[str, tk.DoubleVar] = {}
        self._surv_conf_labels: dict[str, ctk.CTkLabel] = {}

        if not self._surv_defaults:
            # stage_conf.json missing or unreadable: say so rather than drawing
            # an empty card. The run still works — infer_stage.py falls back to
            # its own uniform default — it just isn't tunable from here.
            ctk.CTkLabel(
                surv_frame,
                text="Per-class thresholds unavailable "
                     "(launcher/vision/stage_conf.json missing or unreadable). "
                     "The run will use the inference script's built-in default.",
                font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
                justify="left", wraplength=_WRAP,
            ).pack(anchor="w")
        else:
            n = len(self._surv_defaults)
            self._surv_conf_section = widgets.Collapsible(
                surv_frame, "Confidence per stage",
                subtitle=f"{n} slider{'' if n == 1 else 's'} · defaults are "
                         "usually right",
                expanded=False,
            )
            self._surv_conf_section.pack(anchor="w", fill="x")
            sliders = self._surv_conf_section.content

            widgets.HelpBlock(sliders, [
                (None,
                 "The minimum score a detection of that stage needs before it "
                 "is counted at all. Raising one drops its uncertain calls "
                 "from the counts ENTIRELY — it does not reassign them to a "
                 "neighbouring stage. Defaults come from "
                 "vision/stage_conf.json, the same file the \"Analyze on "
                 "laptop\" button uses, so leaving them alone keeps the two "
                 "paths identical."),
            ], wraplength=_WRAP - 20).pack(anchor="w", fill="x", pady=(2, 6))

            for stage, default in self._surv_defaults.items():
                row = ctk.CTkFrame(sliders, fg_color="transparent")
                row.pack(anchor="w", fill="x", pady=1)
                ctk.CTkLabel(
                    row, text=stage, font=theme.body(), text_color=theme.TEXT,
                    anchor="w", width=90,
                ).pack(side="left")
                var = tk.DoubleVar(value=float(saved.get(stage, default)))
                val_label = ctk.CTkLabel(
                    row, text=f"{var.get():.2f}", font=theme.body(),
                    text_color=theme.TEXT_2, width=40,
                )
                # Bind the label per row: a shared handler would close over the
                # loop variable and every slider would rewrite the last label.
                ctk.CTkSlider(
                    row, from_=0.05, to=0.90, number_of_steps=85,
                    variable=var,
                    command=lambda v, lbl=val_label: lbl.configure(
                        text=f"{float(v):.2f}"),
                    fg_color=theme.CARD, progress_color=theme.ACCENT,
                    button_color=theme.ACCENT,
                    button_hover_color=theme.ACCENT_HOVER,
                    width=170,
                ).pack(side="left", padx=(8, 6))
                val_label.pack(side="left")
                self._surv_conf_vars[stage] = var
                self._surv_conf_labels[stage] = val_label

            widgets.secondary_button(
                sliders, "Reset stage confidences", self._reset_survival_conf,
            ).pack(anchor="w", pady=(6, 2))

        widgets.HairlineSeparator(surv_frame).pack(fill="x", pady=(8, 6))

        # --- the three switches --------------------------------------------
        #
        # Class-confidence correction is a SWITCH, not a value: ticked passes no
        # alpha, so vision/stage_conf.json's number applies; unticked forces 0,
        # an exact no-op. Nothing here knows what the alpha is, which is what
        # keeps that file the single source of truth.
        self._surv_rescore = tk.BooleanVar(
            value=bool(getattr(self._settings, "survival_rescore", True))
        )
        ctk.CTkCheckBox(
            surv_frame,
            text="Correct for uneven class confidence  (recommended)",
            variable=self._surv_rescore, **chk_kw,
        ).pack(anchor="w", pady=(0, 0))

        # Eggs off by default: a plate is almost never a question about worms
        # AND eggs at once, and eggs carry no developmental stage either way.
        self._surv_count_eggs = tk.BooleanVar(
            value=bool(getattr(self._settings, "survival_count_eggs", False))
        )
        ctk.CTkCheckBox(
            surv_frame, text="Count eggs",
            variable=self._surv_count_eggs, **chk_kw,
        ).pack(anchor="w", pady=(6, 0))

        self._surv_save_preview = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            surv_frame, text="Save preview PNGs",
            variable=self._surv_save_preview, **chk_kw,
        ).pack(anchor="w", pady=(6, 0))

        # Off by default. Reuse is the normal path — it is what makes analysing
        # each timepoint as it arrives and combining them later cheap — and the
        # cache invalidates itself whenever a setting that changes the
        # detections changes. This is the escape hatch.
        self._surv_force = tk.BooleanVar(
            value=bool(getattr(self._settings, "survival_force_reanalyze", False))
        )
        ctk.CTkCheckBox(
            surv_frame, text="Re-analyse images even if results already exist",
            variable=self._surv_force, **chk_kw,
        ).pack(anchor="w", pady=(6, 0))

        widgets.HelpBlock(surv_frame, [
            ("Correct for uneven class confidence",
             "The stage classes are not scored on a common scale — the L2 "
             "detector almost never reports a high number even when it is "
             "right. This rebalances them before the final call. It RELABELS "
             "animals; it never changes how many were found."),
            ("Count eggs",
             "Off, eggs are not detected at all and the report says \"not "
             "counted\" rather than 0. Tick it for an egg-survival or "
             "bleached-egg experiment. Eggs never enter the stage index."),
            ("Save preview PNGs",
             "One image per frame with the boxes drawn on, for spot-checking. "
             "Useful once, slow every time. Ticking this also forces every "
             "image to be analysed again, because the previews are drawn "
             "during inference."),
            ("Re-analyse images even if results already exist",
             "Normally an image that a previous run already analysed is not "
             "sent through the model again — its detections are read back from "
             "that run. So you can analyse each timepoint as it comes off the "
             "microscope and then run them all together for the figures, and "
             "the combining run does almost no work. Tick this only to force a "
             "clean re-run."),
        ], wraplength=_WRAP).pack(anchor="w", fill="x", pady=(8, 0))

        widgets.HairlineSeparator(surv_frame).pack(fill="x", pady=(10, 6))
        widgets.HelpBlock(surv_frame, [
            ("Reusing earlier work",
             "Reuse is decided image by image, and it is dropped automatically "
             "whenever anything that changes which animals get detected "
             "changes — the per-stage confidences, the egg setting, the model "
             "file. The class-confidence correction is the exception: it can "
             "be switched on or off without re-analysing, because it only "
             "relabels detections that are already saved. The log says exactly "
             "what was reused and what was not."),
            ("What this run produces",
             "explorer.html (interactive, self-contained), four figures as "
             "PNGs, development_results.xlsx, and soft_stage_scores.csv — "
             "written into a _development_<timestamp> folder inside the first "
             "image folder."),
            ("What it measures",
             "Developmental stage per worm, tiled across the frame, reported "
             "as mean stage index, stage composition and body size per plate "
             "and condition. Survival % is in the workbook but in none of the "
             "figures — its denominator collapses in a dose experiment."),
        ], wraplength=_WRAP).pack(anchor="w", fill="x")
        self._reset_button(surv_frame, self._reset_development)

        # Show the render frame matching the selected analysis type.
        self._mode.trace_add("write", self._on_mode_change)
        self._on_mode_change()


    def _sync_counting_mode(self) -> None:
        """Show the OD box only when the fixed-threshold checkbox is ticked.

        Leaving a greyed-out absolute threshold on screen while the run is
        actually using a per-plate one is how someone ends up believing two
        plates were measured the same way when they were not.
        """
        if self._count_fixed.get():
            self._count_od_row.pack(anchor="w", fill="x", pady=(4, 0))
        else:
            self._count_od_row.pack_forget()

    # -- Reset to defaults --------------------------------------------------
    #
    # Every options card carries this button. These knobs persist between runs,
    # which is right for a setting and dangerous for a value nudged once during
    # one experiment: without a way back it is still there weeks later, quietly
    # shaping a run nobody meant to tune, and the only record of it is a number
    # in a dialog that looks like it has always said that.
    #
    # Defaults come from the Settings dataclass itself, not from config.json, so
    # this restores what a fresh install does rather than whatever was last
    # saved. Like every other control here it changes the dialog only — the
    # values are written to config.json when the run starts.
    def _default(self, name: str, fallback):
        for f in _dc_fields(type(self._settings)):
            if f.name != name:
                continue
            if f.default is not _MISSING:
                return f.default
            if f.default_factory is not _MISSING:      # noqa: B009
                return f.default_factory()
        return fallback

    def _reset_button(self, parent, command) -> None:
        widgets.HairlineSeparator(parent).pack(fill="x", pady=(10, 6))
        btn = widgets.secondary_button(parent, "Reset to defaults", command)
        btn.pack(anchor="w", pady=(0, 2))
        widgets.Tooltip(
            btn,
            "Puts every option on this card back to the value a fresh install "
            "starts with. Applies to this run; the values are saved when you "
            "start the analysis.",
        )

    def _reset_motility(self) -> None:
        self._threshold_var.set(
            str(self._default("motility_long_threshold_s", 5.0)))
        for var in (self._want_tracked, self._want_curvature,
                    self._want_sidebyside, self._want_per_worm_traces):
            var.set(False)

    def _reset_crawling(self) -> None:
        self._crawl_min_track.set(
            str(int(self._default("crawling_min_track_s", 10))))
        for var in (self._crawl_tracked, self._crawl_sidebyside,
                    self._crawl_path_traces):
            var.set(False)

    def _reset_counting(self) -> None:
        self._count_split.set(
            f"{float(self._default('counting_split_sensitivity', 3.0)):.1f}")
        self._count_min_um.set(
            f"{float(self._default('counting_min_colony_um', 200.0)):.0f}")
        sens = float(self._default("counting_sensitivity", 5.0))
        self._count_sens.set(sens)
        self._count_sens_label.configure(text=f"{sens:.1f}")
        self._count_smooth.set(
            f"{float(self._default('counting_smooth_um', 0.0)):.0f}")
        self._count_fixed.set(
            self._default("counting_threshold_mode", "otsu") == "fixed")
        self._count_od.set(
            f"{float(self._default('counting_od_threshold', 0.05)):.3f}")
        self._count_autowell.set(
            self._default("counting_well_mode", "aim") == "auto")
        # The OD box is only on screen while the fixed threshold is ticked, and
        # the reset can untick it.
        self._sync_counting_mode()

    def _reset_development(self) -> None:
        """Sliders back to stage_conf.json, switches back to their defaults.

        Save-preview is not a saved setting — it is a per-run choice that always
        starts unticked — so it resets to off with the rest.
        """
        self._reset_survival_conf()
        self._surv_rescore.set(bool(self._default("survival_rescore", True)))
        self._surv_count_eggs.set(
            bool(self._default("survival_count_eggs", False)))
        self._surv_save_preview.set(False)
        self._surv_force.set(
            bool(self._default("survival_force_reanalyze", False)))

    def _reset_survival_conf(self) -> None:
        """Put every per-stage slider back to the shared stage_conf.json value.

        Restores from the file that was read when the dialog opened, so this
        always agrees with what the Analyze-on-laptop button does, not with
        whatever was last saved into config.json.
        """
        for stage, default in self._surv_defaults.items():
            var = self._surv_conf_vars.get(stage)
            if var is None:
                continue
            var.set(float(default))
            self._surv_conf_labels[stage].configure(text=f"{float(default):.2f}")

    def _on_segment(self, label: str) -> None:
        """Map the capitalised segment label to its canonical mode string.

        Writing self._mode fires the same trace the radios fired, so
        _on_mode_change runs on every switch exactly as before.
        """
        self._mode.set(self._MODE_LABELS[label])

    def _browse(self) -> None:
        initial = self._folder_var.get() or self._settings.mirror_root
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            self._folder_var.set(path)

    # -- Motility / crawling folder list -----------------------------------
    def _video_add_folder(self) -> None:
        """One askdirectory() per Add: Tk has no multi-directory picker."""
        existing = self._video_folders.folders()
        initial = existing[-1] if existing else (
            self._folder_var.get() or self._settings.mirror_root)
        path = filedialog.askdirectory(initialdir=initial, parent=self)
        if not path:
            return
        if path in existing:
            messagebox.showinfo(
                "Already added",
                "That folder is already in the list.", parent=self)
            return
        self._video_folders.add(path)

    def _video_remove_folder(self) -> None:
        idx = self._video_folders.selected_index()
        if idx is None:
            messagebox.showinfo(
                "Nothing selected",
                "Click a folder in the list first, then Remove selected.",
                parent=self)
            return
        self._video_folders.remove(idx)

    # -- Development folder list -------------------------------------------
    def _surv_add_folder(self) -> None:
        """One askdirectory() per Add: Tk has no multi-directory picker."""
        existing = self._surv_folders.folders()
        initial = existing[-1] if existing else (
            self._folder_var.get() or self._settings.mirror_root)
        path = filedialog.askdirectory(initialdir=initial, parent=self)
        if not path:
            return
        if path in existing:
            messagebox.showinfo(
                "Already added",
                "That folder is already in the list.", parent=self)
            return
        self._surv_folders.add(path)

    def _surv_remove_folder(self) -> None:
        idx = self._surv_folders.selected_index()
        if idx is None:
            messagebox.showinfo(
                "Nothing selected",
                "Click a folder in the list first, then Remove selected.",
                parent=self)
            return
        self._surv_folders.remove(idx)

    def _on_mode_change(self, *_args) -> None:
        """Show the controls matching the selected analysis type.

        'Min fragment length (s)' now lives inside the motility Card (it is
        inert for crawling/counting), so it shows/hides with that card and needs
        no separate toggling here. Counting has no cache and no video render, so
        the clear-cache box and both render frames are hidden and a small
        two-knob options frame is shown instead.
        """
        mode = self._mode.get()
        # Reset the row-5 slot; the active branch re-grids what it needs.
        self._motility_render_frame.grid_remove()
        self._crawling_render_frame.grid_remove()
        self._counting_frame.grid_remove()
        self._survival_frame.grid_remove()
        self._video_folder_box.grid_remove()

        if mode == "counting":
            self._clear_cache_check.grid_remove()
            self._folder_label.grid(row=1, column=0, sticky="w", padx=12, pady=6)
            self._folder_entry.grid(row=1, column=1, sticky="ew", padx=12, pady=6)
            self._browse_btn.grid(row=1, column=2, padx=12, pady=6)
            self._counting_frame.grid(
                row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2)
            )
            return

        if mode == "survival":
            self._clear_cache_check.grid_remove()
            # The single folder row is meaningless here — Development takes a
            # LIST, and leaving a second, ignored folder box on screen is how
            # someone ends up running the wrong data.
            self._folder_label.grid_remove()
            self._folder_entry.grid_remove()
            self._browse_btn.grid_remove()
            self._survival_frame.grid(
                row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2)
            )
            return

        # motility / crawling: the folder LIST takes the row-1 slot
        self._folder_label.grid_remove()
        self._folder_entry.grid_remove()
        self._browse_btn.grid_remove()
        self._video_folder_box.grid(
            row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2))
        self._clear_cache_check.grid()
        if mode == "crawling":
            self._crawling_render_frame.grid(
                row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2)
            )
        else:
            self._motility_render_frame.grid(
                row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2)
            )

    def _show_notice(self, *args, **kw) -> None:
        """Put a message in this window's footer and keep it there."""
        self._notice.show(*args, wraplength=self._WIDTH - 90, **kw)
        self._notice.pack(fill="x", padx=14, pady=(8, 0),
                          before=self._footer_buttons)

    def _clear_notice(self) -> None:
        self._notice.hide()

    def _start_development(self) -> None:
        """Validate, then either launch or explain what would happen first.

        Two gates, in this order, because the second is the expensive one:
        folders must exist and hold images (survival_preflight), and every
        folder must have a timepoint we can defend (resolve_timepoints). A
        folder with neither a typed value nor a capture stamp is a hard stop —
        guessing would put a wrong x-axis on every figure and nothing in the
        output would reveal it.
        """
        self._clear_notice()
        entries = self._surv_folders.entries()
        folders = [Path(p) for p, _ in entries]

        errors = survival_preflight(folders)
        if errors:
            self._show_notice(
                "Cannot start", "\n\n".join(errors), accent=theme.DESTRUCTIVE,
                dismiss="OK")
            return

        plans = resolve_timepoints([(Path(p), t) for p, t in entries])
        tp_errors = [p.error for p in plans if p.error]
        if tp_errors:
            self._show_notice(
                "Every folder needs a timepoint",
                "\n\n".join(tp_errors), accent=theme.DESTRUCTIVE,
                dismiss="OK")
            return

        # Round to the slider's own resolution so config.json holds 0.30,
        # not 0.30000000000000004 from the DoubleVar.
        class_conf = {
            stage: round(float(var.get()), 2)
            for stage, var in self._surv_conf_vars.items()
        }
        save_previews = bool(self._surv_save_preview.get())
        count_eggs = bool(self._surv_count_eggs.get())
        rescore = bool(self._surv_rescore.get())
        force_reanalyze = bool(self._surv_force.get())
        # Resolve to an explicit list here so the checkbox always wins over
        # the stage_conf.json default, in both directions.
        exclude_classes = (
            [] if count_eggs else (default_exclude_classes() or ["egg"])
        )
        def launch(n_fresh: int = -1) -> None:
            self._launch_development(
                plans, class_conf=class_conf, save_previews=save_previews,
                exclude_classes=exclude_classes, count_eggs=count_eggs,
                rescore=rescore, force_reanalyze=force_reanalyze,
                n_fresh=n_fresh)

        # What would this run actually do? Say so before doing it — a run that
        # reuses everything finishes in seconds and is otherwise
        # indistinguishable from one that did nothing at all.
        try:
            reuse = plan_reuse(
                plans, class_conf, exclude_classes=exclude_classes,
                save_previews=save_previews, force_reanalyze=force_reanalyze)
        except Exception as exc:
            _log.warning("reuse preview failed", exc_info=True)
            self._show_notice(
                "Could not check for earlier results", str(exc),
                confirm="Analyse everything", on_confirm=launch,
                dismiss="Cancel", accent=theme.DESTRUCTIVE)
            return

        _log.info("development: %d image(s), %d reusable, %d to analyse",
                  reuse.n_images, reuse.n_reused, reuse.n_fresh)
        if not reuse.any_cached:
            launch(reuse.n_fresh)
            return

        n = len(plans)
        detail = "\n".join(reuse.folder_lines())
        if reuse.all_cached:
            self._show_notice(
                "Already analysed — nothing to re-run",
                f"All {reuse.n_images} image(s) in "
                f"{'this folder' if n == 1 else f'these {n} folders'} were "
                "analysed by an earlier run, so the model will not run again. "
                "The figures, the workbook and the explorer will still be "
                "rebuilt"
                + (" — combined across every folder in the list —" if n > 1
                   else "")
                + " from the saved detections, which takes a few seconds.",
                detail=detail, confirm="Build them now",
                on_confirm=lambda: launch(0), dismiss="Cancel")
        else:
            self._show_notice(
                f"{reuse.n_reused} of {reuse.n_images} images are already "
                "analysed",
                f"They will be reused; the other {reuse.n_fresh} still need "
                "analysing.",
                detail=detail, confirm="Start",
                on_confirm=lambda: launch(reuse.n_fresh), dismiss="Cancel")

    def _launch_development(self, plans, *, class_conf, save_previews,
                            exclude_classes, count_eggs, rescore,
                            force_reanalyze, n_fresh: int = -1) -> None:
        """Persist the settings and hand the run to the agent.

        ``n_fresh`` is how many images actually need the model. When it is zero
        NO progress window is opened: the run finishes in a couple of seconds,
        and a window that appears and closes again on its own is exactly what
        "the message just pops up briefly" turned out to be. The main window
        carries a notice instead, which stays put.
        """
        self._on_settings_update(replace(
            self._settings,
            survival_class_conf=class_conf,
            survival_count_eggs=count_eggs,
            survival_rescore=rescore,
            survival_force_reanalyze=force_reanalyze,
        ))
        if n_fresh != 0:
            AnalysisProgressDialog(
                self._parent, self._survival_agent, self._survival_status,
                title="WormScan Development Analysis", noun="plate",
            )
        else:
            notify = getattr(self._parent, "show_run_notice", None)
            if notify is not None:
                notify(
                    "Rebuilding results",
                    "No images need analysing. Building the figures, the "
                    "workbook and the explorer from the saved detections — "
                    "this takes a few seconds.",
                )
        _log.info("development: launching (%s)",
                  "no inference needed" if n_fresh == 0
                  else f"{n_fresh} image(s) to analyse")
        self._survival_agent.start_analysis(
            plans, class_conf=class_conf, save_previews=save_previews,
            exclude_classes=exclude_classes, rescore=rescore,
            force_reanalyze=force_reanalyze,
        )
        self.destroy()

    def _start(self) -> None:
        # Select the pipeline agent/status based on the chosen analysis type.
        if self._mode.get() == "crawling":
            agent = self._crawling_agent
            status = self._crawling_status
            progress_title = "WormScan Crawling Analysis"
        else:
            agent = self._agent
            status = self._status
            progress_title = "WormScan Motility Analysis"

        if (self._status.is_running() or self._crawling_status.is_running()
                or self._counting_status.is_running()
                or self._survival_status.is_running()):
            messagebox.showwarning(
                "Already running",
                "An analysis is already in progress.",
                parent=self,
            )
            return

        # Development: staging inference in the vision venv (subprocess),
        # aggregation / workbook / figures / explorer on this side. It takes a
        # LIST of folders, so it is handled before the single-folder check that
        # every other mode needs.
        if self._mode.get() == "survival":
            self._start_development()
            return

        video_mode = self._mode.get() in ("motility", "crawling")
        video_plans: list = []
        if video_mode:
            # The two video assays take a LIST of folders, one per timepoint.
            entries = self._video_folders.entries()
            if not entries:
                messagebox.showerror(
                    "No folders", "Add at least one video folder.", parent=self)
                return
            missing = [q for q, _t in entries if not Path(q).is_dir()]
            if missing:
                messagebox.showerror(
                    "Invalid folder",
                    "Folder not found:\n" + "\n".join(missing), parent=self)
                return
            from analysis.ffmpeg_utils import find_videos
            empty = [q for q, _t in entries if not find_videos(Path(q))]
            if empty:
                messagebox.showerror(
                    "No videos",
                    "No .mp4 files were found in:\n" + "\n".join(empty),
                    parent=self)
                return
            video_plans = resolve_timepoints(
                [(Path(q), t) for q, t in entries],
                discover=find_videos, kind="video",
                example="20260530T153913_video.mp4")
            tp_errors = [pl.error for pl in video_plans if pl.error]
            if tp_errors:
                messagebox.showerror(
                    "Every folder needs a timepoint",
                    "\n\n".join(tp_errors), parent=self)
                return
            folder = Path(video_plans[0].folder)
        else:
            folder = Path(self._folder_var.get().strip())
            if not folder.is_dir():
                messagebox.showerror(
                    "Invalid folder", f"Folder not found:\n{folder}", parent=self
                )
                return

        # Counting is pure-Python image analysis: no Docker/ffmpeg/threshold.
        # It validates its own two knobs, runs its own pre-flight, and starts.
        if self._mode.get() == "counting":
            try:
                split_sensitivity = float(self._count_split.get())
                if not (0.5 <= split_sensitivity <= 20.0):
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid split sensitivity",
                    "Split sensitivity must be a number between 0.5 and 20.0.",
                    parent=self,
                )
                return
            try:
                min_colony_um = float(self._count_min_um.get())
                if not (0.0 <= min_colony_um <= 2000.0):
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid colony size",
                    "Min colony diameter must be a number between 0 and 2000 µm.",
                    parent=self,
                )
                return

            sensitivity = round(float(self._count_sens.get()), 1)
            threshold_mode = "fixed" if self._count_fixed.get() else "otsu"
            well_mode = "auto" if self._count_autowell.get() else "aim"
            try:
                od_threshold = float(self._count_od.get())
                if not (0.0 < od_threshold <= 1.0):
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid stain threshold",
                    "Stain threshold must be a number between 0.005 and 1.0 "
                    "optical density.",
                    parent=self,
                )
                return
            try:
                smooth_um = float(self._count_smooth.get())
                if not (0.0 <= smooth_um <= 1000.0):
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid colony smoothing",
                    "Colony smoothing must be a number between 0 and 1000 µm "
                    "(0 = off).",
                    parent=self,
                )
                return

            errors = counting_preflight(folder)
            if errors:
                messagebox.showerror(
                    "Pre-flight checks failed",
                    "\n\n".join(errors),
                    parent=self,
                )
                return

            self._on_settings_update(replace(
                self._settings,
                counting_split_sensitivity=split_sensitivity,
                counting_min_colony_um=min_colony_um,
                counting_sensitivity=sensitivity,
                counting_smooth_um=smooth_um,
                counting_threshold_mode=threshold_mode,
                counting_od_threshold=od_threshold,
                counting_well_mode=well_mode,
            ))

            AnalysisProgressDialog(
                self._parent, self._counting_agent, self._counting_status,
                title="WormScan Counting Analysis", noun="plate",
            )
            self._counting_agent.start_analysis(
                folder,
                split_sensitivity=split_sensitivity,
                min_colony_um=min_colony_um,
                sensitivity=sensitivity,
                smooth_um=smooth_um,
                threshold_mode=threshold_mode,
                od_threshold=od_threshold,
                well_mode=well_mode,
            )
            self.destroy()
            return

        try:
            threshold_s = float(self._threshold_var.get())
            if not (1.0 <= threshold_s <= 30.0):
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid threshold",
                "Min fragment length must be a number between 1.0 and 30.0.",
                parent=self,
            )
            return

        min_span_s = 10.0
        if self._mode.get() == "crawling":
            try:
                min_span_s = float(self._crawl_min_track.get())
                if not (1.0 <= min_span_s <= 600.0):
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid track span",
                    "Min track span must be a number between 1 and 600 seconds.",
                    parent=self,
                )
                return

        errors = run_preflight(self._settings, folder)
        if errors:
            messagebox.showerror(
                "Pre-flight checks failed",
                "\n\n".join(errors),
                parent=self,
            )
            return

        # Persist the chosen threshold for next launch
        mode = self._mode.get()
        if mode == "motility":
            new_settings = replace(self._settings, motility_long_threshold_s=threshold_s)
        elif mode == "crawling":
            new_settings = replace(self._settings, crawling_min_track_s=int(min_span_s))
        else:
            new_settings = self._settings
        self._on_settings_update(new_settings)

        # Open progress dialog before waking the agent (so it's ready to poll)
        AnalysisProgressDialog(self._parent, agent, status, title=progress_title)

        if self._mode.get() == "crawling":
            agent.start_analysis(
                video_plans,
                threshold_s=threshold_s,
                clear_cache=self._clear_cache_var.get(),
                want_tracked=self._crawl_tracked.get(),
                want_sidebyside=self._crawl_sidebyside.get(),
                want_path_traces=self._crawl_path_traces.get(),
                min_span_s=min_span_s,
            )
        else:
            agent.start_analysis(
                video_plans,
                threshold_s=threshold_s,
                clear_cache=self._clear_cache_var.get(),
                want_tracked=self._want_tracked.get(),
                want_curvature=self._want_curvature.get(),
                want_sidebyside=self._want_sidebyside.get(),
                want_per_worm_traces=self._want_per_worm_traces.get(),
            )
        self.destroy()


# ---------------------------------------------------------------------------
# Review (grid viewer) — worker status + progress + helpers
# ---------------------------------------------------------------------------

class _ReviewStatus:
    """Thread-safe handoff between the generator worker thread and the UI.

    Worker thread: set_proc() then finish(). UI thread: snapshot() / terminate().
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = True
        self._returncode: Optional[int] = None
        self._html_path: Optional[str] = None
        self._stderr_tail = ""
        self._proc: Optional[subprocess.Popen] = None

    def set_proc(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc

    def finish(self, returncode: int, html_path: Optional[str], stderr_tail: str) -> None:
        with self._lock:
            self._running = False
            self._returncode = returncode
            self._html_path = html_path
            self._stderr_tail = stderr_tail

    def snapshot(self) -> tuple[bool, Optional[int], Optional[str], str]:
        with self._lock:
            return (self._running, self._returncode, self._html_path, self._stderr_tail)

    def terminate(self) -> None:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


class _ReviewProgressDialog(ctk.CTkToplevel):
    """Modeless 'Building viewer…' window with an indeterminate bar.

    Polls the status object at 200ms (UI thread only). On completion it closes
    and hands the result to on_done (also UI thread). Cancel terminates the
    child so it is never orphaned.
    """

    def __init__(self, parent: tk.Tk, status: _ReviewStatus, on_done) -> None:
        super().__init__(parent)
        self.title("WormScan Review")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._status = status
        self._on_done = on_done
        self._cancelled = False
        self._build()
        self.update_idletasks()
        place_beside(self, parent, full_height=False)
        self.after(_PROGRESS_POLL_MS, self._poll)

    def _build(self) -> None:
        self.configure(fg_color=theme.BG)
        card = widgets.Card(self)
        card.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        body = card.content

        ctk.CTkLabel(
            body, text="Building viewer…", font=theme.body(),
            text_color=theme.TEXT, anchor="w", width=360,
        ).pack(fill="x", pady=(0, 8))

        self._bar = widgets.ProgressBar(body, mode="indeterminate")
        self._bar.pack(fill="x", pady=(0, 8))
        self._bar.start()

        ctk.CTkLabel(
            body,
            text="A video build transcodes every clip — this can take minutes the first time.",
            font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
            justify="left", wraplength=340,
        ).pack(fill="x")

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=16, pady=(0, 16))
        widgets.secondary_button(footer, "Cancel", self._on_cancel).pack(side="right")

    def _poll(self) -> None:
        running, rc, html, stderr_tail = self._status.snapshot()
        if running:
            self.after(_PROGRESS_POLL_MS, self._poll)
            return
        self._bar.stop()
        self.destroy()
        if not self._cancelled:
            self._on_done(rc, html, stderr_tail)

    def _on_cancel(self) -> None:
        self._cancelled = True
        self._status.terminate()
        self._bar.stop()
        self.destroy()


def _parse_wrote_path(stdout: str) -> Optional[str]:
    """Pull the path from the generator's final 'Wrote <path>' stdout line."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("Wrote "):
            return stripped[len("Wrote "):].strip()
    return None


def _review_output_path(folders: list[str], video: bool) -> Optional[Path]:
    """Recompute the generator's default output path (fallback if stdout parse
    fails). Mirrors the scripts' day-sort + naming rule exactly."""
    try:
        dirs = [Path(p).resolve() for p in folders]
        dirs.sort(key=lambda d: (
            (0, m.group(1), d.name.lower()) if (m := _REVIEW_DATE_RE.match(d.name))
            else (1, "", d.name.lower())
        ))
        first = dirs[0]
        stem = first.name if len(dirs) == 1 else f"{first.name}__{len(dirs)}days"
        suffix = "_video_viewer.html" if video else "_viewer.html"
        return first.parent / f"{stem}{suffix}"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Review (grid viewer) dialog
# ---------------------------------------------------------------------------

class ReviewDialog(ctk.CTkToplevel):
    """Build an interactive grid viewer (stills or crawling clips) from one or
    more day folders, via the standalone generators in launcher/viewers/.

    Thread boundary mirrors the analysis dialogs: a worker thread runs the
    generator subprocess and writes to a lock-guarded status object; the UI
    thread polls via root.after() and is the only one to touch widgets,
    webbrowser, or messagebox.
    """

    def __init__(
        self,
        parent: tk.Tk,
        settings: cfg.Settings,
        on_settings_update: Callable[[cfg.Settings], None],
    ) -> None:
        super().__init__(parent)
        self.title("Review — Grid Viewer")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._parent = parent
        self._settings = settings
        self._on_settings_update = on_settings_update
        self._build()
        self.update_idletasks()
        place_beside(self, parent, full_height=False)

    def _build(self) -> None:
        self.configure(fg_color=theme.BG)
        self._folders: list[str] = []
        self._detect_cache: dict[str, tuple[Optional[str], int, int]] = {}
        self._resolved: Optional[str] = None

        default_type = getattr(self._settings, "review_type", "auto")
        default_loop = float(getattr(self._settings, "review_loop_s", 3.0))

        card = widgets.Card(self)
        card.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        body = card.content
        body.grid_columnconfigure(1, weight=1)
        pad = {"padx": 6, "pady": 6}

        # Row 0 — folder add/remove list (askdirectory returns one at a time)
        ctk.CTkLabel(
            body, text="Day folders", font=theme.body(), text_color=theme.TEXT,
            anchor="nw",
        ).grid(row=0, column=0, sticky="nw", **pad)
        self._folder_list = widgets.FolderList(body, height=5)
        self._folder_list.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

        fbtn = ctk.CTkFrame(body, fg_color="transparent")
        fbtn.grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(0, 4))
        widgets.secondary_button(fbtn, "Add folder…", self._add_folder).pack(side="left")
        widgets.secondary_button(
            fbtn, "Remove selected", self._remove_folder
        ).pack(side="left", padx=(8, 0))

        # Row 2 — content type radio (auto-detect default)
        ctk.CTkLabel(
            body, text="Content", font=theme.body(), text_color=theme.TEXT, anchor="w",
        ).grid(row=2, column=0, sticky="w", **pad)
        type_frame = ctk.CTkFrame(body, fg_color="transparent")
        type_frame.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        self._type = tk.StringVar(value=default_type)
        radio_kw = dict(
            variable=self._type, font=theme.body(), text_color=theme.TEXT,
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        )
        ctk.CTkRadioButton(
            type_frame, text="Pictures", value="pictures", **radio_kw
        ).pack(side="left")
        ctk.CTkRadioButton(
            type_frame, text="Videos", value="videos", **radio_kw
        ).pack(side="left", padx=(12, 0))
        ctk.CTkRadioButton(
            type_frame, text="Auto-detect", value="auto", **radio_kw
        ).pack(side="left", padx=(12, 0))

        # Row 3 — resolved-type label (e.g. "auto → videos")
        self._resolved_lbl = ctk.CTkLabel(
            body, text="", font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
        )
        self._resolved_lbl.grid(
            row=3, column=1, columnspan=2, sticky="w", padx=6, pady=(0, 4)
        )

        # Row 4 — loop length (videos only; hidden for pictures)
        self._loop_label = ctk.CTkLabel(
            body, text="Loop length (s)", font=theme.body(),
            text_color=theme.TEXT, anchor="w",
        )
        self._loop_var = tk.StringVar(value=f"{default_loop:.1f}")
        self._loop_spin = widgets.Spin(
            body, self._loop_var, from_=1.0, to=10.0, increment=0.5, fmt="%.1f",
        )

        # Footer — Start (primary) / Cancel (secondary)
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=16, pady=(0, 16))
        self._start_btn = widgets.primary_button(footer, "Start", self._start)
        self._start_btn.pack(side="right", padx=(8, 0))
        widgets.secondary_button(footer, "Cancel", self.destroy).pack(side="right")

        self._type.trace_add("write", lambda *_: self._refresh())
        self._refresh()

    # ------------------------------------------------------------------
    # Folder list
    # ------------------------------------------------------------------

    def _add_folder(self) -> None:
        path = filedialog.askdirectory(
            initialdir=str(self._settings.mirror_root), parent=self
        )
        if path:
            self._folders.append(path)
            self._folder_list.set_folders(self._folders)
            self._refresh()

    def _remove_folder(self) -> None:
        idx = self._folder_list.selected_index()
        if idx is None:
            return
        del self._folders[idx]
        self._folder_list.set_folders(self._folders)
        self._refresh()

    # ------------------------------------------------------------------
    # Type resolution
    # ------------------------------------------------------------------

    def _detect_type(self, folder: str) -> tuple[Optional[str], int, int]:
        """Scan a folder's condition subfolders; classify by file majority.

        Returns (resolved_type | None, n_videos, n_images). None when neither
        kind is found. Cached by folder path so radio toggles don't re-scan.
        """
        if folder in self._detect_cache:
            return self._detect_cache[folder]
        vids = imgs = 0
        root = Path(folder)
        try:
            subs = [
                p for p in root.iterdir()
                if p.is_dir() and not p.name.startswith("_")
                and p.name != _REVIEW_CACHE_DIRNAME
            ]
            for sub in subs:
                for f in sub.rglob("*"):
                    if _REVIEW_CACHE_DIRNAME in f.parts or not f.is_file():
                        continue
                    ext = f.suffix.lower()
                    if ext in _REVIEW_VIDEO_EXTS:
                        vids += 1
                    elif ext in _REVIEW_IMAGE_EXTS:
                        imgs += 1
        except OSError:
            pass
        resolved = None if (vids == 0 and imgs == 0) else (
            "videos" if vids > imgs else "pictures"
        )
        result = (resolved, vids, imgs)
        self._detect_cache[folder] = result
        return result

    def _refresh(self) -> None:
        """Recompute resolved type, label, spinbox visibility, Start state."""
        choice = self._type.get()
        err = False
        if not self._folders:
            self._resolved = None
            label = "(add at least one folder)"
        elif choice in ("pictures", "videos"):
            self._resolved = choice
            label = f"type: {choice}"
        else:  # auto-detect from the first folder
            resolved, vids, imgs = self._detect_type(self._folders[0])
            self._resolved = resolved
            if resolved is None:
                err = True
                label = "auto → no images or videos found in first folder"
            else:
                label = f"auto → {resolved}  ({vids} videos, {imgs} images in first folder)"
        self._resolved_lbl.configure(
            text=widgets.middle_truncate(label, 46),
            text_color=theme.DESTRUCTIVE if err else theme.TEXT_2,
        )

        # Loop length only meaningful for videos
        if self._resolved == "videos":
            self._loop_label.grid(row=4, column=0, sticky="w", padx=6, pady=6)
            self._loop_spin.grid(row=4, column=1, sticky="w", padx=6, pady=6)
        else:
            self._loop_label.grid_remove()
            self._loop_spin.grid_remove()

        ok = bool(self._folders) and self._resolved is not None
        self._start_btn.configure(state="normal" if ok else "disabled")

    def _start(self) -> None:
        if not self._folders or self._resolved is None:
            return  # Start is disabled in this state; guard anyway.
        video = self._resolved == "videos"

        # Loop length: validate strictly only when it actually applies (videos).
        loop_s = float(getattr(self._settings, "review_loop_s", 3.0))
        if video:
            try:
                loop_s = float(self._loop_var.get())
                if not (1.0 <= loop_s <= 10.0):
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid loop length",
                    "Loop length must be a number between 1.0 and 10.0 seconds.",
                    parent=self,
                )
                return

        script = Path(__file__).parent / "viewers" / (
            "make_video_viewer.py" if video else "make_image_viewer.py"
        )
        if not script.is_file():
            messagebox.showerror(
                "Viewer script missing", f"Could not find:\n{script}", parent=self
            )
            return

        folders = list(self._folders)
        argv = [sys.executable, str(script), *folders]
        if video:
            argv += ["--target-seconds", str(loop_s)]

        # Persist last-used choices (Settings.load() filters unknown keys).
        self._on_settings_update(
            replace(self._settings, review_type=self._type.get(), review_loop_s=loop_s)
        )

        # Worker thread runs the subprocess; UI thread polls the status object.
        status = _ReviewStatus()
        parent = self._parent
        fallback = _review_output_path(folders, video)

        def on_done(rc: Optional[int], html: Optional[str], stderr_tail: str) -> None:
            if rc == 0:
                target = html or (str(fallback) if fallback else None)
                if target and Path(target).exists():
                    webbrowser.open_new_tab(Path(target).as_uri())
                else:
                    messagebox.showerror(
                        "Viewer not found",
                        "The viewer was built but its HTML path could not be located.",
                        parent=parent,
                    )
            else:
                messagebox.showerror(
                    "Viewer build failed",
                    f"The generator exited with code {rc}.\n\n"
                    f"{stderr_tail or '(no error output)'}",
                    parent=parent,
                )

        threading.Thread(
            target=self._worker, args=(argv, status), daemon=True
        ).start()
        _ReviewProgressDialog(parent, status, on_done)
        self.destroy()

    @staticmethod
    def _worker(argv: list[str], status: _ReviewStatus) -> None:
        """Worker thread: run the generator, capture output, write status.
        Never touches Tk widgets, webbrowser, or messagebox."""
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, creationflags=_REVIEW_NO_WINDOW,
            )
            status.set_proc(proc)
            out, err = proc.communicate()
            status.finish(proc.returncode, _parse_wrote_path(out or ""), (err or "")[-1000:])
        except Exception as exc:  # spawn failure etc.
            status.finish(-1, None, str(exc)[-1000:])


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(ctk.CTk):
    # Fixed window width — the window must NEVER resize to content (stretch-bug
    # fix). Variable status/info strings pass through middle_truncate so a long
    # line can't push the width; the full text lives in a hover Tooltip AND,
    # since truncation with no way back is just a different bug, behind a click
    # on the status row (see _show_status_detail).
    #
    # Height is the full work area, pinned once at start-up. The window is
    # resizable vertically only; horizontally it stays nailed down.
    _WIDTH = 460
    _STATUS_MAX = 28   # chars on the status line (BODY font); fits the card column
    _INFO_MAX = 34     # chars on the info line (CAPTION font); fits the card column

    def __init__(
        self,
        settings: cfg.Settings,
        agent: SyncAgent,
        status: SyncStatus,
        motility_agent: MotilityAgent,
        motility_status: MotilityStatus,
        crawling_agent: CrawlingAgent,
        crawling_status: CrawlingStatus,
        counting_agent: CountingAgent,
        counting_status: CountingStatus,
        survival_agent: SurvivalAgent,
        survival_status: SurvivalStatus,
        analyze_status: object = None,
        update_status: object = None,
    ) -> None:
        theme.init()
        super().__init__()
        self._settings = settings
        self._agent = agent
        self._status = status
        self._motility_agent = motility_agent
        self._motility_status = motility_status
        self._crawling_agent = crawling_agent
        self._crawling_status = crawling_status
        self._counting_agent = counting_agent
        self._counting_status = counting_status
        self._survival_agent = survival_agent
        self._survival_status = survival_status
        # Optional so main.py keeps working if it is not passed. When it is, the
        # "Analyze on laptop" button gets visible feedback instead of the empty
        # console window Windows used to allocate for the subprocess.
        self._analyze_status = analyze_status
        # Optional for the same reason as analyze_status above: main.py keeps
        # working without it. Holds an UpdateInfo once a newer release is seen.
        self._update_status = update_status
        self._update_shown = False
        self._update_info = None
        self._analyze_toast = None
        self._analyze_was_busy = False
        self._button_waiting = False

        # The version in the title makes a screenshot self-identifying, the
        # same way the log header does. Reads "dev" from a source checkout.
        self.title(f"WormScan Launcher  {paths.version_string()}")
        self.configure(fg_color=theme.BG)
        # Width nailed shut (the stretch bug); height is ours to set, and the
        # user may still drag it if they want something shorter.
        self.resizable(False, True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        # Left edge, full working height. Content that does not fill it lives in
        # the recent-runs card, which expands to take up the slack.
        self.update_idletasks()
        place_left_full_height(self, self._WIDTH)
        self.after(150, lambda: place_left_full_height(self, self._WIDTH))

        self._refresh_recent()
        self._analyze_toast = widgets.Toast(self)
        self._poll()

    def _build(self) -> None:
        # --- Header: title left, Settings button right ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(
            header, text="WormScan", font=theme.title(), text_color=theme.TEXT,
        ).pack(side="left")
        settings_btn = widgets.IconButton(
            header, "Settings", self._open_settings, widgets.GLYPH_SETTINGS,
            variant="secondary",
        )
        settings_btn.pack(side="right")
        widgets.Tooltip(settings_btn, "Connection, mirror folder, and sync interval")

        widgets.HairlineSeparator(self).pack(fill="x", padx=16, pady=(0, 8))

        # --- Update notice: created now, packed only if there is ever one ---
        #
        # Built as a bare label on the window, NOT inside a placeholder frame.
        # An empty CTkFrame is not zero-height: CTkFrame defaults to
        # height=200, and pack(fill="x") stretches only the width, so a frame
        # held in reserve for a notice reserves 200 px of blank space above the
        # status card forever. (It did exactly that.)
        #
        # Position is handled at pack time with `before=self._status_card`
        # instead, which inserts into the pack order rather than appending, so
        # the notice lands above the status card and nothing is reserved while
        # there is nothing to say.
        self._update_lbl = ctk.CTkLabel(
            self, text="", font=theme.body(),
            text_color=theme.ACCENT, anchor="w", cursor="hand2",
        )
        self._update_lbl.bind("<Button-1>", lambda _e: self._open_release_page())

        # --- Status card: dot + (status over info) + the two sync buttons ---
        #
        # Packed, not gridded. The buttons are the tallest thing in this card,
        # so they set its height; the card's own 12 px padding then sits equally
        # above and below them, and the text column is centred against them
        # rather than pinned to the top.
        card = widgets.Card(self)
        card.pack(fill="x", padx=16, pady=(0, 10))
        # Referenced by _poll_update, which packs the update notice `before` it.
        self._status_card = card
        row = card.content

        buttons = ctk.CTkFrame(row, fg_color="transparent")
        buttons.pack(side="right", padx=(10, 0))

        self._sync_btn = widgets.IconButton(
            buttons, "Sync Now", self._on_sync_now, widgets.GLYPH_REFRESH,
            variant="primary",
        )
        self._sync_btn.pack(fill="x")
        widgets.Tooltip(self._sync_btn, "Pull new files from the Pi now")

        # Mirror Folder lives directly under Sync Now: it is what you press
        # straight after a sync, to go and look at what arrived. fill="x" inside
        # this frame makes both buttons the width of the wider one.
        mirror_btn = widgets.IconButton(
            buttons, "Mirror Folder", self._open_mirror, widgets.GLYPH_FOLDER,
            variant="secondary",
        )
        mirror_btn.pack(fill="x", pady=(6, 0))
        widgets.Tooltip(mirror_btn, "Open the local folder where Pi data is synced")

        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True)

        self._dot = widgets.StatusDot(left, size=14)
        self._dot.pack(side="left", padx=(0, 8))

        text_col = ctk.CTkFrame(left, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True)
        # expand with no vertical fill = vertically centred against the buttons.
        text_inner = ctk.CTkFrame(text_col, fg_color="transparent")
        text_inner.pack(fill="x", expand=True)

        # The status strings are variable-length and some of them are long
        # (a full image path mid-analysis). They are middle-truncated so they
        # can never push the window width — and the row is clickable, so the
        # full text is one click away instead of lost.
        self._status_full = "Starting…"
        self._info_full = ""
        self._status_lbl = ctk.CTkLabel(
            text_inner, text="Starting…", font=theme.body(),
            text_color=theme.TEXT, anchor="w", cursor="hand2",
        )
        self._status_lbl.pack(fill="x", anchor="w")
        self._info_lbl = ctk.CTkLabel(
            text_inner, text="", font=theme.caption(), text_color=theme.TEXT_2,
            anchor="w", cursor="hand2",
        )
        self._info_lbl.pack(fill="x", anchor="w")
        self._status_tip = widgets.Tooltip(self._status_lbl, "")
        self._info_tip = widgets.Tooltip(self._info_lbl, "")
        for lbl in (self._status_lbl, self._info_lbl, self._dot):
            lbl.bind("<Button-1>", self._show_status_detail, add="+")

        # --- Action stack ---
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 2))

        # Green: the one button that starts something on the hardware rather
        # than working on data already on this machine.
        imaging_btn = widgets.IconButton(
            actions, "Imaging", self._open_imaging, widgets.GLYPH_CAMERA,
            variant="success",
        )
        imaging_btn.pack(fill="x", pady=3)
        widgets.Tooltip(imaging_btn, "Open the Pi camera interface in your browser")

        self._analysis_btn = widgets.IconButton(
            actions, "Analyze", self._open_analysis, widgets.GLYPH_CHART,
            variant="primary",
        )
        self._analysis_btn.pack(fill="x", pady=3)
        widgets.Tooltip(
            self._analysis_btn,
            "Run motility, crawling, development, or colony survival",
        )

        review_btn = widgets.IconButton(
            actions, "Review", self._open_review, widgets.GLYPH_GRID,
            variant="secondary",
        )
        review_btn.pack(fill="x", pady=3)
        widgets.Tooltip(review_btn, "Build a side-by-side grid viewer of your plates")

        # --- Footer, packed to the BOTTOM before the expanding card below it.
        #
        # pack() hands out space in the order it is called, so a widget packed
        # after an expand=True sibling gets whatever is left — which, on a
        # full-height window, was nothing: Shut Down Pi came out half off the
        # bottom edge. Reserving its strip first fixes that for good, whatever
        # the window height turns out to be.
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x")
        widgets.HairlineSeparator(footer).pack(fill="x", padx=16, pady=(0, 8))
        shutdown_btn = widgets.IconButton(
            footer, "Shut Down Pi", self._shutdown_pi, widgets.GLYPH_POWER,
            variant="destructive",
        )
        shutdown_btn.pack(fill="x", padx=16, pady=(0, 14))
        widgets.Tooltip(shutdown_btn, "Safely power off the Raspberry Pi")

        # --- Recent results ---
        #
        # What the empty space at the bottom is for. Every pipeline writes a
        # timestamped folder into the data tree and then the launcher forgets
        # about it; finding last Tuesday's run meant going digging. This lists
        # the most recent ones, newest first, and opens them on click.
        # Run messages land here and STAY until dismissed. They used to be a
        # message box, which on this machine appeared for a frame and vanished;
        # a frame inside the window cannot do that.
        self._notice = widgets.InlineNotice(self)

        recent = widgets.Card(self, title="Recent results")
        self._recent_card = recent
        recent.pack(fill="both", expand=True, padx=16, pady=(8, 8))
        rc = recent.content
        rc.grid_columnconfigure(0, weight=1)
        rc.grid_rowconfigure(0, weight=1)
        self._recent_list = ctk.CTkScrollableFrame(rc, fg_color="transparent")
        self._recent_list.grid(row=0, column=0, sticky="nsew")
        recent_bar = ctk.CTkFrame(rc, fg_color="transparent")
        recent_bar.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        widgets.secondary_button(
            recent_bar, "Refresh", self._refresh_recent).pack(side="left")
        self._recent_note = ctk.CTkLabel(
            recent_bar, text="", font=theme.caption(),
            text_color=theme.TEXT_2, anchor="e",
        )
        self._recent_note.pack(side="right")


    # ------------------------------------------------------------------
    # "Analyze on laptop" — feedback for a button pressed in the browser
    # ------------------------------------------------------------------

    def _poll_analyze_button(self) -> None:
        """Show a corner toast while a single-frame analysis is running.

        The button is on the Pi's web UI, so the user is looking at a browser,
        not at this window — hence a small always-on-top card rather than a
        notice in the launcher. The result also lands in the launcher's own
        notice band, so it is still there when they come back to it.
        """
        st = self._analyze_status
        if st is None or self._analyze_toast is None:
            return
        try:
            snap = st.snapshot()
            if snap.busy:
                if not self._analyze_was_busy:
                    # A new press. If the user closed the card during the last
                    # one, that dismissal was about the last one.
                    self._analyze_toast.reset()
                self._analyze_toast.show(
                    "Analyzing the plate…",
                    snap.label or "Running the staging model on this laptop.")
            else:
                self._analyze_toast.hide()
            self._analyze_was_busy = snap.busy

            done = st.pop_finished()
            if done:
                run_dir = done.get("run_dir")
                if done.get("ok"):
                    self._show_notice(
                        "Plate analysed",
                        "The annotated image and the counts have been opened.",
                        detail=(widgets.middle_truncate(str(run_dir), 52)
                                if run_dir else ""),
                        confirm="Open the folder" if run_dir else "",
                        on_confirm=((lambda p=run_dir: self._open_folder(p))
                                    if run_dir else None),
                        dismiss="Dismiss", accent=theme.SUCCESS)
                else:
                    self._show_notice(
                        "Plate analysis failed",
                        done.get("error") or "Unknown error.",
                        detail=(widgets.middle_truncate(str(run_dir), 52)
                                if run_dir else ""),
                        dismiss="Dismiss", accent=theme.DESTRUCTIVE)
        except Exception:
            # Feedback must never take the launcher's poll loop down with it.
            _log.warning("analyze-button poll failed", exc_info=True)

    # ------------------------------------------------------------------
    # Notices — messages that stay put
    # ------------------------------------------------------------------

    def show_run_notice(self, title: str, message: str) -> None:
        """Public: a dialog-free 'this is happening' message from a child window."""
        self._show_notice(title, message, dismiss="", accent=theme.ACCENT)

    def _show_notice(self, *args, **kw) -> None:
        self._notice.show(*args, wraplength=self._WIDTH - 80, **kw)
        self._notice.pack(fill="x", padx=16, pady=(0, 8),
                          before=self._recent_card)

    def _clear_notice(self) -> None:
        self._notice.hide()

    # ------------------------------------------------------------------
    # Status detail — the way back from a truncated line
    # ------------------------------------------------------------------

    def _show_status_detail(self, _event=None) -> None:
        """The full status text, in the window rather than over it."""
        self._show_notice(
            "Status", self._status_full,
            detail=self._info_full, dismiss="Close", accent=theme.ACCENT)

    # ------------------------------------------------------------------
    # Recent results
    # ------------------------------------------------------------------

    def _find_recent_results(self, limit: int = 12) -> list[Path]:
        """Newest analysis output folders under the mirror root.

        Bounded on purpose: three levels deep and a hard cap on directories
        visited, because this runs on the UI thread and a mirror with tens of
        thousands of plate folders would otherwise freeze the window. Hitting
        the cap means the list is incomplete, not that it is wrong.
        """
        root = Path(self._settings.mirror_root)
        found: list[Path] = []
        visited = 0
        self._recent_truncated = False

        def walk(d: Path, depth: int) -> None:
            nonlocal visited
            if depth > _RESULT_SCAN_DEPTH or visited >= _RESULT_SCAN_MAX_DIRS:
                return
            try:
                children = list(d.iterdir())
            except OSError:
                return
            for child in children:
                if visited >= _RESULT_SCAN_MAX_DIRS:
                    self._recent_truncated = True
                    return
                if not child.is_dir():
                    continue
                visited += 1
                if child.name.startswith(_RESULT_PREFIXES):
                    found.append(child)
                    continue          # results folders hold no results folders
                if child.name.startswith((".", "_")):
                    continue
                walk(child, depth + 1)

        try:
            if root.is_dir():
                walk(root, 1)
        except Exception:
            _log.warning("recent-results scan failed", exc_info=True)
            return []
        found.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0,
                   reverse=True)
        return found[:limit]

    def _refresh_recent(self) -> None:
        for child in self._recent_list.winfo_children():
            child.destroy()
        try:
            runs = self._find_recent_results()
        except Exception:
            runs = []
        if not runs:
            ctk.CTkLabel(
                self._recent_list,
                text="No analysis runs found yet.\nThey appear here once you "
                     "run one — newest first.",
                font=theme.caption(), text_color=theme.TEXT_2,
                anchor="w", justify="left",
            ).pack(anchor="w", pady=2)
            self._recent_note.configure(text="")
            return

        # Folder NAME only. The names are already timestamped and prefixed with
        # the pipeline that wrote them, so they identify a run on their own; the
        # path is supporting detail and belongs in the tooltip, on a delay, so
        # running the mouse down the list does not set off a strobe of tips.
        for path in runs:
            stamp = datetime.fromtimestamp(path.stat().st_mtime)
            row = ctk.CTkFrame(self._recent_list, fg_color="transparent")
            row.pack(fill="x", pady=1)
            btn = ctk.CTkButton(
                row, text=widgets.middle_truncate(path.name, 44), anchor="w",
                command=lambda p=path: self._open_folder(p),
                fg_color="transparent", hover_color=theme.BG,
                text_color=theme.TEXT, corner_radius=theme.BTN_RADIUS,
                font=theme.body(), height=24,
            )
            btn.pack(fill="x")
            widgets.Tooltip(
                btn, f"{path}\n{stamp:%Y-%m-%d %H:%M}  ·  click to open",
                delay_ms=600,
            )
        note = f"{len(runs)} shown"
        if getattr(self, "_recent_truncated", False):
            note += " · search capped"
        self._recent_note.configure(text=note)

    def _open_folder(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("Could not open folder", str(exc), parent=self)

    # ------------------------------------------------------------------
    # UI poll — runs on the main thread via root.after()
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        # Everything below runs inside a Tk `after` callback. An exception here
        # skips the re-arm at the end of the method, which silently kills the
        # whole polling loop: no sync status, no completion notices, no update
        # notice and no toasts for the rest of the session, with the window
        # still responsive so nothing looks wrong. The re-arm is in `finally`
        # and the body is guarded so one bad frame cannot end the loop.
        try:
            self._poll_body()
        except Exception:
            log.exception("UI poll failed")
        finally:
            self.after(_POLL_MS, self._poll)

    def _poll_body(self) -> None:
        self._poll_analyze_button()

        # --- Check analysis completion and surface result dialog ---
        for kind, st, noun in (
            ("Motility", self._motility_status, "video"),
            ("Crawling", self._crawling_status, "video"),
            ("Development", self._survival_status, "plate"),
            ("Counting", self._counting_status, "plate"),
        ):
            result = st.pop_completed()
            if result:
                n_ok = result["n_ok"]
                n_fail = result["n_fail"]
                out_dir = result.get("out_dir")
                self._refresh_recent()
                if result.get("failed"):
                    # A crash used to produce no message at all, which is
                    # indistinguishable from a run that did nothing. Now it
                    # says what broke and where the log is.
                    self._show_notice(
                        f"{kind} analysis failed",
                        str(result.get("error") or "Unknown error.")
                        + ("\n\nlog.txt in the run folder has the full "
                           "traceback." if out_dir else ""),
                        detail=(widgets.middle_truncate(str(out_dir), 52)
                                if out_dir else ""),
                        confirm="Open run folder" if out_dir else "",
                        on_confirm=((lambda p=out_dir: self._open_folder(p))
                                    if out_dir else None),
                        dismiss="Dismiss",
                        accent=theme.DESTRUCTIVE,
                    )
                    continue
                note = result.get("note") or ""
                msg = f"{n_ok} {noun}s processed, {n_fail} failed."
                if note:
                    msg += "\n\n" + note
                self._show_notice(
                    f"{kind} analysis complete", msg,
                    detail=widgets.middle_truncate(str(out_dir), 52),
                    confirm="Open results folder",
                    on_confirm=lambda p=out_dir: self._open_folder(p),
                    dismiss="Dismiss",
                    accent=theme.SUCCESS,
                )

        # --- Status row: a running analysis takes priority ---
        motility_snap = self._motility_status.snapshot()
        crawling_snap = self._crawling_status.snapshot()
        counting_snap = self._counting_status.snapshot()
        survival_snap = self._survival_status.snapshot()
        snap = (
            motility_snap if motility_snap.running
            else crawling_snap if crawling_snap.running
            else counting_snap if counting_snap.running
            else survival_snap
        )
        s_color, s_label, last_sync, files, nbytes = self._status.snapshot()

        if snap.running:
            display_color = snap.color
            display_label = snap.label
        else:
            clock_msg = self._status.get_clock_msg()
            display_color = s_color
            display_label = clock_msg if clock_msg else s_label

        self._dot.set_color(theme.DOT_COLORS.get(display_color, theme.DOT_GRAY))
        self._status_full = display_label
        truncated = len(display_label) > self._STATUS_MAX
        self._status_lbl.configure(
            text=widgets.middle_truncate(display_label, self._STATUS_MAX)
        )
        self._status_tip.set_text(
            display_label + ("\n(click for the full message)" if truncated else "")
        )

        # Sync button lockout resolves on sync color, not display color
        if self._button_waiting and s_color == "green":
            self._button_waiting = False
            self._sync_btn.configure(state="normal")

        parts = []
        if last_sync:
            parts.append(f"Last sync: {last_sync}")
        parts.append(f"{files} files mirrored · {_fmt_bytes(nbytes)}")
        info_full = "  ".join(parts)
        self._info_full = info_full
        self._info_lbl.configure(text=widgets.middle_truncate(info_full, self._INFO_MAX))
        self._info_tip.set_text(info_full)

        self._poll_update()

    def _poll_update(self) -> None:
        """Show the update notice once, if the background check found one."""
        if self._update_shown or self._update_status is None:
            return
        info = self._update_status.snapshot()
        if info is None:
            return
        self._update_shown = True
        self._update_info = info
        self._update_lbl.configure(
            text=f"Update available: {info.latest}  -  click to download"
        )
        self._update_lbl.pack(
            fill="x", padx=16, pady=(0, 8), before=self._status_card)
        widgets.Tooltip(
            self._update_lbl,
            f"You are running {info.current}.\n"
            f"Opens the release page in your browser. Nothing is downloaded "
            f"or installed automatically.",
        )

    def _open_release_page(self) -> None:
        if self._update_info is not None:
            webbrowser.open_new_tab(self._update_info.url)

    # ------------------------------------------------------------------
    # Button handlers — all run on the main thread
    # ------------------------------------------------------------------

    def _open_imaging(self) -> None:
        # Pre-existing: the token was interpolated raw, so a token containing
        # '+' arrived at the Pi as a space and the imaging UI rejected it.
        url = f"{self._settings.pi_url}/?token={quote(self._settings.token, safe='')}"
        webbrowser.open_new_tab(url)

    def _open_analysis(self) -> None:
        AnalysisDialog(
            self,
            self._settings,
            self._motility_agent,
            self._motility_status,
            self._crawling_agent,
            self._crawling_status,
            self._counting_agent,
            self._counting_status,
            self._survival_agent,
            self._survival_status,
            self._on_settings_saved,
        )

    def _open_review(self) -> None:
        ReviewDialog(self, self._settings, self._on_settings_saved)

    def _open_mirror(self) -> None:
        mirror = self._settings.mirror_root
        os.makedirs(mirror, exist_ok=True)
        os.startfile(mirror)

    def _on_sync_now(self) -> None:
        self._sync_btn.configure(state="disabled")
        self._button_waiting = True
        self._agent.wake()

    def _shutdown_pi(self) -> None:
        if not messagebox.askyesno(
            "Shut down Pi",
            "Shut down the Pi now?\n\nAll active captures will stop.",
            default="no",
            parent=self,
        ):
            return
        s = self._settings
        def _post() -> None:
            try:
                import requests
                requests.post(
                    f"{s.pi_url}/shutdown",
                    headers={"X-Auth-Token": s.token},
                    timeout=3,
                )
            except Exception:
                pass
        threading.Thread(target=_post, daemon=True).start()

    def _open_settings(self) -> None:
        SettingsDialog(self, self._settings, self._on_settings_saved)

    def _on_settings_saved(self, new: cfg.Settings) -> None:
        cfg.save(new)
        self._settings = new
        self._agent.update_settings(new)
        self._motility_agent.update_settings(new)
        self._crawling_agent.update_settings(new)
        self._counting_agent.update_settings(new)
        self._survival_agent.update_settings(new)
        # AnalyzeWorker is intentionally absent: it has no status object and is
        # not held by MainWindow. It re-reads config.json per frame instead, so
        # it picks up the per-class thresholds cfg.save() just wrote above.
        _log.info(
            "Settings update propagated to: sync, motility, crawling, "
            "counting, survival"
        )

    def _on_close(self) -> None:
        self._agent.stop()
        self.destroy()
