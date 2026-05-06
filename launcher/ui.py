"""
WormScan Launcher UI — Tkinter main window and settings dialog.

Thread boundary: this module runs entirely on the main (Tk) thread.
It reads sync state only through SyncStatus.snapshot() via root.after().
No widget method is ever called from the sync thread.
"""
import os
import threading
import tkinter as tk
import webbrowser
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

import config as cfg
from analysis.docker_utils import run_preflight
from analysis.motility import MotilityAgent, MotilityStatus
from sync import SyncAgent, SyncStatus

_DOT_COLORS: dict[str, str] = {
    "green": "#4caf50",
    "yellow": "#ffb300",
    "red":    "#f44336",
    "gray":   "#9e9e9e",
}

_POLL_MS = 2000        # main window refresh interval
_PROGRESS_POLL_MS = 200  # progress dialog refresh interval

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
    Modeless progress window that tracks motility analysis in real time.
    Polls MotilityStatus at 200ms. Auto-closes when running becomes False.
    """

    def __init__(
        self,
        parent: tk.Tk,
        agent: MotilityAgent,
        status: MotilityStatus,
    ) -> None:
        super().__init__(parent)
        self.title("WormScan Motility Analysis")
        self.resizable(False, False)
        self.transient(parent)
        # Not modal — no grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._agent = agent
        self._status = status
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
                text=f"Video {snap.current_index + 1} of {total}: {snap.current_basename}"
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
        counting_rb = ttk.Radiobutton(
            rb_frame, text="Counting", variable=self._mode,
            value="counting", state="disabled",
        )
        counting_rb.pack(side="left", padx=(12, 0))
        _add_tooltip(counting_rb, "Not yet built")

        # Row 1 — folder picker
        ttk.Label(self, text="Video folder").grid(row=1, column=0, sticky="w", **pad)
        self._folder_var = tk.StringVar(value=self._settings.mirror_root)
        ttk.Entry(self, textvariable=self._folder_var, width=36).grid(
            row=1, column=1, sticky="ew", **pad
        )
        ttk.Button(self, text="…", width=3, command=self._browse).grid(
            row=1, column=2, **pad
        )

        # Row 2 — threshold spinbox
        ttk.Label(self, text="Min fragment length (s)").grid(
            row=2, column=0, sticky="w", **pad
        )
        self._threshold_var = tk.StringVar(
            value=str(self._settings.motility_long_threshold_s)
        )
        ttk.Spinbox(
            self, textvariable=self._threshold_var,
            from_=1.0, to=30.0, increment=0.5, width=6, format="%.1f",
        ).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(
            self,
            text="Recommended: 5–10 s.  Higher = stricter but biases toward slower worms.",
            font="TkSmallCaptionFont", foreground="#555555",
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4))

        # Row 4 — clear-cache checkbox
        self._clear_cache_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self, text="Clear cache before run",
            variable=self._clear_cache_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 4))

        # Row 5 — render options
        render_frame = ttk.LabelFrame(self, text="Video render options", padding=(8, 4))
        render_frame.grid(row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 2))
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

    def _start(self) -> None:
        if self._status.is_running():
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

        errors = run_preflight(self._settings, folder)
        if errors:
            messagebox.showerror(
                "Pre-flight checks failed",
                "\n\n".join(errors),
                parent=self,
            )
            return

        # Persist the chosen threshold for next launch
        new_settings = replace(self._settings, motility_long_threshold_s=threshold_s)
        self._on_settings_update(new_settings)

        # Open progress dialog before waking the agent (so it's ready to poll)
        AnalysisProgressDialog(self._parent, self._agent, self._status)

        self._agent.start_analysis(
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
# Main window
# ---------------------------------------------------------------------------

class MainWindow(tk.Tk):
    def __init__(
        self,
        settings: cfg.Settings,
        agent: SyncAgent,
        status: SyncStatus,
        motility_agent: MotilityAgent,
        motility_status: MotilityStatus,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._agent = agent
        self._status = status
        self._motility_agent = motility_agent
        self._motility_status = motility_status
        self._button_waiting = False

        self.title("WormScan Launcher")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._poll()

    def _build(self) -> None:
        outer = {"padx": 14, "pady": 6}

        # --- Status row ---
        status_row = ttk.Frame(self)
        status_row.pack(fill="x", **outer)

        self._canvas = tk.Canvas(
            status_row, width=18, height=18, highlightthickness=0
        )
        self._canvas.pack(side="left")
        self._dot = self._canvas.create_oval(2, 2, 16, 16, fill="#9e9e9e", outline="")

        self._status_lbl = ttk.Label(status_row, text="Starting…")
        self._status_lbl.pack(side="left", padx=(8, 0))

        # --- Buttons ---
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=14, pady=2)

        ttk.Button(
            btn_frame, text="Open Imaging UI", command=self._open_imaging
        ).pack(fill="x", pady=3)

        self._analysis_btn = ttk.Button(
            btn_frame, text="Open Analysis", command=self._open_analysis
        )
        self._analysis_btn.pack(fill="x", pady=3)

        ttk.Button(
            btn_frame, text="Open Mirror Folder", command=self._open_mirror
        ).pack(fill="x", pady=3)

        self._sync_btn = ttk.Button(
            btn_frame, text="Sync now", command=self._on_sync_now
        )
        self._sync_btn.pack(fill="x", pady=3)

        ttk.Separator(btn_frame, orient="horizontal").pack(fill="x", pady=(6, 3))

        ttk.Button(
            btn_frame, text="Shut down Pi", command=self._shutdown_pi
        ).pack(fill="x", pady=3)

        # --- Info / settings row ---
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=14, pady=(4, 10))

        self._info_lbl = ttk.Label(bottom, text="", font="TkSmallCaptionFont")
        self._info_lbl.pack(side="left", fill="x", expand=True)

        ttk.Button(
            bottom, text="Settings", command=self._open_settings
        ).pack(side="right")

    # ------------------------------------------------------------------
    # UI poll — runs on the main thread via root.after()
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        # --- Check motility completion and surface result dialog ---
        result = self._motility_status.pop_completed()
        if result:
            n_ok = result["n_ok"]
            n_fail = result["n_fail"]
            out_dir = result["out_dir"]
            msg = (
                f"Motility analysis complete:\n"
                f"  {n_ok} videos processed, {n_fail} failed.\n\n"
                f"Open results folder?"
            )
            if messagebox.askyesno("Analysis Complete", msg):
                os.startfile(str(out_dir))

        # --- Status row: motility takes priority when running ---
        snap = self._motility_status.snapshot()
        s_color, s_label, last_sync, files, nbytes = self._status.snapshot()

        if snap.running:
            display_color = snap.color
            display_label = snap.label
        else:
            clock_msg = self._status.get_clock_msg()
            display_color = s_color
            display_label = clock_msg if clock_msg else s_label

        self._canvas.itemconfig(self._dot, fill=_DOT_COLORS.get(display_color, "#9e9e9e"))
        self._status_lbl.config(text=display_label)

        # Sync button lockout resolves on sync color, not display color
        if self._button_waiting and s_color == "green":
            self._button_waiting = False
            self._sync_btn.config(state="normal")

        parts = []
        if last_sync:
            parts.append(f"Last sync: {last_sync}")
        parts.append(f"{files} files mirrored · {_fmt_bytes(nbytes)}")
        self._info_lbl.config(text="  ".join(parts))

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
            self._on_settings_saved,
        )

    def _open_mirror(self) -> None:
        mirror = self._settings.mirror_root
        os.makedirs(mirror, exist_ok=True)
        os.startfile(mirror)

    def _on_sync_now(self) -> None:
        self._sync_btn.config(state="disabled")
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

    def _on_close(self) -> None:
        self._agent.stop()
        self.destroy()
