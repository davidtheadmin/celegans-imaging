"""
WormScan Launcher — reusable CustomTkinter widget layer.

Pure presentation. Nothing in this module imports the agents, the status
objects, or touches the root.after polling model; later UI phases compose these
components but the thread/contract boundaries live elsewhere.

Signatures here are pinned — later phases depend on them.
"""
import os
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

import theme


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def middle_truncate(text: str, max_chars: int) -> str:
    """Shorten `text` to at most `max_chars` characters as "start…end".

    Keeps the head and tail and elides the middle with an ellipsis, so a long
    status string can sit on one fixed-width line (the full text belongs in a
    Tooltip). Short strings are returned unchanged.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    keep = max_chars - 1            # one char spent on the ellipsis
    head = (keep + 1) // 2
    tail = keep // 2
    if tail == 0:
        return text[:head] + "…"
    return text[:head] + "…" + text[-tail:]


# ---------------------------------------------------------------------------
# Tooltip
# ---------------------------------------------------------------------------

class Tooltip:
    """A light hover tooltip: CARD background, HAIRLINE border, TEXT text.

    `.set_text(full)` updates the contents so a caller can keep the tooltip in
    sync with a label whose (possibly truncated) text changes. An empty text
    suppresses the tooltip entirely.
    """

    def __init__(self, widget: tk.Widget, text: str = "") -> None:
        self._widget = widget
        self._text = text
        self._tip: Optional[tk.Toplevel] = None
        self._label: Optional[tk.Label] = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def set_text(self, full: str) -> None:
        self._text = full or ""
        if self._label is not None:
            self._label.configure(text=self._text)
        if not self._text:
            self._hide()

    def _show(self, event: tk.Event) -> None:
        if not self._text or self._tip is not None:
            return
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 16}")
        # 1px hairline border via an outer frame, light card inside.
        border = tk.Frame(self._tip, background=theme.HAIRLINE)
        border.pack()
        self._label = tk.Label(
            border,
            text=self._text,
            background=theme.CARD,
            foreground=theme.TEXT,
            font=(theme.FONT_FAMILY, theme.CAPTION[0]),
            justify="left",
            padx=8,
            pady=4,
        )
        self._label.pack(padx=1, pady=1)

    def _hide(self, _event: Optional[tk.Event] = None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
            self._label = None


# ---------------------------------------------------------------------------
# Status dot
# ---------------------------------------------------------------------------

class StatusDot(ctk.CTkLabel):
    """A coloured "●" bullet. `.set_color(hex)` recolours it."""

    def __init__(self, parent: tk.Widget, size: int = 12) -> None:
        super().__init__(
            parent,
            text="●",
            font=ctk.CTkFont(family=theme.FONT_FAMILY, size=size),
            text_color=theme.DOT_GRAY,
            width=size + 4,
        )

    def set_color(self, hex_color: str) -> None:
        self.configure(text_color=hex_color)


# ---------------------------------------------------------------------------
# Card + separator
# ---------------------------------------------------------------------------

class Card(ctk.CTkFrame):
    """A white surface with a hairline border and rounded corners.

    Add children to `card.content` (an inner transparent frame). An optional
    title is shown at the top in the TITLE font.
    """

    def __init__(self, parent: tk.Widget, title: str = "") -> None:
        super().__init__(
            parent,
            fg_color=theme.CARD,
            corner_radius=theme.CARD_RADIUS,
            border_width=1,
            border_color=theme.HAIRLINE,
        )
        self._title_lbl: Optional[ctk.CTkLabel] = None
        if title:
            self._title_lbl = ctk.CTkLabel(
                self, text=title, font=theme.title(), text_color=theme.TEXT,
                anchor="w",
            )
            self._title_lbl.pack(fill="x", padx=14, pady=(12, 0))
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(fill="both", expand=True, padx=14, pady=12)

    def set_title(self, title: str) -> None:
        if self._title_lbl is not None:
            self._title_lbl.configure(text=title)


class HairlineSeparator(ctk.CTkFrame):
    """A 1px horizontal rule in the hairline colour."""

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, height=1, fg_color=theme.HAIRLINE, corner_radius=0)


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

def primary_button(
    parent: tk.Widget, text: str, command: Callable[[], None]
) -> ctk.CTkButton:
    """Accent-filled action button with white text."""
    return ctk.CTkButton(
        parent, text=text, command=command,
        fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        text_color="#FFFFFF", corner_radius=theme.BTN_RADIUS,
        font=theme.body(),
    )


def secondary_button(
    parent: tk.Widget, text: str, command: Callable[[], None]
) -> ctk.CTkButton:
    """Quiet button: white fill, hairline border, dark text."""
    return ctk.CTkButton(
        parent, text=text, command=command,
        fg_color=theme.CARD, hover_color=theme.HAIRLINE,
        text_color=theme.TEXT, border_width=1, border_color=theme.HAIRLINE,
        corner_radius=theme.BTN_RADIUS, font=theme.body(),
    )


def destructive_button(
    parent: tk.Widget, text: str, command: Callable[[], None]
) -> ctk.CTkButton:
    """Destructive action button (e.g. Shut down): destructive tint."""
    return ctk.CTkButton(
        parent, text=text, command=command,
        fg_color=theme.DESTRUCTIVE, hover_color="#D8302A",
        text_color="#FFFFFF", corner_radius=theme.BTN_RADIUS,
        font=theme.body(),
    )


# ---------------------------------------------------------------------------
# Spin — entry flanked by ± steppers, bound to a StringVar
# ---------------------------------------------------------------------------

class Spin(ctk.CTkFrame):
    """A numeric stepper that drives a caller-owned StringVar.

    The − / + buttons clamp to [from_, to]; the entry reformats with `fmt` on
    focus-out. Callers keep reading `variable.get()` exactly as with a
    ttk.Spinbox — this widget never changes the variable's identity.
    """

    def __init__(
        self,
        parent: tk.Widget,
        variable: tk.StringVar,
        from_: float,
        to: float,
        increment: float,
        fmt: str = "%.1f",
        width: int = 90,
    ) -> None:
        super().__init__(parent, fg_color="transparent")
        self._var = variable
        self._from = from_
        self._to = to
        self._increment = increment
        self._fmt = fmt

        btn_w = 26
        entry_w = max(36, width - 2 * btn_w - 8)

        self._minus = ctk.CTkButton(
            self, text="−", width=btn_w, command=self._dec,
            fg_color=theme.CARD, hover_color=theme.HAIRLINE,
            text_color=theme.TEXT, border_width=1, border_color=theme.HAIRLINE,
            corner_radius=theme.BTN_RADIUS, font=theme.body(),
        )
        self._minus.pack(side="left")
        self._entry = ctk.CTkEntry(
            self, textvariable=variable, width=entry_w, justify="center",
            fg_color=theme.CARD, text_color=theme.TEXT,
            border_color=theme.HAIRLINE, border_width=1,
            corner_radius=theme.BTN_RADIUS, font=theme.body(),
        )
        self._entry.pack(side="left", padx=4)
        self._plus = ctk.CTkButton(
            self, text="+", width=btn_w, command=self._inc,
            fg_color=theme.CARD, hover_color=theme.HAIRLINE,
            text_color=theme.TEXT, border_width=1, border_color=theme.HAIRLINE,
            corner_radius=theme.BTN_RADIUS, font=theme.body(),
        )
        self._plus.pack(side="left")

        self._entry.bind("<FocusOut>", self._on_focus_out)

    def _parse(self) -> float:
        try:
            return float(self._var.get())
        except (ValueError, TypeError):
            return self._from

    def _clamp(self, value: float) -> float:
        return max(self._from, min(self._to, value))

    def _commit(self, value: float) -> None:
        self._var.set(self._fmt % self._clamp(value))

    def _dec(self) -> None:
        self._commit(self._parse() - self._increment)

    def _inc(self) -> None:
        self._commit(self._parse() + self._increment)

    def _on_focus_out(self, _event: tk.Event) -> None:
        self._commit(self._parse())


# ---------------------------------------------------------------------------
# FolderList — scrollable selectable list
# ---------------------------------------------------------------------------

class FolderList(ctk.CTkScrollableFrame):
    """A scrollable list of folder paths with single-row selection.

    Mirrors tk.Listbox semantics needed by the Review dialog:
      - .set_folders(list) re-renders; each row is middle-truncated with a
        Tooltip carrying the full path.
      - .selected_index() returns the selected row index, or None when nothing
        is selected (matching an empty curselection()).
    Selecting a row highlights it in ACCENT and fires on_select(index).
    """

    def __init__(
        self,
        parent: tk.Widget,
        on_select: Optional[Callable[[int], None]] = None,
        height: int = 5,
    ) -> None:
        # height is in rows; translate to an approximate pixel height.
        super().__init__(
            parent, fg_color=theme.CARD, corner_radius=theme.CARD_RADIUS,
            border_width=1, border_color=theme.HAIRLINE,
            height=height * 26,
        )
        self._on_select = on_select
        self._folders: list[str] = []
        self._rows: list[ctk.CTkButton] = []
        self._tips: list[Tooltip] = []
        self._selected: Optional[int] = None

    def set_folders(self, folders: list[str]) -> None:
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        self._tips.clear()
        self._folders = list(folders)
        self._selected = None
        for i, path in enumerate(self._folders):
            btn = ctk.CTkButton(
                self,
                text=middle_truncate(path, 52),
                anchor="w",
                command=lambda idx=i: self._select(idx),
                fg_color="transparent", hover_color=theme.BG,
                text_color=theme.TEXT, corner_radius=theme.BTN_RADIUS,
                font=theme.body(),
            )
            btn.pack(fill="x", padx=2, pady=1)
            self._rows.append(btn)
            self._tips.append(Tooltip(btn, path))

    def _select(self, index: int) -> None:
        self._selected = index
        for i, row in enumerate(self._rows):
            if i == index:
                row.configure(fg_color=theme.ACCENT, text_color="#FFFFFF")
            else:
                row.configure(fg_color="transparent", text_color=theme.TEXT)
        if self._on_select is not None:
            self._on_select(index)

    def selected_index(self) -> Optional[int]:
        return self._selected


# ---------------------------------------------------------------------------
# ProgressBar — wraps CTkProgressBar (determinate or indeterminate)
# ---------------------------------------------------------------------------

class ProgressBar(ctk.CTkProgressBar):
    """A progress bar that speaks both the analysis dialogs' idioms.

    determinate: .update(current, total) maps to a [0,1] fraction with the edge
    cases the old ttk code relied on (total==0 → 0, current==total → full).
    indeterminate: .start() / .stop().
    """

    def __init__(self, parent: tk.Widget, mode: str = "determinate") -> None:
        super().__init__(
            parent, mode=mode,
            progress_color=theme.ACCENT, fg_color=theme.HAIRLINE,
            corner_radius=theme.BTN_RADIUS,
        )
        self._mode = mode
        if mode == "determinate":
            self.set(0)

    def update(self, current: float, total: float) -> None:  # noqa: A003 - intentional API name
        if total <= 0:
            self.set(0)
            return
        fraction = current / total
        fraction = max(0.0, min(1.0, fraction))
        self.set(fraction)


# ---------------------------------------------------------------------------
# IconButton — CTkButton with a Windows icon-font glyph rendered to an image
# ---------------------------------------------------------------------------

# Segoe Fluent Icons (Win11) preferred; Segoe MDL2 Assets (Win10) fallback.
# Both share these code points, so the glyph constants work either way.
_SEGOE_FLUENT = r"C:\Windows\Fonts\SegoeIcons.ttf"
_SEGOE_MDL2 = r"C:\Windows\Fonts\segmdl2.ttf"


def _resolve_icon_font() -> Optional[str]:
    for path in (_SEGOE_FLUENT, _SEGOE_MDL2):
        if os.path.exists(path):
            return path
    return None


ICON_FONT_PATH: Optional[str] = _resolve_icon_font()

# Verified code points (rendered and eyeballed against Segoe Fluent Icons):
GLYPH_CAMERA = 0xE722    # camera
GLYPH_CHART = 0xE9D9     # activity / analytics
GLYPH_GRID = 0xE8A9      # 2x2 grid view
GLYPH_FOLDER = 0xE8B7    # folder
GLYPH_REFRESH = 0xE72C   # refresh / sync
GLYPH_POWER = 0xE7E8     # power button
GLYPH_SETTINGS = 0xE713  # settings gear

_ICON_PX = 18

_VARIANTS = {
    "primary": dict(
        fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER, text_color="#FFFFFF",
    ),
    "secondary": dict(
        fg_color=theme.CARD, hover_color=theme.HAIRLINE, text_color=theme.TEXT,
        border_width=1, border_color=theme.HAIRLINE,
    ),
    "destructive": dict(
        fg_color=theme.DESTRUCTIVE, hover_color="#D8302A", text_color="#FFFFFF",
    ),
}


def _glyph_image(
    codepoint: Optional[int], color_hex: str, px: int = _ICON_PX
) -> tuple[Optional[Image.Image], Optional[tuple[int, int]]]:
    """Render a single icon-font glyph to a transparent RGBA image in color_hex.

    Returns (image, display_size) or (None, None) if the font is unavailable or
    the glyph fails to render — callers then fall back to text-only.
    """
    if ICON_FONT_PATH is None or codepoint is None:
        return None, None
    try:
        scale = 3  # render oversized, let CTkImage downsample crisply for HiDPI
        font = ImageFont.truetype(ICON_FONT_PATH, px * scale)
        ch = chr(codepoint)
        bb = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), ch, font=font)
        w = max(1, bb[2] - bb[0])
        h = max(1, bb[3] - bb[1])
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((-bb[0], -bb[1]), ch, font=font, fill=color_hex)
        disp_w = max(1, round(w * px / h))
        return img, (disp_w, px)
    except Exception:
        return None, None


def IconButton(
    parent: tk.Widget,
    text: str,
    command: Callable[[], None],
    glyph: Optional[int],
    variant: str = "secondary",
    icon_only: bool = False,
) -> ctk.CTkButton:
    """A normal CTkButton with a Windows icon-font glyph rendered to its left.

    variant ∈ {primary, secondary, destructive} maps to the theme button colors;
    the glyph is drawn in the button's text color at ~18px. If the icon font is
    missing or the glyph won't render, the button degrades to text-only without
    crashing. Returns a plain CTkButton so callers can .pack()/.configure() and
    attach a Tooltip exactly as with the factory buttons.
    """
    colors = dict(_VARIANTS.get(variant, _VARIANTS["secondary"]))
    glyph_color = colors["text_color"]
    img, size = _glyph_image(glyph, glyph_color)

    btn_text = "" if icon_only else text
    anchor = "center" if icon_only else "w"

    common = dict(
        text=btn_text, command=command, corner_radius=theme.BTN_RADIUS,
        font=theme.body(), anchor=anchor, **colors,
    )
    if icon_only:
        common["width"] = 40

    if img is not None:
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=size)
        btn = ctk.CTkButton(parent, image=ctk_img, compound="left", **common)
        btn._icon_image = ctk_img  # keep a reference so it isn't GC'd
    else:
        # Font missing / glyph failed: text-only fallback. For icon_only with no
        # glyph, fall back to showing the text so the control is still usable.
        if icon_only:
            common["text"] = text
            common["anchor"] = "center"
        btn = ctk.CTkButton(parent, **common)
    return btn
