"""Deterministic FFmpeg/FFprobe helpers used by production and post."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class MediaToolError(RuntimeError):
    pass


def _binary(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise MediaToolError(f"{name} is not installed or not on PATH")
    return value


def run_media_command(
    arguments: list[str],
    *,
    timeout: float = 300,
) -> dict[str, Any]:
    started = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    result = {
        "command": arguments,
        "returncode": started.returncode,
        "stdout": started.stdout,
        "stderr": started.stderr,
    }
    if started.returncode != 0:
        tail = started.stderr.strip()[-2000:]
        raise MediaToolError(
            f"command failed ({started.returncode}): {tail}"
        )
    return result


def _fraction(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    if "/" not in value:
        return float(value)
    numerator, denominator = value.split("/", 1)
    return float(numerator) / float(denominator or 1)


def probe_media(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise MediaToolError(f"media file does not exist: {source}")
    command = [
        _binary("ffprobe"),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(source),
    ]
    result = run_media_command(command, timeout=60)
    payload = json.loads(result["stdout"])
    streams = payload.get("streams", [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        {},
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        {},
    )
    format_info = payload.get("format", {})
    duration = (
        video.get("duration")
        or format_info.get("duration")
        or 0
    )
    return {
        "path": str(source.resolve()),
        "bytes": source.stat().st_size,
        "duration_seconds": round(float(duration), 4),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(
            _fraction(
                video.get("avg_frame_rate")
                or video.get("r_frame_rate")
            ),
            4,
        ),
        "frame_count": int(video.get("nb_frames") or 0),
        "video_codec": video.get("codec_name"),
        "pixel_format": video.get("pix_fmt"),
        "audio_codec": audio.get("codec_name"),
        "has_audio": bool(audio),
        "format_name": format_info.get("format_name"),
    }


def decode_check(path: str | Path) -> dict[str, Any]:
    command = [
        _binary("ffmpeg"),
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "null",
        "-",
    ]
    return run_media_command(command, timeout=300)


def generate_mock_video(
    output_path: str | Path,
    *,
    duration_seconds: float,
    width: int,
    height: int,
    fps: int,
    cut_id: int,
) -> dict[str, Any]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    colors = [
        "0x1f3a5f",
        "0x264653",
        "0x3d405b",
        "0x5f4b32",
        "0x273c2c",
        "0x3b2e5a",
    ]
    color = colors[(cut_id - 1) % len(colors)]
    command = [
        _binary("ffmpeg"),
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s={width}x{height}:r={fps}",
        "-t",
        f"{duration_seconds:.4f}",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "30",
        "-pix_fmt",
        "yuv420p",
        str(output),
    ]
    result = run_media_command(command, timeout=120)
    result["probe"] = probe_media(output)
    return result


def downscale_image(
    source_path: str | Path,
    destination_path: str | Path,
    *,
    max_edge: int = 1280,
    quality: int = 4,
) -> str:
    """レビュー・提出Package用に画像を縮小する。

    元画像は最大20MB規模になるため、長辺 max_edge px へ収める。
    原本は変更せず、追跡は sha256 / source_url 側で担保する。
    """
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _binary("ffmpeg"),
        "-y",
        "-i",
        str(source_path),
        "-vf",
        (
            f"scale='if(gt(iw,ih),min({max_edge},iw),-2)'"
            f":'if(gt(iw,ih),-2,min({max_edge},ih))'"
        ),
        "-q:v",
        str(quality),
        str(destination),
    ]
    run_media_command(command, timeout=120)
    return str(destination)


def extract_representative_frames(
    source_path: str | Path,
    output_dir: str | Path,
    *,
    count: int = 3,
) -> list[str]:
    probe = probe_media(source_path)
    duration = max(float(probe["duration_seconds"]), 0.001)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    frames: list[str] = []
    for index in range(count):
        ratio = (index + 1) / (count + 1)
        timestamp = max(0.0, min(duration - 0.001, duration * ratio))
        destination = directory / f"frame_{index + 1:02d}.jpg"
        command = [
            _binary("ffmpeg"),
            "-y",
            "-ss",
            f"{timestamp:.4f}",
            "-i",
            str(source_path),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(destination),
        ]
        run_media_command(command, timeout=60)
        frames.append(str(destination))
    return frames


def normalize_video_clip(
    source_path: str | Path,
    output_path: str | Path,
    *,
    duration_seconds: float,
    width: int,
    height: int,
    fps: int,
) -> dict[str, Any]:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps},format=yuv420p"
    )
    command = [
        _binary("ffmpeg"),
        "-y",
        "-i",
        str(source_path),
        "-t",
        f"{duration_seconds:.4f}",
        "-vf",
        video_filter,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        str(destination),
    ]
    result = run_media_command(command, timeout=300)
    result["probe"] = probe_media(destination)
    return result


def image_to_video_clip(
    source_path: str | Path,
    output_path: str | Path,
    *,
    duration_seconds: float,
    width: int,
    height: int,
    fps: int,
) -> dict[str, Any]:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps},format=yuv420p"
    )
    command = [
        _binary("ffmpeg"),
        "-y",
        "-loop",
        "1",
        "-i",
        str(source_path),
        "-t",
        f"{duration_seconds:.4f}",
        "-vf",
        video_filter,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        str(destination),
    ]
    result = run_media_command(command, timeout=300)
    result["probe"] = probe_media(destination)
    return result


def concat_video_clips(
    clip_paths: list[str | Path],
    output_path: str | Path,
    *,
    manifest_path: str | Path,
) -> dict[str, Any]:
    if not clip_paths:
        raise MediaToolError("no clips supplied for concatenation")
    manifest = Path(manifest_path)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for raw_path in clip_paths:
        resolved = str(Path(raw_path).resolve()).replace("'", "'\\''")
        lines.append(f"file '{resolved}'")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _binary("ffmpeg"),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(manifest),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    result = run_media_command(command, timeout=300)
    result["probe"] = probe_media(destination)
    return result
