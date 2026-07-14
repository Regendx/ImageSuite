from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
import os
import gc
import ctypes
import subprocess
import sys
import tempfile
import math

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageOps

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".jfif", ".mp4", ".webm"}
IMAGE_FILE_FILTER = "Images and animations (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.jfif *.mp4 *.webm)"

SOFT_ANIMATION_DURATION_MS = 60_000
SOFT_ANIMATION_FRAMES = 900
SOFT_ANIMATION_DECODED_PIXELS = 160_000_000
HARD_ANIMATION_DURATION_MS = 300_000
HARD_ANIMATION_FRAMES = 9_000
HARD_ANIMATION_DECODED_PIXELS = 1_000_000_000
ANIMATION_FRAMES_KEY = "_imagesuite_animation_frames"
ANIMATION_DURATIONS_KEY = "_imagesuite_animation_durations"
ANIMATION_LOOP_KEY = "_imagesuite_animation_loop"
ANIMATION_TOTAL_MS_KEY = "_imagesuite_animation_total_ms"
ANIMATION_FORMAT_KEY = "_imagesuite_animation_format"
ANIMATION_REDUCED_KEY = "_imagesuite_animation_reduced"


class UnsupportedImageError(ValueError):
    """Raised when opening an image would silently discard frames or pages."""


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


def _choose_animation_reduction(frame_count: int, total_ms: int, width: int, height: int) -> tuple[int, float, bool]:
    hard_frames = max(1, frame_count)
    hard_duration = max(1, total_ms)
    if hard_frames > HARD_ANIMATION_FRAMES or hard_duration > HARD_ANIMATION_DURATION_MS:
        raise UnsupportedImageError(
            f"This animation is too large to edit safely ({frame_count} frames, {total_ms / 1000:.1f}s). "
            "Trim it or lower its frame rate first."
        )
    stride = max(1, math.ceil(frame_count / SOFT_ANIMATION_FRAMES), math.ceil(total_ms / SOFT_ANIMATION_DURATION_MS))
    kept_frames = max(1, math.ceil(frame_count / stride))
    scale = 1.0
    total_pixels = width * height * kept_frames
    if total_pixels > SOFT_ANIMATION_DECODED_PIXELS:
        scale = min(scale, (SOFT_ANIMATION_DECODED_PIXELS / total_pixels) ** 0.5)
    if width * height * kept_frames > HARD_ANIMATION_DECODED_PIXELS:
        raise UnsupportedImageError(
            "This animation is too large to edit safely even after downscaling. "
            "Lower its resolution or shorten it first."
        )
    scale = max(0.2, min(1.0, scale))
    reduced = stride > 1 or scale < 0.999
    return stride, scale, reduced


def _resize_frame_if_needed(frame: Image.Image, scale: float) -> Image.Image:
    if scale >= 0.999:
        return frame.convert("RGBA").copy()
    width = max(1, round(frame.width * scale))
    height = max(1, round(frame.height * scale))
    return frame.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)


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


def read_animated_video(path: str | Path) -> tuple[list[Image.Image], list[int], int, bool]:
    source = Path(path)
    reader = imageio.get_reader(str(source))
    try:
        meta = reader.get_meta_data() or {}
        fps = float(meta.get("fps") or 12.0)
        fps = min(60.0, max(1.0, fps))
        duration_s = float(meta.get("duration") or 0.0)
        size = meta.get("size") or meta.get("source_size") or (0, 0)
        width, height = int(size[0] or 0), int(size[1] or 0)
        estimated_frames = int(round(duration_s * fps)) if duration_s > 0 else 0
        if estimated_frames <= 0:
            try:
                estimated_frames = int(reader.count_frames())
            except Exception:
                estimated_frames = 0
        if width <= 0 or height <= 0:
            first = reader.get_data(0)
            height, width = first.shape[:2]
        total_ms = max(100, int(round(duration_s * 1000))) if duration_s > 0 else max(100, int(round((estimated_frames or 1) * 1000 / fps)))
        frame_count_hint = estimated_frames if estimated_frames > 0 else max(1, int(round(total_ms * fps / 1000)))
        stride, scale, reduced = _choose_animation_reduction(frame_count_hint, total_ms, width, height)
        decoded: list[Image.Image] = []
        durations: list[int] = []
        frame_ms = max(10, int(round(1000 / fps)))
        accumulated = 0
        kept = 0
        for index, frame_data in enumerate(reader):
            accumulated += frame_ms
            if index % stride != 0:
                continue
            pil = Image.fromarray(np.asarray(frame_data)).convert("RGBA")
            decoded.append(_resize_frame_if_needed(pil, scale))
            durations.append(accumulated)
            accumulated = 0
            kept += 1
            if kept > HARD_ANIMATION_FRAMES:
                raise UnsupportedImageError("This animation is too large to edit safely.")
        if not decoded:
            raise UnsupportedImageError(f"{source.name} does not contain readable video frames.")
        if accumulated:
            durations[-1] += accumulated
        return decoded, durations, 0, reduced
    finally:
        reader.close()


def read_animated_media(path: str | Path) -> tuple[list[Image.Image], list[int], int, str, bool]:
    suffix = Path(path).suffix.lower()
    if suffix == ".gif":
        frames, durations, loop, reduced = read_animated_gif(path)
        return frames, durations, loop, "gif", reduced
    if suffix in {".mp4", ".webm"}:
        frames, durations, loop, reduced = read_animated_video(path)
        return frames, durations, loop, suffix.lstrip("."), reduced
    raise UnsupportedImageError(f"{Path(path).name} is not a supported animation format.")


def _gif_thumbnail(path: str | Path, max_side: int) -> Image.Image:
    with Image.open(path) as raw:
        raw.seek(0)
        image = raw.convert("RGBA")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        return image


def _video_thumbnail(path: str | Path, max_side: int) -> Image.Image:
    reader = imageio.get_reader(str(path))
    try:
        frame_data = reader.get_data(0)
        image = Image.fromarray(np.asarray(frame_data)).convert("RGBA")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        return image
    finally:
        reader.close()


def read_image(path: str | Path) -> Image.Image:
    source = Path(path)
    if source.suffix.lower() in {".mp4", ".webm"}:
        frames, durations, loop, fmt, reduced = read_animated_media(source)
        image = frames[0].copy()
        return _attach_animation_info(image, frames, durations, loop, fmt, reduced)
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
    rgba = frame.convert("RGBA")
    width, height = rgba.size
    even_width = width + (width % 2)
    even_height = height + (height % 2)
    background = Image.new("RGB", (even_width, even_height), (0, 0, 0))
    background.paste(rgba, (0, 0), mask=rgba.getchannel("A"))
    return np.asarray(background, dtype=np.uint8)


def _prepare_gif_frame(frame: Image.Image, colors: int, dither: bool) -> Image.Image:
    palette_colors = max(2, min(256, int(colors or 256)))
    dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    return frame.convert("RGBA").quantize(colors=palette_colors, method=Image.Quantize.FASTOCTREE, dither=dither_mode)


def _save_animation_video(
    frames: list[Image.Image],
    durations: list[int],
    target: Path,
    *,
    fps: int = 0,
    bitrate_kbps: int = 0,
) -> None:
    step_ms = max(16, min(100, min(durations)))
    export_fps = int(fps) if fps and int(fps) > 0 else min(60, max(1, round(1000 / step_ms)))
    codec = "libx264" if target.suffix.lower() == ".mp4" else "libvpx-vp9"
    writer_kwargs = {"fps": export_fps, "codec": codec}
    if bitrate_kbps and int(bitrate_kbps) > 0:
        writer_kwargs["bitrate"] = f"{int(bitrate_kbps)}k"
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=target.suffix, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        writer = imageio.get_writer(str(temporary), **writer_kwargs)
        try:
            for frame, duration in zip(frames, durations):
                repeats = max(1, round(duration * export_fps / 1000))
                rgb = _flatten_frame_for_video(frame)
                for _ in range(repeats):
                    writer.append_data(rgb)
        finally:
            writer.close()
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


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
) -> None:
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix not in {".gif", ".mp4", ".webm"}:
        raise ValueError("Animated images must be saved as GIF, MP4, or WebM files.")
    frame_list = [frame if frame.mode == "RGBA" else frame.convert("RGBA") for frame in frames]
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
        return
    _save_animation_video(frame_list, duration_list, target, fps=fps, bitrate_kbps=bitrate_kbps)


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
