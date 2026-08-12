"""
WormScan Launcher — design tokens and theme initialisation.

A light, Apple-like palette and a small set of font helpers, kept in one place
so the widget layer (widgets.py) and the rewritten UI (later phases) share a
single source of truth. View layer only — nothing here touches the agents,
status objects, or the polling model.

Usage:
    import theme
    theme.init()          # once, before the root window is built
    lbl = ctk.CTkLabel(parent, text="…", font=theme.body())
"""
import customtkinter as ctk

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

BG = "#F5F5F7"           # window background
CARD = "#FFFFFF"         # card / surface
HAIRLINE = "#E5E5EA"     # 1px borders / separators
TEXT = "#1D1D1F"         # primary text
TEXT_2 = "#6E6E73"       # secondary / caption text
ACCENT = "#007AFF"       # primary action
ACCENT_HOVER = "#0063CC"
# Green action. Deliberately deeper than the status-dot green below: the dot is
# a 14 px disc on white and can be bright, a full-width button carries white
# text and needs the contrast.
SUCCESS = "#248A3D"
SUCCESS_HOVER = "#1C6F31"
DESTRUCTIVE = "#FF3B30"  # destructive action tint

# Status-dot colours, keyed by the agents' emitted colour strings.
# The agents emit "green"/"yellow"/"red"/"gray"; this maps those names onto the
# Apple-like hexes WITHOUT requiring any change to the agent contract.
DOT_GREEN = "#34C759"
DOT_AMBER = "#FF9F0A"
DOT_RED = "#FF3B30"
DOT_GRAY = "#8E8E93"

DOT_COLORS: dict[str, str] = {
    "green": DOT_GREEN,
    "yellow": DOT_AMBER,   # agents say "yellow" — we render it amber
    "red": DOT_RED,
    "gray": DOT_GRAY,
}

# ---------------------------------------------------------------------------
# Radii
# ---------------------------------------------------------------------------

CARD_RADIUS = 12
BTN_RADIUS = 8

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

FONT_FAMILY = "Segoe UI"
TITLE = (15, "bold")
BODY = (13, "normal")
BODY_BOLD = (13, "bold")
CAPTION = (11, "normal")
CAPTION_BOLD = (11, "bold")


def _font(spec: tuple[int, str]) -> ctk.CTkFont:
    size, weight = spec
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


def title() -> ctk.CTkFont:
    """~15 semibold — section / dialog titles."""
    return _font(TITLE)


def body() -> ctk.CTkFont:
    """13 regular — default body text."""
    return _font(BODY)


def body_bold() -> ctk.CTkFont:
    """13 semibold — control labels, and the term in a help line."""
    return _font(BODY_BOLD)


def caption() -> ctk.CTkFont:
    """11 regular — secondary captions / help text."""
    return _font(CAPTION)


def caption_bold() -> ctk.CTkFont:
    """11 semibold — the term at the head of a help line."""
    return _font(CAPTION_BOLD)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init() -> None:
    """Set the global CustomTkinter appearance. Call once before the root is built."""
    ctk.set_appearance_mode("light")
    # CustomTkinter ships a "blue" default theme; our buttons/cards set their own
    # colours explicitly (see widgets.py), so we only need to pin light mode here.
    ctk.set_default_color_theme("blue")
