from __future__ import annotations

from typing import Callable, Iterable
from functools import lru_cache
import math
import colorsys

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageMath, ImageOps

from imagesuite.models import RectMask


def _rgba(image: Image.Image) -> Image.Image:
    return image.convert("RGBA")


def _with_original_alpha(source: Image.Image, result: Image.Image) -> Image.Image:
    """Apply source alpha while consuming both temporary input images."""
    output = result if result.mode == "RGBA" else result.convert("RGBA")
    if output is not result:
        result.close()
    alpha = source.getchannel("A") if "A" in source.getbands() else Image.new("L", source.size, 255)
    try:
        output.putalpha(alpha)
    finally:
        alpha.close()
        if source is not output:
            source.close()
    return output


def _seed(width: int, height: int, *values: int) -> int:
    seed = (width * 73856093) ^ (height * 19349663)
    for value in values:
        seed ^= int(value) * 83492791
    return seed & 0xFFFFFFFF


def _shift_plane_no_wrap(plane: np.ndarray, dx: int) -> np.ndarray:
    if dx == 0:
        return plane.copy()
    width = plane.shape[1]
    dx = max(-width + 1, min(width - 1, int(dx)))
    out = np.empty_like(plane)
    if dx > 0:
        out[:, dx:] = plane[:, :-dx]
        out[:, :dx] = plane[:, :1]
    else:
        amount = -dx
        out[:, :-amount] = plane[:, amount:]
        out[:, -amount:] = plane[:, -1:]
    return out


def pixelate(image: Image.Image, block: int) -> Image.Image:
    rgba = _rgba(image)
    block = max(2, int(block))
    small = rgba.resize(
        (max(1, math.ceil(rgba.width / block)), max(1, math.ceil(rgba.height / block))),
        Image.Resampling.BOX,
    )
    try:
        result = small.resize(rgba.size, Image.Resampling.NEAREST)
    finally:
        small.close()
    return _with_original_alpha(rgba, result)


def mosaic(image: Image.Image, tile: int) -> Image.Image:
    rgba = _rgba(image)
    tile = max(3, int(tile))
    columns = max(1, math.ceil(rgba.width / tile))
    rows = max(1, math.ceil(rgba.height / tile))
    out = rgba.resize((columns, rows), Image.Resampling.BOX).resize(rgba.size, Image.Resampling.NEAREST)
    if tile >= 7:
        overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        line_alpha = min(80, 25 + tile)
        for x in range(tile, rgba.width, tile):
            draw.line((x, 0, x, rgba.height), fill=(0, 0, 0, line_alpha))
        for y in range(tile, rgba.height, tile):
            draw.line((0, y, rgba.width, y), fill=(0, 0, 0, line_alpha))
        out = Image.alpha_composite(out, overlay)
    return _with_original_alpha(rgba, out)


def fill_color(image: Image.Image, color: tuple[int, int, int, int]) -> Image.Image:
    rgba = _rgba(image)
    return _with_original_alpha(rgba, Image.new("RGBA", rgba.size, color))


def gaussian_blur(image: Image.Image, radius: float) -> Image.Image:
    rgba = _rgba(image)
    return _with_original_alpha(rgba, rgba.filter(ImageFilter.GaussianBlur(max(0.25, float(radius)))))


def deep_blur(image: Image.Image, strength: int = 70, *, scale: float = 1.0) -> Image.Image:
    rgba = _rgba(image)
    strength = max(1, min(100, int(strength)))
    factor = max(2, round(2 + strength / 8))
    small = rgba.resize(
        (max(1, math.ceil(rgba.width / factor)), max(1, math.ceil(rgba.height / factor))),
        Image.Resampling.BOX,
    )
    out = small.resize(rgba.size, Image.Resampling.BILINEAR)
    out = out.filter(ImageFilter.GaussianBlur(max(0.5, strength / 8 * max(0.001, scale))))
    return _with_original_alpha(rgba, out)


def frosted_glass(image: Image.Image, strength: int = 60, *, scale: float = 1.0) -> Image.Image:
    rgba = _rgba(image)
    strength = max(1, min(100, int(strength)))
    blurred = gaussian_blur(rgba, max(0.5, strength / 7 * max(0.001, scale)))
    rgb = np.asarray(blurred.convert("RGB"), dtype=np.int16)
    rng = np.random.default_rng(_seed(rgba.width, rgba.height, strength, 17))
    noise = rng.normal(0.0, 3.0 + strength / 6.0, rgb.shape[:2] + (1,))
    frosted = np.clip(rgb + noise, 0, 255).astype(np.uint8)
    out = Image.fromarray(frosted, "RGB").convert("RGBA")
    return _with_original_alpha(rgba, out)


def glass_tiles(image: Image.Image, tile: int = 20, strength: int = 75) -> Image.Image:
    rgba = _rgba(image)
    tile = max(4, int(tile))
    strength = max(1, min(100, int(strength)))
    rgb = np.asarray(rgba.convert("RGB"))
    height, width = rgb.shape[:2]
    pad_h = (-height) % tile
    pad_w = (-width) % tile
    padded = np.pad(rgb, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
    rows, cols = padded.shape[0] // tile, padded.shape[1] // tile
    blocks = padded.reshape(rows, tile, cols, tile, 3).transpose(0, 2, 1, 3, 4)
    flat = blocks.reshape(rows * cols, tile, tile, 3)
    out_flat = flat.copy()
    rng = np.random.default_rng(_seed(width, height, tile, strength, 29))
    count = max(1, round(len(flat) * strength / 100))
    targets = rng.choice(len(flat), size=count, replace=False)
    sources = rng.permutation(len(flat))[:count]
    out_flat[targets] = flat[sources]
    rebuilt = out_flat.reshape(rows, cols, tile, tile, 3).transpose(0, 2, 1, 3, 4).reshape(rows * tile, cols * tile, 3)
    out = Image.fromarray(rebuilt[:height, :width], "RGB").convert("RGBA")
    return _with_original_alpha(rgba, out)


def noise_redaction(image: Image.Image, strength: int = 80) -> Image.Image:
    rgba = _rgba(image)
    strength = max(1, min(100, int(strength)))
    source = np.asarray(rgba.convert("RGB"), dtype=np.uint16)
    rng = np.random.default_rng(_seed(rgba.width, rgba.height, strength, 41))
    noise = rng.integers(0, 256, source.shape, dtype=np.uint8).astype(np.uint16)
    mixed = ((source * (100 - strength) + noise * strength) // 100).astype(np.uint8)
    return _with_original_alpha(rgba, Image.fromarray(mixed, "RGB"))


def marker_scribble(image: Image.Image, size: int = 18, strength: int = 80) -> Image.Image:
    rgba = _rgba(image)
    size = max(5, int(size))
    strength = max(1, min(100, int(strength)))
    background = ImageEnhance.Brightness(deep_blur(rgba, max(45, strength))).enhance(max(0.28, 0.72 - strength / 230))
    overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    rng = np.random.default_rng(_seed(rgba.width, rgba.height, size, strength, 53))
    strokes = max(4, 3 + strength // 14)
    stroke_width = max(4, round(size * (0.55 + strength / 250)))
    for index in range(strokes):
        center_y = (index + 0.5) * rgba.height / strokes
        points = []
        step = max(18, size * 2)
        for x in range(-step, rgba.width + step * 2, step):
            jitter = int(rng.integers(-max(2, size), max(3, size + 1)))
            points.append((x, round(center_y + jitter)))
        draw.line(points, fill=(5, 5, 5, min(255, 185 + strength // 2)), width=stroke_width, joint="curve")
    result = Image.alpha_composite(background, overlay)
    return _with_original_alpha(rgba, result)

def redaction_tape(image: Image.Image, size: int = 16, strength: int = 90) -> Image.Image:
    rgba = _rgba(image)
    size = max(6, int(size))
    strength = max(1, min(100, int(strength)))
    rng = np.random.default_rng(_seed(rgba.width, rgba.height, size, strength, 67))
    texture = rng.normal(18, max(2.0, 10 - strength / 14), (rgba.height, rgba.width, 1))
    texture = np.clip(texture, 0, 42).astype(np.uint8)
    rgb = np.repeat(texture, 3, axis=2)
    out = Image.fromarray(rgb, "RGB").convert("RGBA")
    draw = ImageDraw.Draw(out, "RGBA")
    crease_gap = max(8, size * 2)
    for y in range(crease_gap // 2, rgba.height, crease_gap):
        jitter = int(rng.integers(-size // 3, size // 3 + 1))
        draw.line((0, y + jitter, rgba.width, y + jitter), fill=(90, 90, 90, 45), width=max(1, size // 8))
    return _with_original_alpha(rgba, out)


def halftone_dots(image: Image.Image, cell: int = 12, strength: int = 70) -> Image.Image:
    rgba = _rgba(image)
    cell = max(4, int(cell))
    strength = max(1, min(100, int(strength)))
    cols = max(1, math.ceil(rgba.width / cell))
    rows = max(1, math.ceil(rgba.height / cell))
    gray = np.asarray(ImageOps.grayscale(rgba).resize((cols, rows), Image.Resampling.BOX), dtype=np.float32)
    darkness = 1.0 - gray / 255.0
    max_radius = cell * (0.28 + strength / 250)
    radii_squared = (max_radius * np.sqrt(np.clip(darkness, 0.0, 1.0))) ** 2
    axis = np.arange(cell, dtype=np.float32) + 0.5 - cell / 2
    distance_squared = axis[:, None] ** 2 + axis[None, :] ** 2
    dots = distance_squared[None, None, :, :] <= radii_squared[:, :, None, None]
    pattern = np.where(dots, 8, 246).astype(np.uint8)
    pattern = pattern.transpose(0, 2, 1, 3).reshape(rows * cell, cols * cell)[:rgba.height, :rgba.width]
    out = Image.fromarray(pattern, "L").convert("RGBA")
    return _with_original_alpha(rgba, out)

def glitch_blocks(image: Image.Image, size: int = 14, strength: int = 70) -> Image.Image:
    rgba = _rgba(image)
    size = max(4, int(size))
    strength = max(1, min(100, int(strength)))
    source = np.asarray(rgba.convert("RGB"))
    out = source.copy()
    rng = np.random.default_rng(_seed(rgba.width, rgba.height, size, strength, 79))
    bands = max(4, round(4 + strength / 5))
    max_shift = max(2, round(size * (0.5 + strength / 35)))
    for _ in range(bands):
        band_h = int(rng.integers(max(2, size // 3), max(3, size * 2)))
        top = int(rng.integers(0, max(1, rgba.height - band_h + 1)))
        shift = int(rng.integers(-max_shift, max_shift + 1))
        band = source[top:top + band_h]
        if shift > 0:
            out[top:top + band_h, shift:] = band[:, :-shift]
            out[top:top + band_h, :shift] = band[:, :1]
        elif shift < 0:
            amount = -shift
            out[top:top + band_h, :-amount] = band[:, amount:]
            out[top:top + band_h, -amount:] = band[:, -1:]
    channel_shift = max(1, round(max_shift / 3))
    out[:, :, 0] = _shift_plane_no_wrap(out[:, :, 0], channel_shift)
    out[:, :, 2] = _shift_plane_no_wrap(out[:, :, 2], -channel_shift)
    return _with_original_alpha(rgba, Image.fromarray(out, "RGB"))


def crt_distortion(image: Image.Image, size: int = 8, strength: int = 65) -> Image.Image:
    rgba = _rgba(image)
    size = max(3, int(size))
    strength = max(1, min(100, int(strength)))
    rgb = np.asarray(rgba.convert("RGB"), dtype=np.int16)
    shift = max(1, round(size * strength / 120))
    red = _shift_plane_no_wrap(rgb[:, :, 0], shift)
    green = rgb[:, :, 1]
    blue = _shift_plane_no_wrap(rgb[:, :, 2], -shift)
    out = np.stack((red, green, blue), axis=2)
    row_factor = np.ones((rgba.height, 1, 1), dtype=np.float32)
    thickness = max(1, size // 3)
    for y in range(0, rgba.height, size):
        row_factor[y:y + thickness] = max(0.25, 1.0 - strength / 125)
    out = out.astype(np.float32) * row_factor
    rng = np.random.default_rng(_seed(rgba.width, rgba.height, size, strength, 83))
    noise = rng.normal(0.0, strength / 18, out.shape[:2] + (1,))
    out = np.clip(out + noise, 0, 255).astype(np.uint8)
    return _with_original_alpha(rgba, Image.fromarray(out, "RGB"))


def silhouette_censor(image: Image.Image, strength: int = 70, *, scale: float = 1.0) -> Image.Image:
    rgba = _rgba(image)
    strength = max(1, min(100, int(strength)))
    gray = ImageOps.grayscale(rgba).filter(ImageFilter.GaussianBlur(max(0.5, strength / 18 * max(0.001, scale))))
    gray = ImageOps.autocontrast(gray, cutoff=1)
    threshold = max(48, min(210, 150 - round((strength - 50) * 0.55)))
    mask = gray.point(lambda value: 238 if value >= threshold else 14)
    out = Image.merge("RGBA", (mask, mask, mask, rgba.getchannel("A")))
    return out


def comic_cutout(image: Image.Image, strength: int = 65) -> Image.Image:
    rgba = _rgba(image)
    strength = max(1, min(100, int(strength)))
    rgb = rgba.convert("RGB")
    bits = max(2, 6 - strength // 22)
    colors = ImageOps.posterize(ImageEnhance.Color(rgb).enhance(1.15), bits)
    edges = ImageOps.grayscale(rgb).filter(ImageFilter.FIND_EDGES)
    edges = ImageOps.autocontrast(edges, cutoff=2).point(lambda value: 0 if value > 45 + strength else 255)
    outlined = ImageChops.multiply(colors, Image.merge("RGB", (edges, edges, edges)))
    return _with_original_alpha(rgba, outlined)


def thermal_map(image: Image.Image, strength: int = 70) -> Image.Image:
    rgba = _rgba(image)
    gray = np.asarray(ImageOps.grayscale(rgba), dtype=np.float32) / 255.0
    strength = max(1, min(100, int(strength)))
    contrast = 0.75 + strength / 70
    gray = np.clip((gray - 0.5) * contrast + 0.5, 0.0, 1.0)
    stops = np.array([0.0, 0.2, 0.42, 0.65, 0.82, 1.0])
    red = np.interp(gray, stops, [0, 0, 20, 240, 255, 255])
    green = np.interp(gray, stops, [0, 40, 220, 255, 80, 255])
    blue = np.interp(gray, stops, [20, 210, 255, 30, 0, 255])
    rgb = np.stack((red, green, blue), axis=2).astype(np.uint8)
    return _with_original_alpha(rgba, Image.fromarray(rgb, "RGB"))


@lru_cache(maxsize=24)
def _ascii_font(cell: int) -> ImageFont.ImageFont:
    size = max(6, round(cell * 0.95))
    try:
        return ImageFont.truetype("DejaVuSansMono.ttf", size)
    except OSError:
        return ImageFont.load_default()


_ASCII_PRINTABLE = "".join(chr(code) for code in range(32, 127))
_ASCII_CONTOURS = "|-/\\"


@lru_cache(maxsize=48)
def _ascii_glyph_masks(cell: int, characters: str) -> np.ndarray:
    """Return centered monochrome glyph tiles for one ASCII cell size.

    Rendering every glyph through FreeType for every animation frame dominated
    ASCII processing time.  The shapes depend only on the cell size and
    character set, so cache them once and recolor/composite them in bulk.
    """
    font = _ascii_font(cell)
    masks: list[np.ndarray] = []
    for char in characters:
        tile = Image.new("L", (cell, cell), 0)
        draw = ImageDraw.Draw(tile)
        bbox = draw.textbbox((0, 0), char, font=font)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        position = (
            (cell - width) / 2 - bbox[0],
            (cell - height) / 2 - bbox[1],
        )
        draw.text(position, char, fill=255, font=font)
        masks.append(np.asarray(tile, dtype=np.uint8).copy())
        tile.close()
    result = np.stack(masks, axis=0)
    result.setflags(write=False)
    return result


@lru_cache(maxsize=256)
def _ascii_palette_data(cell: int, density: int) -> tuple[str, np.ndarray]:
    """Return a font-calibrated character ramp and 8-bit luminance LUT.

    Fixed ramps are only approximate for a particular font. Measure every
    printable ASCII glyph at the active cell size, sort by actual ink coverage,
    then sample the complete light-to-dark span. This keeps low-density ramps
    simple without losing solid shadows and lets high density expose much finer
    tonal steps.
    """
    density = max(0, min(100, int(density)))
    masks = _ascii_glyph_masks(cell, _ASCII_PRINTABLE)
    coverage = masks.reshape(len(_ASCII_PRINTABLE), -1).mean(axis=1, dtype=np.float64)
    ordered = np.argsort(coverage, kind="stable")

    max_levels = len(ordered)
    levels = max(6, min(max_levels, 6 + round((max_levels - 6) * density / 100)))
    if levels >= max_levels:
        selected = ordered
    else:
        ordered_coverage = coverage[ordered]
        target_coverage = np.linspace(ordered_coverage[0], ordered_coverage[-1], levels)
        selected_positions = np.abs(
            ordered_coverage[:, None] - target_coverage[None, :]
        ).argmin(axis=0)
        selected_positions = np.unique(selected_positions)
        selected = ordered[selected_positions]
    selected_coverage = coverage[selected]

    minimum = float(selected_coverage[0])
    span = max(1e-6, float(selected_coverage[-1]) - minimum)
    darkness = np.clip((selected_coverage - minimum) * (255.0 / span), 0.0, 255.0)
    targets = np.arange(256, dtype=np.float64)
    nearest = np.abs(targets[:, None] - darkness[None, :]).argmin(axis=1)
    glyph_lut = selected[nearest].astype(np.intp, copy=False)
    glyph_lut.setflags(write=False)
    palette = "".join(_ASCII_PRINTABLE[index] for index in selected)
    return palette, glyph_lut


def _ascii_quantize_colors(colors: np.ndarray, preservation: int) -> np.ndarray:
    """Use the color slider to control RGB precision and temporal stability."""
    preservation = max(0, min(100, int(preservation)))
    channel_bits = min(8, 4 + preservation // 25)
    values = colors.astype(np.float32, copy=False)
    if channel_bits >= 8:
        return values
    levels = (1 << channel_bits) - 1
    return np.rint(values * (levels / 255.0)) * (255.0 / levels)

def _ascii_contour_char(gx: float, gy: float) -> str:
    ax, ay = abs(gx), abs(gy)
    if ax > ay * 1.8:
        return "|"
    if ay > ax * 1.8:
        return "-"
    return "/" if gx * gy >= 0 else "\\"


def _ascii_sobel(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    padded = np.pad(values.astype(np.float32, copy=False), 1, mode="edge")
    gx = (
        -padded[:-2, :-2] + padded[:-2, 2:]
        - 2 * padded[1:-1, :-2] + 2 * padded[1:-1, 2:]
        - padded[2:, :-2] + padded[2:, 2:]
    )
    gy = (
        -padded[:-2, :-2] - 2 * padded[:-2, 1:-1] - padded[:-2, 2:]
        + padded[2:, :-2] + 2 * padded[2:, 1:-1] + padded[2:, 2:]
    )
    return gx, gy


def _ascii_edge_score(gx: np.ndarray, gy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    magnitude = np.hypot(gx, gy)
    scale = float(np.percentile(magnitude, 92)) if magnitude.size else 1.0
    if scale <= 0:
        scale = 1.0
    relative = np.clip(magnitude / scale, 0.0, 1.0)
    # A percentile score alone classifies a smooth full-frame gradient as an
    # edge everywhere. Keep gradual tonal/color transitions in the glyph ramp
    # and reserve contour characters for sufficiently abrupt changes.
    absolute = np.clip((magnitude - 180.0) / 580.0, 0.0, 1.0)
    return relative * absolute, magnitude


def ascii_art_tuned(
    image: Image.Image,
    amount: int = 100,
    cell: int = 10,
    contrast: int = 60,
    density: int = 60,
    color_amount: int = 25,
    contour_strength: int = 72,
    tone_polarity: int = 0,
) -> Image.Image:
    """Render color-aware ASCII with luminance glyphs and chromatic contours."""
    rgba = _rgba(image)
    amount = max(0, min(100, int(amount)))
    cell = max(5, int(cell))
    contrast = max(0, min(100, int(contrast)))
    density = max(0, min(100, int(density)))
    color_amount = max(0, min(100, int(color_amount)))
    contour_strength = max(0, min(100, int(contour_strength)))
    tone_polarity = max(0, min(100, int(tone_polarity)))

    # Keep pathological 4K/8K + tiny-cell jobs responsive without altering
    # ordinary settings. The chosen cell only grows when the user requests
    # hundreds of thousands of individually rendered glyphs.
    max_glyphs = 220_000
    requested_glyphs = math.ceil(rgba.width / cell) * math.ceil(rgba.height / cell)
    if requested_glyphs > max_glyphs:
        cell = max(cell, math.ceil(math.sqrt((rgba.width * rgba.height) / max_glyphs)))

    charset, glyph_lut = _ascii_palette_data(cell, density)
    cols = max(1, math.ceil(rgba.width / cell))
    rows = max(1, math.ceil(rgba.height / cell))

    gray = ImageOps.grayscale(rgba)
    gray = ImageEnhance.Contrast(gray).enhance(0.75 + contrast / 45)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray_small = gray.resize((cols, rows), Image.Resampling.BOX)

    rgb = rgba.convert("RGB")
    averaged_color_small = rgb.resize((cols, rows), Image.Resampling.BOX)
    centered_color_small = rgb.resize((cols, rows), Image.Resampling.NEAREST)
    averaged_colors = np.asarray(averaged_color_small, dtype=np.float32)
    centered_colors = np.asarray(centered_color_small, dtype=np.float32)

    # BOX averaging is stable for backgrounds but can turn two adjacent hues
    # into a muddy intermediate color. Bias glyph ink toward the cell center,
    # increasingly so when the user asks for stronger color preservation.
    center_weight = 0.42 + 0.43 * (color_amount / 100)
    colors = averaged_colors * (1.0 - center_weight) + centered_colors * center_weight
    colors = _ascii_quantize_colors(colors, color_amount)
    background_colors = _ascii_quantize_colors(averaged_colors, color_amount)
    lum = np.asarray(gray_small, dtype=np.float32)

    # Luminance-only contours miss boundaries between colors with similar
    # brightness. Add two opponent-color channels and choose the strongest
    # local gradient for both edge strength and contour orientation.
    luma_gx, luma_gy = _ascii_sobel(lum)
    luma_score, luma_magnitude = _ascii_edge_score(luma_gx, luma_gy)
    red_green = averaged_colors[:, :, 0] - averaged_colors[:, :, 1]
    yellow_blue = (averaged_colors[:, :, 0] + averaged_colors[:, :, 1]) * 0.5 - averaged_colors[:, :, 2]
    rg_gx, rg_gy = _ascii_sobel(red_green)
    yb_gx, yb_gy = _ascii_sobel(yellow_blue)
    rg_score, rg_magnitude = _ascii_edge_score(rg_gx, rg_gy)
    yb_score, yb_magnitude = _ascii_edge_score(yb_gx, yb_gy)

    chroma_weight = 0.58 + 0.42 * (color_amount / 100)
    edge_score = np.maximum(luma_score, np.maximum(rg_score, yb_score) * chroma_weight)
    contour_gx = luma_gx.copy()
    contour_gy = luma_gy.copy()
    strongest_magnitude = luma_magnitude.copy()
    use_rg = rg_magnitude > strongest_magnitude
    contour_gx[use_rg] = rg_gx[use_rg]
    contour_gy[use_rg] = rg_gy[use_rg]
    strongest_magnitude[use_rg] = rg_magnitude[use_rg]
    use_yb = yb_magnitude > strongest_magnitude
    contour_gx[use_yb] = yb_gx[use_yb]
    contour_gy[use_yb] = yb_gy[use_yb]

    contour_threshold = 1.01 if contour_strength <= 0 else 0.92 - (contour_strength / 100) * 0.72

    # Keep the terminal-like dark base while retaining enough block color to
    # separate neighboring hues instead of reducing them to the same gray.
    quantized_background = Image.fromarray(np.clip(background_colors, 0, 255).astype(np.uint8), "RGB")
    source_blocks = quantized_background.resize(rgba.size, Image.Resampling.BILINEAR)
    quantized_background.close()
    grayscale_blocks = ImageOps.grayscale(source_blocks).convert("RGB")
    color_strength = color_amount / 100
    source_blocks = Image.blend(grayscale_blocks, source_blocks, color_strength)
    source_blocks = ImageEnhance.Color(source_blocks).enhance(1.0 + 0.55 * color_strength)
    source_blocks = ImageEnhance.Brightness(source_blocks).enhance(0.24 + 0.12 * color_strength)
    background_mix = 0.15 + 0.42 * color_strength
    canvas = Image.blend(Image.new("RGB", rgba.size, (10, 11, 14)), source_blocks, background_mix)
    # Preserve hue independently of glyph brightness. Linear interpolation
    # with gray suppressed color differences too aggressively, especially at
    # the former default of 30 percent.
    sample_luma = (
        colors[:, :, 0] * 0.2126
        + colors[:, :, 1] * 0.7152
        + colors[:, :, 2] * 0.0722
    )
    chroma = colors - sample_luma[:, :, None]
    chroma_gain = 1.25 * color_strength

    # Build the full glyph layer in NumPy instead of invoking FreeType once per
    # cell.  This matters especially for video, where the same font and cell
    # geometry are reused hundreds or thousands of times.
    #
    # Tone polarity slides the glyph-density preference between the classic
    # "darker areas use denser characters" mapping and the inverse
    # "lighter areas use denser characters" mapping.  The midpoint keeps useful
    # contrast by emphasizing both highlight and shadow extremes instead of
    # collapsing to one flat density.
    lum_norm = lum / 255.0
    dark_dense = 1.0 - lum_norm
    light_dense = lum_norm
    extremes_dense = np.clip(np.abs(lum_norm - 0.5) * 2.0, 0.0, 1.0)
    if tone_polarity <= 50:
        blend = tone_polarity / 50.0
        density_map = dark_dense * (1.0 - blend) + extremes_dense * blend
    else:
        blend = (tone_polarity - 50) / 50.0
        density_map = extremes_dense * (1.0 - blend) + light_dense * blend
    target_darkness = np.clip(np.rint(density_map * 255.0), 0, 255).astype(np.uint8)
    glyph_indices = glyph_lut[target_darkness].copy()
    contour_cells = edge_score >= contour_threshold
    if np.any(contour_cells):
        ax = np.abs(contour_gx)
        ay = np.abs(contour_gy)
        contour_indices = np.full((rows, cols), _ASCII_PRINTABLE.index("\\"), dtype=np.intp)
        contour_indices[contour_gx * contour_gy >= 0] = _ASCII_PRINTABLE.index("/")
        contour_indices[ay > ax * 1.8] = _ASCII_PRINTABLE.index("-")
        contour_indices[ax > ay * 1.8] = _ASCII_PRINTABLE.index("|")
        glyph_indices[contour_cells] = contour_indices[contour_cells]

    mono = np.clip(np.rint(238.0 - lum * 0.56), 72.0, 245.0)
    mono[contour_cells] = 242.0
    ink_colors = np.clip(mono[:, :, None] + chroma * chroma_gain, 0, 255).astype(np.uint8)

    glyph_tiles = _ascii_glyph_masks(cell, _ASCII_PRINTABLE)[glyph_indices]
    glyph_mask_array = glyph_tiles.transpose(0, 2, 1, 3).reshape(rows * cell, cols * cell)
    glyph_mask = Image.fromarray(glyph_mask_array, "L")
    ink_blocks = Image.fromarray(ink_colors, "RGB").resize(
        (cols * cell, rows * cell), Image.Resampling.NEAREST
    )
    if glyph_mask.size != rgba.size:
        glyph_mask = glyph_mask.crop((0, 0, rgba.width, rgba.height))
        ink_blocks = ink_blocks.crop((0, 0, rgba.width, rgba.height))
    canvas.paste(ink_blocks, (0, 0), glyph_mask)
    glyph_mask.close()
    ink_blocks.close()

    out = canvas.convert("RGBA")
    return mix_effect(rgba, out, amount)

def blueprint_tuned(
    image: Image.Image,
    amount: int = 100,
    spacing: int = 20,
    line_softness: int = 14,
    contrast: int = 70,
    grid_opacity: int = 28,
) -> Image.Image:
    rgba = _rgba(image)
    spacing = max(8, int(spacing))
    line_softness = max(0, min(100, int(line_softness)))
    contrast = max(0, min(100, int(contrast)))
    grid_opacity = max(0, min(100, int(grid_opacity)))
    gray = ImageOps.grayscale(rgba)
    smooth = gray.filter(ImageFilter.GaussianBlur(max(0.0, line_softness / 18)))
    edges = ImageOps.autocontrast(smooth.filter(ImageFilter.FIND_EDGES), cutoff=1)
    edges = ImageEnhance.Contrast(edges).enhance(1.1 + contrast / 45)
    edges = edges.point(lambda value: max(0, min(255, round(value * 1.2))))
    base = Image.new("RGBA", rgba.size, (16, 48, 110, 255))
    grid = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(grid)
    alpha = round(grid_opacity * 1.6)
    half = max(6, spacing // 2)
    for x in range(0, rgba.width, half):
        draw.line((x, 0, x, rgba.height), fill=(180, 210, 255, alpha if x % spacing else min(255, alpha + 28)), width=1)
    for y in range(0, rgba.height, half):
        draw.line((0, y, rgba.width, y), fill=(180, 210, 255, alpha if y % spacing else min(255, alpha + 28)), width=1)
    edge_colored = ImageOps.colorize(edges, black=(22, 72, 155), white=(235, 248, 255)).convert("RGBA")
    edge_colored.putalpha(edges)
    out = Image.alpha_composite(base, grid)
    out = Image.alpha_composite(out, edge_colored)
    return mix_effect(rgba, out, amount)


def neon_edges_tuned(
    image: Image.Image,
    amount: int = 100,
    blur_radius: int = 8,
    edge_strength: int = 72,
    glow: int = 60,
    hue_shift: int = 0,
) -> Image.Image:
    rgba = _rgba(image)
    blur_radius = max(0, int(blur_radius))
    edge_strength = max(0, min(100, int(edge_strength)))
    glow = max(0, min(100, int(glow)))
    hue = ((int(hue_shift) % 360) / 360.0)
    edge_color = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, 0.9, 1.0))
    halo_color = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb((hue + 0.07) % 1.0, 0.65, 1.0))
    gray = ImageOps.grayscale(rgba)
    if blur_radius:
        gray = gray.filter(ImageFilter.GaussianBlur(max(0.25, blur_radius / 6)))
    edges = ImageOps.autocontrast(gray.filter(ImageFilter.FIND_EDGES), cutoff=1)
    edges = ImageEnhance.Contrast(edges).enhance(1.0 + edge_strength / 35)
    core = ImageOps.colorize(edges, black=(0, 0, 0), white=edge_color).convert("RGBA")
    core.putalpha(edges)
    glow_mask = edges.filter(ImageFilter.GaussianBlur(max(0.4, 1 + glow / 18)))
    glow_mask = glow_mask.point(lambda value: round(value * glow / 100))
    glow_layer = ImageOps.colorize(glow_mask, black=(0, 0, 0), white=halo_color).convert("RGBA")
    glow_layer.putalpha(glow_mask)
    out = Image.alpha_composite(Image.new("RGBA", rgba.size, (8, 10, 14, 255)), glow_layer)
    out = Image.alpha_composite(out, core)
    return mix_effect(rgba, out, amount)


def topographic_lines_tuned(
    image: Image.Image,
    amount: int = 100,
    step: int = 16,
    smoothing: int = 10,
    contrast: int = 68,
    line_width: int = 2,
) -> Image.Image:
    rgba = _rgba(image)
    step = max(6, int(step))
    smoothing = max(0, min(100, int(smoothing)))
    contrast = max(0, min(100, int(contrast)))
    line_width = max(1, int(line_width))
    gray = ImageOps.grayscale(rgba)
    if smoothing:
        gray = gray.filter(ImageFilter.GaussianBlur(max(0.0, smoothing / 14)))
    gray = ImageEnhance.Contrast(gray).enhance(0.9 + contrast / 50)
    values = np.asarray(gray, dtype=np.uint8)
    residue = np.mod(values, step)
    threshold = max(1, min(step - 1, line_width))
    mask = (residue < threshold).astype(np.uint8) * 255
    mask_image = Image.fromarray(mask, "L")
    bg = Image.new("RGBA", rgba.size, (244, 241, 233, 255))
    lines = ImageOps.colorize(mask_image, black=(0, 0, 0), white=(82, 72, 58)).convert("RGBA")
    out = Image.alpha_composite(bg, lines)
    return mix_effect(rgba, out, amount)


def compose_transforms(transforms: Iterable[Callable[[Image.Image], Image.Image]]) -> Callable[[Image.Image], Image.Image]:
    chain = list(transforms)
    if not chain:
        return lambda im: im.copy()

    def apply(image: Image.Image) -> Image.Image:
        result = image
        owns_result = False
        try:
            for transform in chain:
                transformed = transform(result)
                if transformed is None:
                    raise ValueError("An effect transform returned no image.")
                if transformed is result:
                    continue
                if owns_result:
                    result.close()
                result = transformed
                owns_result = True
            return result if owns_result else image.copy()
        except Exception:
            if owns_result:
                result.close()
            raise

    return apply


def _transformed_rgba_copy(
    image: Image.Image,
    transform: Callable[[Image.Image], Image.Image],
) -> Image.Image:
    """Run a transform without transferring ownership of the caller's image."""
    source = image.copy()
    result: Image.Image | None = None
    try:
        result = transform(source)
        if result is None:
            raise ValueError("An effect transform returned no image.")
        if result is source:
            if source.mode == "RGBA":
                return source
            converted = source.convert("RGBA")
            source.close()
            return converted
        source.close()
        if result.mode == "RGBA":
            return result
        converted = result.convert("RGBA")
        result.close()
        return converted
    except Exception:
        if result is not None and result is not source:
            result.close()
        source.close()
        raise

def _soft_mask(mask: Image.Image, feather: int) -> Image.Image:
    feather = max(0, int(feather))
    if feather <= 0:
        return mask
    softened = mask.filter(ImageFilter.GaussianBlur(max(0.5, feather / 2.5)))
    # Gaussian blur has a tiny infinite tail. Clamp imperceptible values so
    # pixels well outside the transition remain byte-for-byte unchanged.
    try:
        return softened.point(lambda value: 0 if value <= 8 else 255 if value >= 247 else value)
    finally:
        softened.close()


def _expand_mask(mask: Image.Image, padding: int) -> Image.Image:
    padding = max(0, int(padding))
    if padding <= 0:
        return mask
    if padding <= 12:
        return mask.filter(ImageFilter.MaxFilter(padding * 2 + 1))
    # Very large MaxFilter kernels become disproportionately expensive. A
    # thresholded Gaussian gives a fast, stable approximation for wide lasso
    # expansion while the feather pass below still produces the final edge.
    expanded = mask.filter(ImageFilter.GaussianBlur(max(0.5, padding / 2.2)))
    try:
        return expanded.point(lambda value: 255 if value >= 4 else 0)
    finally:
        expanded.close()


def target_mask(
    size: tuple[int, int],
    selection: RectMask | None,
    lasso: list[tuple[int, int]],
    feather: int = 0,
    padding: int = 0,
) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    if lasso:
        if len(lasso) >= 3:
            draw.polygon(lasso, fill=255)
            expanded = _expand_mask(mask, padding)
            if expanded is not mask:
                mask.close()
                mask = expanded
        softened = _soft_mask(mask, feather)
        if softened is not mask:
            mask.close()
        return softened
    if selection is not None:
        if selection.width > 0 and selection.height > 0:
            left = max(0, selection.left - padding)
            top = max(0, selection.top - padding)
            right = min(size[0], selection.right + padding)
            bottom = min(size[1], selection.bottom + padding)
            draw.rectangle((left, top, right, bottom), fill=255)
        softened = _soft_mask(mask, feather)
        if softened is not mask:
            mask.close()
        return softened
    mask.close()
    return Image.new("L", size, 255)


def focus_mask(
    size: tuple[int, int],
    faces: list[RectMask],
    feather: int = 0,
    padding: int = 0,
) -> Image.Image:
    protected = Image.new("L", size, 0)
    draw = ImageDraw.Draw(protected)
    for face in faces:
        box = (
            max(0, face.left - padding),
            max(0, face.top - padding),
            min(size[0], face.right + padding),
            min(size[1], face.bottom + padding),
        )
        draw.ellipse(box, fill=255)
    softened = _soft_mask(protected, feather)
    if softened is not protected:
        protected.close()
        protected = softened
    try:
        return ImageOps.invert(protected)
    finally:
        protected.close()


def apply_to_target(
    image: Image.Image,
    transform: Callable[[Image.Image], Image.Image],
    selection: RectMask | None,
    lasso: list[tuple[int, int]],
    feather: int = 0,
    padding: int = 0,
) -> Image.Image:
    processed = _transformed_rgba_copy(image, transform)
    base = image.copy() if image.mode == "RGBA" else image.convert("RGBA")
    mask = target_mask(image.size, selection, lasso, feather=feather, padding=padding)
    try:
        if processed.size != image.size:
            resized = processed.resize(image.size, Image.Resampling.LANCZOS)
            processed.close()
            processed = resized
        return Image.composite(processed, base, mask)
    finally:
        processed.close()
        base.close()
        mask.close()


def apply_outside_faces(
    image: Image.Image,
    transform: Callable[[Image.Image], Image.Image],
    faces: list[RectMask],
    feather: int = 0,
    padding: int = 0,
) -> Image.Image:
    processed = _transformed_rgba_copy(image, transform)
    base = image.copy() if image.mode == "RGBA" else image.convert("RGBA")
    mask = focus_mask(image.size, faces, feather=feather, padding=padding)
    try:
        if processed.size != image.size:
            resized = processed.resize(image.size, Image.Resampling.LANCZOS)
            processed.close()
            processed = resized
        return Image.composite(processed, base, mask)
    finally:
        processed.close()
        base.close()
        mask.close()

def adjustments(image: Image.Image, brightness: int = 0, contrast: int = 0, saturation: int = 0, sharpness: int = 0) -> Image.Image:
    result = image.convert("RGBA")
    if brightness:
        result = ImageEnhance.Brightness(result).enhance(max(0.0, 1 + brightness / 100))
    if contrast:
        result = ImageEnhance.Contrast(result).enhance(max(0.0, 1 + contrast / 100))
    if saturation:
        result = ImageEnhance.Color(result).enhance(max(0.0, 1 + saturation / 100))
    if sharpness:
        result = ImageEnhance.Sharpness(result).enhance(max(0.0, 1 + sharpness / 100))
    return result


def auto_enhance(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    rgb = ImageOps.autocontrast(rgba.convert("RGB"), cutoff=1)
    rgb = ImageEnhance.Color(rgb).enhance(1.04)
    rgb = ImageEnhance.Sharpness(rgb).enhance(1.12)
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def vignette(image: Image.Image, strength: int) -> Image.Image:
    rgba = image.convert("RGBA")
    w, h = rgba.size
    x = Image.linear_gradient("L").resize((w, h)).rotate(90)
    y = Image.linear_gradient("L").resize((w, h))
    x = ImageChops.multiply(x, ImageOps.invert(x))
    y = ImageChops.multiply(y, ImageOps.invert(y))
    mask = ImageChops.multiply(x, y)
    mask = ImageOps.autocontrast(mask)
    mask = ImageEnhance.Contrast(mask).enhance(1.5)
    mask = ImageOps.invert(mask)
    mask = mask.point(lambda p: int(p * max(0.0, min(1.0, strength / 100))))
    dark = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    out = Image.composite(dark, rgba, mask)
    out.putalpha(rgba.getchannel("A"))
    return out


def glow(image: Image.Image, strength: int, *, scale: float = 1.0) -> Image.Image:
    rgba = image.convert("RGBA")
    blur = rgba.filter(ImageFilter.GaussianBlur(max(0.25, strength / 8 * max(0.001, scale))))
    result = Image.blend(rgba, blur, min(0.75, strength / 115))
    result = ImageEnhance.Brightness(result).enhance(1 + strength / 170)
    result.putalpha(rgba.getchannel("A"))
    return result


def sketch(image: Image.Image, strength: int, *, scale: float = 1.0) -> Image.Image:
    gray = ImageOps.grayscale(image)
    inverted = ImageOps.invert(gray)
    blurred = inverted.filter(ImageFilter.GaussianBlur(radius=max(0.25, strength / 12 * max(0.001, scale))))
    # Pillow has no ImageChops.divide operation. Compute the classic color-dodge
    # blend in ImageMath, adding one to the denominator to avoid division by zero.
    dodge = ImageMath.unsafe_eval("(gray * 255) / (255 - blur + 1)", gray=gray, blur=blurred).convert("L")
    dodge = ImageEnhance.Contrast(dodge).enhance(1 + strength / 70)
    alpha = image.convert("RGBA").getchannel("A")
    out = dodge.convert("RGBA")
    out.putalpha(alpha)
    return out


def cinematic(image: Image.Image, strength: int = 35) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    out = adjustments(rgba, brightness=3, contrast=12 + strength // 5, saturation=-5, sharpness=8)
    overlay = Image.new("RGBA", out.size, (18, 28, 52, min(70, strength)))
    out = Image.alpha_composite(out.convert("RGBA"), overlay)
    out.putalpha(alpha)
    return vignette(out, min(55, strength))


def rotate_keep(image: Image.Image, degrees: float) -> Image.Image:
    return image.rotate(-degrees, expand=True, resample=Image.Resampling.BICUBIC)

# Tunable censor effects used by the dynamic parameter panel.  The older helpers
# above remain as small compatibility building blocks for saved presets/tests.

def mix_effect(source: Image.Image, processed: Image.Image, amount: int) -> Image.Image:
    """Blend two owned effect temporaries and release superseded buffers."""
    rgba = source if source.mode == "RGBA" else source.convert("RGBA")
    if rgba is not source:
        source.close()
    amount = max(0, min(100, int(amount)))
    if amount <= 0:
        if processed is not rgba:
            processed.close()
        return rgba
    result = processed if processed.mode == "RGBA" else processed.convert("RGBA")
    if result is not processed:
        processed.close()
    if result.size != rgba.size:
        resized = result.resize(rgba.size, Image.Resampling.LANCZOS)
        result.close()
        result = resized
    if amount < 100:
        blended = Image.blend(rgba, result, amount / 100.0)
        result.close()
        result = blended
    return _with_original_alpha(rgba, result)


def _grain_rgb(size: tuple[int, int], strength: float, seed: int, *, colored: int = 0, scale: int = 1) -> Image.Image:
    width, height = size
    scale = max(1, int(scale))
    small_size = (max(1, math.ceil(width / scale)), max(1, math.ceil(height / scale)))
    rng = np.random.default_rng(seed)
    channels = 3 if colored > 0 else 1
    noise = rng.normal(127.5, max(1.0, float(strength)), (small_size[1], small_size[0], channels))
    noise = np.clip(noise, 0, 255).astype(np.uint8)
    if channels == 1:
        noise = np.repeat(noise, 3, axis=2)
    elif colored < 100:
        mono = np.mean(noise, axis=2, keepdims=True).astype(np.uint16)
        color_noise = noise.astype(np.uint16)
        noise = ((mono * (100 - colored) + color_noise * colored) // 100).astype(np.uint8)
    image = Image.fromarray(noise, "RGB")
    return image.resize(size, Image.Resampling.NEAREST if scale > 1 else Image.Resampling.BILINEAR)


def privacy_blur(
    image: Image.Image,
    radius: float = 18,
    destruction: int = 65,
    grain: int = 0,
    amount: int = 100,
    *,
    scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    destruction = max(0, min(100, int(destruction)))
    factor = max(1, round(1 + destruction / 10))
    small = rgba.resize(
        (max(1, math.ceil(rgba.width / factor)), max(1, math.ceil(rgba.height / factor))),
        Image.Resampling.BOX,
    )
    result = small.resize(rgba.size, Image.Resampling.BILINEAR)
    result = result.filter(ImageFilter.GaussianBlur(max(0.25, float(radius) * max(0.001, scale))))
    if grain:
        texture = _grain_rgb(rgba.size, 4 + grain / 5, _seed(rgba.width, rgba.height, radius, destruction, grain, 101))
        result = Image.blend(result.convert("RGB"), texture, min(0.28, grain / 400)).convert("RGBA")
    return mix_effect(rgba, result, amount)


def directional_blur(
    image: Image.Image,
    length: int = 22,
    samples: int = 9,
    angle: int = 0,
    amount: int = 100,
    grain: int = 0,
    *,
    scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    rgb = np.asarray(rgba.convert("RGB"), dtype=np.float32)
    height, width = rgb.shape[:2]
    length = max(1, round(int(length) * max(0.001, scale)))
    samples = max(2, min(21, int(samples)))
    radians = math.radians(angle)
    offsets = np.linspace(-length / 2, length / 2, samples)
    y_base = np.arange(height)[:, None]
    x_base = np.arange(width)[None, :]
    total = np.zeros_like(rgb, dtype=np.float32)
    for offset in offsets:
        dx = round(math.cos(radians) * offset)
        dy = round(math.sin(radians) * offset)
        ys = np.clip(y_base - dy, 0, height - 1)
        xs = np.clip(x_base - dx, 0, width - 1)
        total += rgb[ys, xs]
    out = np.clip(total / len(offsets), 0, 255).astype(np.uint8)
    result = Image.fromarray(out, "RGB")
    if grain:
        texture = _grain_rgb(rgba.size, 2 + grain / 7, _seed(width, height, length, samples, angle, grain, 103))
        result = Image.blend(result, texture, min(0.18, grain / 550))
    return mix_effect(rgba, result, amount)


def pixelate_tuned(
    image: Image.Image,
    block: int = 18,
    softness: int = 0,
    levels: int = 256,
    grid: int = 0,
    amount: int = 100,
) -> Image.Image:
    rgba = _rgba(image)
    block = max(2, int(block))
    columns = max(1, math.ceil(rgba.width / block))
    rows = max(1, math.ceil(rgba.height / block))
    small = rgba.convert("RGB").resize((columns, rows), Image.Resampling.BOX)
    levels = max(2, min(256, int(levels)))
    if levels < 256:
        step = 255 / (levels - 1)
        array = np.asarray(small, dtype=np.float32)
        array = np.clip(np.round(array / step) * step, 0, 255).astype(np.uint8)
        small = Image.fromarray(array, "RGB")
    nearest = small.resize(rgba.size, Image.Resampling.NEAREST)
    if softness:
        smooth = small.resize(rgba.size, Image.Resampling.BILINEAR)
        nearest = Image.blend(nearest, smooth, min(0.8, softness / 125))
    if grid:
        overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        alpha = min(150, round(grid * 1.5))
        for x in range(block, rgba.width, block):
            draw.line((x, 0, x, rgba.height), fill=(0, 0, 0, alpha), width=1)
        for y in range(block, rgba.height, block):
            draw.line((0, y, rgba.width, y), fill=(0, 0, 0, alpha), width=1)
        nearest = Image.alpha_composite(nearest.convert("RGBA"), overlay)
    return mix_effect(rgba, nearest, amount)


def mosaic_tuned(
    image: Image.Image,
    tile: int = 22,
    preblur: int = 15,
    grid_opacity: int = 45,
    grid_width: int = 1,
    amount: int = 100,
    *,
    scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    tile = max(3, round(int(tile) * max(0.001, scale)))
    source = rgba
    if preblur:
        source = rgba.filter(ImageFilter.GaussianBlur(max(0.2, preblur / 12 * max(0.001, scale))))
    columns = max(1, math.ceil(rgba.width / tile))
    rows = max(1, math.ceil(rgba.height / tile))
    result = source.resize((columns, rows), Image.Resampling.BOX).resize(rgba.size, Image.Resampling.NEAREST)
    if grid_opacity:
        overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        alpha = min(190, round(grid_opacity * 1.9))
        width = max(1, round(grid_width * max(0.001, scale)))
        for x in range(tile, rgba.width, tile):
            draw.line((x, 0, x, rgba.height), fill=(0, 0, 0, alpha), width=width)
        for y in range(tile, rgba.height, tile):
            draw.line((0, y, rgba.width, y), fill=(0, 0, 0, alpha), width=width)
        result = Image.alpha_composite(result.convert("RGBA"), overlay)
    return mix_effect(rgba, result, amount)


def frosted_glass_tuned(
    image: Image.Image,
    blur: int = 12,
    refraction: int = 35,
    grain: int = 25,
    scale_size: int = 10,
    amount: int = 100,
    *,
    preview_scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    rgb = np.asarray(rgba.convert("RGB"))
    height, width = rgb.shape[:2]
    cell = max(2, round(scale_size * max(0.001, preview_scale)))
    rng = np.random.default_rng(_seed(width, height, blur, refraction, grain, scale_size, 107))
    coarse_h = max(1, math.ceil(height / cell))
    coarse_w = max(1, math.ceil(width / cell))
    displacement_x = rng.normal(0, max(0.2, refraction / 18), (coarse_h, coarse_w)).astype(np.float32)
    displacement_y = rng.normal(0, max(0.2, refraction / 18), (coarse_h, coarse_w)).astype(np.float32)
    dx = np.asarray(Image.fromarray(displacement_x, "F").resize((width, height), Image.Resampling.BILINEAR))
    dy = np.asarray(Image.fromarray(displacement_y, "F").resize((width, height), Image.Resampling.BILINEAR))
    yy, xx = np.indices((height, width))
    sample_x = np.clip(np.rint(xx + dx).astype(np.int32), 0, width - 1)
    sample_y = np.clip(np.rint(yy + dy).astype(np.int32), 0, height - 1)
    displaced = Image.fromarray(rgb[sample_y, sample_x], "RGB")
    displaced = displaced.filter(ImageFilter.GaussianBlur(max(0.25, blur * max(0.001, preview_scale))))
    if grain:
        texture = _grain_rgb(rgba.size, 3 + grain / 4, _seed(width, height, grain, scale_size, 109), scale=max(1, cell // 3))
        displaced = Image.blend(displaced, texture, min(0.25, grain / 400))
    return mix_effect(rgba, displaced, amount)


def faceted_glass(
    image: Image.Image,
    cell: int = 28,
    irregularity: int = 45,
    edge_strength: int = 30,
    skew: int = 0,
    amount: int = 100,
    *,
    scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    cell = max(6, round(int(cell) * max(0.001, scale)))
    width, height = rgba.size
    source = np.asarray(rgba.convert("RGB"))
    rng = np.random.default_rng(_seed(width, height, cell, irregularity, edge_strength, skew, 113))
    points: list[list[tuple[int, int]]] = []
    jitter = round(cell * irregularity / 250)
    skew_offset = round(cell * skew / 100)
    for row, y in enumerate(range(0, height + cell, cell)):
        line = []
        for x in range(0, width + cell, cell):
            px = x + (skew_offset if row % 2 else 0)
            if 0 < px < width:
                px += int(rng.integers(-jitter, jitter + 1))
            py = y
            if 0 < py < height:
                py += int(rng.integers(-jitter, jitter + 1))
            line.append((max(0, min(width, px)), max(0, min(height, py))))
        points.append(line)
    result = Image.new("RGB", rgba.size)
    draw = ImageDraw.Draw(result)
    edge = (0, 0, 0) if edge_strength else None
    edge_width = max(1, round(edge_strength / 30)) if edge_strength else 0
    for row in range(len(points) - 1):
        for col in range(len(points[row]) - 1):
            p00, p10 = points[row][col], points[row][col + 1]
            p01, p11 = points[row + 1][col], points[row + 1][col + 1]
            triangles = ((p00, p10, p11), (p00, p11, p01)) if (row + col) % 2 == 0 else ((p00, p10, p01), (p10, p11, p01))
            for triangle in triangles:
                cx = max(0, min(width - 1, round(sum(point[0] for point in triangle) / 3)))
                cy = max(0, min(height - 1, round(sum(point[1] for point in triangle) / 3)))
                color = tuple(int(value) for value in source[cy, cx])
                draw.polygon(triangle, fill=color, outline=edge, width=edge_width)
    return mix_effect(rgba, result, amount)


def encrypted_tiles(
    image: Image.Image,
    tile: int = 24,
    scramble: int = 80,
    rotation: int = 50,
    color_shift: int = 25,
    amount: int = 100,
) -> Image.Image:
    rgba = _rgba(image)
    tile = max(4, int(tile))
    rgb = np.asarray(rgba.convert("RGB"))
    height, width = rgb.shape[:2]
    pad_h = (-height) % tile
    pad_w = (-width) % tile
    padded = np.pad(rgb, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
    rows, cols = padded.shape[0] // tile, padded.shape[1] // tile
    blocks = padded.reshape(rows, tile, cols, tile, 3).transpose(0, 2, 1, 3, 4).copy()
    flat = blocks.reshape(rows * cols, tile, tile, 3)
    rng = np.random.default_rng(_seed(width, height, tile, scramble, rotation, color_shift, 127))
    count = round(len(flat) * max(0, min(100, scramble)) / 100)
    if count:
        targets = rng.choice(len(flat), size=count, replace=False)
        sources = rng.permutation(len(flat))[:count]
        flat[targets] = flat[sources]
        for index in targets:
            if rng.integers(0, 100) < rotation:
                flat[index] = np.rot90(flat[index], int(rng.integers(1, 4)))
            if color_shift:
                shift = rng.integers(-color_shift, color_shift + 1, 3)
                flat[index] = np.clip(flat[index].astype(np.int16) + shift, 0, 255).astype(np.uint8)
    rebuilt = flat.reshape(rows, cols, tile, tile, 3).transpose(0, 2, 1, 3, 4).reshape(rows * tile, cols * tile, 3)
    return mix_effect(rgba, Image.fromarray(rebuilt[:height, :width], "RGB"), amount)


def _shift_rgb_no_wrap(array: np.ndarray, dx: int, dy: int) -> np.ndarray:
    height, width = array.shape[:2]
    ys = np.clip(np.arange(height) - int(dy), 0, height - 1)
    xs = np.clip(np.arange(width) - int(dx), 0, width - 1)
    return array[ys[:, None], xs[None, :]]


def prism_split(
    image: Image.Image,
    separation: int = 12,
    angle: int = 0,
    softness: int = 0,
    saturation: int = 25,
    amount: int = 100,
    *,
    scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    source = np.asarray(rgba.convert("RGB"))
    distance = max(1, round(separation * max(0.001, scale)))
    radians = math.radians(angle)
    dx = round(math.cos(radians) * distance)
    dy = round(math.sin(radians) * distance)
    red = _shift_rgb_no_wrap(source, dx, dy)[:, :, 0]
    green = source[:, :, 1]
    blue = _shift_rgb_no_wrap(source, -dx, -dy)[:, :, 2]
    result = Image.fromarray(np.stack((red, green, blue), axis=2), "RGB")
    if softness:
        result = result.filter(ImageFilter.GaussianBlur(max(0.2, softness / 18 * max(0.001, scale))))
    if saturation:
        result = ImageEnhance.Color(result).enhance(1 + saturation / 100)
    return mix_effect(rgba, result, amount)


def wave_scramble(
    image: Image.Image,
    wavelength: int = 40,
    amplitude: int = 16,
    complexity: int = 2,
    angle: int = 0,
    amount: int = 100,
    *,
    scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    source = np.asarray(rgba.convert("RGB"))
    height, width = source.shape[:2]
    wavelength = max(4, round(wavelength * max(0.001, scale)))
    amplitude = max(0, round(amplitude * max(0.001, scale)))
    complexity = max(1, min(6, int(complexity)))
    yy, xx = np.indices((height, width), dtype=np.float32)
    radians = math.radians(angle)
    along = xx * math.cos(radians) + yy * math.sin(radians)
    phase = yy * math.cos(radians) - xx * math.sin(radians)
    displacement = np.zeros_like(along)
    for harmonic in range(1, complexity + 1):
        displacement += np.sin(phase * 2 * math.pi * harmonic / wavelength) / harmonic
    displacement *= amplitude / max(1.0, sum(1 / harmonic for harmonic in range(1, complexity + 1)))
    sample_x = np.clip(np.rint(xx - displacement * math.cos(radians)).astype(np.int32), 0, width - 1)
    sample_y = np.clip(np.rint(yy - displacement * math.sin(radians)).astype(np.int32), 0, height - 1)
    return mix_effect(rgba, Image.fromarray(source[sample_y, sample_x], "RGB"), amount)


def solid_redaction(
    image: Image.Image,
    color: tuple[int, int, int],
    opacity: int = 100,
    texture_scale: int = 20,
    texture: int = 8,
    grain: int = 4,
    angle: int = 0,
    *,
    scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    height, width = rgba.height, rgba.width
    base = np.empty((height, width, 3), dtype=np.float32)
    base[:] = color
    yy, xx = np.indices((height, width))
    radians = math.radians(angle)
    coordinate = xx * math.cos(radians) + yy * math.sin(radians)
    frequency = max(2.0, texture_scale * max(0.001, scale))
    if texture:
        base += np.sin(coordinate * 2 * math.pi / frequency)[:, :, None] * (texture / 7)
    if grain:
        rng = np.random.default_rng(_seed(width, height, opacity, texture_scale, texture, grain, angle, 131))
        base += rng.normal(0, max(0.5, grain / 7), (height, width, 1))
    overlay = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    return mix_effect(rgba, overlay, opacity)


def noise_redaction_tuned(
    image: Image.Image,
    amount: int = 90,
    grain_size: int = 3,
    softness: int = 0,
    color: int = 45,
    contrast: int = 65,
) -> Image.Image:
    rgba = _rgba(image)
    texture = _grain_rgb(
        rgba.size,
        30 + contrast,
        _seed(rgba.width, rgba.height, amount, grain_size, softness, color, contrast, 137),
        colored=color,
        scale=max(1, grain_size),
    )
    if softness:
        texture = texture.filter(ImageFilter.GaussianBlur(softness / 20))
    return mix_effect(rgba, texture, amount)


def marker_scribble_tuned(
    image: Image.Image,
    opacity: int = 95,
    width: int = 18,
    feather: int = 8,
    density: int = 70,
    angle: int = 0,
) -> Image.Image:
    rgba = _rgba(image)
    overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    rng = np.random.default_rng(_seed(rgba.width, rgba.height, opacity, width, feather, density, angle, 139))
    radians = math.radians(angle)
    diagonal = math.hypot(rgba.width, rgba.height)
    strokes = max(2, round(2 + density / 10))
    spacing = diagonal / strokes
    direction = np.array([math.cos(radians), math.sin(radians)])
    normal = np.array([-math.sin(radians), math.cos(radians)])
    center = np.array([rgba.width / 2, rgba.height / 2])
    for index in range(strokes):
        offset = (index - (strokes - 1) / 2) * spacing
        start = center + normal * offset - direction * diagonal
        end = center + normal * offset + direction * diagonal
        jitter = max(1, width)
        points = []
        for step in np.linspace(0, 1, max(4, round(diagonal / max(12, width * 1.5)))):
            point = start * (1 - step) + end * step + normal * int(rng.integers(-jitter, jitter + 1))
            points.append((round(point[0]), round(point[1])))
        draw.line(points, fill=(3, 3, 3, round(255 * opacity / 100)), width=max(2, width), joint="curve")
    if feather:
        overlay = overlay.filter(ImageFilter.GaussianBlur(feather / 12))
    base = privacy_blur(rgba, max(2, width / 3), 45, 0, 100)
    return _with_original_alpha(rgba, Image.alpha_composite(base, overlay))


def redaction_tape_tuned(
    image: Image.Image,
    opacity: int = 100,
    crease_spacing: int = 22,
    softness: int = 8,
    texture: int = 55,
    angle: int = 0,
) -> Image.Image:
    rgba = _rgba(image)
    diagonal = math.ceil(math.hypot(rgba.width, rgba.height))
    canvas = Image.new("RGB", (diagonal, diagonal), (12, 12, 12))
    array = np.asarray(canvas, dtype=np.int16)
    rng = np.random.default_rng(_seed(rgba.width, rgba.height, opacity, crease_spacing, softness, texture, angle, 149))
    array = np.clip(array + rng.normal(0, max(1, texture / 8), array.shape[:2] + (1,)), 0, 255).astype(np.uint8)
    canvas = Image.fromarray(array, "RGB")
    draw = ImageDraw.Draw(canvas, "RGBA")
    spacing = max(6, crease_spacing)
    for y in range(spacing // 2, diagonal, spacing):
        draw.line((0, y, diagonal, y), fill=(105, 105, 105, min(100, 20 + texture)), width=max(1, spacing // 10))
    if softness:
        canvas = canvas.filter(ImageFilter.GaussianBlur(softness / 30))
    rotated = canvas.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
    left = (diagonal - rgba.width) // 2
    top = (diagonal - rgba.height) // 2
    return mix_effect(rgba, rotated.crop((left, top, left + rgba.width, top + rgba.height)), opacity)


def halftone_tuned(
    image: Image.Image,
    amount: int = 100,
    cell: int = 12,
    softness: int = 15,
    contrast: int = 70,
    angle: int = 0,
) -> Image.Image:
    rgba = _rgba(image)
    diagonal = math.ceil(math.hypot(rgba.width, rgba.height))
    padded = Image.new("RGB", (diagonal, diagonal), "white")
    padded.paste(rgba.convert("RGB"), ((diagonal - rgba.width) // 2, (diagonal - rgba.height) // 2))
    rotated = padded.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor="white")
    cell = max(3, int(cell))
    cols = max(1, math.ceil(diagonal / cell))
    rows = max(1, math.ceil(diagonal / cell))
    gray = np.asarray(ImageOps.grayscale(rotated).resize((cols, rows), Image.Resampling.BOX), dtype=np.float32)
    gray = np.clip((gray - 127.5) * (0.6 + contrast / 55) + 127.5, 0, 255)
    darkness = 1.0 - gray / 255.0
    max_radius = cell * 0.68
    radii = max_radius * np.sqrt(darkness)
    axis = np.arange(cell, dtype=np.float32) + 0.5 - cell / 2
    distance = np.sqrt(axis[:, None] ** 2 + axis[None, :] ** 2)
    edge = max(0.25, softness / 30)
    dots = np.clip((radii[:, :, None, None] - distance[None, None, :, :]) / edge + 0.5, 0, 1)
    pattern = (255 * (1 - dots)).astype(np.uint8).transpose(0, 2, 1, 3).reshape(rows * cell, cols * cell)[:diagonal, :diagonal]
    rendered = Image.fromarray(pattern, "L").convert("RGB").rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor="white")
    left = (diagonal - rgba.width) // 2
    top = (diagonal - rgba.height) // 2
    return mix_effect(rgba, rendered.crop((left, top, left + rgba.width, top + rgba.height)), amount)


def barcode_redaction(
    image: Image.Image,
    amount: int = 100,
    bar_width: int = 8,
    softness: int = 0,
    contrast: int = 75,
    angle: int = 0,
) -> Image.Image:
    rgba = _rgba(image)
    gray = np.asarray(ImageOps.grayscale(rgba), dtype=np.float32)
    height, width = gray.shape
    yy, xx = np.indices((height, width))
    radians = math.radians(angle)
    coordinate = xx * math.cos(radians) + yy * math.sin(radians)
    coordinate -= coordinate.min()
    period = max(4, int(bar_width) * 2)
    groups = np.floor(coordinate / period).astype(np.int32)
    sums = np.bincount(groups.ravel(), weights=gray.ravel())
    counts = np.bincount(groups.ravel())
    means = sums / np.maximum(1, counts)
    darkness = np.clip(1.0 - means[groups] / 255.0, 0.0, 1.0)
    darkness = np.clip((darkness - 0.5) * (0.7 + contrast / 45) + 0.5, 0.0, 1.0)
    local = np.mod(coordinate, period) / period
    black_width = 0.12 + darkness * 0.78
    bars = np.where(local < black_width, 10, 247).astype(np.uint8)
    rendered = Image.fromarray(bars, "L")
    if softness:
        rendered = rendered.filter(ImageFilter.GaussianBlur(softness / 30))
    return mix_effect(rgba, rendered.convert("RGB"), amount)


def ordered_dither(
    image: Image.Image,
    amount: int = 100,
    scale_size: int = 3,
    levels: int = 2,
    contrast: int = 65,
    angle: int = 0,
) -> Image.Image:
    rgba = _rgba(image)
    gray = np.asarray(ImageOps.grayscale(rgba), dtype=np.float32)
    gray = np.clip((gray - 127.5) * (0.6 + contrast / 60) + 127.5, 0, 255)
    bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float32)
    bayer = (bayer + 0.5) / 16.0 - 0.5
    scale_size = max(1, int(scale_size))
    pattern = np.tile(np.repeat(np.repeat(bayer, scale_size, axis=0), scale_size, axis=1), (math.ceil(rgba.height / (4 * scale_size)), math.ceil(rgba.width / (4 * scale_size))))[:rgba.height, :rgba.width]
    if angle % 360:
        pattern_image = Image.fromarray(np.uint8((pattern + 0.5) * 255), "L").rotate(angle, resample=Image.Resampling.NEAREST, expand=False, fillcolor=128)
        pattern = np.asarray(pattern_image, dtype=np.float32) / 255.0 - 0.5
    levels = max(2, min(8, int(levels)))
    adjusted = np.clip(gray / 255.0 + pattern / levels, 0, 1)
    quantized = np.round(adjusted * (levels - 1)) / (levels - 1)
    return mix_effect(rgba, Image.fromarray(np.uint8(quantized * 255), "L").convert("RGB"), amount)


def glitch_blocks_tuned(
    image: Image.Image,
    amount: int = 100,
    block: int = 14,
    displacement: int = 35,
    density: int = 65,
    angle: int = 0,
) -> Image.Image:
    rgba = _rgba(image)
    source = np.asarray(rgba.convert("RGB"))
    out = source.copy()
    height, width = source.shape[:2]
    block = max(3, int(block))
    radians = math.radians(angle)
    max_shift = max(1, int(displacement))
    rng = np.random.default_rng(_seed(width, height, amount, block, displacement, density, angle, 151))
    count = max(1, round((width * height) / max(1, block * block) * density / 500))
    for _ in range(count):
        box_w = int(rng.integers(block, max(block + 1, block * 4)))
        box_h = int(rng.integers(max(2, block // 2), max(3, block * 2)))
        left = int(rng.integers(0, max(1, width - box_w + 1)))
        top = int(rng.integers(0, max(1, height - box_h + 1)))
        distance = int(rng.integers(-max_shift, max_shift + 1))
        dx = round(math.cos(radians) * distance)
        dy = round(math.sin(radians) * distance)
        source_left = max(0, min(width - box_w, left - dx))
        source_top = max(0, min(height - box_h, top - dy))
        out[top:top + box_h, left:left + box_w] = source[source_top:source_top + box_h, source_left:source_left + box_w]
    return mix_effect(rgba, Image.fromarray(out, "RGB"), amount)


def crt_distortion_tuned(
    image: Image.Image,
    amount: int = 100,
    spacing: int = 7,
    bloom: int = 20,
    noise: int = 25,
    separation: int = 8,
    *,
    scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    source = np.asarray(rgba.convert("RGB"), dtype=np.float32)
    distance = max(1, round(separation * max(0.001, scale)))
    red = _shift_plane_no_wrap(source[:, :, 0], distance)
    green = source[:, :, 1]
    blue = _shift_plane_no_wrap(source[:, :, 2], -distance)
    out = np.stack((red, green, blue), axis=2)
    spacing = max(2, round(spacing * max(0.001, scale)))
    row_factor = np.ones((rgba.height, 1, 1), dtype=np.float32)
    row_factor[::spacing] = 0.45
    out *= row_factor
    if noise:
        rng = np.random.default_rng(_seed(rgba.width, rgba.height, amount, spacing, bloom, noise, separation, 157))
        out += rng.normal(0, noise / 3, out.shape[:2] + (1,))
    result = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")
    if bloom:
        glow = result.filter(ImageFilter.GaussianBlur(max(0.2, bloom / 20 * max(0.001, scale))))
        result = Image.blend(result, glow, min(0.45, bloom / 220))
    return mix_effect(rgba, result, amount)


def silhouette_tuned(
    image: Image.Image,
    amount: int = 100,
    smoothing: int = 8,
    threshold: int = 50,
    contrast: int = 75,
    tone: int = 50,
    *,
    scale: float = 1.0,
) -> Image.Image:
    rgba = _rgba(image)
    gray = ImageOps.grayscale(rgba)
    if smoothing:
        gray = gray.filter(ImageFilter.GaussianBlur(smoothing / 8 * max(0.001, scale)))
    gray = ImageOps.autocontrast(gray, cutoff=max(0, round((100 - contrast) / 12)))
    cutoff = max(0, min(255, round(255 * threshold / 100)))
    dark = max(0, min(100, tone))
    light = 255 - dark
    result = gray.point(lambda value: light if value >= cutoff else dark).convert("RGB")
    return mix_effect(rgba, result, amount)


def comic_cutout_tuned(
    image: Image.Image,
    amount: int = 100,
    levels: int = 5,
    edge_width: int = 2,
    edge_strength: int = 70,
    saturation: int = 30,
) -> Image.Image:
    rgba = _rgba(image)
    rgb = ImageEnhance.Color(rgba.convert("RGB")).enhance(1 + saturation / 100)
    levels = max(2, min(16, int(levels)))
    step = 255 / (levels - 1)
    array = np.asarray(rgb, dtype=np.float32)
    colors = Image.fromarray(np.uint8(np.clip(np.round(array / step) * step, 0, 255)), "RGB")
    edges = ImageOps.grayscale(rgb).filter(ImageFilter.FIND_EDGES)
    if edge_width > 1:
        edges = edges.filter(ImageFilter.MaxFilter(max(3, edge_width * 2 + 1)))
    threshold = max(10, 180 - edge_strength * 1.5)
    mask = edges.point(lambda value: 0 if value > threshold else 255)
    result = ImageChops.multiply(colors, Image.merge("RGB", (mask, mask, mask)))
    return mix_effect(rgba, result, amount)


def thermal_map_tuned(
    image: Image.Image,
    amount: int = 100,
    smoothing: int = 3,
    contrast: int = 70,
    palette_shift: int = 0,
    detail: int = 60,
) -> Image.Image:
    rgba = _rgba(image)
    gray_image = ImageOps.grayscale(rgba)
    if smoothing:
        gray_image = gray_image.filter(ImageFilter.GaussianBlur(smoothing / 4))
    gray = np.asarray(gray_image, dtype=np.float32) / 255.0
    gray = np.clip((gray - 0.5) * (0.55 + contrast / 55) + 0.5, 0, 1)
    phase = (gray + palette_shift / 100) % 1.0
    red = np.clip(1.5 - np.abs(4 * phase - 3), 0, 1)
    green = np.clip(1.5 - np.abs(4 * phase - 2), 0, 1)
    blue = np.clip(1.5 - np.abs(4 * phase - 1), 0, 1)
    rgb = np.stack((red, green, blue), axis=2)
    if detail:
        edge = np.asarray(ImageOps.grayscale(rgba).filter(ImageFilter.FIND_EDGES), dtype=np.float32) / 255.0
        rgb *= np.clip(1 - edge[:, :, None] * detail / 180, 0.3, 1)
    return mix_effect(rgba, Image.fromarray(np.uint8(rgb * 255), "RGB"), amount)


def photocopy_redaction(
    image: Image.Image,
    amount: int = 100,
    grain_size: int = 2,
    threshold: int = 55,
    edge_strength: int = 65,
    ink_spread: int = 20,
) -> Image.Image:
    rgba = _rgba(image)
    gray = ImageOps.grayscale(rgba)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    gray_array = np.asarray(gray, dtype=np.int16)
    edge_array = np.asarray(edges, dtype=np.int16)
    rng = np.random.default_rng(_seed(rgba.width, rgba.height, amount, grain_size, threshold, edge_strength, ink_spread, 163))
    coarse = rng.normal(0, 10 + edge_strength / 5, (max(1, math.ceil(rgba.height / max(1, grain_size))), max(1, math.ceil(rgba.width / max(1, grain_size)))))
    noise = np.asarray(Image.fromarray(coarse.astype(np.float32), "F").resize(rgba.size, Image.Resampling.NEAREST))
    composite = gray_array - edge_array * edge_strength / 100 + noise
    cutoff = 255 * threshold / 100
    mask = Image.fromarray(np.where(composite > cutoff, 255, 0).astype(np.uint8), "L")
    if ink_spread:
        size = max(3, min(15, (ink_spread // 10) * 2 + 3))
        mask = mask.filter(ImageFilter.MinFilter(size))
    return mix_effect(rgba, mask.convert("RGB"), amount)
