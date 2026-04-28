"""
WormScan Launcher UI — Tkinter main window and settings dialog.

Thread boundary: this module runs entirely on the main (Tk) thread.
It reads sync state only through SyncStatus.snapshot() via root.after().
No widget method is ever called from the sync thread.
"""
import os
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

import config as cfg
from sync import SyncAgent, SyncStatus

_DOT_COLORS: dict[str, str] = {
    "green": "#4caf50",
    "yellow": "#ffb300",
    "red":    "#f44336",
    "gray":   "#9e9e9e",
}

_POLL_MS = 2000   # how often the UI refreshes from SyncStatus


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
        new = cfg.Settings(
            pi_url=self._pi_url.get().rstrip("/"),
            token=self._token.get(),
            mirror_root=self._mirror.get(),
            poll_interval_s=poll,
        )
        self._on_save(new)
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
    ) -> None:
        super().__init__()
        self._settings = settings
        self._agent = agent
        self._status = status
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
            btn_frame, text="Open Analysis", state="disabled"
        )
        self._analysis_btn.pack(fill="x", pady=3)
        _add_tooltip(self._analysis_btn, "Not yet built")

        ttk.Button(
            btn_frame, text="Open Mirror Folder", command=self._open_mirror
        ).pack(fill="x", pady=3)

        self._sync_btn = ttk.Button(
            btn_frame, text="Sync now", command=self._on_sync_now
        )
        self._sync_btn.pack(fill="x", pady=3)

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
        color, label, last_sync, files, nbytes = self._status.snapshot()

        clock_msg = self._status.get_clock_msg()
        display_label = clock_msg if clock_msg else label

        self._canvas.itemconfig(self._dot, fill=_DOT_COLORS.get(color, "#9e9e9e"))
        self._status_lbl.config(text=display_label)

        if self._button_waiting and color == "green":
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
        # Token in the URL here is consumed by the browser's JavaScript
        # (index.html reads ?token= and removes it from the address bar).
        # This is intentional: the static file serving is unauthenticated,
        # and the JS then uses the token for subsequent API calls via headers.
        url = f"{self._settings.pi_url}/?token={self._settings.token}"
        webbrowser.open_new_tab(url)

    def _open_mirror(self) -> None:
        mirror = self._settings.mirror_root
        os.makedirs(mirror, exist_ok=True)
        os.startfile(mirror)   # Windows only — launcher is Windows-only

    def _on_sync_now(self) -> None:
        self._sync_btn.config(state="disabled")
        self._button_waiting = True
        self._agent.wake()

    def _open_settings(self) -> None:
        SettingsDialog(self, self._settings, self._on_settings_saved)

    def _on_settings_saved(self, new: cfg.Settings) -> None:
        cfg.save(new)
        self._settings = new
        self._agent.update_settings(new)

    def _on_close(self) -> None:
        self._agent.stop()
        self.destroy()
