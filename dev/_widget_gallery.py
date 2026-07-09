"""
Throwaway visual gallery for the WormScan widget layer (Phase 1).

NOT imported anywhere. Run it to eyeball every component:

    python -m launcher._widget_gallery
    # or:  python launcher/_widget_gallery.py

Shows every button variant, a titled Card, a HairlineSeparator, a cyclable
StatusDot, two Spins with a live readout, a FolderList with a remove-selected
proof, a Tooltip on an over-long truncated label, and both ProgressBar modes.
"""
import os
import sys

# Make `import theme` / `import widgets` resolve whether launched as a module
# (python -m launcher._widget_gallery) or as a script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk  # noqa: E402

import customtkinter as ctk  # noqa: E402

import theme  # noqa: E402
import widgets  # noqa: E402


def main() -> None:
    theme.init()

    root = ctk.CTk()
    root.title("WormScan widget gallery")
    root.configure(fg_color=theme.BG)
    root.geometry("460x900")
    root.resizable(False, False)

    outer = ctk.CTkScrollableFrame(root, fg_color=theme.BG, width=440, height=880)
    outer.pack(fill="both", expand=True, padx=8, pady=8)

    def heading(text: str) -> None:
        ctk.CTkLabel(
            outer, text=text, font=theme.title(), text_color=theme.TEXT, anchor="w"
        ).pack(fill="x", pady=(12, 2))

    # --- Buttons ---
    heading("Buttons")
    widgets.primary_button(outer, "Primary action", lambda: None).pack(fill="x", pady=3)
    widgets.secondary_button(outer, "Secondary action", lambda: None).pack(fill="x", pady=3)
    widgets.destructive_button(outer, "Destructive action", lambda: None).pack(fill="x", pady=3)

    # --- IconButtons (Windows icon-font glyphs) ---
    heading(f"IconButton  (font: {widgets.ICON_FONT_PATH or 'NONE -> text only'})")
    icon_specs = [
        ("Imaging", widgets.GLYPH_CAMERA, "secondary"),
        ("Analyze", widgets.GLYPH_CHART, "primary"),
        ("Review", widgets.GLYPH_GRID, "secondary"),
        ("Mirror Folder", widgets.GLYPH_FOLDER, "secondary"),
        ("Sync Now", widgets.GLYPH_REFRESH, "primary"),
        ("Shut Down Pi", widgets.GLYPH_POWER, "destructive"),
        ("Settings", widgets.GLYPH_SETTINGS, "secondary"),
    ]
    for text, glyph, variant in icon_specs:
        widgets.IconButton(outer, text, lambda: None, glyph, variant=variant).pack(
            fill="x", pady=3
        )
    icon_only_row = ctk.CTkFrame(outer, fg_color="transparent")
    icon_only_row.pack(fill="x", pady=3)
    ctk.CTkLabel(
        icon_only_row, text="icon_only:", font=theme.body(), text_color=theme.TEXT
    ).pack(side="left", padx=(0, 8))
    widgets.IconButton(
        icon_only_row, "Settings", lambda: None, widgets.GLYPH_SETTINGS,
        variant="secondary", icon_only=True,
    ).pack(side="left")

    # --- Card ---
    heading("Card")
    card = widgets.Card(outer, title="A titled card")
    card.pack(fill="x", pady=4)
    ctk.CTkLabel(
        card.content, text="Children live in card.content.", font=theme.body(),
        text_color=theme.TEXT_2, anchor="w",
    ).pack(fill="x")
    ctk.CTkLabel(
        card.content, text="Second content line.", font=theme.body(),
        text_color=theme.TEXT_2, anchor="w",
    ).pack(fill="x")

    # --- Separator ---
    heading("HairlineSeparator")
    widgets.HairlineSeparator(outer).pack(fill="x", pady=8)

    # --- StatusDot ---
    heading("StatusDot (click to cycle)")
    dot_row = ctk.CTkFrame(outer, fg_color="transparent")
    dot_row.pack(fill="x", pady=2)
    dot = widgets.StatusDot(dot_row, size=16)
    dot.pack(side="left")
    dot_label = ctk.CTkLabel(dot_row, text="gray", font=theme.body(), text_color=theme.TEXT)
    dot_label.pack(side="left", padx=8)

    cycle = ["gray", "green", "yellow", "red"]
    state = {"i": 0}

    def cycle_dot() -> None:
        state["i"] = (state["i"] + 1) % len(cycle)
        name = cycle[state["i"]]
        dot.set_color(theme.DOT_COLORS[name])
        dot_label.configure(text=f"{name} → {theme.DOT_COLORS[name]}")

    dot.set_color(theme.DOT_COLORS["gray"])
    widgets.secondary_button(dot_row, "Cycle", cycle_dot).pack(side="right")

    # --- Spins ---
    heading("Spin (live readout)")
    spin_card = widgets.Card(outer)
    spin_card.pack(fill="x", pady=4)

    v1 = tk.StringVar(value="5.0")
    v2 = tk.StringVar(value="30")
    readout = ctk.CTkLabel(
        spin_card.content, text="", font=theme.caption(), text_color=theme.TEXT_2, anchor="w"
    )

    def refresh_readout(*_a) -> None:
        readout.configure(text=f"%.1f spin = {v1.get()}    %.0f spin = {v2.get()}")

    r1 = ctk.CTkFrame(spin_card.content, fg_color="transparent")
    r1.pack(fill="x", pady=2)
    ctk.CTkLabel(r1, text="Min fragment length (s)", font=theme.body(), text_color=theme.TEXT).pack(side="left")
    widgets.Spin(r1, v1, from_=1.0, to=30.0, increment=0.5, fmt="%.1f", width=110).pack(side="right")

    r2 = ctk.CTkFrame(spin_card.content, fg_color="transparent")
    r2.pack(fill="x", pady=2)
    ctk.CTkLabel(r2, text="Min track span (s)", font=theme.body(), text_color=theme.TEXT).pack(side="left")
    widgets.Spin(r2, v2, from_=1, to=600, increment=5, fmt="%.0f", width=110).pack(side="right")

    readout.pack(fill="x", pady=(6, 0))
    v1.trace_add("write", refresh_readout)
    v2.trace_add("write", refresh_readout)
    refresh_readout()

    # --- FolderList ---
    heading("FolderList (select a row, then Remove)")
    fake = [
        r"C:\Users\Isabe\Documents\WormScan\experiments\2026-06-10 dose-response day0\N2 50uM\plate03",
        r"C:\Users\Isabe\Documents\WormScan\experiments\2026-06-11 dose-response day1\daf-16 100uM\plate07",
        r"C:\Users\Isabe\Documents\WormScan\pictures\2026-06-12\survival-scan-final-replicate-batch",
    ]
    flist = widgets.FolderList(outer, height=5)
    flist.pack(fill="x", pady=4)
    flist.set_folders(fake)

    sel_label = ctk.CTkLabel(
        outer, text="selected_index() → None", font=theme.caption(), text_color=theme.TEXT_2, anchor="w"
    )

    def on_select(_idx: int) -> None:
        sel_label.configure(text=f"selected_index() → {flist.selected_index()}")

    flist._on_select = on_select  # noqa: SLF001 - gallery wiring only

    def remove_selected() -> None:
        idx = flist.selected_index()
        if idx is None:
            sel_label.configure(text="selected_index() → None (nothing to remove)")
            return
        del fake[idx]
        flist.set_folders(fake)
        flist._on_select = on_select  # re-wire after re-render
        sel_label.configure(text=f"Removed index {idx}; selected_index() → {flist.selected_index()}")

    frow = ctk.CTkFrame(outer, fg_color="transparent")
    frow.pack(fill="x", pady=2)
    widgets.secondary_button(frow, "Remove selected", remove_selected).pack(side="left")
    sel_label.pack(fill="x", pady=(4, 0))

    # --- Tooltip on a truncated label ---
    heading("Tooltip on truncated label (hover it)")
    long_text = (
        "This is a deliberately over-long status string that would otherwise "
        "stretch the fixed-width window far past its intended width."
    )
    trunc_lbl = ctk.CTkLabel(
        outer, text=widgets.middle_truncate(long_text, 48), font=theme.body(),
        text_color=theme.TEXT, anchor="w",
    )
    trunc_lbl.pack(fill="x", pady=2)
    widgets.Tooltip(trunc_lbl, long_text)

    # --- ProgressBars ---
    heading("ProgressBar — determinate (slider)")
    det = widgets.ProgressBar(outer, mode="determinate")
    det.pack(fill="x", pady=4)
    det.update(0, 100)
    slider = ctk.CTkSlider(
        outer, from_=0, to=100, command=lambda v: det.update(float(v), 100),
        progress_color=theme.ACCENT, button_color=theme.ACCENT, button_hover_color=theme.ACCENT_HOVER,
    )
    slider.set(0)
    slider.pack(fill="x", pady=2)

    heading("ProgressBar — indeterminate (start/stop)")
    indet = widgets.ProgressBar(outer, mode="indeterminate")
    indet.pack(fill="x", pady=4)
    ind_row = ctk.CTkFrame(outer, fg_color="transparent")
    ind_row.pack(fill="x", pady=2)
    widgets.secondary_button(ind_row, "Start", indet.start).pack(side="left")
    widgets.secondary_button(ind_row, "Stop", indet.stop).pack(side="left", padx=8)

    root.mainloop()


if __name__ == "__main__":
    main()
