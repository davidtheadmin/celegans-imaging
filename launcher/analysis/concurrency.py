"""
Autosizer for parallel video analysis.

Both the motility and crawling pipelines process videos through Docker
(Tierpsy) containers. The per-video work is independent and mostly idle-CPU,
so several videos can run concurrently. This module decides how many.

Worker count is derived from Docker's view of available resources (so it
respects the Docker Desktop VM limits on Windows/macOS, which is what actually
bounds the containers), with a hard cap of 8.
"""
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Fallback when docker info is unavailable or unparseable.
_FALLBACK_CPUS = 2
_FALLBACK_MEM_GB = 4.0

_MAX_WORKERS = 8


def docker_resources(docker_cmd: str = "docker") -> tuple[int, float]:
    """
    Return (cpus, mem_gb) as seen by the Docker engine.

    Runs `docker info --format "{{.NCPU}} {{.MemTotal}}"` and parses it.
    On ANY failure (docker down, parse error, timeout) returns the
    conservative fallback (2, 4.0) and logs a warning.
    """
    try:
        result = subprocess.run(
            [docker_cmd, "info", "--format", "{{.NCPU}} {{.MemTotal}}"],
            capture_output=True, text=True, timeout=20,
            creationflags=_NO_WINDOW,
        )
        if result.returncode != 0:
            log.warning(
                "docker info failed (rc=%s): %s; using fallback (%d cpu, %.1f GB)",
                result.returncode, result.stderr.strip()[-200:],
                _FALLBACK_CPUS, _FALLBACK_MEM_GB,
            )
            return _FALLBACK_CPUS, _FALLBACK_MEM_GB
        parts = result.stdout.split()
        ncpu = int(parts[0])
        mem_bytes = int(parts[1])
        mem_gb = mem_bytes / (1024 ** 3)
        if ncpu < 1 or mem_gb <= 0:
            raise ValueError(f"implausible values: cpus={ncpu} mem_gb={mem_gb}")
        return ncpu, mem_gb
    except Exception as exc:
        log.warning(
            "docker_resources failed (%s); using fallback (%d cpu, %.1f GB)",
            exc, _FALLBACK_CPUS, _FALLBACK_MEM_GB,
        )
        return _FALLBACK_CPUS, _FALLBACK_MEM_GB


def auto_workers(docker_cmd: str = "docker") -> tuple[int, int, float]:
    """
    Return (workers, cpus, mem_gb).

    workers = max(1, min(cpus // 2, (mem_gb - 1.5) // 2, 8))

    Each Tierpsy container is single-process (--max_num_process 1) and peaks
    around 1-2 GB, so we leave ~1.5 GB headroom and budget ~2 GB per worker.
    The caller logs the (cpus, mem_gb) alongside the chosen worker count.
    """
    cpus, mem_gb = docker_resources(docker_cmd)
    by_cpu = max(1, cpus // 2)
    by_ram = max(1, int((mem_gb - 1.5) // 2))
    workers = max(1, min(by_cpu, by_ram, _MAX_WORKERS))
    return workers, cpus, mem_gb


def resolve_workers(concurrent_videos: object, docker_cmd: str = "docker") -> tuple[int, int, float]:
    """
    Resolve the configured `concurrent_videos` setting to a worker count.

    "auto" (or any non-int) -> auto_workers().
    An int (or int-like string) -> that value, clamped to [1, _MAX_WORKERS].

    Returns (workers, cpus, mem_gb) so the caller can log how the count was
    derived. For an explicit override, cpus/mem_gb are still reported from
    docker_resources() for the log line.
    """
    cpus, mem_gb = docker_resources(docker_cmd)
    if isinstance(concurrent_videos, str) and concurrent_videos.strip().lower() == "auto":
        by_cpu = max(1, cpus // 2)
        by_ram = max(1, int((mem_gb - 1.5) // 2))
        workers = max(1, min(by_cpu, by_ram, _MAX_WORKERS))
        return workers, cpus, mem_gb
    try:
        workers = int(concurrent_videos)
        workers = max(1, min(workers, _MAX_WORKERS))
        return workers, cpus, mem_gb
    except (TypeError, ValueError):
        by_cpu = max(1, cpus // 2)
        by_ram = max(1, int((mem_gb - 1.5) // 2))
        workers = max(1, min(by_cpu, by_ram, _MAX_WORKERS))
        return workers, cpus, mem_gb


def ffmpeg_threads_per_worker(workers: int) -> int:
    """
    Per-worker ffmpeg thread cap to avoid oversubscription at the transcode
    stage when N workers each launch ffmpeg concurrently.

    Spreads host cores across workers, minimum 1. MJPEG (-q:v 3) is
    intra-frame, so this does not change pixel output — only avoids contention.
    """
    host_cpus = os.cpu_count() or _FALLBACK_CPUS
    return max(1, host_cpus // max(1, workers))
