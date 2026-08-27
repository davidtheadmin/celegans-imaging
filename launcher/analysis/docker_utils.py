"""
Tierpsy-in-a-container, and the pre-flight that checks it can run.

The engine-specific knowledge all moved to analysis/engine.py; what is left
here is the Tierpsy call itself and the pre-flight. The module keeps its old
name and its old public API (`run_tierpsy`, `run_preflight`) so nothing that
imports it had to change.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import sys
from pathlib import Path
from typing import Callable

from analysis import engine as engine_mod
from analysis.stage_tracker import tierpsy_phase
from analysis.engine import Engine

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _to_docker_path(path: Path) -> str:
    """Windows absolute path -> forward-slash form. Kept for callers that import it."""
    return path.as_posix()


def resolve_engine(settings: object) -> Engine | None:
    """Find the engine a Settings object asks for. None when none is usable."""
    return engine_mod.detect(
        preferred=getattr(settings, "container_engine", "auto"),
        docker_command=getattr(settings, "docker_command", "docker"),
    )


def resolve_image(settings: object) -> str:
    """The fully-qualified Tierpsy image reference this run should use."""
    return engine_mod.qualify_image(
        getattr(settings, "tierpsy_image", "docker.io/tierpsy/tierpsy-tracker"),
        getattr(settings, "tierpsy_image_tag", "latest"),
    )


def run_tierpsy(
    video_avi: Path,
    json_file: Path,
    image: str,
    docker_cmd: str = "docker",
    timeout_s: int = 600,
    engine: Engine | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """
    Run Tierpsy on a single video. Returns (stdout, stderr).
    Raises RuntimeError on non-zero exit or timeout.

    `engine` is the supported argument. `docker_cmd` is the pre-engine form and
    is honoured when no engine is passed, so an out-of-tree caller (the dev
    param-sweep script, for instance) keeps working without edits.

    STREAMED, NOT CAPTURED. This used to be a blocking subprocess.run, which
    meant the whole 20-odd minutes of Tierpsy arrived at once when it was over
    and there was no way to tell a working container from a wedged one. It now
    reads the pipe line by line and, when `on_stage` is given, calls it with
    each checkpoint name Tierpsy announces ("Compressing video", "Calculating
    skeletons", …). stderr is folded into the same stream so the ordering is
    the container's own; the returned stderr is therefore always "".
    """
    if engine is None:
        engine = Engine(command=docker_cmd, kind="docker", version="(assumed)")

    cmd = engine_mod.build_tierpsy_cmd(engine, video_avi, json_file, image)
    captured: list[str] = []
    timed_out = {"value": False}

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, creationflags=_NO_WINDOW,
    )

    def _kill_on_timeout() -> None:
        timed_out["value"] = True
        try:
            proc.kill()
        except Exception:                                          # noqa: BLE001
            pass

    timer = threading.Timer(timeout_s, _kill_on_timeout)
    timer.start()
    try:
        assert proc.stdout is not None
        last = ""
        for line in proc.stdout:
            captured.append(line)
            if on_stage is not None:
                phase = tierpsy_phase(line)
                if phase and phase != last:
                    last = phase
                    try:
                        on_stage(phase)
                    except Exception:                              # noqa: BLE001
                        pass   # a status update never breaks a Tierpsy run
        proc.wait()
    finally:
        timer.cancel()

    combined = "".join(captured)
    if timed_out["value"]:
        raise RuntimeError(f"Tierpsy timed out after {timeout_s}s")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Tierpsy exited {proc.returncode}:\n{combined.strip()[-1000:]}"
        )
    return combined, ""


def preflight_engine(settings: object, folder: Path) -> tuple[Engine | None, list[str]]:
    """
    Pre-flight, returning the engine it found alongside the error list.

    Callers that need the engine (the pipelines) use this; `run_preflight`
    below keeps the old errors-only contract for the UI.
    """
    errors: list[str] = []

    engine = resolve_engine(settings)
    if engine is None:
        # Distinguish "not installed" from "installed but the VM is stopped" —
        # they need completely different things from the user.
        stopped = next(
            (c for c in ("docker", "podman", "nerdctl")
             if engine_mod.installed_but_stopped(c)),
            None,
        )
        if stopped is not None:
            kind = "podman" if stopped == "podman" else (
                "nerdctl" if stopped == "nerdctl" else "docker")
            errors.append(engine_mod.not_running_hint(kind))
        else:
            errors.append(engine_mod.missing_hint())
        # Everything below needs a working engine.
        errors.extend(_non_engine_checks(folder))
        return None, errors

    log.info("pre-flight using %s", engine)

    image = resolve_image(settings)
    if not engine_mod.image_present(engine, image):
        errors.append(
            f"The Tierpsy image is not downloaded yet.\n"
            f"    Run:  {engine.command} pull {image}\n"
            f"    (several GB, once per machine — or use the 'WormScan — Set up\n"
            f"    video analysis' shortcut in the Start Menu, which does it for you)"
        )

    errors.extend(_non_engine_checks(folder))
    return engine, errors


def run_preflight(settings: object, folder: Path) -> list[str]:
    """Pre-flight checks. Empty list means everything is OK."""
    _, errors = preflight_engine(settings, folder)
    return errors


def _non_engine_checks(folder: Path) -> list[str]:
    """ffmpeg, ffprobe, and 'are there actually any videos here'."""
    errors: list[str] = []

    def _rc(cmd: list[str]) -> int:
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20,
                creationflags=_NO_WINDOW,
            )
            return r.returncode
        except FileNotFoundError:
            return -1
        except (subprocess.TimeoutExpired, OSError):
            return -2

    for tool in ("ffmpeg", "ffprobe"):
        if _rc([tool, "-version"]) != 0:
            errors.append(
                f"{tool} not found.\n"
                f"    It ships with WormScan — if you are seeing this on an\n"
                f"    installed copy, re-run the installer. On a dev checkout:\n"
                f"    winget install Gyan.FFmpeg"
            )

    if _count_mp4s(folder) == 0:
        errors.append("No .mp4 files found (checked up to 3 levels deep)")

    return errors


def _count_mp4s(folder: Path, max_depth: int = 3) -> int:
    count = 0

    def _recurse(path: Path, depth: int) -> None:
        nonlocal count
        if depth > max_depth:
            return
        try:
            for child in path.iterdir():
                if child.is_file() and child.suffix.lower() == ".mp4":
                    count += 1
                elif child.is_dir() and not child.name.startswith("_"):
                    _recurse(child, depth + 1)
        except PermissionError:
            pass

    _recurse(folder, 1)
    return count
