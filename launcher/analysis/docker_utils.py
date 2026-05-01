import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _to_docker_path(path: Path) -> str:
    """Convert a Windows absolute path to Docker-compatible forward-slash form."""
    return path.as_posix()


def run_tierpsy(
    video_avi: Path,
    json_file: Path,
    image: str,
    docker_cmd: str = "docker",
    timeout_s: int = 600,
) -> tuple[str, str]:
    """
    Run Tierpsy on a single video via Docker. Returns (stdout, stderr).
    Raises RuntimeError on non-zero exit or timeout.

    tierpsy_process is batch-oriented: it scans --video_dir_root for files
    matching --pattern_include rather than accepting a single --video_file.
    We mount the video's parent as /data and pass the basename as the pattern
    so only this one file is processed.
    """
    parent_posix = _to_docker_path(video_avi.parent)
    cmd = [
        docker_cmd, "run", "--rm",
        "-v", f"{parent_posix}:/data",
        image,
        "tierpsy_process",
        "--video_dir_root",   "/data",
        "--mask_dir_root",    "/data/MaskedVideos",
        "--results_dir_root", "/data/Results",
        "--pattern_include",  video_avi.name,
        "--json_file",        f"/data/{json_file.name}",
        "--max_num_process",  "1",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Tierpsy timed out after {timeout_s}s")
    if result.returncode != 0:
        raise RuntimeError(
            f"Tierpsy exited {result.returncode}:\n{result.stderr.strip()[-1000:]}"
        )
    return result.stdout, result.stderr


def run_preflight(settings: object, folder: Path) -> list[str]:
    """
    Run all pre-flight checks. Returns a list of human-readable error messages.
    An empty list means everything is OK.
    """
    errors: list[str] = []
    docker_cmd: str = getattr(settings, "docker_command", "docker")
    tierpsy_image: str = getattr(settings, "tierpsy_image", "tierpsy/tierpsy-tracker")
    tierpsy_tag: str = getattr(settings, "tierpsy_image_tag", "latest")
    full_image = f"{tierpsy_image}:{tierpsy_tag}"

    def _run(cmd: list[str]) -> int:
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20,
                creationflags=_NO_WINDOW,
            )
            return r.returncode
        except FileNotFoundError:
            return -1
        except subprocess.TimeoutExpired:
            return -2

    # 1. Docker installed
    if _run([docker_cmd, "--version"]) != 0:
        errors.append("Docker not installed or not in PATH")
        return errors  # remaining Docker checks are pointless

    # 2. Docker running
    if _run([docker_cmd, "info"]) != 0:
        errors.append(
            "Docker is installed but not running. Start Docker Desktop."
        )
        return errors

    # 3. Tierpsy image present
    if _run([docker_cmd, "image", "inspect", full_image]) != 0:
        errors.append(
            f"Tierpsy image not pulled. Run: {docker_cmd} pull {tierpsy_image}"
        )

    # 4. ffmpeg
    if _run(["ffmpeg", "-version"]) != 0:
        errors.append(
            "ffmpeg not installed or not in PATH. Install with: winget install Gyan.FFmpeg"
        )

    # 5. ffprobe
    if _run(["ffprobe", "-version"]) != 0:
        errors.append(
            "ffprobe not installed or not in PATH. Install with: winget install Gyan.FFmpeg"
        )

    # 6. Videos present
    mp4_count = _count_mp4s(folder)
    if mp4_count == 0:
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
