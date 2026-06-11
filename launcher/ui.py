"""
WormScan Launcher UI — Tkinter main window and settings dialog.

Thread boundary: this module runs entirely on the main (Tk) thread.
It reads sync state only through SyncStatus.snapshot() via root.after().
No widget method is ever called from the sync thread.
"""
import logging
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

import customtkinter as ctk

import config as cfg
import theme
import widgets
from analysis.docker_utils import run_preflight
from analysis.motility import MotilityAgent, MotilityStatus
from analysis.crawling import CrawlingAgent, CrawlingStatus
from analysis.counting_agent import (
    CountingAgent, CountingStatus, counting_preflight,
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

class SettingsDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        settings: cfg.Settings,
        on_save: Callable[[cfg.Settings], None],
    ) -> None:
        super().__init__(parent)
        self.title("WormScan Settings")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._current = settings
        self._on_save = on_save
        self._build(settings)

    def _build(self, s: cfg.Settings) -> None:
        pad = {"padx": 10, "pady": 5}

        ttk.Label(self, text="Pi URL").grid(row=0, column=0, sticky="w", **pad)
        self._pi_url = ttk.Entry(self, width=36)
        self._pi_url.insert(0, s.pi_url)
        self._pi_url.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(self, text="Token").grid(row=1, column=0, sticky="w", **pad)
        self._token = ttk.Entry(self, width=36, show="*")
        self._token.insert(0, s.token)
        self._token.grid(row=1, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(self, text="Mirror folder").grid(row=2, column=0, sticky="w", **pad)
        self._mirror = ttk.Entry(self, width=30)
        self._mirror.insert(0, s.mirror_root)
        self._mirror.grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(self, text="…", width=3, command=self._browse).grid(
            row=2, column=2, **pad
        )

        ttk.Label(self, text="Poll interval (s)").grid(row=3, column=0, sticky="w", **pad)
        self._poll = ttk.Entry(self, width=8)
        self._poll.insert(0, str(s.poll_interval_s))
        self._poll.grid(row=3, column=1, sticky="w", **pad)

        log_path = cfg.APP_DATA / "launcher.log"
        ttk.Label(
            self, text=f"Log: {log_path}", font="TkSmallCaptionFont",
            foreground="#666666",
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=(4, 10))
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=6)

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
        new = replace(
            self._current,
            pi_url=self._pi_url.get().rstrip("/"),
            token=self._token.get(),
            mirror_root=self._mirror.get(),
            poll_interval_s=poll,
        )
        self._on_save(new)
        self.destroy()


# ---------------------------------------------------------------------------
# Analysis progress dialog (modeless)
# ---------------------------------------------------------------------------

class AnalysisProgressDialog(tk.Toplevel):
    """
    Modeless progress window that tracks an analysis run in real time.
    Polls the status object at 200ms. Auto-closes when running becomes False.
    Works with either the motility or crawling agent/status (identical interface).
    """

    def __init__(
        self,
        parent: tk.Tk,
        agent: "MotilityAgent | CrawlingAgent | CountingAgent",
        status: "MotilityStatus | CrawlingStatus | CountingStatus",
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
        self._flavour_idx = 0
        self._flavour_tick = 0
        self._build()
        self.after(_PROGRESS_POLL_MS, self._poll)

    def _build(self) -> None:
        pad = {"padx": 16, "pady": 6}

        self._video_lbl = ttk.Label(self, text="Starting…", width=52)
        self._video_lbl.pack(**pad)

        self._bar = ttk.Progressbar(
            self, mode="determinate", length=400, maximum=1, value=0
        )
        self._bar.pack(padx=16, pady=4)

        self._stage_lbl = ttk.Label(
            self, text="", font="TkSmallCaptionFont", foreground="#555555", width=52
        )
        self._stage_lbl.pack(**pad)

        self._flavour_lbl = ttk.Label(
            self, text=_FLAVOUR_TEXTS[0], font="TkSmallCaptionFont",
            foreground="#999999", width=52,
        )
        self._flavour_lbl.pack(padx=16, pady=(0, 4))

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=16, pady=(4, 12))
        ttk.Button(btn_frame, text="Cancel", command=self._on_cancel).pack(side="right")

    def _poll(self) -> None:
        snap = self._status.snapshot()

        if not snap.running:
            self.destroy()
            return

        total = snap.total
        if total > 0:
            self._bar.config(maximum=total, value=snap.current_index)
            self._video_lbl.config(
                text=f"{self._noun.capitalize()} {snap.current_index + 1} of {total}: {snap.current_basename}"
                if snap.current_index < total
                else f"Finishing… ({total} of {total} done)"
            )

        self._stage_lbl.config(text=snap.current_stage)

        self._flavour_tick += 1
        if self._flavour_tick >= 15:
            self._flavour_tick = 0
            self._flavour_idx = (self._flavour_idx + 1) % len(_FLAVOUR_TEXTS)
            self._flavour_lbl.config(text=_FLAVOUR_TEXTS[self._flavour_idx])

        self.after(_PROGRESS_POLL_MS, self._poll)

    def _on_cancel(self) -> None:
        self._agent.cancel()
        self.destroy()


# ---------------------------------------------------------------------------
# Analysis setup dialog
# ---------------------------------------------------------------------------

class AnalysisDialog(tk.Toplevel):
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
        on_settings_update: Callable[[cfg.Settings], None],
    ) -> None:
        super().__init__(parent)
        self.title("WormScan Analysis")
        self.resizable(False, False)
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
        self._on_settings_update = on_settings_update
        self._build()

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}

        # Row 0 — analysis type
        ttk.Label(self, text="Analysis type").grid(row=0, column=0, sticky="w", **pad)
        self._mode = tk.StringVar(value="motility")
        rb_frame = ttk.Frame(self)
        rb_frame.grid(row=0, column=1, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(
            rb_frame, text="Motility", variable=self._mode, value="motility"
        ).pack(side="left")
        ttk.Radiobutton(
            rb_frame, text="Crawling", variable=self._mode, value="crawling"
        ).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(
            rb_frame, text="Counting", variable=self._mode, value="counting",
        ).pack(side="left", padx=(12, 0))

        # Row 1 — folder picker
        ttk.Label(self, text="Video folder").grid(row=1, column=0, sticky="w", **pad)
        self._folder_var = tk.StringVar(value=self._settings.mirror_root)
        ttk.Entry(self, textvariable=self._folder_var, width=36).grid(
            row=1, column=1, sticky="ew", **pad
        )
        ttk.Button(self, text="…", width=3, command=self._browse).grid(
            row=1, column=2, **pad
        )

        # Row 2 — threshold spinbox (motility-only; hidden when Crawling selected)
        self._threshold_label = ttk.Label(self, text="Min fragment length (s)")
        self._threshold_label.grid(row=2, column=0, sticky="w", **pad)
        self._threshold_var = tk.StringVar(
            value=str(self._settings.motility_long_threshold_s)
        )
        self._threshold_spin = ttk.Spinbox(
            self, textvariable=self._threshold_var,
            from_=1.0, to=30.0, increment=0.5, width=6, format="%.1f",
        )
        self._threshold_spin.grid(row=2, column=1, sticky="w", **pad)

        self._threshold_help = ttk.Label(
            self,
            text="Recommended: 5–10 s.  Higher = stricter but biases toward slower worms.",
            font="TkSmallCaptionFont", foreground="#555555",
        )
        self._threshold_help.grid(
            row=3, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4)
        )

        # Row 4 — clear-cache checkbox
        self._clear_cache_var = tk.BooleanVar(value=False)
        self._clear_cache_check = ttk.Checkbutton(
            self, text="Clear cache before run",
            variable=self._clear_cache_var,
        )
        self._clear_cache_check.grid(
            row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4)
        )

        # Row 5 — render options (motility): unchanged motility binding
        self._motility_render_frame = ttk.LabelFrame(
            self, text="Video render options", padding=(8, 4)
        )
        self._motility_render_frame.grid(
            row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2)
        )
        render_frame = self._motility_render_frame
        self._want_tracked = tk.BooleanVar(value=False)
        self._want_curvature = tk.BooleanVar(value=False)
        self._want_sidebyside = tk.BooleanVar(value=False)
        self._want_per_worm_traces = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            render_frame, text="Tracked (skeleton + worm IDs)",
            variable=self._want_tracked,
        ).pack(anchor="w")
        ttk.Checkbutton(
            render_frame, text="Curvature (red = positive, blue = negative)",
            variable=self._want_curvature,
        ).pack(anchor="w")
        ttk.Checkbutton(
            render_frame, text="Side-by-side (original | masked + tracked)",
            variable=self._want_sidebyside,
        ).pack(anchor="w")
        ttk.Checkbutton(
            render_frame,
            text="Per-worm curvature traces (PNG + MP4 per fully-tracked worm)",
            variable=self._want_per_worm_traces,
        ).pack(anchor="w")
        ttk.Label(
            render_frame,
            text="Adds 30–90 s render time per video per option.",
            font="TkSmallCaptionFont", foreground="#888888",
        ).pack(anchor="w", pady=(2, 0))

        # Row 5 — render options (crawling): tracking, side-by-side, path traces
        self._crawling_render_frame = ttk.LabelFrame(
            self, text="Video render options", padding=(8, 4)
        )
        self._crawl_tracked = tk.BooleanVar(value=False)
        self._crawl_sidebyside = tk.BooleanVar(value=False)
        self._crawl_path_traces = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self._crawling_render_frame, text="Tracked (skeleton + worm IDs)",
            variable=self._crawl_tracked,
        ).pack(anchor="w")
        ttk.Checkbutton(
            self._crawling_render_frame, text="Side-by-side (original | masked + tracked)",
            variable=self._crawl_sidebyside,
        ).pack(anchor="w")
        ttk.Checkbutton(
            self._crawling_render_frame, text="Path traces (fading centroid trails)",
            variable=self._crawl_path_traces,
        ).pack(anchor="w")
        ttk.Label(
            self._crawling_render_frame,
            text="Adds 1–3 min render time per video per option.",
            font="TkSmallCaptionFont", foreground="#888888",
        ).pack(anchor="w", pady=(2, 0))

        # Min track span — quality filter; renders show only passing worms.
        _min_track_row = ttk.Frame(self._crawling_render_frame)
        _min_track_row.pack(anchor="w", fill="x", pady=(6, 0))
        ttk.Label(_min_track_row, text="Min track span (s)").pack(side="left")
        self._crawl_min_track = tk.StringVar(
            value=str(int(getattr(self._settings, "crawling_min_track_s", 30)))
        )
        ttk.Spinbox(
            _min_track_row, textvariable=self._crawl_min_track,
            from_=1, to=600, increment=5, width=6, format="%.0f",
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            self._crawling_render_frame,
            text="Worms on the plate for less than this span are dropped from the aggregate and not drawn.",
            font="TkSmallCaptionFont", foreground="#888888",
        ).pack(anchor="w", pady=(2, 0))

        # Row 5 — counting options: the two prominent tuning knobs. Everything
        # else uses counting.py defaults.
        self._counting_frame = ttk.LabelFrame(
            self, text="Counting options", padding=(8, 4)
        )
        _split_row = ttk.Frame(self._counting_frame)
        _split_row.pack(anchor="w", fill="x")
        ttk.Label(_split_row, text="Split sensitivity").pack(side="left")
        self._count_split = tk.StringVar(
            value=f"{float(getattr(self._settings, 'counting_split_sensitivity', 3.0)):.1f}"
        )
        ttk.Spinbox(
            _split_row, textvariable=self._count_split,
            from_=0.5, to=20.0, increment=0.5, width=6, format="%.1f",
        ).pack(side="left", padx=(6, 0))

        _mincol_row = ttk.Frame(self._counting_frame)
        _mincol_row.pack(anchor="w", fill="x", pady=(6, 0))
        ttk.Label(_mincol_row, text="Min colony diameter (µm)").pack(side="left")
        self._count_min_um = tk.StringVar(
            value=f"{float(getattr(self._settings, 'counting_min_colony_um', 200.0)):.0f}"
        )
        ttk.Spinbox(
            _mincol_row, textvariable=self._count_min_um,
            from_=0.0, to=2000.0, increment=50.0, width=6, format="%.0f",
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            self._counting_frame,
            text="Higher split sensitivity = fewer splits (big colonies stay whole). "
                 "Min diameter rejects specks below this size.",
            font="TkSmallCaptionFont", foreground="#888888", wraplength=360,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        # Show the render frame matching the selected analysis type.
        self._mode.trace_add("write", self._on_mode_change)
        self._on_mode_change()

        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=6, column=0, columnspan=3, pady=(4, 12))
        ttk.Button(btn_frame, text="Start", command=self._start).pack(
            side="left", padx=6
        )
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(
            side="left", padx=6
        )

    def _browse(self) -> None:
        initial = self._folder_var.get() or self._settings.mirror_root
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            self._folder_var.set(path)

    def _on_mode_change(self, *_args) -> None:
        """Show the controls matching the selected analysis type.

        'Min fragment length (s)' (rows 2–3) is motility-only — threshold_s is
        inert for crawling/counting — so it is hidden for those. Counting has no
        cache and no video render, so the clear-cache box and both render frames
        are hidden and a small two-knob options frame is shown instead.
        """
        mode = self._mode.get()
        # Reset the row-5 slot; the active branch re-grids what it needs.
        self._motility_render_frame.grid_remove()
        self._crawling_render_frame.grid_remove()
        self._counting_frame.grid_remove()

        if mode == "counting":
            self._threshold_label.grid_remove()
            self._threshold_spin.grid_remove()
            self._threshold_help.grid_remove()
            self._clear_cache_check.grid_remove()
            self._counting_frame.grid(
                row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2)
            )
            return

        self._clear_cache_check.grid()
        if mode == "crawling":
            self._threshold_label.grid_remove()
            self._threshold_spin.grid_remove()
            self._threshold_help.grid_remove()
            self._crawling_render_frame.grid(
                row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2)
            )
        else:
            self._threshold_label.grid()
            self._threshold_spin.grid()
            self._threshold_help.grid()
            self._motility_render_frame.grid(
                row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2)
            )

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
                or self._counting_status.is_running()):
            messagebox.showwarning(
                "Already running",
                "An analysis is already in progress.",
                parent=self,
            )
            return

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
            ))

            AnalysisProgressDialog(
                self._parent, self._counting_agent, self._counting_status,
                title="WormScan Counting Analysis", noun="plate",
            )
            self._counting_agent.start_analysis(
                folder,
                split_sensitivity=split_sensitivity,
                min_colony_um=min_colony_um,
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

        min_span_s = 30.0
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
                folder,
                threshold_s=threshold_s,
                clear_cache=self._clear_cache_var.get(),
                want_tracked=self._crawl_tracked.get(),
                want_sidebyside=self._crawl_sidebyside.get(),
                want_path_traces=self._crawl_path_traces.get(),
                min_span_s=min_span_s,
            )
        else:
            agent.start_analysis(
                folder,
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


class _ReviewProgressDialog(tk.Toplevel):
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
        self.after(_PROGRESS_POLL_MS, self._poll)

    def _build(self) -> None:
        ttk.Label(self, text="Building viewer…", width=44).pack(padx=20, pady=(16, 8))
        self._bar = ttk.Progressbar(self, mode="indeterminate", length=320)
        self._bar.pack(padx=20, pady=4)
        self._bar.start(12)
        ttk.Label(
            self,
            text="A video build transcodes every clip — this can take minutes the first time.",
            font="TkSmallCaptionFont", foreground="#888888", width=52,
        ).pack(padx=20, pady=(4, 8))
        btn = ttk.Frame(self)
        btn.pack(fill="x", padx=20, pady=(0, 12))
        ttk.Button(btn, text="Cancel", command=self._on_cancel).pack(side="right")

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

class ReviewDialog(tk.Toplevel):
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

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        self._folders: list[str] = []
        self._detect_cache: dict[str, tuple[Optional[str], int, int]] = {}
        self._resolved: Optional[str] = None

        default_type = getattr(self._settings, "review_type", "auto")
        default_loop = float(getattr(self._settings, "review_loop_s", 3.0))

        # Row 0 — folder add/remove list (askdirectory returns one at a time)
        ttk.Label(self, text="Day folders").grid(row=0, column=0, sticky="nw", **pad)
        list_frame = ttk.Frame(self)
        list_frame.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        self._folder_list = tk.Listbox(list_frame, height=5, width=46)
        self._folder_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(
            list_frame, orient="vertical", command=self._folder_list.yview
        )
        sb.pack(side="left", fill="y")
        self._folder_list.config(yscrollcommand=sb.set)

        fbtn = ttk.Frame(self)
        fbtn.grid(row=1, column=1, columnspan=2, sticky="w", padx=12, pady=(0, 4))
        ttk.Button(fbtn, text="Add folder…", command=self._add_folder).pack(side="left")
        ttk.Button(
            fbtn, text="Remove selected", command=self._remove_folder
        ).pack(side="left", padx=(8, 0))

        # Row 2 — content type radio (auto-detect default)
        ttk.Label(self, text="Content").grid(row=2, column=0, sticky="w", **pad)
        type_frame = ttk.Frame(self)
        type_frame.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        self._type = tk.StringVar(value=default_type)
        ttk.Radiobutton(
            type_frame, text="Pictures", variable=self._type, value="pictures"
        ).pack(side="left")
        ttk.Radiobutton(
            type_frame, text="Videos", variable=self._type, value="videos"
        ).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(
            type_frame, text="Auto-detect", variable=self._type, value="auto"
        ).pack(side="left", padx=(12, 0))

        # Row 3 — resolved-type label (e.g. "auto → videos")
        self._resolved_lbl = ttk.Label(
            self, text="", font="TkSmallCaptionFont", foreground="#555555"
        )
        self._resolved_lbl.grid(
            row=3, column=1, columnspan=2, sticky="w", padx=12, pady=(0, 4)
        )

        # Row 4 — loop length (videos only; hidden for pictures)
        self._loop_label = ttk.Label(self, text="Loop length (s)")
        self._loop_var = tk.StringVar(value=f"{default_loop:.1f}")
        self._loop_spin = ttk.Spinbox(
            self, textvariable=self._loop_var,
            from_=1.0, to=10.0, increment=0.5, width=6, format="%.1f",
        )

        # Row 5 — buttons
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=(8, 12))
        self._start_btn = ttk.Button(btn_frame, text="Start", command=self._start)
        self._start_btn.pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(
            side="left", padx=6
        )

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
            self._folder_list.insert(tk.END, path)
            self._refresh()

    def _remove_folder(self) -> None:
        sel = self._folder_list.curselection()
        if not sel:
            return
        idx = sel[0]
        self._folder_list.delete(idx)
        del self._folders[idx]
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
        self._resolved_lbl.config(text=label, foreground="#c96565" if err else "#555555")

        # Loop length only meaningful for videos
        if self._resolved == "videos":
            self._loop_label.grid(row=4, column=0, sticky="w", padx=12, pady=6)
            self._loop_spin.grid(row=4, column=1, sticky="w", padx=12, pady=6)
        else:
            self._loop_label.grid_remove()
            self._loop_spin.grid_remove()

        ok = bool(self._folders) and self._resolved is not None
        self._start_btn.config(state="normal" if ok else "disabled")

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
    # line can't push the width; the full text lives in a hover Tooltip.
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
        self._button_waiting = False

        self.title("WormScan Launcher")
        self.configure(fg_color=theme.BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        # Pin the width; height fits content once, then never tracks it again.
        self.update_idletasks()
        self.geometry(f"{self._WIDTH}x{self.winfo_reqheight()}")

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

        # --- Status card: dot + (status over info) + Sync Now ---
        card = widgets.Card(self)
        card.pack(fill="x", padx=16, pady=(0, 10))
        row = card.content
        row.grid_columnconfigure(1, weight=1)

        self._dot = widgets.StatusDot(row, size=14)
        self._dot.grid(row=0, column=0, rowspan=2, padx=(0, 8))

        self._status_lbl = ctk.CTkLabel(
            row, text="Starting…", font=theme.body(), text_color=theme.TEXT,
            anchor="w",
        )
        self._status_lbl.grid(row=0, column=1, sticky="w")
        self._info_lbl = ctk.CTkLabel(
            row, text="", font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
        )
        self._info_lbl.grid(row=1, column=1, sticky="w")
        self._status_tip = widgets.Tooltip(self._status_lbl, "")
        self._info_tip = widgets.Tooltip(self._info_lbl, "")

        self._sync_btn = widgets.IconButton(
            row, "Sync Now", self._on_sync_now, widgets.GLYPH_REFRESH,
            variant="primary",
        )
        self._sync_btn.grid(row=0, column=2, rowspan=2, padx=(8, 0))
        widgets.Tooltip(self._sync_btn, "Pull new files from the Pi now")

        # --- Action stack ---
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 2))

        imaging_btn = widgets.IconButton(
            actions, "Imaging", self._open_imaging, widgets.GLYPH_CAMERA,
            variant="secondary",
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
            "Run motility, crawling, or colony counting on recorded videos",
        )

        review_btn = widgets.IconButton(
            actions, "Review", self._open_review, widgets.GLYPH_GRID,
            variant="secondary",
        )
        review_btn.pack(fill="x", pady=3)
        widgets.Tooltip(review_btn, "Build a side-by-side grid viewer of your plates")

        mirror_btn = widgets.IconButton(
            actions, "Mirror Folder", self._open_mirror, widgets.GLYPH_FOLDER,
            variant="secondary",
        )
        mirror_btn.pack(fill="x", pady=3)
        widgets.Tooltip(mirror_btn, "Open the local folder where Pi data is synced")

        widgets.HairlineSeparator(self).pack(fill="x", padx=16, pady=(8, 8))

        shutdown_btn = widgets.IconButton(
            self, "Shut Down Pi", self._shutdown_pi, widgets.GLYPH_POWER,
            variant="destructive",
        )
        shutdown_btn.pack(fill="x", padx=16, pady=(0, 14))
        widgets.Tooltip(shutdown_btn, "Safely power off the Raspberry Pi")

    # ------------------------------------------------------------------
    # UI poll — runs on the main thread via root.after()
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        # --- Check analysis completion and surface result dialog ---
        for kind, st, noun in (
            ("Motility", self._motility_status, "video"),
            ("Crawling", self._crawling_status, "video"),
            ("Counting", self._counting_status, "plate"),
        ):
            result = st.pop_completed()
            if result:
                n_ok = result["n_ok"]
                n_fail = result["n_fail"]
                out_dir = result["out_dir"]
                msg = (
                    f"{kind} analysis complete:\n"
                    f"  {n_ok} {noun}s processed, {n_fail} failed.\n\n"
                    f"Open results folder?"
                )
                if messagebox.askyesno("Analysis Complete", msg):
                    os.startfile(str(out_dir))

        # --- Status row: a running analysis takes priority ---
        motility_snap = self._motility_status.snapshot()
        crawling_snap = self._crawling_status.snapshot()
        counting_snap = self._counting_status.snapshot()
        snap = (
            motility_snap if motility_snap.running
            else crawling_snap if crawling_snap.running
            else counting_snap
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
        self._status_lbl.configure(
            text=widgets.middle_truncate(display_label, self._STATUS_MAX)
        )
        self._status_tip.set_text(display_label)

        # Sync button lockout resolves on sync color, not display color
        if self._button_waiting and s_color == "green":
            self._button_waiting = False
            self._sync_btn.configure(state="normal")

        parts = []
        if last_sync:
            parts.append(f"Last sync: {last_sync}")
        parts.append(f"{files} files mirrored · {_fmt_bytes(nbytes)}")
        info_full = "  ".join(parts)
        self._info_lbl.configure(text=widgets.middle_truncate(info_full, self._INFO_MAX))
        self._info_tip.set_text(info_full)

        self.after(_POLL_MS, self._poll)

    # ------------------------------------------------------------------
    # Button handlers — all run on the main thread
    # ------------------------------------------------------------------

    def _open_imaging(self) -> None:
        url = f"{self._settings.pi_url}/?token={self._settings.token}"
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
        _log.info("Settings update propagated to: sync, motility, crawling, counting")

    def _on_close(self) -> None:
        self._agent.stop()
        self.destroy()
