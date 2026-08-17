"""
Autosizer for parallel video analysis.

Both the motility and crawling pipelines process videos through Tierpsy
containers. The per-video work is independent and mostly idle-CPU, so several
videos can run concurrently. This module decides how many.

Worker count is derived from the *engine's* view of available resources — not
the host's — because on Windows the engine runs in a VM whose CPU and memory
limits are what actually bound the containers.

Reading those numbers is engine-specific (Docker and Podman expose them under
different template keys), so the query itself lives in analysis/engine.py.
This module only does the arithmetic.
"""
import logging
import os
import sys

from analysis import engine as engine_mod
from analysis.engine import Engine

log = logging.getLogger(__name__)

# Fallback when the engine is unavailable or its output is unparseable.
_FALLBACK_CPUS = 2
_FALLBACK_MEM_GB = 4.0

_MAX_WORKERS = 8


def _as_engine(engine_or_cmd: object) -> Engine | None:
    """
    Accept either an Engine or the legacy docker-command string.

    The string form is what the pipelines used to pass. Rather than assume it
    speaks Docker's dialect — the mistake that made a Podman install silently
    fall back to two workers — we probe it, so the right `info` template gets
    used whatever it turns out to be.
    """
    if isinstance(engine_or_cmd, Engine):
        return engine_or_cmd
    if isinstance(engine_or_cmd, str) and engine_or_cmd.strip():
        return engine_mod.probe(engine_or_cmd.strip())
    return None


def engine_resources(engine_or_cmd: object = "docker") -> tuple[int, float]:
    """
    Return (cpus, mem_gb) as seen by the container engine.

    On ANY failure returns the conservative fallback (2, 4.0) and logs why.
    """
    engine = _as_engine(engine_or_cmd)
    if engine is None:
        log.warning(
            "no usable container engine for resource query; "
            "using fallback (%d cpu, %.1f GB)", _FALLBACK_CPUS, _FALLBACK_MEM_GB,
        )
        return _FALLBACK_CPUS, _FALLBACK_MEM_GB
    return engine_mod.resources(engine, fallback=(_FALLBACK_CPUS, _FALLBACK_MEM_GB))


# Old name, kept so nothing outside this package breaks.
docker_resources = engine_resources


def _from_resources(cpus: int, mem_gb: float) -> int:
    """workers = max(1, min(cpus // 2, (mem_gb - 1.5) // 2, 8))"""
    by_cpu = max(1, cpus // 2)
    by_ram = max(1, int((mem_gb - 1.5) // 2))
    return max(1, min(by_cpu, by_ram, _MAX_WORKERS))


def auto_workers(engine_or_cmd: object = "docker") -> tuple[int, int, float]:
    """
    Return (workers, cpus, mem_gb).

    Each Tierpsy container is single-process (--max_num_process 1) and peaks
    around 1-2 GB, so we leave ~1.5 GB headroom and budget ~2 GB per worker.
    The caller logs the (cpus, mem_gb) alongside the chosen worker count.
    """
    cpus, mem_gb = engine_resources(engine_or_cmd)
    return _from_resources(cpus, mem_gb), cpus, mem_gb


def resolve_workers(
    concurrent_videos: object,
    engine_or_cmd: object = "docker",
) -> tuple[int, int, float]:
    """
    Resolve the configured `concurrent_videos` setting to a worker count.

    "auto" (or any non-int) -> derived from engine resources.
    An int (or int-like string) -> that value, clamped to [1, _MAX_WORKERS].

    Returns (workers, cpus, mem_gb) so the caller can log how the count was
    derived. For an explicit override, cpus/mem_gb are still reported for the
    log line.
    """
    cpus, mem_gb = engine_resources(engine_or_cmd)

    if isinstance(concurrent_videos, str) and concurrent_videos.strip().lower() == "auto":
        return _from_resources(cpus, mem_gb), cpus, mem_gb
    try:
        workers = max(1, min(int(concurrent_videos), _MAX_WORKERS))
        return workers, cpus, mem_gb
    except (TypeError, ValueError):
        return _from_resources(cpus, mem_gb), cpus, mem_gb


def ffmpeg_threads_per_worker(workers: int) -> int:
    """
    Per-worker ffmpeg thread cap to avoid oversubscription at the transcode
    stage when N workers each launch ffmpeg concurrently.

    Spreads host cores across workers, minimum 1. MJPEG (-q:v 3) is
    intra-frame, so this does not change pixel output — only avoids contention.
    """
    host_cpus = os.cpu_count() or _FALLBACK_CPUS
    return max(1, host_cpus // max(1, workers))
