from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from threading import Lock
import os

from PIL import Image, ImageColor, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from imagesuite.utils import (
    HARD_ANIMATION_DECODED_PIXELS,
    available_memory_bytes,
    read_animated_media,
    read_image,  # noqa: F401 - retained as a compatibility seam for callers/tests
    read_processing_image,
    probe_video,
    reserve_destination,
    save_animation,
    save_image,
    unique_destination,
)

# Ponytail: one cached AI model matches the single-AI-worker design. Upgrade to a
# bounded per-device cache only if concurrent model workflows are added later.
_AI_MODEL_LOCK = Lock()
_AI_MODEL_CACHE: tuple[tuple[str, str, str], Any, int, str] | None = None


@dataclass
class UpscaleSettings:
    mode: str = "Scale factor"
    scale_factor: float = 4.0
    target_width: int = 1920
    target_height: int = 1080
    method: str = "Lanczos"
    model_path: str = ""
    device: str = "Auto"
    tile_size: int = 0
    ai_precision: str = "Auto"
    ai_oom_recovery: bool = True
    ai_preview_max_side: int = 640
    output_format: str = "PNG"
    jpeg_quality: int = 95
    webp_quality: int = 92
    animation_fps: int = 0
    video_bitrate_kbps: int = 0
    gif_colors: int = 256
    gif_dither: bool = True
    gif_optimize: bool = False
    preserve_transparency: bool = True
    preserve_audio: bool = True
    preserve_metadata: bool = True
    create_timestamped_folder: bool = True
    export_zip: bool = False
    skip_if_larger: bool = False
    max_workers: int = 1
    sharpen: float = 0.15
    denoise: float = 0.0
    contrast: float = 1.0
    brightness: float = 1.0
    saturation: float = 1.0
    text_watermark: bool = False
    watermark_text: str = "© YourWatermark"
    font_path: str = ""
    font_size: int = 48
    text_rotation: float = 0.0
    font_color: str = "#FFFFFF"
    text_opacity: float = 0.65
    text_anchor: str = "Bottom-Right"
    text_x_percent: float = 100.0
    text_y_percent: float = 100.0
    margin_x: int = 12
    margin_y: int = 12
    outline: bool = True
    outline_color: str = "#000000"
    outline_width: int = 2
    shadow: bool = True
    shadow_offset: int = 2
    shadow_opacity: float = 0.6
    background: bool = False
    background_color: str = "#000000"
    background_opacity: float = 0.45
    image_watermark: bool = False
    image_watermark_path: str = ""
    image_scale: float = 0.12
    image_opacity: float = 0.5
    image_x_percent: float = 100.0
    image_y_percent: float = 100.0



@dataclass(frozen=True)
class WorkerPlan:
    requested: int
    effective: int
    estimated_bytes_per_worker: int
    available_bytes: int
    reason: str


def _estimated_file_working_set(path: str | Path, settings: UpscaleSettings) -> tuple[int, bool]:
    """Estimate peak bytes while one file is decoded, transformed, and encoded."""
    source = Path(path)
    if source.suffix.lower() in {".mp4", ".webm"}:
        try:
            meta = probe_video(source)
            width, height = int(meta["width"]), int(meta["height"])
            fps = float(meta["fps"] or 12.0)
            duration = max(0.02, int(meta["duration_ms"]) / 1000)
            frames = max(2, int(round(fps * duration)))
            animated = True
            has_alpha = False
        except Exception:
            return 256 * 1024**2, True
    else:
        try:
            with Image.open(path) as raw:
                width, height = raw.size
                frames = int(getattr(raw, "n_frames", 1) or 1)
                animated = raw.format == "GIF" and frames > 1
                has_alpha = "A" in raw.getbands() or "transparency" in raw.info
        except Exception:
            # A conservative fallback avoids letting unreadable headers produce 50 heavy tasks.
            return 256 * 1024**2, False

    if settings.mode == "Target size":
        target_width, target_height = max(1, settings.target_width), max(1, settings.target_height)
    else:
        target_width = max(1, round(width * settings.scale_factor))
        target_height = max(1, round(height * settings.scale_factor))
    needs_alpha = bool(animated or settings.text_watermark or settings.image_watermark or (has_alpha and settings.preserve_transparency))
    channels = 4 if needs_alpha else 3
    source_bytes = width * height * channels
    target_bytes = target_width * target_height * channels

    if animated:
        # Animated encoders retain completed frames; keep estimates conservative.
        estimate = target_bytes * frames + max(source_bytes, target_bytes) * 3
        return max(128 * 1024**2, int(estimate * 1.20)), True

    has_finishing = settings.denoise > 0 or settings.sharpen != 0 or any(
        value != 1.0 for value in (settings.contrast, settings.brightness, settings.saturation)
    )
    copies = 1.55  # resize result + encoder/native temporary storage
    if has_finishing:
        copies += 0.85
    if settings.text_watermark or settings.image_watermark:
        copies += 0.75
    if settings.output_format in {"PNG", "TIFF"}:
        copies += 0.35
    return max(8 * 1024**2, source_bytes + int(target_bytes * copies)), False


def plan_worker_count(
    paths: Sequence[str | Path],
    settings: UpscaleSettings,
    *,
    available_bytes: int | None = None,
    cpu_count: int | None = None,
) -> WorkerPlan:
    requested = max(1, min(50, int(settings.max_workers)))
    total = max(1, len(paths))
    if settings.method == "AI model":
        return WorkerPlan(requested, 1, 0, available_memory_bytes() if available_bytes is None else available_bytes, "AI model uses one worker")

    available = max(256 * 1024**2, available_memory_bytes() if available_bytes is None else int(available_bytes))
    path_list = list(paths)
    if len(path_list) <= 128:
        sample = path_list
    else:
        # Header inspection stays bounded for very large queues. Include likely
        # heavy files, GIFs, and an even spread so one unusual source is unlikely
        # to be missed without opening thousands of files before the batch starts.
        gifs = [value for value in path_list if Path(value).suffix.lower() in {".gif", ".mp4", ".webm"}][:32]
        stride = max(1, len(path_list) // 32)
        spread = path_list[::stride][:32]
        sample = list(dict.fromkeys([*path_list[:32], *path_list[-32:], *gifs, *spread]))
    estimates = [_estimated_file_working_set(path, settings) for path in sample]
    estimated = max((value for value, _animated in estimates), default=256 * 1024**2)
    contains_animation = any(animated for _value, animated in estimates)
    memory_budget = max(256 * 1024**2, int(available * 0.35))
    memory_cap = max(1, memory_budget // max(1, estimated))
    logical_cpus = max(1, cpu_count if cpu_count is not None else (os.cpu_count() or 4))
    encoder_heavy = settings.output_format in {"PNG", "WebP"}
    if estimated <= 8 * 1024**2:
        # Truly small images benefit from I/O-heavy parallelism.
        cpu_cap = min(50, max(2, logical_cpus * 2))
    elif estimated <= 96 * 1024**2:
        # Measured photo batches peak before all logical cores are saturated:
        # JPEG/TIFF around six workers, PNG/WebP around four.
        cpu_cap = min(4 if encoder_heavy else 6, logical_cpus)
    elif estimated <= 512 * 1024**2:
        cpu_cap = min(3 if encoder_heavy else 4, logical_cpus)
    else:
        cpu_cap = min(2, logical_cpus)
    if contains_animation:
        # Animated jobs retain processed frames for GIF encoding; keep them scarce.
        cpu_cap = min(cpu_cap, 2)
    effective = max(1, min(requested, total, memory_cap, cpu_cap))
    limits: list[str] = []
    if effective < requested:
        if effective == memory_cap:
            limits.append("available memory")
        if effective == cpu_cap:
            limits.append("CPU/animation safety")
        if effective == total:
            limits.append("queue size")
    reason = ", ".join(limits) if limits else "requested limit"
    return WorkerPlan(requested, effective, estimated, available, reason)

def available_models(folder: str | Path) -> list[Path]:
    root = Path(folder)
    if not root.exists():
        return []
    return sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".pth", ".pt", ".safetensors"}), key=lambda p: p.name.lower())


def calculate_size(image: Image.Image, settings: UpscaleSettings) -> tuple[int, int]:
    if settings.mode == "Target size":
        return max(1, settings.target_width), max(1, settings.target_height)
    return max(1, round(image.width * settings.scale_factor)), max(1, round(image.height * settings.scale_factor))


def validate_settings(settings: UpscaleSettings, source_size: tuple[int, int] | None = None) -> None:
    if settings.mode not in {"Scale factor", "Target size"}:
        raise ValueError(f"Unknown size mode: {settings.mode}")
    if settings.mode == "Scale factor" and settings.scale_factor <= 0:
        raise ValueError("Scale factor must be greater than zero.")
    if settings.mode == "Target size" and (settings.target_width <= 0 or settings.target_height <= 0):
        raise ValueError("Target width and height must be greater than zero.")
    if settings.output_format not in {"PNG", "JPEG", "WebP", "TIFF", "GIF", "MP4", "WebM"}:
        raise ValueError(f"Unsupported output format: {settings.output_format}")
    if not 1 <= int(settings.max_workers) <= 50:
        raise ValueError("Parallel workers must be between 1 and 50.")
    if int(settings.animation_fps) < 0 or int(settings.animation_fps) > 120:
        raise ValueError("Animation FPS must be between 0 and 120.")
    if int(settings.video_bitrate_kbps) < 0 or int(settings.video_bitrate_kbps) > 50000:
        raise ValueError("Video bitrate must be between 0 and 50000 kbps.")
    if not 2 <= int(settings.gif_colors) <= 256:
        raise ValueError("GIF colors must be between 2 and 256.")
    if settings.ai_precision not in {"Auto", "FP16", "FP32"}:
        raise ValueError(f"Unknown AI precision: {settings.ai_precision}")
    if int(settings.tile_size) < 0 or int(settings.tile_size) > 4096:
        raise ValueError("AI tile size must be Auto or between 64 and 4096 pixels.")
    if 0 < int(settings.tile_size) < 64:
        raise ValueError("Manual AI tile size must be at least 64 pixels.")
    if not 256 <= int(settings.ai_preview_max_side) <= 1600:
        raise ValueError("AI preview size must be between 256 and 1600 pixels.")
    if settings.text_watermark:
        _rgb(settings.font_color, "font color")
        if settings.outline:
            _rgb(settings.outline_color, "outline color")
        if settings.background:
            _rgb(settings.background_color, "background color")
        if settings.font_path and not Path(settings.font_path).is_file():
            raise ValueError(f"Watermark font was not found: {settings.font_path}")
    if settings.image_watermark and not Path(settings.image_watermark_path).is_file():
        raise ValueError("Choose a valid watermark image.")
    if source_size:
        if settings.mode == "Target size":
            target = (settings.target_width, settings.target_height)
        else:
            target = (round(source_size[0] * settings.scale_factor), round(source_size[1] * settings.scale_factor))
        # Ponytail: 300 MP is a deliberate safety ceiling against accidental RAM exhaustion.
        # Raise it only after adding streamed output and measured memory budgeting.
        if target[0] * target[1] > 300_000_000:
            raise ValueError(f"Requested output is {target[0]} × {target[1]} (>300 megapixels). Choose a smaller size.")


def pil_upscale(image: Image.Image, size: tuple[int, int], method: str) -> Image.Image:
    methods = {
        "Nearest": Image.Resampling.NEAREST,
        "Bilinear": Image.Resampling.BILINEAR,
        "Bicubic": Image.Resampling.BICUBIC,
        "Lanczos": Image.Resampling.LANCZOS,
    }
    return image.resize(size, methods.get(method, Image.Resampling.LANCZOS))


def _torch_device(choice: str):
    import torch
    if choice == "CPU":
        return torch.device("cpu")
    if choice == "CUDA":
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA was selected, but PyTorch cannot access a CUDA device.")
        return torch.device("cuda")
    if choice == "DirectML":
        try:
            import torch_directml
            return torch_directml.device()
        except Exception as exc:
            raise RuntimeError("DirectML support requires torch-directml.") from exc
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _resolved_ai_precision(choice: str, device: Any) -> str:
    if choice == "FP32":
        return "FP32"
    if choice == "FP16":
        if "cuda" not in str(device).lower():
            return "FP32"
        return "FP16"
    return "FP16" if "cuda" in str(device).lower() else "FP32"


def _configure_torch_for_ai(torch: Any, device: Any) -> None:
    if "cuda" not in str(device).lower():
        return
    try:
        torch.backends.cudnn.benchmark = True
    except Exception:
        pass
    try:
        torch.set_float32_matmul_precision("high")
    except Exception:
        pass


def _cached_ai_model(model_path: str, device: Any, loader_type: Any, torch: Any, precision: str = "Auto") -> tuple[Any, int, str]:
    global _AI_MODEL_CACHE
    actual_precision = _resolved_ai_precision(precision, device)
    key = (str(Path(model_path).resolve()), str(device), actual_precision)
    with _AI_MODEL_LOCK:
        if _AI_MODEL_CACHE and _AI_MODEL_CACHE[0] == key:
            return _AI_MODEL_CACHE[1], _AI_MODEL_CACHE[2], _AI_MODEL_CACHE[3]

        previous = _AI_MODEL_CACHE
        _AI_MODEL_CACHE = None
        if previous is not None:
            previous_device = previous[0][1]
            del previous
            if "cuda" in previous_device:
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass

        try:
            descriptor = loader_type().load_from_file(model_path)
            model = descriptor.model.eval().to(device)
            if actual_precision == "FP16" and hasattr(model, "half"):
                model = model.half()
            elif hasattr(model, "float"):
                model = model.float()
            scale = int(getattr(descriptor, "scale", 4) or 4)
            if scale < 1 or scale > 16:
                raise RuntimeError(f"The model reported an unsupported scale: {scale}×")
            _configure_torch_for_ai(torch, device)
            _AI_MODEL_CACHE = (key, model, scale, actual_precision)
            return model, scale, actual_precision
        except Exception:
            _AI_MODEL_CACHE = None
            raise


def release_ai_model() -> None:
    """Release the batch model after processing instead of pinning CPU/GPU RAM."""
    global _AI_MODEL_CACHE
    with _AI_MODEL_LOCK:
        cached = _AI_MODEL_CACHE
        _AI_MODEL_CACHE = None
    if cached is None:
        return
    device_name = cached[0][1]
    del cached
    if "cuda" in device_name:
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass


def ai_environment_summary(device_choice: str = "Auto") -> str:
    try:
        import torch
    except Exception:
        return "AI support is not installed. Use Enhance → Install / Repair AI."
    lines = [f"PyTorch {getattr(torch, '__version__', 'unknown')}"]
    try:
        device = _torch_device(device_choice)
        lines.append(f"Device: {device}")
        if "cuda" in str(device).lower():
            name = torch.cuda.get_device_name(device)
            free, total = torch.cuda.mem_get_info(device)
            lines.append(f"GPU: {name}")
            lines.append(f"VRAM: {free / 1024**3:.1f} GB free / {total / 1024**3:.1f} GB")
            lines.append("Auto precision: FP16")
        elif device_choice == "DirectML":
            lines.append("Precision: FP32")
        else:
            lines.append("Precision: FP32")
    except Exception as exc:
        lines.append(f"Unavailable: {exc}")
    return " · ".join(lines)


def prepare_ai_model(settings: UpscaleSettings, progress: Optional[Callable[[str], None]] = None) -> tuple[Any, int, Any, str]:
    if not settings.model_path:
        raise RuntimeError("Choose an AI model first.")
    model_path = Path(settings.model_path)
    if not model_path.is_file():
        raise RuntimeError(f"AI model was not found: {model_path}")
    if model_path.suffix.lower() not in {".pth", ".pt", ".safetensors"}:
        raise RuntimeError(f"Unsupported AI model file: {model_path.suffix}")
    try:
        import torch
        from spandrel import ModelLoader
    except Exception as exc:
        raise RuntimeError("AI upscaling requires torch and spandrel. Use Enhance → Install / Repair AI first.") from exc
    device = _torch_device(settings.device)
    if progress:
        progress(f"Loading {model_path.name} on {device}")
    model, scale, precision = _cached_ai_model(str(model_path), device, ModelLoader, torch, settings.ai_precision)
    if progress:
        progress(f"AI ready: {scale}× model · {precision} · {device}")
    return model, scale, device, precision


def _cuda_free_bytes(torch: Any, device: Any) -> int:
    try:
        free, _total = torch.cuda.mem_get_info(device)
        return int(free)
    except Exception:
        return 0


def recommend_ai_tile(image_size: tuple[int, int], device: Any, precision: str, torch: Any) -> int:
    max_side = max(image_size)
    if "cuda" in str(device).lower():
        free = _cuda_free_bytes(torch, device)
        if free >= 14 * 1024**3:
            tile = 1024
        elif free >= 9 * 1024**3:
            tile = 768
        elif free >= 6 * 1024**3:
            tile = 512
        elif free >= 4 * 1024**3:
            tile = 384
        else:
            tile = 256
        if precision == "FP32":
            tile = max(128, int(tile * 0.75) // 32 * 32)
    else:
        available = available_memory_bytes()
        tile = 512 if available >= 12 * 1024**3 else 384 if available >= 6 * 1024**3 else 256
    return max(64, min(max_side, tile))


def _is_out_of_memory(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "out of memory" in message or "not enough memory" in message or "allocation" in message and "failed" in message


def _tile_candidates(initial: int, recover: bool) -> list[int]:
    initial = max(64, int(initial))
    if not recover:
        return [initial]
    values = [initial]
    for value in (1024, 768, 512, 384, 256, 192, 128, 96, 64):
        if value < initial and value not in values:
            values.append(value)
    return values


def _tensor_tile_to_image(model: Any, tensor_u8: Any, device: Any, precision: str, torch: Any) -> Image.Image:
    dtype = torch.float16 if precision == "FP16" else torch.float32
    tensor = tensor_u8.to(device=device, dtype=dtype).div_(255.0)
    with torch.inference_mode():
        result = model(tensor)
    result = result.squeeze(0).float().clamp(0, 1).cpu().numpy().transpose(1, 2, 0)
    return Image.fromarray((result * 255.0).round().astype("uint8"), "RGB")


def _model_tile_to_image(model: Any, image: Image.Image, device: Any, precision: str, torch: Any) -> Image.Image:
    import numpy as np

    rgb = image if image.mode == "RGB" else image.convert("RGB")
    try:
        array = np.asarray(rgb, dtype=np.uint8)
        tensor_u8 = torch.from_numpy(array.transpose(2, 0, 1).copy()).unsqueeze(0)
        return _tensor_tile_to_image(model, tensor_u8, device, precision, torch)
    finally:
        if rgb is not image:
            rgb.close()


def _run_model_stitched(
    model: Any,
    image: Image.Image,
    tile: int,
    scale: int,
    device: Any,
    precision: str,
    torch: Any,
    progress: Optional[Callable[[str], None]] = None,
) -> Image.Image:
    import numpy as np

    width, height = image.size
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    try:
        array = np.asarray(rgb, dtype=np.uint8)
        source_u8 = torch.from_numpy(array.transpose(2, 0, 1).copy()).unsqueeze(0)
    finally:
        if rgb is not image:
            rgb.close()
    if width <= tile and height <= tile:
        return _tensor_tile_to_image(model, source_u8, device, precision, torch)

    overlap = min(24, max(8, tile // 16))
    core = max(32, tile - overlap * 2)
    x_positions = list(range(0, width, core))
    y_positions = list(range(0, height, core))
    total_tiles = len(x_positions) * len(y_positions)
    output = Image.new("RGB", (width * scale, height * scale))
    done = 0
    try:
        for top in y_positions:
            core_bottom = min(height, top + core)
            for left in x_positions:
                core_right = min(width, left + core)
                input_left = max(0, left - overlap)
                input_top = max(0, top - overlap)
                input_right = min(width, core_right + overlap)
                input_bottom = min(height, core_bottom + overlap)
                tile_tensor = source_u8[:, :, input_top:input_bottom, input_left:input_right]
                tile_output = _tensor_tile_to_image(model, tile_tensor, device, precision, torch)
                crop_left = (left - input_left) * scale
                crop_top = (top - input_top) * scale
                crop_right = crop_left + (core_right - left) * scale
                crop_bottom = crop_top + (core_bottom - top) * scale
                core_output = tile_output.crop((crop_left, crop_top, crop_right, crop_bottom))
                try:
                    output.paste(core_output, (left * scale, top * scale))
                finally:
                    core_output.close()
                    tile_output.close()
                done += 1
                if progress and (done == 1 or done == total_tiles or done % max(1, total_tiles // 10) == 0):
                    progress(f"AI tiles: {done}/{total_tiles}")
        return output
    except Exception:
        output.close()
        raise


def _ai_upscale_once(
    image: Image.Image,
    settings: UpscaleSettings,
    model: Any,
    scale: int,
    device: Any,
    precision: str,
    torch: Any,
    progress: Optional[Callable[[str], None]],
) -> Image.Image:
    tile = int(settings.tile_size) or recommend_ai_tile(image.size, device, precision, torch)
    if progress:
        source = "auto" if int(settings.tile_size) == 0 else "manual"
        progress(f"AI inference: {precision} · {tile}px tile ({source})")
    last_error: BaseException | None = None
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    try:
        for candidate in _tile_candidates(tile, settings.ai_oom_recovery):
            try:
                if candidate != tile and progress:
                    progress(f"Retrying with {candidate}px tiles after memory pressure")
                return _run_model_stitched(model, rgb, candidate, scale, device, precision, torch, progress)
            except RuntimeError as exc:
                if not _is_out_of_memory(exc):
                    raise
                last_error = exc
                if "cuda" in str(device).lower():
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                continue
    finally:
        if rgb is not image:
            rgb.close()
    raise RuntimeError(
        "The AI model ran out of memory even after automatic tile reduction. "
        "Close other GPU applications, choose Low memory, or use CPU."
    ) from last_error


def ai_upscale(image: Image.Image, settings: UpscaleSettings, progress: Optional[Callable[[str], None]] = None) -> Image.Image:
    try:
        import torch
    except Exception as exc:
        raise RuntimeError("AI upscaling requires torch, numpy, and spandrel. Use Enhance → Install / Repair AI.") from exc
    if progress:
        progress("Preparing AI model")
    model, scale, device, precision = prepare_ai_model(settings, progress)
    try:
        out = _ai_upscale_once(image, settings, model, scale, device, precision, torch, progress)
    except RuntimeError as exc:
        if precision == "FP16" and settings.ai_precision == "Auto" and not _is_out_of_memory(exc):
            if progress:
                progress("FP16 was not supported by this model; retrying in FP32")
            release_ai_model()
            fp32_settings = replace(settings, ai_precision="FP32")
            model, scale, device, precision = prepare_ai_model(fp32_settings, progress)
            out = _ai_upscale_once(image, fp32_settings, model, scale, device, precision, torch, progress)
        else:
            raise
    if image.mode == "RGBA" and settings.preserve_transparency:
        alpha = image.getchannel("A").resize(out.size, Image.Resampling.LANCZOS)
        rgba = out.convert("RGBA")
        out.close()
        out = rgba
        out.putalpha(alpha)
        alpha.close()
    target = calculate_size(image, settings)
    if out.size != target:
        resized = out.resize(target, Image.Resampling.LANCZOS)
        out.close()
        out = resized
    return out


def finish_image(image: Image.Image, settings: UpscaleSettings) -> Image.Image:
    """Apply only requested finishing passes while preserving RGB/RGBA mode."""
    result = image

    def replace_result(updated: Image.Image) -> None:
        nonlocal result
        previous = result
        result = updated
        if previous is not image and previous is not updated:
            previous.close()

    if settings.denoise > 0:
        filtered = result.filter(ImageFilter.MedianFilter(size=3))
        try:
            replace_result(Image.blend(result, filtered, max(0.0, min(1.0, settings.denoise))))
        finally:
            filtered.close()
    if settings.sharpen:
        replace_result(ImageEnhance.Sharpness(result).enhance(max(0.0, 1 + settings.sharpen)))
    if settings.contrast != 1.0:
        replace_result(ImageEnhance.Contrast(result).enhance(max(0.0, settings.contrast)))
    if settings.brightness != 1.0:
        replace_result(ImageEnhance.Brightness(result).enhance(max(0.0, settings.brightness)))
    if settings.saturation != 1.0:
        replace_result(ImageEnhance.Color(result).enhance(max(0.0, settings.saturation)))
    return result


@lru_cache(maxsize=32)
def _cached_font(path: str, size: int) -> ImageFont.ImageFont:
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _font(settings: UpscaleSettings) -> ImageFont.ImageFont:
    return _cached_font(settings.font_path, settings.font_size)


@lru_cache(maxsize=1)
def _cached_watermark_image(path: str, mtime_ns: int, size_bytes: int) -> Image.Image:
    del mtime_ns, size_bytes
    with Image.open(path) as raw:
        return raw.convert("RGBA").copy()


def _rgb(value: str, label: str) -> tuple[int, int, int]:
    try:
        return ImageColor.getrgb(value)[:3]
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {value!r}") from exc


def _anchor_position(canvas: tuple[int, int], item: tuple[int, int], anchor: str, x_percent: float, y_percent: float, margin_x: int, margin_y: int) -> tuple[int, int]:
    width, height = canvas
    item_w, item_h = item
    anchors = {
        "Top-Left": (margin_x, margin_y),
        "Top-Center": ((width - item_w) // 2, margin_y),
        "Top-Right": (width - item_w - margin_x, margin_y),
        "Center-Left": (margin_x, (height - item_h) // 2),
        "Center": ((width - item_w) // 2, (height - item_h) // 2),
        "Center-Right": (width - item_w - margin_x, (height - item_h) // 2),
        "Bottom-Left": (margin_x, height - item_h - margin_y),
        "Bottom-Center": ((width - item_w) // 2, height - item_h - margin_y),
        "Bottom-Right": (width - item_w - margin_x, height - item_h - margin_y),
        "Custom": (round((width - item_w) * x_percent / 100), round((height - item_h) * y_percent / 100)),
    }
    x, y = anchors.get(anchor, anchors["Bottom-Right"])
    return max(0, min(width - item_w, x)), max(0, min(height - item_h, y))


def add_watermarks(image: Image.Image, settings: UpscaleSettings) -> Image.Image:
    if not (settings.text_watermark and settings.watermark_text) and not (settings.image_watermark and settings.image_watermark_path):
        return image
    base = image.copy() if image.mode == "RGBA" else image.convert("RGBA")
    if settings.text_watermark and settings.watermark_text:
        font = _font(settings)
        dummy = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        try:
            draw = ImageDraw.Draw(dummy)
            bbox = draw.multiline_textbbox((0, 0), settings.watermark_text, font=font, stroke_width=settings.outline_width if settings.outline else 0)
        finally:
            dummy.close()
        text_w, text_h = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
        pad = max(6, settings.font_size // 6)
        layer = Image.new("RGBA", (text_w + pad * 2 + settings.shadow_offset, text_h + pad * 2 + settings.shadow_offset), (0, 0, 0, 0))
        try:
            ld = ImageDraw.Draw(layer)
            if settings.background:
                rgb = _rgb(settings.background_color, "background color")
                ld.rounded_rectangle((0, 0, layer.width - 1, layer.height - 1), radius=max(3, pad // 2), fill=(*rgb, round(settings.background_opacity * 255)))
            outline_color = _rgb(settings.outline_color, "outline color") if settings.outline else (0, 0, 0)
            if settings.shadow:
                ld.multiline_text((pad + settings.shadow_offset, pad + settings.shadow_offset), settings.watermark_text, font=font, fill=(0, 0, 0, round(settings.shadow_opacity * 255)), stroke_width=settings.outline_width if settings.outline else 0, stroke_fill=outline_color)
            rgb = _rgb(settings.font_color, "text color")
            ld.multiline_text((pad, pad), settings.watermark_text, font=font, fill=(*rgb, round(settings.text_opacity * 255)), stroke_width=settings.outline_width if settings.outline else 0, stroke_fill=outline_color)
            composite_layer = layer
            if settings.text_rotation:
                composite_layer = layer.rotate(-settings.text_rotation, expand=True, resample=Image.Resampling.BICUBIC)
            try:
                pos = _anchor_position(base.size, composite_layer.size, settings.text_anchor, settings.text_x_percent, settings.text_y_percent, settings.margin_x, settings.margin_y)
                base.alpha_composite(composite_layer, pos)
            finally:
                if composite_layer is not layer:
                    composite_layer.close()
        finally:
            layer.close()
    if settings.image_watermark and settings.image_watermark_path:
        logo_path = Path(settings.image_watermark_path)
        stat = logo_path.stat()
        cached_logo = _cached_watermark_image(str(logo_path.resolve()), stat.st_mtime_ns, stat.st_size)
        target_w = max(1, round(base.width * settings.image_scale))
        target_h = max(1, round(cached_logo.height * target_w / cached_logo.width))
        logo = cached_logo.resize((target_w, target_h), Image.Resampling.LANCZOS)
        try:
            alpha = logo.getchannel("A").point(lambda p: round(p * settings.image_opacity))
            try:
                logo.putalpha(alpha)
            finally:
                alpha.close()
            pos = _anchor_position(base.size, logo.size, "Custom", settings.image_x_percent, settings.image_y_percent, settings.margin_x, settings.margin_y)
            base.alpha_composite(logo, pos)
        finally:
            logo.close()
    return base


def process_image(image: Image.Image, settings: UpscaleSettings, progress: Optional[Callable[[str], None]] = None) -> Image.Image:
    validate_settings(settings, image.size)
    target = calculate_size(image, settings)
    if settings.method == "AI model":
        result = ai_upscale(image, settings, progress)
    else:
        if progress:
            progress(f"Resizing with {settings.method}")
        result = pil_upscale(image, target, settings.method)
    try:
        finished = finish_image(result, settings)
        if finished is not result:
            result.close()
            result = finished
        watermarked = add_watermarks(result, settings)
        if watermarked is not result:
            result.close()
            result = watermarked
        return result
    except Exception:
        result.close()
        raise


def output_path(source: str | Path, output_folder: str | Path, settings: UpscaleSettings, *, animated: bool = False) -> Path:
    src = Path(source)
    suffix_map = {"PNG": ".png", "JPEG": ".jpg", "WebP": ".webp", "TIFF": ".tiff", "GIF": ".gif", "MP4": ".mp4", "WebM": ".webm"}
    suffix = suffix_map.get(settings.output_format, ".gif" if animated else ".png")
    if animated and suffix not in {".gif", ".mp4", ".webm"}:
        suffix = ".gif"
    candidate = Path(output_folder) / f"{src.stem}_upscaled{suffix}"
    return unique_destination(candidate.parent, candidate)


def _reserve_output_path(source: str | Path, output_folder: str | Path, settings: UpscaleSettings, *, animated: bool = False) -> Path:
    src = Path(source)
    suffix_map = {"PNG": ".png", "JPEG": ".jpg", "WebP": ".webp", "TIFF": ".tiff", "GIF": ".gif", "MP4": ".mp4", "WebM": ".webm"}
    suffix = suffix_map.get(settings.output_format, ".gif" if animated else ".png")
    if animated and suffix not in {".gif", ".mp4", ".webm"}:
        suffix = ".gif"
    return reserve_destination(output_folder, f"{src.stem}_upscaled{suffix}")


def _gif_frame_duration(raw: Image.Image) -> int:
    try:
        return max(10, int(raw.info.get("duration", 100) or 100))
    except (TypeError, ValueError):
        return 100


def _process_animated_media(
    source: str | Path,
    output_folder: str | Path,
    settings: UpscaleSettings,
    progress: Optional[Callable[[str], None]],
) -> Path | None:
    processed_frames: list[Image.Image] = []
    target: Path | None = None
    frames, durations, loop, _fmt, reduced = read_animated_media(source)
    frame_count = len(frames)
    if frame_count < 2:
        raise ValueError("The source is not an animated animation.")
    validate_settings(settings, frames[0].size)
    target_size = calculate_size(frames[0], settings)
    if settings.skip_if_larger and frames[0].width >= target_size[0] and frames[0].height >= target_size[1]:
        if progress:
            progress("Skipped: source already meets target size")
        return None
    output_pixels = target_size[0] * target_size[1] * frame_count
    if output_pixels > HARD_ANIMATION_DECODED_PIXELS:
        raise ValueError(
            "The requested animated output is too large to process safely in memory. "
            "Reduce the scale, resolution, or frame rate."
        )
    for index, frame in enumerate(frames):
        if progress:
            note = " (optimized import)" if reduced else ""
            progress(f"Processing animation frame {index + 1}/{frame_count}{note}")
        try:
            processed_frames.append(process_image(frame, settings, None))
        finally:
            frame.close()

    try:
        target = _reserve_output_path(source, output_folder, settings, animated=True)
        preserve_source_audio = (
            settings.preserve_audio
            and Path(source).suffix.lower() in {".mp4", ".webm"}
            and target.suffix.lower() in {".mp4", ".webm"}
        )
        save_animation(
            processed_frames,
            durations,
            target,
            loop=loop,
            fps=settings.animation_fps,
            bitrate_kbps=settings.video_bitrate_kbps,
            gif_colors=settings.gif_colors,
            gif_dither=settings.gif_dither,
            gif_optimize=settings.gif_optimize,
            audio_source=Path(source) if preserve_source_audio else None,
            audio_start_ms=0,
            audio_duration_ms=sum(durations),
        )
        return target
    except Exception:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    finally:
        for frame in processed_frames:
            frame.close()
        processed_frames.clear()


def process_file(source: str | Path, output_folder: str | Path, settings: UpscaleSettings, progress: Optional[Callable[[str], None]] = None) -> Path | None:
    source_path = Path(source)
    if source_path.suffix.lower() in {".mp4", ".webm"}:
        return _process_animated_media(source_path, output_folder, settings, progress)
    try:
        with Image.open(source_path) as header:
            animated = header.format == "GIF" and int(getattr(header, "n_frames", 1) or 1) > 1
    except Exception:
        animated = False
    if animated:
        return _process_animated_media(source_path, output_folder, settings, progress)
    if settings.output_format in {"MP4", "WebM"}:
        raise ValueError("MP4 and WebM export are available for animated sources only.")

    image: Image.Image | None = None
    result: Image.Image | None = None
    target: Path | None = None
    try:
        image = read_processing_image(source_path, preserve_transparency=settings.preserve_transparency)
        source_metadata = dict(image.info)
        target_size = calculate_size(image, settings)
        if settings.skip_if_larger and image.width >= target_size[0] and image.height >= target_size[1]:
            if progress:
                progress("Skipped: source already meets target size")
            return None

        result = process_image(image, settings, progress)
        quality = settings.jpeg_quality if settings.output_format == "JPEG" else settings.webp_quality
        target = _reserve_output_path(source_path, output_folder, settings)
        # Batch throughput matters more than a few percent of extra compression.
        # Editor saves retain the slower optimize=True default.
        if progress:
            progress(f"Encoding {settings.output_format}")
        save_image(
            result,
            target,
            quality=quality,
            optimize=False,
            metadata=source_metadata if settings.preserve_metadata else {},
            png_compress_level=3 if settings.output_format == "PNG" else None,
        )
        return target
    except Exception:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    finally:
        if result is not None and result is not image:
            result.close()
        if image is not None:
            image.close()

