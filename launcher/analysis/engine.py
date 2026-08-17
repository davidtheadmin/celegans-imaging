"""
Container engine abstraction — Docker, Podman or nerdctl, one code path.

Tierpsy is the only thing in WormScan that needs a container engine, and it is
used by exactly two pipelines (motility and crawling). Colony Survival and Worm
Survival never come near this module.

The engine surface is four command shapes, and that is all:

    <cmd> --version                     is it installed
    <cmd> info    --format ...          is it running, and how big is it
    <cmd> image inspect <image>         is the Tierpsy image pulled
    <cmd> run --rm -v ... <image> ...   do the work

Everything engine-specific in WormScan lives in this file. There are exactly
three differences that matter in practice:

1. `info --format` uses a different template per engine. Docker exposes
   {{.NCPU}} and {{.MemTotal}} at the top level; Podman puts them under
   {{.Host.Cpus}} and {{.Host.MemTotal}}. Passing Docker's template to Podman
   does not raise anything useful — it just fails to produce parseable output,
   and the caller silently falls back to a 2-CPU guess. That is a several-fold
   throughput loss whose only symptom is a warning in launcher.log, which is
   why it is handled here rather than left to a try/except somewhere.

2. Podman refuses to resolve a short image name when it cannot prompt
   ("short-name resolution enforced but cannot prompt without a TTY"), and
   every call we make is a captured subprocess with no TTY. So image names are
   fully qualified — docker.io/tierpsy/tierpsy-tracker rather than
   tierpsy/tierpsy-tracker. Docker and nerdctl accept the qualified form
   happily, so one string works everywhere and there is no branch.

3. The "it is installed but not running" hint names a different application.

Deliberately NOT handled here: rootless UID mapping (`--userns=keep-id`) and
SELinux mount labels (`:z`). Both matter for rootless Podman on Linux and
neither matters on Windows, which is the only platform WormScan runs on today.
When that changes, they belong in `mount_flags()` below and nowhere else.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# Probed in this order when container_engine is "auto". Docker first because a
# Rancher Desktop install set to the dockerd (moby) engine presents exactly the
# docker CLI, which is the configuration WormScan is tested against.
_CANDIDATES = ("docker", "podman", "nerdctl")

# `info` templates, by engine kind.
_INFO_FORMAT = {
    "docker":  "{{.NCPU}} {{.MemTotal}}",
    "nerdctl": "{{.NCPU}} {{.MemTotal}}",
    "podman":  "{{.Host.Cpus}} {{.Host.MemTotal}}",
}

_NOT_RUNNING_HINT = {
    "docker": (
        "The container engine is installed but not running.\n"
        "    Start Rancher Desktop (or Docker Desktop) and wait for it to "
        "report ready, then try again."
    ),
    "podman": (
        "Podman is installed but its machine is not running.\n"
        "    Start Podman Desktop, or run:  podman machine start"
    ),
    "nerdctl": (
        "nerdctl is installed but the containerd backend is not running.\n"
        "    Start Rancher Desktop and wait for it to report ready."
    ),
}

_PROBE_TIMEOUT_S = 20


@dataclass(frozen=True)
class Engine:
    """A container engine that answered `info` successfully."""
    command: str   # what to exec — "docker", "podman", "nerdctl", or a full path
    kind: str      # which dialect it speaks — docker | podman | nerdctl
    version: str   # first line of `<cmd> --version`, for the log

    def __str__(self) -> str:
        return f"{self.kind} ({self.command}) — {self.version}"


def _run(cmd: list[str], timeout_s: int = _PROBE_TIMEOUT_S) -> tuple[int, str, str]:
    """Run a probe command. Returns (rc, stdout, stderr); rc -1 missing, -2 timeout."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
            creationflags=_NO_WINDOW,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", "not found on PATH"
    except subprocess.TimeoutExpired:
        return -2, "", f"timed out after {timeout_s}s"
    except OSError as exc:
        return -1, "", str(exc)


def _kind_of(command: str, version_text: str) -> str:
    """
    Which dialect does this command speak?

    Decided from the `--version` banner rather than the executable name,
    because `docker` may be a shim: podman-docker installs a `docker` that is
    Podman, and its banner says so. Getting this wrong is what produces the
    silent 2-CPU fallback, so the banner is the authority and the filename is
    only the fallback.
    """
    blob = version_text.lower()
    if "podman" in blob:
        return "podman"
    if "nerdctl" in blob:
        return "nerdctl"
    if "docker" in blob:
        return "docker"
    name = Path(command).stem.lower()
    return name if name in _INFO_FORMAT else "docker"


def probe(command: str) -> Engine | None:
    """
    Is `command` a usable engine right now? None if missing OR not running.

    Both failures are lumped together on purpose: an engine whose daemon is
    down is no more useful than one that is not installed, and the preflight
    reports the difference separately with a proper hint.
    """
    rc, out, _ = _run([command, "--version"])
    if rc != 0:
        return None
    version = (out.strip().splitlines() or [""])[0]
    kind = _kind_of(command, version)

    rc, _, err = _run([command, "info", "--format", "{{json .}}"])
    if rc != 0:
        log.info("%s found (%s) but not running: %s", command, version, err.strip()[:200])
        return None
    return Engine(command=command, kind=kind, version=version)


def installed_but_stopped(command: str) -> bool:
    """True when `<cmd> --version` works but `<cmd> info` does not."""
    rc, _, _ = _run([command, "--version"])
    if rc != 0:
        return False
    rc, _, _ = _run([command, "info", "--format", "{{json .}}"])
    return rc != 0


def detect(preferred: str = "auto", docker_command: str = "docker") -> Engine | None:
    """
    Find a usable engine.

    `preferred` is the container_engine setting: "auto", or one of
    docker/podman/nerdctl, or an absolute path to an executable.
    `docker_command` is the legacy setting, honoured when preferred is "auto"
    and it has been changed from the default — so an existing config.json that
    pointed at a custom docker binary keeps working untouched.
    """
    pref = (preferred or "auto").strip()

    if pref and pref.lower() != "auto":
        eng = probe(pref)
        if eng is None:
            log.warning("configured container engine %r is not usable", pref)
        return eng

    # "auto": honour a customised legacy docker_command first, then probe.
    order = list(_CANDIDATES)
    legacy = (docker_command or "").strip()
    if legacy and legacy != "docker":
        order.insert(0, legacy)

    for cand in order:
        eng = probe(cand)
        if eng is not None:
            log.info("container engine detected: %s", eng)
            return eng
    return None


def missing_hint() -> str:
    """What to tell the user when no engine is present at all."""
    return (
        "No container engine found (looked for docker, podman, nerdctl).\n"
        "    Tierpsy runs inside a container, so motility and crawling need one.\n"
        "    Run the 'WormScan — Set up video analysis' shortcut in the Start\n"
        "    Menu, which installs Rancher Desktop and pulls the Tierpsy image.\n"
        "    Colony Survival and Worm Survival do not need this."
    )


def not_running_hint(kind: str) -> str:
    return _NOT_RUNNING_HINT.get(kind, _NOT_RUNNING_HINT["docker"])


def qualify_image(image: str, tag: str = "") -> str:
    """
    Return a fully-qualified image reference.

    Podman will not resolve a short name without a TTY, and every call here is
    a captured subprocess. Docker and nerdctl accept the qualified form, so we
    always qualify and never branch on engine.

    A name is already qualified if its first path segment looks like a registry
    host — it contains a dot or a port, or is exactly "localhost".
    """
    ref = (image or "").strip()
    if not ref:
        return ref
    head = ref.split("/", 1)[0]
    if "/" not in ref or not ("." in head or ":" in head or head == "localhost"):
        ref = "docker.io/" + ref
    if tag and ":" not in ref.rsplit("/", 1)[-1]:
        ref = f"{ref}:{tag}"
    return ref


def mount_flags() -> str:
    """
    Suffix for the -v bind mount (e.g. ":z" for SELinux). Empty on Windows.

    Exists as a seam rather than a feature: when WormScan grows a Linux target,
    rootless Podman will need a label here and this is the one place to add it.
    """
    return ""


def resources(engine: Engine, fallback: tuple[int, float] = (2, 4.0)) -> tuple[int, float]:
    """
    (cpus, mem_gb) as the engine sees them, for sizing the worker pool.

    Falls back on any failure, but — unlike the previous version of this code —
    it says loudly *which* engine and *which* template produced the failure,
    because the failure mode is invisible otherwise: analysis still runs, just
    at a fraction of the speed, and the only trace is one warning line.
    """
    fmt = _INFO_FORMAT.get(engine.kind, _INFO_FORMAT["docker"])
    rc, out, err = _run([engine.command, "info", "--format", fmt])
    if rc != 0:
        log.warning(
            "%s info failed (rc=%s, template=%r): %s; using fallback (%d cpu, %.1f GB)",
            engine.command, rc, fmt, err.strip()[-200:], fallback[0], fallback[1],
        )
        return fallback
    try:
        parts = out.split()
        ncpu = int(parts[0])
        mem_gb = int(parts[1]) / (1024 ** 3)
        if ncpu < 1 or mem_gb <= 0:
            raise ValueError(f"implausible: cpus={ncpu} mem_gb={mem_gb}")
        return ncpu, mem_gb
    except Exception as exc:
        log.warning(
            "could not parse `%s info --format %r` output %r (%s); "
            "using fallback (%d cpu, %.1f GB)",
            engine.command, fmt, out.strip()[:120], exc, fallback[0], fallback[1],
        )
        return fallback


def image_present(engine: Engine, image: str) -> bool:
    rc, _, _ = _run([engine.command, "image", "inspect", image])
    return rc == 0


def pull_command(engine: Engine, image: str) -> list[str]:
    return [engine.command, "pull", image]


def build_tierpsy_cmd(
    engine: Engine,
    video_avi: Path,
    json_file: Path,
    image: str,
) -> list[str]:
    """
    The Tierpsy invocation — the single definition, used by both pipelines.

    tierpsy_process is batch-oriented: it scans --video_dir_root for files
    matching --pattern_include rather than taking a single --video_file. So we
    mount the video's parent directory as /data and pass the basename as the
    pattern, and exactly one file gets processed.

    This lived in two places before (docker_utils.run_tierpsy and crawling's
    instrumented copy), which meant every engine fix had to be made twice.
    Both now call this.
    """
    parent = video_avi.parent.as_posix()
    return [
        engine.command, "run", "--rm",
        "-v", f"{parent}:/data{mount_flags()}",
        image,
        "tierpsy_process",
        "--video_dir_root",   "/data",
        "--mask_dir_root",    "/data/MaskedVideos",
        "--results_dir_root", "/data/Results",
        "--pattern_include",  video_avi.name,
        "--json_file",        f"/data/{json_file.name}",
        "--max_num_process",  "1",
    ]
