from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
import os
import gc
import ctypes
import subprocess
import sys
import tempfile
import math
import re

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageOps

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".jfif", ".mp4", ".webm"}
IMAGE_FILE_FILTER = "Images and animations (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.jfif *.mp4 *.webm)"

SOFT_ANIMATION_DURATION_MS = 60_000
SOFT_ANIMATION_FRAMES = 900
SOFT_ANIMATION_DECODED_PIXELS = 160_000_000
HARD_ANIMATION_FRAMES = 9_000
HARD_ANIMATION_DECODED_PIXELS = 1_000_000_000
ANIMATION_FRAMES_KEY = "_imagesuite_animation_frames"
ANIMATION_DURATIONS_KEY = "_imagesuite_animation_durations"
ANIMATION_LOOP_KEY = "_imagesuite_animation_loop"
ANIMATION_TOTAL_MS_KEY = "_imagesuite_animation_total_ms"
ANIMATION_FORMAT_KEY = "_imagesuite_animation_format"
ANIMATION_REDUCED_KEY = "_imagesuite_animation_reduced"
VIDEO_SOURCE_PATH_KEY = "_imagesuite_video_source_path"
VIDEO_SOURCE_START_MS_KEY = "_imagesuite_video_source_start_ms"
VIDEO_SOURCE_DURATION_MS_KEY = "_imagesuite_video_source_duration_ms"


class UnsupportedImageError(ValueError):
    """Raised when opening an image would silently discard frames or pages."""


class AnimationReadCancelled(RuntimeError):
    """Raised when a caller cancels a streamed video import."""


def available_memory_bytes() -> int:
    """Return currently available physical memory without an extra dependency."""
    if os.name == "nt":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_phys", ctypes.c_ulonglong),
                ("avail_phys", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("avail_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("avail_virtual", ctypes.c_ulonglong),
                ("avail_extended_virtual", ctypes.c_ulonglong),
            ]
        status = MemoryStatusEx()
        status.length = ctypes.sizeof(MemoryStatusEx)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.avail_phys)
        except Exception:
            pass
    try:
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return 4 * 1024**3


def _frame_duration_ms(raw: Image.Image) -> int:
    value = raw.info.get("duration", 100)
    try:
        duration = int(value)
    except (TypeError, ValueError):
        duration = 100
    return max(10, duration or 100)


def choose_animation_reduction(frame_count: int, total_ms: int, width: int, height: int) -> tuple[int, float, bool]:
    """Choose a bounded proxy without imposing an arbitrary source duration cap.

    Long videos are represented by fewer editable frames instead of being
    rejected before the existing proxy path can run. The total playback time is
    preserved in the per-frame durations, while direct MP4/WebM export can still
    read the untouched original source at full quality.
    """
    frame_count = max(1, int(frame_count))
    total_ms = max(1, int(total_ms))
    width = max(1, int(width))
    height = max(1, int(height))
    stride = max(1, math.ceil(frame_count / SOFT_ANIMATION_FRAMES), math.ceil(total_ms / SOFT_ANIMATION_DURATION_MS))
    kept_frames = max(1, math.ceil(frame_count / stride))
    if kept_frames > HARD_ANIMATION_FRAMES:
        stride = max(stride, math.ceil(frame_count / HARD_ANIMATION_FRAMES))
        kept_frames = max(1, math.ceil(frame_count / stride))
    scale = 1.0
    total_pixels = width * height * kept_frames
    if total_pixels > SOFT_ANIMATION_DECODED_PIXELS:
        scale = min(scale, (SOFT_ANIMATION_DECODED_PIXELS / total_pixels) ** 0.5)
    hard_scale = (HARD_ANIMATION_DECODED_PIXELS / total_pixels) ** 0.5 if total_pixels else 1.0
    scale = min(scale, hard_scale)
    if scale < 0.2:
        raise UnsupportedImageError(
            "This video would need an extremely small editing proxy. "
            "Choose a lower import resolution or a shorter working range."
        )
    scale = max(0.2, min(1.0, scale))
    reduced = stride > 1 or scale < 0.999
    return stride, scale, reduced


# Compatibility alias for older callers and third-party tests.
_choose_animation_reduction = choose_animation_reduction


def _resize_frame_if_needed(frame: Image.Image, scale: float) -> Image.Image:
    rgba = frame.copy() if frame.mode == "RGBA" else frame.convert("RGBA")
    if scale >= 0.999:
        return rgba
    width = max(1, round(frame.width * scale))
    height = max(1, round(frame.height * scale))
    try:
        return rgba.resize((width, height), Image.Resampling.LANCZOS)
    finally:
        rgba.close()


def _attach_animation_info(image: Image.Image, frames: list[Image.Image], durations: list[int], loop: int, fmt: str, reduced: bool) -> Image.Image:
    image.info[ANIMATION_FRAMES_KEY] = frames
    image.info[ANIMATION_DURATIONS_KEY] = durations
    image.info[ANIMATION_LOOP_KEY] = loop
    image.info[ANIMATION_TOTAL_MS_KEY] = sum(durations)
    image.info[ANIMATION_FORMAT_KEY] = fmt
    image.info[ANIMATION_REDUCED_KEY] = reduced
    return image


def read_animated_gif(path: str | Path) -> tuple[list[Image.Image], list[int], int, bool]:
    source = Path(path)
    with Image.open(source) as raw:
        frames = int(getattr(raw, "n_frames", 1) or 1)
        if raw.format != "GIF" or frames <= 1:
            raise UnsupportedImageError(f"{source.name} is not an animated GIF.")
        loop = int(raw.info.get("loop", 0) or 0)
        durations_all: list[int] = []
        total_ms = 0
        for index in range(frames):
            raw.seek(index)
            duration = _frame_duration_ms(raw)
            durations_all.append(duration)
            total_ms += duration
        stride, scale, reduced = _choose_animation_reduction(frames, total_ms, raw.width, raw.height)
        decoded: list[Image.Image] = []
        durations: list[int] = []
        for start in range(0, frames, stride):
            raw.seek(start)
            frame = _resize_frame_if_needed(raw, scale)
            decoded.append(frame)
            durations.append(sum(durations_all[start:start + stride]))
        return decoded, durations, loop, reduced


def _ffmpeg_process_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def probe_video(path: str | Path) -> dict[str, int | float]:
    """Read video metadata with the bundled FFmpeg without decoding frames."""
    source = Path(path)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-i",
        str(source),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        **_ffmpeg_process_kwargs(),
    )
    output = result.stderr or result.stdout or ""
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    duration_s = 0.0
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration_s = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    video_lines = [line for line in output.splitlines() if " Video: " in line]
    video_line = video_lines[0] if video_lines else ""
    size_match = re.search(r"(?<![\d.])(\d{2,5})x(\d{2,5})(?![\d.])", video_line)
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s+fps\b", video_line)
    if not size_match:
        detail = output.strip() or f"FFmpeg could not inspect {source.name}."
        raise UnsupportedImageError(f"Could not read video dimensions from {source.name}.\n{detail[-800:]}")
    width, height = (int(value) for value in size_match.groups())
    fps = min(60.0, max(1.0, float(fps_match.group(1)) if fps_match else 12.0))
    estimated_frames = int(round(duration_s * fps)) if duration_s > 0 else 0
    return {
        "duration_ms": int(round(duration_s * 1000)),
        "fps": fps,
        "width": width,
        "height": height,
        "estimated_frames": estimated_frames,
        "file_size": source.stat().st_size if source.exists() else 0,
    }


def read_video_timeline_thumbnails(
    path: str | Path,
    *,
    duration_ms: int | None = None,
    count: int = 12,
    size: tuple[int, int] = (160, 90),
    cancel: Callable[[], bool] | None = None,
) -> list[Image.Image]:
    """Read a small evenly spaced filmstrip using fast independent FFmpeg seeks."""
    source = Path(path)
    thumbnail_count = max(2, min(24, int(count)))
    target_width, target_height = max(32, int(size[0])), max(24, int(size[1]))
    thumbnails: list[Image.Image] = []
    try:
        source_duration_ms = int(duration_ms or probe_video(source)["duration_ms"] or 1000)
        total_ms = max(20, int(duration_ms or source_duration_ms or 1000))
        for index in range(thumbnail_count):
            if cancel is not None and cancel():
                break
            position_ms = round(max(0, total_ms - 50) * index / max(1, thumbnail_count - 1))
            filter_graph = (
                f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=0x141414"
            )
            command = [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{position_ms / 1000:.3f}",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                filter_graph,
                "-an",
                "-sn",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ]
            result = subprocess.run(command, capture_output=True, check=False, timeout=20)
            expected = target_width * target_height * 3
            if result.returncode != 0 or len(result.stdout) < expected:
                if thumbnails:
                    thumbnails.append(thumbnails[-1].copy())
                    continue
                detail = result.stderr.decode(errors="replace").strip() if result.stderr else "FFmpeg returned no frame."
                raise RuntimeError(detail)
            thumbnails.append(Image.frombytes("RGB", (target_width, target_height), result.stdout[:expected]))
        return thumbnails
    except Exception:
        for thumbnail in thumbnails:
            thumbnail.close()
        raise


def _even_frame_durations(total_ms: int, frame_count: int) -> list[int]:
    """Split an exact 10 ms-aligned duration across a frame count."""
    count = max(1, int(frame_count))
    ticks = max(count, int(round(max(10, total_ms) / 10)))
    base, remainder = divmod(ticks, count)
    return [(base + (1 if index < remainder else 0)) * 10 for index in range(count)]


def read_animated_video(
    path: str | Path,
    *,
    start_ms: int = 0,
    duration_ms: int | None = None,
    target_fps: float | None = None,
    max_side: int | None = None,
    progress: Callable[[int, int], bool | None] | None = None,
) -> tuple[list[Image.Image], list[int], int, bool]:
    """Stream a bounded editable proxy directly from the bundled FFmpeg."""
    source = Path(path)
    info = probe_video(source)
    source_duration = int(info["duration_ms"])
    start = max(0, int(start_ms))
    if source_duration > 0:
        start = min(start, max(0, source_duration - 20))
        remaining = max(20, source_duration - start)
    else:
        remaining = max(20, int(duration_ms or SOFT_ANIMATION_DURATION_MS))
    selected_duration = remaining if duration_ms is None else max(20, min(int(duration_ms), remaining))
    source_fps = min(60.0, max(1.0, float(info["fps"] or 12.0)))
    requested_fps = min(60.0, max(1.0, float(target_fps or source_fps)))
    width, height = int(info["width"]), int(info["height"])
    requested_frames = max(2, int(math.ceil(selected_duration * requested_fps / 1000)))
    stride, budget_scale, reduced = _choose_animation_reduction(
        requested_frames, selected_duration, width, height
    )
    decode_fps = max(1.0, requested_fps / stride)
    side_scale = 1.0
    if max_side and int(max_side) > 0 and max(width, height) > int(max_side):
        side_scale = int(max_side) / max(width, height)
    scale = min(budget_scale, side_scale)
    output_width = max(1, round(width * scale))
    output_height = max(1, round(height * scale))
    reduced = (
        reduced
        or side_scale < 0.999
        or abs(decode_fps - source_fps) > 0.01
        or start > 0
        or (source_duration > 0 and selected_duration < remaining)
    )

    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error"]
    if start:
        command.extend(["-ss", f"{start / 1000:.3f}"])
    command.extend(["-i", str(source), "-t", f"{selected_duration / 1000:.3f}"])
    command.extend([
        "-map", "0:v:0",
        "-an", "-sn",
        "-vf", f"fps={decode_fps:.6f},scale={output_width}:{output_height}:flags=lanczos",
        "-pix_fmt", "rgb24",
        "-f", "rawvideo",
        "pipe:1",
    ])
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_ffmpeg_process_kwargs(),
    )
    frame_bytes = output_width * output_height * 3
    decoded: list[Image.Image] = []
    expected = max(2, int(math.ceil(selected_duration * decode_fps / 1000)))
    error_text = ""
    try:
        assert process.stdout is not None
        while True:
            data = process.stdout.read(frame_bytes)
            if not data:
                break
            if len(data) != frame_bytes:
                raise RuntimeError("FFmpeg returned an incomplete video frame.")
            rgb = Image.frombytes("RGB", (output_width, output_height), data)
            try:
                decoded.append(rgb.convert("RGBA"))
            finally:
                rgb.close()
            if progress is not None and progress(len(decoded), expected) is False:
                raise AnimationReadCancelled("Video import cancelled.")
            if len(decoded) > HARD_ANIMATION_FRAMES:
                raise UnsupportedImageError(
                    "This video segment still contains too many editable frames. "
                    "Lower its import frame rate or duration."
                )
        if process.stderr is not None:
            error_text = process.stderr.read().decode(errors="replace").strip()
        return_code = process.wait(timeout=5)
        if return_code != 0:
            raise RuntimeError(error_text or f"FFmpeg video decoding failed with code {return_code}.")
    except Exception:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except Exception:
            pass
        for frame in decoded:
            frame.close()
        decoded.clear()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    if not decoded:
        raise UnsupportedImageError(f"{source.name} does not contain readable frames in the selected range.")
    durations = _even_frame_durations(selected_duration, len(decoded))
    return decoded, durations, 0, reduced


def read_animated_media(
    path: str | Path,
    *,
    video_options: Mapping[str, object] | None = None,
    progress: Callable[[int, int], bool | None] | None = None,
) -> tuple[list[Image.Image], list[int], int, str, bool]:
    suffix = Path(path).suffix.lower()
    if suffix == ".gif":
        frames, durations, loop, reduced = read_animated_gif(path)
        return frames, durations, loop, "gif", reduced
    if suffix in {".mp4", ".webm"}:
        frames, durations, loop, reduced = read_animated_video(path, progress=progress, **dict(video_options or {}))
        return frames, durations, loop, suffix.lstrip("."), reduced
    raise UnsupportedImageError(f"{Path(path).name} is not a supported animation format.")


def _gif_thumbnail(path: str | Path, max_side: int) -> Image.Image:
    with Image.open(path) as raw:
        raw.seek(0)
        image = raw.convert("RGBA")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        return image


def _video_thumbnail(path: str | Path, max_side: int) -> Image.Image:
    frames, _durations, _loop, _reduced = read_animated_video(
        path,
        duration_ms=100,
        target_fps=1,
        max_side=max_side,
    )
    image = frames[0]
    for extra in frames[1:]:
        extra.close()
    return image


def read_image(
    path: str | Path,
    *,
    video_options: Mapping[str, object] | None = None,
    progress: Callable[[int, int], bool | None] | None = None,
) -> Image.Image:
    source = Path(path)
    if source.suffix.lower() in {".mp4", ".webm"}:
        frames, durations, loop, fmt, reduced = read_animated_media(source, video_options=video_options, progress=progress)
        image = frames[0].copy()
        attached = _attach_animation_info(image, frames, durations, loop, fmt, reduced)
        options = dict(video_options or {})
        attached.info[VIDEO_SOURCE_PATH_KEY] = str(source.resolve())
        attached.info[VIDEO_SOURCE_START_MS_KEY] = max(0, int(options.get("start_ms") or 0))
        attached.info[VIDEO_SOURCE_DURATION_MS_KEY] = sum(durations)
        return attached
    with Image.open(source) as raw:
        if source.suffix.lower() == ".gif" and int(getattr(raw, "n_frames", 1) or 1) > 1:
            raw.close()
            frames, durations, loop, fmt, reduced = read_animated_media(source)
            image = frames[0].copy()
            return _attach_animation_info(image, frames, durations, loop, fmt, reduced)
        frames = int(getattr(raw, "n_frames", 1) or 1)
        image_format = raw.format
        if frames > 1 and image_format != "GIF":
            raise UnsupportedImageError(
                f"{source.name} contains {frames} pages. "
                "ImageSuite edits single-page TIFF images only; export the page you need first."
            )
        metadata = {
            key: raw.info[key]
            for key in ("icc_profile", "dpi")
            if key in raw.info and raw.info[key]
        }
        image = ImageOps.exif_transpose(raw).convert("RGBA")
        image.info.update(metadata)
        return image

def _image_has_transparency(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _flatten_rgba_to_rgb(image: Image.Image, background: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    flattened = Image.new("RGB", rgba.size, background)
    flattened.paste(rgba, mask=rgba.getchannel("A"))
    if rgba is not image:
        rgba.close()
    return flattened


def read_processing_image(path: str | Path, *, preserve_transparency: bool = True) -> Image.Image:
    """Read one still image using the lightest safe working mode for batch processing."""
    source = Path(path)
    if source.suffix.lower() in {".gif", ".mp4", ".webm"}:
        return read_image(source)
    with Image.open(source) as raw:
        frames = int(getattr(raw, "n_frames", 1) or 1)
        if frames > 1:
            raise UnsupportedImageError(
                f"{source.name} contains {frames} pages. "
                "ImageSuite edits single-page TIFF images only; export the page you need first."
            )
        metadata = {
            key: raw.info[key]
            for key in ("icc_profile", "dpi")
            if key in raw.info and raw.info[key]
        }
        has_transparency = _image_has_transparency(raw)
        oriented = ImageOps.exif_transpose(raw)
        try:
            if preserve_transparency and has_transparency:
                image = oriented.convert("RGBA")
            elif has_transparency:
                image = _flatten_rgba_to_rgb(oriented)
            else:
                image = oriented.convert("RGB")
        finally:
            if oriented is not raw:
                oriented.close()
        image.info.update(metadata)
        return image


def read_thumbnail(path: str | Path, max_side: int = 1600) -> Image.Image:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".gif":
        return _gif_thumbnail(source, max_side)
    if suffix in {".mp4", ".webm"}:
        return _video_thumbnail(source, max_side)
    with Image.open(path) as raw:
        try:
            raw.draft("RGB", (max_side, max_side))
        except Exception:
            pass
        image = ImageOps.exif_transpose(raw)
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        return image.convert("RGBA")


def save_image(
    image: Image.Image,
    path: str | Path,
    quality: int = 95,
    *,
    optimize: bool = True,
    metadata: Mapping[str, object] | None = None,
    png_compress_level: int | None = None,
) -> None:
    """Save through a sibling temporary file so a failed write cannot corrupt the target."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported output format: {target.suffix or '(none)'}")

    out = image
    owned_output: Image.Image | None = None
    kwargs: dict[str, object] = {}
    if suffix in {".jpg", ".jpeg", ".jfif"}:
        if image.mode == "RGB":
            out = image
        elif _image_has_transparency(image):
            owned_output = _flatten_rgba_to_rgb(image)
            out = owned_output
        else:
            owned_output = image.convert("RGB")
            out = owned_output
        kwargs.update(quality=quality, optimize=optimize)
    elif suffix == ".webp":
        kwargs.update(quality=quality, method=6 if optimize else 3)
    elif suffix == ".png":
        kwargs.update(optimize=optimize)
        if png_compress_level is not None:
            kwargs["compress_level"] = max(0, min(9, int(png_compress_level)))

    source_info = image.info if metadata is None else metadata
    icc_profile = source_info.get("icc_profile") if source_info else None
    dpi = source_info.get("dpi") if source_info else None
    if icc_profile and suffix in {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".tif", ".tiff"}:
        kwargs["icc_profile"] = icc_profile
    if dpi and suffix in {".png", ".jpg", ".jpeg", ".jfif", ".bmp", ".tif", ".tiff"}:
        kwargs["dpi"] = dpi

    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=target.suffix, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        out.save(temporary, **kwargs)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        if owned_output is not None:
            owned_output.close()


def _flatten_frame_for_video(frame: Image.Image) -> np.ndarray:
    rgba = frame.copy() if frame.mode == "RGBA" else frame.convert("RGBA")
    width, height = rgba.size
    even_width = width + (width % 2)
    even_height = height + (height % 2)
    background = Image.new("RGB", (even_width, even_height), (0, 0, 0))
    try:
        background.paste(rgba, (0, 0), mask=rgba.getchannel("A"))
        return np.array(background, dtype=np.uint8, copy=True)
    finally:
        background.close()
        rgba.close()


def _prepare_gif_frame(frame: Image.Image, colors: int, dither: bool) -> Image.Image:
    palette_colors = max(2, min(256, int(colors or 256)))
    dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    rgba = frame.copy() if frame.mode == "RGBA" else frame.convert("RGBA")
    try:
        return rgba.quantize(colors=palette_colors, method=Image.Quantize.FASTOCTREE, dither=dither_mode)
    finally:
        rgba.close()


def _save_animation_video(
    frames: list[Image.Image],
    durations: list[int],
    target: Path,
    *,
    fps: int = 0,
    bitrate_kbps: int = 0,
    audio_source: str | Path | None = None,
    audio_start_ms: int = 0,
    audio_duration_ms: int | None = None,
) -> None:
    step_ms = max(16, min(100, min(durations)))
    export_fps = int(fps) if fps and int(fps) > 0 else min(60, max(1, round(1000 / step_ms)))
    first = _flatten_frame_for_video(frames[0])
    height, width = first.shape[:2]
    codec = "libx264" if target.suffix.lower() == ".mp4" else "libvpx-vp9"
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=target.suffix, delete=False) as handle:
        temporary = Path(handle.name)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-r", str(export_fps), "-i", "pipe:0", "-an", "-c:v", codec,
    ]
    if bitrate_kbps and int(bitrate_kbps) > 0:
        command.extend(["-b:v", f"{int(bitrate_kbps)}k"])
    elif target.suffix.lower() == ".webm":
        command.extend(["-crf", "32", "-b:v", "0"])
    if target.suffix.lower() == ".mp4":
        command.extend(["-pix_fmt", "yuv420p", "-movflags", "+faststart"])
    else:
        command.extend(["-pix_fmt", "yuv420p"])
    command.append(str(temporary))
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **_ffmpeg_process_kwargs(),
    )
    error_text = ""
    try:
        assert process.stdin is not None
        for index, (frame, duration) in enumerate(zip(frames, durations)):
            rgb = first if index == 0 else _flatten_frame_for_video(frame)
            repeats = max(1, round(duration * export_fps / 1000))
            payload = rgb.tobytes()
            for _ in range(repeats):
                process.stdin.write(payload)
        process.stdin.close()
        if process.stderr is not None:
            error_text = process.stderr.read().decode(errors="replace").strip()
        return_code = process.wait(timeout=30)
        if return_code != 0:
            raise RuntimeError(error_text or f"FFmpeg video encoding failed with code {return_code}.")
        if audio_source is not None:
            mux_source_audio(
                temporary,
                audio_source,
                target,
                start_ms=audio_start_ms,
                duration_ms=audio_duration_ms or sum(durations),
            )
        else:
            os.replace(temporary, target)
    except Exception:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except Exception:
            pass
        raise
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stderr is not None:
            process.stderr.close()
        temporary.unlink(missing_ok=True)


def export_video_segment(
    source: str | Path,
    target: str | Path,
    *,
    start_ms: int = 0,
    duration_ms: int | None = None,
    fps: int = 0,
    bitrate_kbps: int = 0,
    preserve_audio: bool = True,
) -> None:
    """Transcode a source-video segment directly with FFmpeg.

    No GIF or Python frame sequence is created. FFmpeg reads the original MP4
    or WebM, trims it, preserves its audio when present, and writes the target
    video through a temporary file so replacing the source path is safe.
    """
    source_path = Path(source)
    target_path = Path(target)
    suffix = target_path.suffix.lower()
    if source_path.suffix.lower() not in {".mp4", ".webm"}:
        raise ValueError("Direct video export requires an MP4 or WebM source.")
    if suffix not in {".mp4", ".webm"}:
        raise ValueError("Direct video export can only create MP4 or WebM files.")
    if not source_path.exists():
        raise FileNotFoundError(f"The original video is no longer available: {source_path}")

    start = max(0, int(start_ms))
    duration = None if duration_ms is None else max(20, int(duration_ms))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target_path.parent, suffix=suffix, delete=False) as handle:
        temporary = Path(handle.name)

    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if start:
        command.extend(["-ss", f"{start / 1000:.3f}"])
    command.extend(["-i", str(source_path)])
    if duration is not None:
        command.extend(["-t", f"{duration / 1000:.3f}"])
    command.extend(["-map", "0:v:0"])
    if preserve_audio:
        command.extend(["-map", "0:a?"])

    if suffix == ".mp4":
        command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium"])
        if bitrate_kbps > 0:
            command.extend(["-b:v", f"{int(bitrate_kbps)}k"])
        else:
            command.extend(["-crf", "18"])
        if preserve_audio:
            command.extend(["-c:a", "aac", "-b:a", "192k"])
        command.extend(["-movflags", "+faststart"])
    else:
        command.extend(["-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p"])
        if bitrate_kbps > 0:
            command.extend(["-b:v", f"{int(bitrate_kbps)}k"])
        else:
            command.extend(["-crf", "30", "-b:v", "0"])
        if preserve_audio:
            command.extend(["-c:a", "libopus", "-b:a", "160k"])

    if fps > 0:
        command.extend(["-r", str(min(60, max(1, int(fps))))])
    command.extend(["-map_metadata", "0", "-avoid_negative_ts", "make_zero", str(temporary)])

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "FFmpeg did not provide an error message.").strip()
            raise RuntimeError(detail)
        os.replace(temporary, target_path)
    finally:
        temporary.unlink(missing_ok=True)


def mux_source_audio(
    rendered_video: str | Path,
    audio_source: str | Path,
    target: str | Path,
    *,
    start_ms: int = 0,
    duration_ms: int | None = None,
) -> None:
    """Attach a matching source-audio segment to an already-rendered video.

    The edited video stream is copied without another quality-loss pass. Audio
    is optional, so sources without an audio track still export successfully.
    """
    rendered = Path(rendered_video)
    source = Path(audio_source)
    destination = Path(target)
    suffix = destination.suffix.lower()
    if suffix not in {".mp4", ".webm"}:
        raise ValueError("Source audio can only be attached to MP4 or WebM output.")
    if not rendered.exists():
        raise FileNotFoundError(f"The rendered video is unavailable: {rendered}")
    if not source.exists():
        raise FileNotFoundError(f"The original audio source is unavailable: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=suffix, delete=False) as handle:
        temporary = Path(handle.name)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(rendered),
    ]
    start = max(0, int(start_ms))
    if start:
        command.extend(["-ss", f"{start / 1000:.3f}"])
    command.extend(["-i", str(source), "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy"])
    if suffix == ".mp4":
        command.extend(["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])
    else:
        command.extend(["-c:a", "libopus", "-b:a", "160k"])
    if duration_ms is not None:
        command.extend(["-t", f"{max(20, int(duration_ms)) / 1000:.3f}"])
    command.extend(["-shortest", "-map_metadata", "1", str(temporary)])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "FFmpeg did not provide an error message.").strip()
            raise RuntimeError(detail)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def slice_animation(
    frames: Iterable[Image.Image],
    durations: Iterable[int],
    *,
    start_ms: int = 0,
    duration_ms: int | None = None,
) -> tuple[list[Image.Image], list[int]]:
    """Return the frames overlapping an exact timeline segment."""
    frame_list = list(frames)
    duration_list = [max(10, int(value or 100)) for value in durations]
    if not frame_list or len(frame_list) != len(duration_list):
        raise ValueError("Animation frame durations do not match the frame count.")
    total = sum(duration_list)
    start = max(0, min(int(start_ms), max(0, total - 20)))
    length = total - start if duration_ms is None else max(20, min(int(duration_ms), total - start))
    end = start + length
    selected_frames: list[Image.Image] = []
    selected_durations: list[int] = []
    cursor = 0
    for frame, frame_duration in zip(frame_list, duration_list):
        frame_end = cursor + frame_duration
        overlap = min(end, frame_end) - max(start, cursor)
        if overlap > 0:
            selected_frames.append(frame)
            selected_durations.append(max(10, int(overlap)))
        cursor = frame_end
        if cursor >= end:
            break
    if not selected_frames:
        raise ValueError("The selected export range contains no animation frames.")
    difference = length - sum(selected_durations)
    selected_durations[-1] += difference
    if len(selected_frames) == 1:
        if length < 20:
            raise ValueError("Animated export requires at least 0.02 seconds.")
        first = max(10, (length // 20) * 10)
        second = length - first
        if second < 10:
            first -= 10
            second += 10
        selected_frames = [selected_frames[0], selected_frames[0]]
        selected_durations = [first, second]
    return selected_frames, selected_durations


def retime_animation_durations(durations: Iterable[int], target_duration_ms: int) -> list[int]:
    """Scale frame timing to an exact GIF-safe duration in 10 ms units."""
    values = [max(10, int(value or 100)) for value in durations]
    if not values:
        raise ValueError("Animation timing is empty.")
    target_ticks = max(2, int(round(target_duration_ms / 10)))
    if target_ticks < len(values):
        raise ValueError(
            f"That duration is too short for {len(values):,} frames. "
            f"Use at least {len(values) / 100:.2f} seconds or export a shorter source range."
        )
    remaining = target_ticks - len(values)
    total_weight = sum(values)
    shares = [remaining * value / total_weight for value in values]
    extras = [int(value) for value in shares]
    leftover = remaining - sum(extras)
    order = sorted(range(len(values)), key=lambda index: shares[index] - extras[index], reverse=True)
    for index in order[:leftover]:
        extras[index] += 1
    return [(1 + extra) * 10 for extra in extras]


def save_animation(
    frames: Iterable[Image.Image],
    durations: Iterable[int],
    path: str | Path,
    *,
    loop: int = 0,
    fps: int = 0,
    bitrate_kbps: int = 0,
    gif_colors: int = 256,
    gif_dither: bool = True,
    gif_optimize: bool = False,
    audio_source: str | Path | None = None,
    audio_start_ms: int = 0,
    audio_duration_ms: int | None = None,
) -> None:
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix not in {".gif", ".mp4", ".webm"}:
        raise ValueError("Animated images must be saved as GIF, MP4, or WebM files.")
    frame_list: list[Image.Image] = []
    owned_frames: list[Image.Image] = []
    for frame in frames:
        if frame.mode == "RGBA":
            frame_list.append(frame)
        else:
            converted = frame.convert("RGBA")
            frame_list.append(converted)
            owned_frames.append(converted)
    try:
        if len(frame_list) < 2:
            raise ValueError("Animated media requires at least two frames.")
        duration_list = [max(10, int(value or 100)) for value in durations]
        if len(duration_list) != len(frame_list):
            raise ValueError("Animation frame durations do not match the frame count.")
        target.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".gif":
            palettized = [_prepare_gif_frame(frame, gif_colors, gif_dither) for frame in frame_list]
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".gif", delete=False) as handle:
                temporary = Path(handle.name)
            try:
                palettized[0].save(
                    temporary,
                    save_all=True,
                    append_images=palettized[1:],
                    duration=duration_list,
                    loop=max(0, int(loop)),
                    disposal=2,
                    optimize=bool(gif_optimize),
                )
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
                for frame in palettized:
                    frame.close()
            return
        _save_animation_video(
            frame_list,
            duration_list,
            target,
            fps=fps,
            bitrate_kbps=bitrate_kbps,
            audio_source=audio_source,
            audio_start_ms=audio_start_ms,
            audio_duration_ms=audio_duration_ms,
        )
    finally:
        for frame in owned_frames:
            frame.close()


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
        mode="w",
        encoding=encoding,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def image_files(root: str | Path, recursive: bool = True) -> list[Path]:
    path = Path(root)
    if path.is_file():
        return [path] if path.suffix.lower() in IMAGE_EXTENSIONS else []
    if not path.exists():
        return []
    iterator: Iterable[Path] = path.rglob("*") if recursive else path.glob("*")
    return sorted(
        (entry for entry in iterator if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda entry: str(entry).lower(),
    )


def expand_image_paths(paths: Iterable[str | Path], recursive: bool = True) -> list[Path]:
    """Expand files and folders once, de-duplicating paths while preserving input order."""
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        candidates = image_files(path, recursive) if path.is_dir() else image_files(path)
        for candidate in candidates:
            try:
                key = candidate.resolve()
            except OSError:
                key = candidate
            if key not in seen:
                seen.add(key)
                result.append(candidate)
    return result


def reserve_destination(folder: str | Path, source: str | Path) -> Path:
    """Atomically reserve a unique output path with a zero-byte placeholder.

    The caller must replace the placeholder with the finished file or unlink it
    after a failure. Unlike a process-local lock, O_EXCL remains safe across
    worker threads and separate ImageSuite processes.
    """
    dest = Path(folder)
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(source)
    index = 1
    while True:
        name = src.name if index == 1 else f"{src.stem} ({index}){src.suffix}"
        candidate = dest / name
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            index += 1
            continue
        os.close(descriptor)
        return candidate


def unique_destination(folder: str | Path, source: str | Path) -> Path:
    dest = Path(folder)
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(source)
    candidate = dest / src.name
    index = 2
    while candidate.exists():
        candidate = dest / f"{src.stem} ({index}){src.suffix}"
        index += 1
    return candidate


def open_folder(path: str | Path) -> None:
    folder = Path(path)
    if folder.suffix and folder.exists() and folder.is_file():
        folder = folder.parent
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


def release_unused_memory() -> None:
    """Return unused native image buffers to the OS after a completed batch."""
    gc.collect()
    try:
        if os.name == "nt":
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.psapi.EmptyWorkingSet(process)
        elif sys.platform.startswith("linux"):
            ctypes.CDLL(None).malloc_trim(0)
    except Exception:
        pass


def app_data_dir() -> Path:
    app_root = Path(sys.argv[0]).resolve().parent
    if (app_root / "portable.flag").exists():
        path = app_root / "data"
    elif os.name == "nt":
        path = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ImageSuite"
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ImageSuite"
    path.mkdir(parents=True, exist_ok=True)
    return path
