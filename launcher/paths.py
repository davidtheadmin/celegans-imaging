"""
Where WormScan reads its tunable files from.

Four files decide what the pipelines actually do, and all four are things you
retune far more often than you change code:

    staging.pt            the staging model
    stage_conf.json       per-class floors, tiling, seam, size gate, rescore
    motility_params.json  Tierpsy parameters for the motility pipeline
    crawling_params.json  Tierpsy parameters for the crawling pipeline

Each is looked for in two places, in order:

    1. the user data dir   %APPDATA%\\WormScan\\        <- an override
    2. next to the code    (whatever the install shipped)   <- the default

Nothing is ever copied into the user dir automatically, and that is the whole
design. If the installer both shipped a file *and* seeded a copy into %APPDATA%,
then the next installer carrying a retrained model would be silently shadowed by
the stale copy left behind by the first install — and nothing would say so. An
empty user dir means the shipped defaults win, every time. An override exists
only because a human put it there.

That makes "ship a new model" a matter of sending someone one file, without a
rebuild or a reinstall, while keeping the no-override case completely boring.

Every resolution is logged at INFO saying which of the two won, and
`overrides_in_use()` returns the same information in a form the analysis
pipelines can stamp into their run_info sheet — so a result that looks strange
can always be traced back to the exact files that produced it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# launcher/ — this file's own directory. Everything shipped is relative to it,
# so the dev tree and an installed copy resolve identically.
_LAUNCHER_DIR = Path(__file__).resolve().parent

# Computed the same way as config.APP_DATA, but independently: this module is
# imported by analysis/ code and must not drag config (and its logging setup)
# in behind it. If you change one, change the other.
_USER_DATA = Path(os.environ.get("APPDATA") or Path.home()) / "WormScan"

# name in the user data dir  ->  the shipped file it overrides
_TUNABLES: dict[str, Path] = {
    "staging.pt":           _LAUNCHER_DIR / "vision" / "models" / "staging.pt",
    "stage_conf.json":      _LAUNCHER_DIR / "vision" / "stage_conf.json",
    "motility_params.json": _LAUNCHER_DIR / "motility_params.json",
    "crawling_params.json": _LAUNCHER_DIR / "crawling_params.json",
}

# Remember what we said, so repeated calls do not spam the log but the first
# resolution of each file is always recorded.
_announced: set[str] = set()


def user_data_dir() -> Path:
    """%APPDATA%\\WormScan — where overrides go. Not created by this module."""
    return _USER_DATA


def launcher_dir() -> Path:
    """The launcher/ directory of this install. Shipped files live under it."""
    return _LAUNCHER_DIR


def resolve(name: str) -> Path:
    """
    Return the path WormScan should actually read for `name`.

    The user data dir wins if a file of that name is sitting there; otherwise
    the shipped default. Returns the shipped path even when it does not exist,
    so callers keep their existing "missing file" error messages and paths
    rather than getting a surprise from this module.
    """
    try:
        shipped = _TUNABLES[name]
    except KeyError:
        raise KeyError(
            f"{name!r} is not a tunable. Known: {sorted(_TUNABLES)}"
        ) from None

    override = _USER_DATA / name
    try:
        overridden = override.is_file()
    except OSError:
        overridden = False

    chosen = override if overridden else shipped
    if name not in _announced:
        _announced.add(name)
        if overridden:
            log.info("%s: using OVERRIDE from %s", name, override)
        else:
            log.info("%s: using shipped default %s", name, shipped)
    return chosen


def is_overridden(name: str) -> bool:
    """True when the user data dir is supplying `name` instead of the install."""
    try:
        return (_USER_DATA / name).is_file()
    except OSError:
        return False


def overrides_in_use() -> dict[str, str]:
    """
    {tunable name: path being used} for every file currently coming from the
    user data dir. Empty dict on a stock install.

    Intended for run_info / log headers: a run that used an overridden model or
    threshold set should say so in its own output, the same way the inference
    meta line already reports what actually ran rather than what was asked for.
    """
    return {
        name: str(_USER_DATA / name)
        for name in _TUNABLES
        if is_overridden(name)
    }


def describe() -> str:
    """One-line human summary for a log header."""
    active = overrides_in_use()
    if not active:
        return "tunables: all shipped defaults"
    return "tunables: OVERRIDDEN -> " + ", ".join(sorted(active))


# Convenience accessors — use these rather than resolve("...") at call sites,
# so a rename of a tunable is a change in this file only.

def staging_model() -> Path:
    return resolve("staging.pt")


def stage_conf() -> Path:
    return resolve("stage_conf.json")


def motility_params() -> Path:
    return resolve("motility_params.json")


def crawling_params() -> Path:
    return resolve("crawling_params.json")


# ---------------------------------------------------------------------------
# Tools shipped alongside the app (installed builds only)
# ---------------------------------------------------------------------------
#
# An installed WormScan carries its own ffmpeg/ffprobe so the user never has to
# install them or get them onto PATH. Layout:
#
#     <install>\ffmpeg\bin\{ffmpeg,ffprobe}.exe
#     <install>\app\launcher\          <- _LAUNCHER_DIR
#
# In a dev checkout that directory does not exist and this is a no-op, so the
# repo keeps using whatever ffmpeg is on PATH. Every ffmpeg/ffprobe call in the
# codebase is a plain subprocess with inherited environment, so prepending to
# os.environ["PATH"] once at startup reaches all of them without touching a
# single call site.

def bundled_tools_dir() -> Path:
    return _LAUNCHER_DIR.parent.parent / "ffmpeg" / "bin"


def ensure_bundled_tools_on_path() -> bool:
    """Prepend the bundled tools dir to PATH. True if it was there to add."""
    tools = bundled_tools_dir()
    if not tools.is_dir():
        return False
    current = os.environ.get("PATH", "")
    entry = str(tools)
    if entry.lower() not in [p.strip().lower() for p in current.split(os.pathsep)]:
        os.environ["PATH"] = entry + os.pathsep + current
        log.info("bundled tools on PATH: %s", entry)
    return True


# ---------------------------------------------------------------------------
# Virtual environments
# ---------------------------------------------------------------------------
#
# The venvs live in DIFFERENT places in a dev checkout and an install, and the
# reason is Windows MAX_PATH, not tidiness.
#
# torch ships a licence tree 167 characters deep:
#     torch-<ver>.dist-info/licenses/third_party/kineto/libkineto/third_party/
#     dynolog/third_party/prometheus-cpp/3rdparty/civetweb/src/third_party/
#     duktape-1.5.2/LICENSE.txt
#
# Windows caps a path at 260 characters unless long-path support is enabled
# machine-wide, which needs administrator rights we deliberately do not ask
# for. So everything before site-packages must fit in 260 - 1 - 167 = 92
# characters. Nesting the venv at app\launcher\vision\.venv-vision spends 32
# of those on nothing, and a real install blew the limit by four characters.
#
# Installed, therefore, the venvs sit directly under the install root. A dev
# checkout keeps them where they have always been -- the repo lives somewhere
# short and nothing needs to move.
#
#     dev        launcher/vision/.venv-vision/
#     installed  <install>/venv-vision/
#
# Resolution tries dev first, then installed, and falls back to the dev path so
# a "not found" error still names the location a developer would expect.

# Longest path inside the torch wheel, measured, not estimated. Update it if
# torch is upgraded and the licence tree grows.
MAX_PACKAGE_PATH = 167
WINDOWS_MAX_PATH = 260


def _venv_python(*candidates: Path) -> Path:
    for venv in candidates:
        for exe in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
            if exe.is_file():
                return exe
    first = candidates[0]
    return first / "Scripts" / "python.exe"


def vision_python() -> Path:
    """The interpreter that runs staging inference."""
    return _venv_python(
        _LAUNCHER_DIR / "vision" / ".venv-vision",   # dev checkout
        _LAUNCHER_DIR.parent.parent / "venv-vision",  # installed
    )


def site_packages_headroom(venv: Path) -> int:
    """
    Spare characters before Windows MAX_PATH bites for this venv.

    Negative means installing torch here WILL fail with WinError 206. Used by
    the installer to refuse a too-deep directory in two seconds rather than
    after three minutes of pip.
    """
    prefix = venv / "Lib" / "site-packages"
    return WINDOWS_MAX_PATH - 1 - MAX_PACKAGE_PATH - len(str(prefix))


# ---------------------------------------------------------------------------
# Build identity
# ---------------------------------------------------------------------------

def build_info() -> dict:
    """
    Version / commit / build date, written by the installer build script.

    Empty dict in a dev checkout. Shown in the window title and logged at
    startup so a bug report from an installed copy names the build it came
    from — the frozen-app equivalent of the meta line the inference layer
    already echoes into run_info.
    """
    import json
    f = _LAUNCHER_DIR / "_build_info.json"
    try:
        # utf-8-sig, not utf-8: the build script writes this with PowerShell's
        # Set-Content -Encoding UTF8, which on Windows PowerShell 5.1 emits a
        # BOM. json.loads rejects a leading BOM outright, so reading as plain
        # utf-8 would throw and silently report every installed build as "dev"
        # -- losing exactly the traceability this file exists to provide.
        data = json.loads(f.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def version_string() -> str:
    info = build_info()
    if not info:
        return "dev"
    v = info.get("version") or "?"
    sha = info.get("commit") or ""
    return f"{v} ({sha[:7]})" if sha else str(v)
