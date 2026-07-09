"""
Thin ffmpeg wrapper: cut a short clip from a longer video with re-encoding
so the output starts on a clean keyframe.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="Cut a clip from a video using ffmpeg.")
    p.add_argument("--input", required=True, help="Path to input video")
    p.add_argument("--start", required=True, help="Start time (HH:MM:SS or seconds)")
    p.add_argument("--duration", required=True, type=float, help="Clip duration in seconds")
    p.add_argument("--output", help="Output path (default: <stem>_clip_<start>_<duration>s.mp4)")
    return p.parse_args()


def resolve_output(input_path: Path, start: str, duration: float, output: str | None) -> Path:
    if output:
        return Path(output)
    safe_start = start.replace(":", "-")
    return input_path.parent / f"{input_path.stem}_clip_{safe_start}_{duration:.0f}s.mp4"


def run_ffmpeg(input_path: Path, start: str, duration: float, output_path: Path) -> bool:
    cmd = [
        "ffmpeg",
        "-ss", start,
        "-t", str(duration),
        "-i", str(input_path),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-y",
        str(output_path),
    ]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode == 0


def probe_output(output_path: Path):
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("ffprobe failed — cannot report clip stats.", file=sys.stderr)
        return
    data = json.loads(result.stdout)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            duration = float(stream.get("duration", 0))
            fps_raw = stream.get("r_frame_rate", "?")
            try:
                num, den = fps_raw.split("/")
                fps = float(num) / float(den)
                fps_str = f"{fps:.3f}"
            except Exception:
                fps_str = fps_raw
            print(f"\nClip ready: {output_path}")
            print(f"  Duration : {duration:.3f} s")
            print(f"  FPS      : {fps_str}")
            return
    print(f"\nClip ready: {output_path} (could not parse stream stats)")


def main():
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = resolve_output(input_path, args.start, args.duration, args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ok = run_ffmpeg(input_path, args.start, args.duration, output_path)
    if not ok:
        print("ffmpeg exited with an error — clip not created.", file=sys.stderr)
        sys.exit(1)

    probe_output(output_path)


if __name__ == "__main__":
    main()
