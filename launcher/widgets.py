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


def _leaf_name(path: str) -> str:
    """Last path component, tolerating a trailing separator. Falls back to the
    whole string for a drive root like 'D:\\', which has no leaf."""
    return os.path.basename(str(path).rstrip("/\\")) or str(path)


# ---------------------------------------------------------------------------
# Tooltip
# ---------------------------------------------------------------------------

class Tooltip:
    """A light hover tooltip: CARD background, HAIRLINE border, TEXT text.

    `.set_text(full)` updates the contents so a caller can keep the tooltip in
    sync with a label whose (possibly truncated) text changes. An empty text
    suppresses the tooltip entirely.

    `delay_ms` holds the tip back until the pointer has rested. Use it for
    supporting detail — a full path under a folder name — where a tip that
    fires the instant the mouse crosses the row would flicker its way down a
    list. Leave it at 0 for a tip that IS the explanation.
    """

    def __init__(self, widget: tk.Widget, text: str = "",
                 delay_ms: int = 0) -> None:
        self._widget = widget
        self._text = text
        self._delay_ms = max(0, int(delay_ms))
        self._after_id: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        self._label: Optional[tk.Label] = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def set_text(self, full: str) -> None:
        self._text = full or ""
        if self._label is not None:
            self._label.configure(text=self._text)
        if not self._text:
            self._hide()

    def _enter(self, event: tk.Event) -> None:
        if not self._delay_ms:
            self._show(event)
            return
        self._cancel_pending()
        # The event object is dead by the time the callback runs, so the
        # pointer position is captured now.
        x, y = event.x_root, event.y_root
        try:
            self._after_id = self._widget.after(
                self._delay_ms, lambda: self._show_at(x, y))
        except tk.TclError:
            pass

    def _cancel_pending(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except (tk.TclError, ValueError):
                pass
            self._after_id = None

    def _show_at(self, x_root: int, y_root: int) -> None:
        self._after_id = None
        try:
            if not self._widget.winfo_exists():
                return
        except tk.TclError:
            return
        self._place(x_root, y_root)

    def _show(self, event: tk.Event) -> None:
        self._place(event.x_root, event.y_root)

    def _place(self, x_root: int, y_root: int) -> None:
        if not self._text or self._tip is not None:
            return
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x_root + 12}+{y_root + 16}")
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
        self._cancel_pending()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
            self._label = None


# ---------------------------------------------------------------------------
# Toast — a small always-on-top status card
# ---------------------------------------------------------------------------

class Toast:
    """A small always-on-top card in the corner of the screen. Closable, and
    draggable anywhere the user prefers it.

    For telling the user something while they are looking at a DIFFERENT
    application. "Analyze on laptop" is pressed in a browser on the Pi's web UI,
    so a message inside the launcher window would be behind whatever they are
    actually looking at; before this, the only feedback was the console window
    Windows allocated for the subprocess, which was empty.

    Built the same way as Tooltip — a plain tk.Toplevel with
    wm_overrideredirect and no CustomTkinter involved. That is deliberate: the
    tooltip is the one overlay in this app that has never misbehaved, so this
    copies it rather than inventing yet another approach.

    Because it is borderless it has no window-manager title bar, so both
    affordances are drawn: a × at the top right, and a drag anywhere else on
    the card. A position the user drags to is remembered and reused, including
    for later messages — if they moved it out of the way once, it stays out of
    the way.

    Closing it MUTES it until reset() is called, so it does not reappear on the
    next poll a second later. The caller resets when a new job starts.

    Driven from the UI thread only. show() is idempotent: calling it again just
    updates the text.
    """

    _DRAG_CURSOR = "fleur"
    # Sized to be readable across a room from the microscope, not to be
    # unobtrusive: the whole point is that it is seen while the user is looking
    # at a browser window.
    _WRAP = 360
    _PAD_X = 18
    _PAD_Y = 14

    def __init__(self, master: tk.Misc) -> None:
        self._master = master
        self._win: Optional[tk.Toplevel] = None
        self._title: Optional[tk.Label] = None
        self._body: Optional[tk.Label] = None
        self._pos: Optional[tuple[int, int]] = None   # sticky once dragged
        self._drag: Optional[tuple[int, int, int, int]] = None
        self._muted = False

    # -- public --------------------------------------------------------
    def show(self, title: str, message: str = "", accent: str = "") -> None:
        if self._muted:
            return
        try:
            if self._win is None or not self._win.winfo_exists():
                self._build(accent or theme.ACCENT)
            self._title.configure(text=title)
            self._body.configure(text=message)
            if message:
                self._body.pack(anchor="w", fill="x", pady=(4, 0))
            else:
                self._body.pack_forget()
            self._place()
            self._win.deiconify()
            self._win.lift()
        except tk.TclError:
            self._win = None

    def hide(self) -> None:
        """Take it off screen. Does NOT mute — this is the caller saying the
        job is over, not the user saying they do not want to see it."""
        self._destroy()

    def close(self) -> None:
        """The × . Hides it AND stops it coming back until reset()."""
        self._muted = True
        self._destroy()

    def reset(self) -> None:
        """A new job: the user's dismissal of the last one no longer applies."""
        self._muted = False

    # -- internals -----------------------------------------------------
    def _destroy(self) -> None:
        if self._win is None:
            return
        try:
            self._win.destroy()
        except tk.TclError:
            pass
        self._win = None
        self._title = self._body = None

    def _build(self, accent: str) -> None:
        win = tk.Toplevel(self._master)
        win.wm_overrideredirect(True)
        try:
            win.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        border = tk.Frame(win, background=theme.HAIRLINE)
        border.pack(fill="both", expand=True)
        inner = tk.Frame(border, background=theme.CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        stripe = tk.Frame(inner, background=accent, width=6)
        stripe.pack(side="left", fill="y")
        text = tk.Frame(inner, background=theme.CARD)
        text.pack(side="left", fill="both", expand=True,
                  padx=self._PAD_X, pady=self._PAD_Y)

        head = tk.Frame(text, background=theme.CARD)
        head.pack(fill="x")
        self._title = tk.Label(
            head, text="", background=theme.CARD, foreground=theme.TEXT,
            font=(theme.FONT_FAMILY, theme.BODY[0] + 3, "bold"),
            anchor="w", justify="left", wraplength=self._WRAP,
        )
        self._title.pack(side="left")
        close = tk.Label(
            head, text="\u00d7", background=theme.CARD,
            foreground=theme.TEXT_2, cursor="hand2",
            font=(theme.FONT_FAMILY, theme.BODY[0] + 7), padx=8,
        )
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self.close())
        close.bind("<Enter>", lambda _e: close.configure(foreground=theme.TEXT))
        close.bind("<Leave>", lambda _e: close.configure(foreground=theme.TEXT_2))

        self._body = tk.Label(
            text, text="", background=theme.CARD, foreground=theme.TEXT_2,
            font=(theme.FONT_FAMILY, theme.BODY[0]),
            anchor="w", justify="left", wraplength=self._WRAP,
        )
        self._body.pack(anchor="w", fill="x", pady=(4, 0))

        # Drag anywhere except the × — an overrideredirect window has no title
        # bar to grab, so the card itself is the handle.
        for widget in (border, inner, stripe, text, head, self._title,
                       self._body):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.configure(cursor=self._DRAG_CURSOR)
        self._win = win

    def _drag_start(self, event: tk.Event) -> None:
        if self._win is None:
            return
        self._drag = (event.x_root, event.y_root,
                      self._win.winfo_x(), self._win.winfo_y())

    def _drag_move(self, event: tk.Event) -> None:
        if self._win is None or self._drag is None:
            return
        sx, sy, ox, oy = self._drag
        x = ox + (event.x_root - sx)
        y = oy + (event.y_root - sy)
        self._pos = (x, y)
        try:
            self._win.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

    def _place(self) -> None:
        win = self._win
        win.update_idletasks()
        if self._pos is not None:
            win.wm_geometry(f"+{self._pos[0]}+{self._pos[1]}")
            return
        width = win.winfo_reqwidth()
        try:
            screen_w = win.wm_maxsize()[0]
        except tk.TclError:
            screen_w = win.winfo_screenwidth()
        win.wm_geometry(f"+{max(0, screen_w - width - 24)}+24")


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
# Help text
# ---------------------------------------------------------------------------

class HelpBlock(ctk.CTkFrame):
    """A short list of "**term** — what it does" lines.

    Tk labels have no inline rich text, so each line is two labels: the term in
    semibold, the explanation in the caption colour underneath it. That is the
    whole trick, and it turns a wall of prose into something scannable — which
    is the point, because nobody reads a six-sentence paragraph attached to a
    slider.

    ``items`` is a list of (term, text) pairs. Pass term=None (or "") for a
    plain paragraph with no heading.
    """

    def __init__(
        self,
        parent: tk.Widget,
        items: list[tuple[Optional[str], str]],
        wraplength: int = 360,
    ) -> None:
        super().__init__(parent, fg_color="transparent")
        for i, (term, text) in enumerate(items):
            if term:
                ctk.CTkLabel(
                    self, text=term, font=theme.caption_bold(),
                    text_color=theme.TEXT, anchor="w", justify="left",
                    wraplength=wraplength,
                ).pack(anchor="w", pady=(6 if i else 0, 0))
            ctk.CTkLabel(
                self, text=text, font=theme.caption(), text_color=theme.TEXT_2,
                anchor="w", justify="left", wraplength=wraplength,
            ).pack(anchor="w", pady=(0, 0))


class LabeledRow(ctk.CTkFrame):
    """A control row whose label is semibold, so it matches its HelpBlock term.

    Pack the control into ``.content``; the label sits to its left.
    """

    def __init__(self, parent: tk.Widget, label: str, width: int = 0) -> None:
        super().__init__(parent, fg_color="transparent")
        kw = dict(text=label, font=theme.body_bold(), text_color=theme.TEXT,
                  anchor="w")
        if width:
            kw["width"] = width
        ctk.CTkLabel(self, **kw).pack(side="left")
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(side="left", padx=(8, 0))


# ---------------------------------------------------------------------------
# Collapsible — a disclosure section
# ---------------------------------------------------------------------------

class Collapsible(ctk.CTkFrame):
    """A titled section that folds away. Children go in ``.content``.

    Exists because a per-class slider stack is the tallest thing in a dialog and
    the least often touched — it pushed the Start button off the bottom of the
    screen. Closed by default; the header says how many rows are hidden so the
    control does not simply vanish.
    """

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        subtitle: str = "",
        expanded: bool = False,
    ) -> None:
        super().__init__(parent, fg_color="transparent")
        self._title = title
        self._subtitle = subtitle
        self._expanded = bool(expanded)
        self._btn = ctk.CTkButton(
            self, text="", command=self.toggle, anchor="w",
            fg_color="transparent", hover_color=theme.HAIRLINE,
            text_color=theme.TEXT, corner_radius=theme.BTN_RADIUS,
            font=theme.body_bold(), height=26,
        )
        self._btn.pack(fill="x")
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self._sync()

    def _sync(self) -> None:
        arrow = "\u25be" if self._expanded else "\u25b8"   # ▾ / ▸
        tail = f"   {self._subtitle}" if self._subtitle else ""
        self._btn.configure(text=f"{arrow}  {self._title}{tail}")
        if self._expanded:
            self.content.pack(fill="x", padx=(14, 0), pady=(2, 0))
        else:
            self.content.pack_forget()

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._sync()

    def set_subtitle(self, subtitle: str) -> None:
        self._subtitle = subtitle
        self._sync()

    def is_expanded(self) -> bool:
        return self._expanded


# ---------------------------------------------------------------------------
# InlineNotice — a message that lives INSIDE a window
# ---------------------------------------------------------------------------

class InlineNotice(ctk.CTkFrame):
    """A banner with a title, a message, optional detail and up to two buttons.

    This exists because two separate attempts at a pop-up — first
    tkinter.messagebox, then our own modal Toplevel with an explicit grab —
    both flashed up and vanished on this machine. A message that has its own
    window depends on the window manager, on CustomTkinter's title-bar
    handling, on grab ownership and on which window happens to be topmost, and
    any one of those can eat it. This has no window: it is a frame inside the
    window the user is already looking at. There is nothing left to go wrong.

    It is also better behaviour. The message stays on screen until the user
    acts on it, instead of being a thing they had to catch.
    """

    def __init__(self, parent: tk.Widget, accent: str = "") -> None:
        super().__init__(parent, fg_color=theme.CARD,
                         corner_radius=theme.CARD_RADIUS,
                         border_width=1, border_color=theme.HAIRLINE)
        self._accent = accent or theme.ACCENT
        self._on_confirm: Optional[Callable[[], None]] = None
        self._on_dismiss: Optional[Callable[[], None]] = None

        # A coloured spine down the left edge, so it reads as a notice rather
        # than as one more card.
        self._stripe = ctk.CTkFrame(self, width=4, corner_radius=2,
                                    fg_color=self._accent)
        self._stripe.pack(side="left", fill="y", padx=(6, 0), pady=8)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=10, pady=8)
        self._title = ctk.CTkLabel(
            body, text="", font=theme.body_bold(), text_color=theme.TEXT,
            anchor="w", justify="left",
        )
        self._title.pack(fill="x")
        self._message = ctk.CTkLabel(
            body, text="", font=theme.caption(), text_color=theme.TEXT_2,
            anchor="w", justify="left", wraplength=380,
        )
        self._message.pack(fill="x", pady=(2, 0))
        self._detail = ctk.CTkLabel(
            body, text="", anchor="w", justify="left",
            font=ctk.CTkFont(family="Consolas", size=theme.CAPTION[0]),
            text_color=theme.TEXT_2,
        )
        self._buttons = ctk.CTkFrame(body, fg_color="transparent")
        self._confirm_btn = primary_button(self._buttons, "OK", self._confirm)
        self._dismiss_btn = secondary_button(self._buttons, "Close",
                                             self._dismiss)

    def show(
        self,
        title: str,
        message: str,
        *,
        detail: str = "",
        confirm: str = "",
        on_confirm: Optional[Callable[[], None]] = None,
        dismiss: str = "Close",
        on_dismiss: Optional[Callable[[], None]] = None,
        accent: str = "",
        wraplength: int = 380,
    ) -> None:
        self._on_confirm = on_confirm
        self._on_dismiss = on_dismiss
        self._stripe.configure(fg_color=accent or self._accent)
        self._title.configure(text=title)
        self._message.configure(text=message, wraplength=wraplength)
        if detail:
            self._detail.configure(text=detail)
            self._detail.pack(fill="x", pady=(6, 0))
        else:
            self._detail.pack_forget()

        self._buttons.pack_forget()
        self._confirm_btn.pack_forget()
        self._dismiss_btn.pack_forget()
        if confirm or dismiss:
            self._buttons.pack(fill="x", pady=(8, 0))
        if confirm:
            self._confirm_btn.configure(text=confirm)
            self._confirm_btn.pack(side="left")
        if dismiss:
            self._dismiss_btn.configure(text=dismiss)
            self._dismiss_btn.pack(side="left",
                                   padx=(8 if confirm else 0, 0))

    def hide(self) -> None:
        """Clear and remove the banner. Safe to call when it is not shown."""
        self._on_confirm = None
        self._on_dismiss = None
        try:
            self.pack_forget()
        except tk.TclError:
            pass

    def _confirm(self) -> None:
        cb = self._on_confirm
        self.hide()
        if cb is not None:
            cb()

    def _dismiss(self) -> None:
        cb = self._on_dismiss
        self.hide()
        if cb is not None:
            cb()


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
        width: int = 140,
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


class FolderTimeList(ctk.CTkScrollableFrame):
    """FolderList plus a per-row text box for that folder's timepoint.

    Tk has no multi-directory picker, so the Development dialog adds folders one
    askdirectory() at a time; this is the list they land in. Each row is
    [folder name][timepoint (h)], with the full path in a delayed Tooltip.

    The box GROWS WITH ITS CONTENT, between one row and ``max_rows``, and
    collapses to a single hint line when it is empty. A fixed-height list is
    dead space most of the time — this control usually holds one to three
    entries, and it sits above everything else in the tallest dialog in the app,
    so every row it does not reserve is a row of options that stays on screen.

    A blank timepoint is meaningful, not missing: it means "derive this one from
    the image capture times". So the box starts empty and is never auto-filled —
    a pre-filled 0 would be indistinguishable from a considered 0.

    Selecting a row highlights it; .selected_index() and .entries() are what the
    dialog reads.
    """

    _ROW_H = 24        # px per row, including its 1px of padding
    _CHROME = 8        # border + internal padding of the scroll frame

    def __init__(self, parent: tk.Widget, max_rows: int = 4) -> None:
        self._max_rows = max(1, int(max_rows))
        super().__init__(
            parent, fg_color=theme.CARD, corner_radius=theme.CARD_RADIUS,
            border_width=1, border_color=theme.HAIRLINE,
            height=self._ROW_H + self._CHROME,
        )
        self._folders: list[str] = []
        self._vars: list[tk.StringVar] = []
        self._rows: list[tk.Widget] = []
        self._buttons: list[ctk.CTkButton] = []
        self._empty: Optional[ctk.CTkLabel] = None
        self._selected: Optional[int] = None
        self._render()

    # -- data ----------------------------------------------------------
    def add(self, path: str, timepoint: str = "") -> None:
        self._folders.append(path)
        self._vars.append(tk.StringVar(value=timepoint))
        self._render()

    def remove(self, index: int) -> None:
        if 0 <= index < len(self._folders):
            del self._folders[index]
            del self._vars[index]
            self._selected = None
            self._render()

    def entries(self) -> list[tuple[str, str]]:
        """[(folder, timepoint_text)] in display order."""
        return [(f, v.get().strip()) for f, v in zip(self._folders, self._vars)]

    def folders(self) -> list[str]:
        return list(self._folders)

    def selected_index(self) -> Optional[int]:
        return self._selected

    # -- rendering -----------------------------------------------------
    def _render(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        self._buttons.clear()
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None

        if not self._folders:
            self._empty = ctk.CTkLabel(
                self, text="No folders yet — click Add folder…",
                font=theme.caption(), text_color=theme.TEXT_2, anchor="w",
                height=self._ROW_H - 4,
            )
            self._empty.pack(anchor="w", padx=6)
            self.configure(height=self._ROW_H + self._CHROME)
            return

        for i, path in enumerate(self._folders):
            row = ctk.CTkFrame(self, fg_color="transparent", height=self._ROW_H)
            row.pack(fill="x", padx=2, pady=0)
            btn = ctk.CTkButton(
                row,
                # Folder name, not the whole path — the path is in the tooltip.
                text=middle_truncate(_leaf_name(path), 34),
                anchor="w",
                command=lambda idx=i: self._select(idx),
                fg_color="transparent", hover_color=theme.BG,
                text_color=theme.TEXT, corner_radius=theme.BTN_RADIUS,
                font=theme.body(), width=220, height=self._ROW_H - 4,
            )
            btn.pack(side="left", fill="x", expand=True)
            Tooltip(btn, path, delay_ms=600)
            entry = ctk.CTkEntry(
                row, textvariable=self._vars[i], width=52,
                height=self._ROW_H - 4,
                placeholder_text="auto",
                fg_color=theme.BG, text_color=theme.TEXT,
                border_color=theme.HAIRLINE, border_width=1,
                corner_radius=theme.BTN_RADIUS, font=theme.body(),
                justify="right",
            )
            entry.pack(side="left", padx=(6, 2))
            Tooltip(entry,
                    "Elapsed hours for this folder. Leave blank to derive it "
                    "from the capture timestamps in the image filenames.",
                    delay_ms=400)
            ctk.CTkLabel(
                row, text="h", font=theme.caption(), text_color=theme.TEXT_2,
                width=10,
            ).pack(side="left")
            self._rows.append(row)
            self._buttons.append(btn)

        rows = min(len(self._folders), self._max_rows)
        self.configure(height=rows * self._ROW_H + self._CHROME)
        self._select(self._selected if self._selected is not None else -1)

    def _select(self, index: int) -> None:
        self._selected = index if 0 <= index < len(self._buttons) else None
        for i, btn in enumerate(self._buttons):
            if i == self._selected:
                btn.configure(fg_color=theme.ACCENT, text_color="#FFFFFF")
            else:
                btn.configure(fg_color="transparent", text_color=theme.TEXT)


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
    "success": dict(
        fg_color=theme.SUCCESS, hover_color=theme.SUCCESS_HOVER,
        text_color="#FFFFFF",
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

    variant ∈ {primary, secondary, success, destructive} maps to the theme
    button colors;
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
