from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from imagesuite.editor import effects
from imagesuite.models import RectMask
from imagesuite.similarity.engine import FileFingerprint, build_groups, fingerprint, visual_similarity
from imagesuite.upscale import engine as upscale_engine
from imagesuite.upscale.engine import UpscaleSettings, process_file, process_image
from imagesuite.utils import expand_image_paths, save_image


def test_pixelate_preserves_size():
    image = Image.new("RGBA", (64, 48), "red")
    assert effects.pixelate(image, 8).size == image.size


def test_target_effect_changes_only_selection():
    image = Image.new("RGBA", (20, 20), "white")
    result = effects.apply_to_target(image, lambda im: Image.new("RGBA", im.size, "black"), RectMask(5, 5, 10, 10), [])
    assert result.getpixel((0, 0))[:3] == (255, 255, 255)
    assert result.getpixel((7, 7))[:3] == (0, 0, 0)


def test_target_effect_without_selection_changes_entire_image():
    image = Image.new("RGBA", (20, 20), "white")
    result = effects.apply_to_target(image, lambda im: Image.new("RGBA", im.size, "black"), None, [])
    assert result.getbbox() == (0, 0, 20, 20)
    assert result.getpixel((0, 0))[:3] == (0, 0, 0)
    assert result.getpixel((19, 19))[:3] == (0, 0, 0)


def test_pil_upscale_and_watermark():
    image = Image.new("RGBA", (20, 10), "blue")
    settings = UpscaleSettings(scale_factor=2, method="Nearest", text_watermark=True, watermark_text="X", font_size=8)
    result = process_image(image, settings)
    assert result.size == (40, 20)


def test_similarity_identical_images(tmp_path: Path):
    left = tmp_path / "a.png"; right = tmp_path / "b.png"
    Image.new("RGB", (32, 32), "green").save(left)
    Image.new("RGB", (32, 32), "green").save(right)
    assert visual_similarity(fingerprint(left), fingerprint(right)) == 100.0


def test_expand_image_paths_filters_and_deduplicates(tmp_path: Path):
    first = tmp_path / "first.png"
    second = tmp_path / "nested" / "second.jpg"
    ignored = tmp_path / "notes.txt"
    second.parent.mkdir()
    Image.new("RGB", (8, 8), "red").save(first)
    Image.new("RGB", (8, 8), "blue").save(second)
    ignored.write_text("not an image", encoding="utf-8")

    assert expand_image_paths([first, tmp_path]) == [first, second]


def test_atomic_save_preserves_existing_file_on_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / "image.png"
    Image.new("RGB", (8, 8), "red").save(target)
    original = target.read_bytes()

    def fail_save(_image, path, **_kwargs):
        Path(path).write_bytes(b"partial")
        raise OSError("simulated write failure")

    monkeypatch.setattr(Image.Image, "save", fail_save)
    with pytest.raises(OSError, match="simulated write failure"):
        save_image(Image.new("RGB", (8, 8), "blue"), target)

    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_rgba_can_save_back_to_jfif(tmp_path: Path):
    target = tmp_path / "image.jfif"
    save_image(Image.new("RGBA", (8, 8), (255, 0, 0, 128)), target)
    with Image.open(target) as saved:
        assert saved.mode == "RGB"


def test_similarity_cancellation_discards_partial_groups():
    fp = FileFingerprint("a", 1, 8, 8, 0.0, 0, 0, (0.0,) * 24, 0.0)
    assert build_groups([fp, fp], 90, cancelled=lambda: True) == []


def test_parallel_outputs_with_same_stem_do_not_overwrite(tmp_path: Path):
    left = tmp_path / "left" / "same.png"
    right = tmp_path / "right" / "same.png"
    output = tmp_path / "output"
    left.parent.mkdir()
    right.parent.mkdir()
    Image.new("RGB", (8, 8), "red").save(left)
    Image.new("RGB", (8, 8), "blue").save(right)
    settings = UpscaleSettings(scale_factor=1, method="Nearest")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda path: process_file(path, output, settings), (left, right)))

    assert len({Path(result).name for result in results if result}) == 2
    assert len(list(output.glob("*.png"))) == 2


def test_ai_model_cache_reuses_the_selected_model(tmp_path: Path):
    class FakeModel:
        def eval(self):
            return self

        def to(self, _device):
            return self

    class FakeLoader:
        calls = 0

        def load_from_file(self, _path):
            type(self).calls += 1
            return type("Descriptor", (), {"model": FakeModel(), "scale": 4})()

    class FakeCuda:
        @staticmethod
        def empty_cache():
            pass

    class FakeTorch:
        cuda = FakeCuda()

    upscale_engine._AI_MODEL_CACHE = None
    try:
        first = upscale_engine._cached_ai_model(str(tmp_path / "model.pth"), "cpu", FakeLoader, FakeTorch)
        second = upscale_engine._cached_ai_model(str(tmp_path / "model.pth"), "cpu", FakeLoader, FakeTorch)
        assert first == second
        assert FakeLoader.calls == 1
    finally:
        upscale_engine._AI_MODEL_CACHE = None


def test_fingerprint_cache_reuses_unchanged_file(tmp_path: Path):
    path = tmp_path / "cached.png"
    Image.new("RGB", (12, 12), "purple").save(path)
    first = fingerprint(path)
    second = fingerprint(path)
    assert second is first


def test_read_thumbnail_bounds_preview_size(tmp_path: Path):
    from imagesuite.utils import read_thumbnail

    path = tmp_path / "large.png"
    Image.new("RGB", (4000, 2000), "orange").save(path)
    preview = read_thumbnail(path, 500)
    assert preview.size == (500, 250)


def test_move_batch_rolls_back_partial_failure(tmp_path: Path, monkeypatch):
    from imagesuite.similarity import engine

    first = tmp_path / "source" / "first.png"
    second = tmp_path / "source" / "second.png"
    destination = tmp_path / "removed"
    first.parent.mkdir()
    Image.new("RGB", (8, 8), "red").save(first)
    Image.new("RGB", (8, 8), "blue").save(second)
    real_move = engine.shutil.move
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated move failure")
        return real_move(source, target)

    monkeypatch.setattr(engine.shutil, "move", fail_second)
    with pytest.raises(OSError, match="simulated move failure"):
        engine.move_files([str(first), str(second)], destination)

    assert first.exists()
    assert second.exists()
    assert not list(destination.glob("*.png"))


def test_document_history_restores_rectangle_lasso_and_faces():
    from imagesuite.models import ImageDocument

    document = ImageDocument.from_image(Image.new("RGBA", (64, 64), "white"))

    document.push_mask_undo()
    assert document.undo_stack[-1].image is None
    document.selection = RectMask(1, 2, 20, 22)

    document.push_mask_undo()
    document.lasso_points = [(3, 3), (30, 4), (10, 35)]

    document.push_mask_undo()
    document.face_masks.append(RectMask(8, 8, 24, 24))

    assert document.undo()
    assert document.face_masks == []
    assert document.lasso_points == [(3, 3), (30, 4), (10, 35)]

    assert document.undo()
    assert document.lasso_points == []
    assert document.selection == RectMask(1, 2, 20, 22)

    assert document.undo()
    assert document.selection is None

    assert document.redo()
    assert document.selection == RectMask(1, 2, 20, 22)


def test_undoing_image_edit_preserves_masks_from_that_edit():
    from imagesuite.models import ImageDocument

    document = ImageDocument.from_image(Image.new("RGBA", (32, 32), "white"))
    document.selection = RectMask(2, 2, 12, 12)
    document.face_masks = [RectMask(14, 14, 24, 24)]
    document.lasso_points = [(1, 1), (5, 1), (3, 5)]

    document.commit(Image.new("RGBA", (32, 32), "black"))
    assert document.undo()

    assert document.image.getpixel((0, 0))[:3] == (255, 255, 255)
    assert document.selection == RectMask(2, 2, 12, 12)
    assert document.face_masks == [RectMask(14, 14, 24, 24)]
    assert document.lasso_points == [(1, 1), (5, 1), (3, 5)]


def test_saved_revision_tracks_undo_redo_and_branching():
    from imagesuite.models import ImageDocument

    document = ImageDocument.from_image(Image.new("RGBA", (12, 12), "white"))
    document.commit(Image.new("RGBA", (12, 12), "red"))
    document.mark_saved()
    assert not document.dirty

    assert document.undo()
    assert document.dirty
    assert document.redo()
    assert not document.dirty

    assert document.undo()
    document.commit(Image.new("RGBA", (12, 12), "blue"))
    assert document.dirty
    assert document.image_revision != document.saved_revision


def test_history_memory_ceiling_discards_old_pixel_snapshots():
    from imagesuite.models import ImageDocument

    document = ImageDocument.from_image(Image.new("RGBA", (100, 100), "white"))
    document.max_history = 100
    document.max_history_bytes = 60_000
    for color in ("red", "green", "blue", "black"):
        document.commit(Image.new("RGBA", (100, 100), color))

    assert len(document.undo_stack) == 1
    assert sum(state.approximate_bytes for state in document.undo_stack) <= document.max_history_bytes


def test_animated_gif_up_to_ten_seconds_is_loaded(tmp_path: Path):
    from imagesuite.models import ImageDocument
    from imagesuite.utils import ANIMATION_FRAMES_KEY, read_image

    path = tmp_path / "animated.gif"
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "green", "blue")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[2000, 3000, 5000], loop=2)

    image = read_image(path)
    assert len(image.info[ANIMATION_FRAMES_KEY]) == 3
    document = ImageDocument.from_image(image, path)
    assert document.is_animated
    assert document.frame_count == 3
    assert document.animation_duration_ms == 10_000
    assert document.animation_loop == 2


def test_animated_gif_over_ten_seconds_is_reduced_instead_of_rejected(tmp_path: Path):
    from imagesuite.utils import ANIMATION_DURATIONS_KEY, ANIMATION_FRAMES_KEY, read_image

    path = tmp_path / "too_long.gif"
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue", "green", "yellow", "purple", "white", "black")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[15000, 15000, 15000, 15000, 15000, 15000, 15000], loop=0)

    image = read_image(path)
    loaded = image.info[ANIMATION_FRAMES_KEY]
    durations = image.info[ANIMATION_DURATIONS_KEY]
    assert len(loaded) < 7
    assert sum(durations) == 105_000


def test_multipage_tiff_is_still_rejected(tmp_path: Path):
    from imagesuite.utils import UnsupportedImageError, read_image

    path = tmp_path / "pages.tiff"
    frames = [Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "blue")]
    frames[0].save(path, save_all=True, append_images=frames[1:])

    with pytest.raises(UnsupportedImageError, match="2 pages"):
        read_image(path)


def test_animation_edit_save_and_undo_preserve_all_frames(tmp_path: Path):
    from imagesuite.models import ImageDocument
    from imagesuite.utils import read_image, save_animation

    source = tmp_path / "source.gif"
    target = tmp_path / "edited.gif"
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=[400, 600], loop=1)
    document = ImageDocument.from_image(read_image(source), source)

    document.apply_transform(lambda frame: Image.eval(frame, lambda value: 255 - value))
    assert document.animation_frames[0].getpixel((5, 5))[:3] == (0, 255, 255)
    assert document.animation_frames[1].getpixel((5, 5))[:3] == (255, 255, 0)
    assert document.undo()
    assert document.animation_frames[0].getpixel((5, 5))[:3] == (255, 0, 0)
    assert document.animation_frames[1].getpixel((5, 5))[:3] == (0, 0, 255)

    document.redo()
    save_animation(document.animation_frames, document.frame_durations, target, loop=document.animation_loop)
    with Image.open(target) as saved:
        assert saved.n_frames == 2
        assert saved.info.get("loop") == 1


def test_enhance_preserves_animated_gif_frames(tmp_path: Path):
    source = tmp_path / "source.gif"
    output = tmp_path / "output"
    frames = [Image.new("RGB", (8, 6), color) for color in ("red", "green", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0)

    result = process_file(source, output, UpscaleSettings(scale_factor=2, method="Nearest"))
    assert result is not None and result.suffix.lower() == ".gif"
    with Image.open(result) as saved:
        assert saved.n_frames == 3
        assert saved.size == (16, 12)
        durations = []
        for index in range(saved.n_frames):
            saved.seek(index)
            durations.append(int(saved.info.get("duration", 0)))
        assert durations == [100, 200, 300]


def test_dpi_metadata_survives_supported_save(tmp_path: Path):
    from imagesuite.utils import read_image

    target = tmp_path / "metadata.png"
    save_image(Image.new("RGB", (8, 8), "white"), target, metadata={"dpi": (144, 144)})
    loaded = read_image(target)
    dpi = loaded.info.get("dpi")
    assert dpi is not None
    assert abs(float(dpi[0]) - 144) < 1


def test_compose_transforms_applies_censor_chain_in_order():
    image = Image.new("RGBA", (10, 10), (255, 0, 0, 255))
    chain = effects.compose_transforms([
        lambda im: effects.fill_color(im, (0, 0, 0, 255)),
        lambda im: effects.fill_color(im, (255, 255, 255, 255)),
    ])
    result = chain(image)
    assert result.getpixel((5, 5))[:3] == (255, 255, 255)


def test_curated_censor_effects_preserve_size_mode_and_alpha():
    source = Image.new("RGBA", (96, 72), (120, 80, 40, 0))
    source.putpixel((48, 36), (20, 200, 120, 137))
    transforms = {
        "soft_blur": lambda im: effects.gaussian_blur(im, 5),
        "deep_blur": lambda im: effects.deep_blur(im, 72),
        "pixelate": lambda im: effects.pixelate(im, 8),
        "mosaic": lambda im: effects.mosaic(im, 10),
        "frosted_glass": lambda im: effects.frosted_glass(im, 65),
        "glass_tiles": lambda im: effects.glass_tiles(im, 12, 75),
        "black": lambda im: effects.fill_color(im, (0, 0, 0, 255)),
        "white": lambda im: effects.fill_color(im, (255, 255, 255, 255)),
        "noise": lambda im: effects.noise_redaction(im, 80),
        "scribble": lambda im: effects.marker_scribble(im, 12, 75),
        "tape": lambda im: effects.redaction_tape(im, 12, 90),
        "halftone": lambda im: effects.halftone_dots(im, 10, 70),
        "glitch": lambda im: effects.glitch_blocks(im, 10, 70),
        "crt": lambda im: effects.crt_distortion(im, 6, 65),
        "silhouette": lambda im: effects.silhouette_censor(im, 70),
        "comic": lambda im: effects.comic_cutout(im, 65),
        "thermal": lambda im: effects.thermal_map(im, 70),
    }
    expected_alpha = source.getchannel("A").tobytes()
    for name, transform in transforms.items():
        result = transform(source)
        assert result.mode == "RGBA", name
        assert result.size == source.size, name
        assert result.getchannel("A").tobytes() == expected_alpha, name


def test_randomized_censor_effects_are_deterministic():
    source = Image.new("RGBA", (80, 60), (120, 80, 40, 200))
    transforms = [
        lambda im: effects.noise_redaction(im, 77),
        lambda im: effects.frosted_glass(im, 64),
        lambda im: effects.glass_tiles(im, 12, 81),
        lambda im: effects.marker_scribble(im, 14, 79),
        lambda im: effects.redaction_tape(im, 12, 91),
        lambda im: effects.glitch_blocks(im, 11, 73),
        lambda im: effects.crt_distortion(im, 7, 68),
    ]
    for transform in transforms:
        assert transform(source).tobytes() == transform(source).tobytes()


def test_halftone_is_a_repeating_dot_pattern_not_four_stretched_quadrants():
    source = Image.new("RGBA", (96, 72), "gray")
    result = effects.halftone_dots(source, 8, 75).convert("L")
    transitions = sum(
        result.getpixel((x, y)) != result.getpixel((x - 1, y))
        for y in range(result.height)
        for x in range(1, result.width)
    )
    assert transitions >= 100


def test_censor_chain_can_target_only_a_selection():
    image = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
    chain = effects.compose_transforms([
        lambda im: effects.pixelate(im, 4),
        lambda im: effects.fill_color(im, (0, 0, 0, 255)),
    ])
    result = effects.apply_to_target(image, chain, RectMask(5, 5, 10, 10), [])
    assert result.getpixel((0, 0))[:3] == (255, 255, 255)
    assert result.getpixel((7, 7))[:3] == (0, 0, 0)



def test_curated_effect_defaults_are_visually_distinct():
    from PySide6.QtWidgets import QApplication
    from imagesuite.editor.workspace import EditorWorkspace

    app = QApplication.instance() or QApplication([])
    width, height = 96, 72
    source = Image.new("RGBA", (width, height))
    pixels = source.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (x * 255 // (width - 1), y * 255 // (height - 1), (x + y) * 255 // (width + height - 2), 255)

    workspace = EditorWorkspace()
    outputs = {
        workspace._effect_transform(workspace._default_effect_spec(name))(source).tobytes()
        for name in workspace.CENSOR_EFFECTS
    }
    assert len(outputs) == len(workspace.CENSOR_EFFECTS)
    assert source.tobytes() not in outputs
    workspace.close()


def test_all_dynamic_censor_defaults_preserve_alpha_and_are_deterministic():
    from PySide6.QtWidgets import QApplication
    from imagesuite.editor.workspace import EditorWorkspace

    app = QApplication.instance() or QApplication([])
    source = Image.new("RGBA", (64, 48), (80, 120, 160, 0))
    for y in range(source.height):
        for x in range(source.width):
            source.putpixel((x, y), (x * 4 % 256, y * 5 % 256, (x + y) * 3 % 256, (x * 7 + y * 11) % 256))
    expected_alpha = source.getchannel("A").tobytes()
    workspace = EditorWorkspace()
    for name in workspace.CENSOR_EFFECTS:
        transform = workspace._effect_transform(workspace._default_effect_spec(name))
        first = transform(source)
        second = transform(source)
        assert first.getchannel("A").tobytes() == expected_alpha, name
        assert first.tobytes() == second.tobytes(), name
    workspace.close()


def test_ascii_blueprint_neon_and_topographic_preserve_size_and_alpha():
    source = Image.new("RGBA", (72, 54), (20, 40, 60, 128))
    expected_alpha = source.getchannel("A").tobytes()
    transforms = [
        lambda im: effects.ascii_art_tuned(im, 100, 8, 60, 75, 40),
        lambda im: effects.blueprint_tuned(im, 100, 20, 14, 72, 28),
        lambda im: effects.neon_edges_tuned(im, 100, 8, 72, 64, 0),
        lambda im: effects.topographic_lines_tuned(im, 100, 14, 12, 68, 2),
    ]
    for transform in transforms:
        result = transform(source)
        assert result.size == source.size
        assert result.getchannel("A").tobytes() == expected_alpha


def test_new_creative_effects_are_deterministic():
    source = Image.new("RGBA", (64, 48), (90, 120, 150, 220))
    transforms = [
        lambda im: effects.ascii_art_tuned(im, 92, 9, 60, 72, 28),
        lambda im: effects.blueprint_tuned(im, 100, 22, 16, 76, 34),
        lambda im: effects.neon_edges_tuned(im, 100, 9, 82, 72, -22),
        lambda im: effects.topographic_lines_tuned(im, 100, 14, 14, 74, 2),
    ]
    for transform in transforms:
        assert transform(source).tobytes() == transform(source).tobytes()


def test_parallel_worker_limit_accepts_50_and_rejects_more():
    settings = UpscaleSettings(max_workers=50)
    upscale_engine.validate_settings(settings)
    settings.max_workers = 51
    with pytest.raises(ValueError, match="between 1 and 50"):
        upscale_engine.validate_settings(settings)


def test_worker_planner_treats_fifty_as_a_memory_safe_maximum(tmp_path: Path):
    source = tmp_path / "large.png"
    Image.new("RGB", (1600, 1200), "navy").save(source)
    settings = UpscaleSettings(method="Lanczos", scale_factor=4, max_workers=50)
    constrained = upscale_engine.plan_worker_count(
        [source] * 60,
        settings,
        available_bytes=2 * 1024**3,
        cpu_count=32,
    )
    assert 1 <= constrained.effective < 50
    assert constrained.requested == 50
    assert "memory" in constrained.reason


def test_worker_planner_can_reach_fifty_for_small_files_on_large_machine(tmp_path: Path):
    source = tmp_path / "small.png"
    Image.new("RGB", (32, 32), "navy").save(source)
    settings = UpscaleSettings(method="Nearest", scale_factor=1, max_workers=50)
    plan = upscale_engine.plan_worker_count(
        [source] * 60,
        settings,
        available_bytes=128 * 1024**3,
        cpu_count=32,
    )
    assert plan.effective == 50


def test_add_watermarks_does_not_copy_when_disabled():
    image = Image.new("RGBA", (40, 30), "red")
    result = upscale_engine.add_watermarks(image, UpscaleSettings())
    assert result is image


def test_animated_batch_streams_frames_without_read_image(tmp_path: Path, monkeypatch):
    source = tmp_path / "animated.gif"
    frames = [Image.new("RGBA", (12, 10), color) for color in ("red", "green", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=[100, 100, 100], loop=0)
    monkeypatch.setattr(upscale_engine, "read_image", lambda _path: (_ for _ in ()).throw(AssertionError("read_image should not decode all GIF frames")))
    output = process_file(source, tmp_path / "out", UpscaleSettings(method="Nearest", scale_factor=1))
    assert output is not None and output.exists()
    with Image.open(output) as saved:
        assert saved.n_frames == 3


def test_fingerprint_keeps_original_dimensions_while_using_small_preview(tmp_path: Path):
    source = tmp_path / "large.jpg"
    Image.new("RGB", (1800, 900), "orange").save(source)
    result = fingerprint(source)
    assert (result.width, result.height) == (1800, 900)


def test_atomic_destination_reservation_is_unique_across_threads(tmp_path: Path):
    from imagesuite.utils import reserve_destination

    with ThreadPoolExecutor(max_workers=20) as pool:
        paths = list(pool.map(lambda _index: reserve_destination(tmp_path, "result.png"), range(40)))
    assert len(set(paths)) == 40
    assert all(path.exists() for path in paths)


def test_release_ai_model_clears_cached_model(tmp_path: Path):
    upscale_engine._AI_MODEL_CACHE = ((str(tmp_path / "model.pth"), "cpu"), object(), 4)
    upscale_engine.release_ai_model()
    assert upscale_engine._AI_MODEL_CACHE is None


def test_read_thumbnail_for_gif_uses_first_frame_only(tmp_path: Path):
    from imagesuite.utils import ANIMATION_FRAMES_KEY, read_thumbnail

    path = tmp_path / "animated.gif"
    first = Image.new("RGBA", (32, 24), "red")
    second = Image.new("RGBA", (32, 24), "blue")
    first.save(path, save_all=True, append_images=[second], duration=[100, 100], loop=0)

    preview = read_thumbnail(path, 20)
    try:
        assert preview.size == (20, 15)
        assert ANIMATION_FRAMES_KEY not in preview.info
        assert preview.getpixel((5, 5))[0] > preview.getpixel((5, 5))[2]
    finally:
        preview.close()


def test_preview_only_ai_worker_releases_model(tmp_path: Path, monkeypatch):
    from imagesuite.upscale.workspace import UpscaleWorker

    calls: list[str] = []

    monkeypatch.setattr("imagesuite.upscale.workspace.validate_settings", lambda settings: calls.append("validate"))
    monkeypatch.setattr("imagesuite.upscale.workspace.prepare_ai_model", lambda settings, progress: calls.append("prepare"))
    monkeypatch.setattr("imagesuite.upscale.workspace.release_ai_model", lambda: calls.append("release"))
    monkeypatch.setattr("imagesuite.upscale.workspace.release_unused_memory", lambda: calls.append("cleanup"))
    monkeypatch.setattr("imagesuite.upscale.workspace.read_thumbnail", lambda path, max_side=1200: Image.new("RGBA", (24, 24), "white"))
    monkeypatch.setattr("imagesuite.upscale.workspace.process_image", lambda image, settings, progress=None: image.copy())

    source = tmp_path / "source.png"
    Image.new("RGBA", (10, 10), "white").save(source)
    worker = UpscaleWorker([source], tmp_path / "out", UpscaleSettings(method="AI model", model_path="model.pth"), preview_only=True)
    worker.run()

    assert calls.count("prepare") == 1
    assert calls.count("release") == 1
    assert calls[-1] == "cleanup"


def test_auto_ai_tile_uses_available_cuda_memory(monkeypatch):
    class FakeCuda:
        @staticmethod
        def mem_get_info(_device):
            return 10 * 1024**3, 12 * 1024**3

    class FakeTorch:
        cuda = FakeCuda()

    assert upscale_engine.recommend_ai_tile((4000, 3000), "cuda:0", "FP16", FakeTorch()) == 768
    assert upscale_engine.recommend_ai_tile((4000, 3000), "cuda:0", "FP32", FakeTorch()) == 576


def test_ai_oom_recovery_retries_smaller_tiles(monkeypatch):
    calls: list[int] = []

    def fake_run(_model, image, tile, _scale, _device, _precision, _torch, _progress=None):
        calls.append(tile)
        if len(calls) == 1:
            raise RuntimeError("CUDA out of memory")
        return Image.new("RGB", (image.width * 2, image.height * 2), "white")

    monkeypatch.setattr(upscale_engine, "_run_model_stitched", fake_run)
    settings = UpscaleSettings(method="AI model", tile_size=512, ai_oom_recovery=True)
    result = upscale_engine._ai_upscale_once(
        Image.new("RGB", (32, 24), "black"), settings, object(), 2, "cpu", "FP32", object(), None
    )
    assert calls[:2] == [512, 384]
    assert result.size == (64, 48)


def test_animated_output_respects_mp4_and_webm_selection(tmp_path: Path):
    source = tmp_path / "source.gif"
    Image.new("RGB", (8, 8), "red").save(source)
    assert upscale_engine.output_path(source, tmp_path, UpscaleSettings(output_format="MP4"), animated=True).suffix == ".mp4"
    assert upscale_engine.output_path(source, tmp_path, UpscaleSettings(output_format="WebM"), animated=True).suffix == ".webm"


def test_stitched_ai_output_uses_bounded_tiles():
    torch = pytest.importorskip("torch")

    class Nearest2x(torch.nn.Module):
        def forward(self, tensor):
            return torch.nn.functional.interpolate(tensor, scale_factor=2, mode="nearest")

    source = Image.new("RGB", (130, 90), "purple")
    result = upscale_engine._run_model_stitched(Nearest2x().eval(), source, 64, 2, torch.device("cpu"), "FP32", torch)
    assert result.size == (260, 180)
    assert result.getpixel((200, 120)) == source.getpixel((100, 60))


def test_processing_reader_keeps_opaque_images_rgb_and_preserves_alpha_on_request(tmp_path: Path):
    from imagesuite.utils import read_processing_image

    opaque = tmp_path / "opaque.jpg"
    transparent = tmp_path / "transparent.png"
    Image.new("RGB", (12, 10), (20, 40, 60)).save(opaque)
    Image.new("RGBA", (12, 10), (255, 0, 0, 0)).save(transparent)

    rgb = read_processing_image(opaque, preserve_transparency=True)
    rgba = read_processing_image(transparent, preserve_transparency=True)
    flattened = read_processing_image(transparent, preserve_transparency=False)
    try:
        assert rgb.mode == "RGB"
        assert rgba.mode == "RGBA"
        assert rgba.getpixel((0, 0))[3] == 0
        assert flattened.mode == "RGB"
        assert flattened.getpixel((0, 0)) == (255, 255, 255)
    finally:
        rgb.close()
        rgba.close()
        flattened.close()


def test_resize_only_pipeline_preserves_rgb_and_matches_pillow():
    image = Image.effect_noise((64, 48), 40).convert("RGB")
    settings = UpscaleSettings(
        scale_factor=2,
        method="Bicubic",
        output_format="JPEG",
        sharpen=0,
        denoise=0,
        contrast=1,
        brightness=1,
        saturation=1,
    )
    expected = image.resize((128, 96), Image.Resampling.BICUBIC)
    result = process_image(image, settings)
    try:
        assert result.mode == "RGB"
        assert result.tobytes() == expected.tobytes()
    finally:
        result.close()
        expected.close()
        image.close()


def test_fast_png_compression_is_lossless(tmp_path: Path):
    source = Image.effect_noise((96, 72), 80).convert("RGB")
    normal = tmp_path / "normal.png"
    fast = tmp_path / "fast.png"
    save_image(source, normal, optimize=False, png_compress_level=6)
    save_image(source, fast, optimize=False, png_compress_level=3)
    with Image.open(normal) as left, Image.open(fast) as right:
        assert left.mode == right.mode == "RGB"
        assert left.tobytes() == right.tobytes()


def test_worker_plan_uses_measured_photo_caps(monkeypatch):
    monkeypatch.setattr(upscale_engine, "_estimated_file_working_set", lambda _path, _settings: (64 * 1024**2, False))
    jpeg = UpscaleSettings(output_format="JPEG", max_workers=50)
    png = UpscaleSettings(output_format="PNG", max_workers=50)
    assert upscale_engine.plan_worker_count(["a.jpg"] * 50, jpeg, available_bytes=32 * 1024**3, cpu_count=16).effective == 6
    assert upscale_engine.plan_worker_count(["a.jpg"] * 50, png, available_bytes=32 * 1024**3, cpu_count=16).effective == 4


def test_ascii_contour_strength_adds_edge_oriented_structure():
    source = Image.new("RGBA", (160, 100), "white")
    from PIL import ImageDraw
    draw = ImageDraw.Draw(source)
    draw.rectangle((20, 15, 75, 85), fill="black")
    draw.ellipse((90, 18, 145, 82), fill=(40, 100, 220, 255))

    plain = effects.ascii_art_tuned(source, 100, 8, 55, 55, 25, 0)
    contoured = effects.ascii_art_tuned(source, 100, 8, 55, 55, 25, 95)

    assert contoured.tobytes() != plain.tobytes()
    assert contoured.size == source.size
    assert contoured.getchannel("A").tobytes() == source.getchannel("A").tobytes()


def test_target_feather_has_soft_transition_without_touching_far_pixels():
    source = Image.new("RGBA", (100, 80), "white")
    selection = RectMask(30, 20, 70, 60)
    result = effects.apply_to_target(
        source,
        lambda image: Image.new("RGBA", image.size, "black"),
        selection,
        [],
        feather=20,
        padding=10,
    )

    assert result.getpixel((50, 40))[:3] == (0, 0, 0)
    assert result.getpixel((0, 0))[:3] == (255, 255, 255)
    edge_value = result.getpixel((18, 40))[0]
    assert 0 < edge_value < 255


def test_face_feather_keeps_face_core_and_blends_protection_edge():
    source = Image.new("RGBA", (100, 80), "red")
    result = effects.apply_outside_faces(
        source,
        lambda image: Image.new("RGBA", image.size, "black"),
        [RectMask(30, 20, 70, 60)],
        feather=20,
        padding=8,
    )

    assert result.getpixel((50, 40))[:3] == (255, 0, 0)
    assert result.getpixel((0, 0))[:3] == (0, 0, 0)
    transition = result.getpixel((22, 40))[:3]
    assert transition not in {(255, 0, 0), (0, 0, 0)}


def test_large_animation_edit_auto_reduces_and_preserves_duration(tmp_path: Path):
    from imagesuite.models import ImageDocument
    from imagesuite.utils import read_image

    source = tmp_path / "large-working-copy.gif"
    frames = [Image.new("RGB", (40, 30), (index * 20 % 255, 40, 80)) for index in range(12)]
    durations = [80 + index for index in range(12)]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=durations, loop=0)
    document = ImageDocument.from_image(read_image(source), source)
    original_duration = document.animation_duration_ms
    original_count = document.frame_count

    summary = document.apply_transform(lambda frame: frame, budget_pixels=4_000)

    assert summary is not None and summary.reduced
    assert document.frame_count < original_count or document.image.size != (40, 30)
    assert document.animation_duration_ms == original_duration
    assert document.image is document.animation_frames[0]
    assert document.undo()
    assert document.frame_count == original_count
    assert document.animation_duration_ms == original_duration
    assert document.redo()
    assert document.animation_duration_ms == original_duration
    document.close()


def test_animation_commit_transfers_frames_to_history_without_copying(tmp_path: Path):
    from imagesuite.models import ImageDocument
    from imagesuite.utils import read_image

    source = tmp_path / "history-transfer.gif"
    frames = [Image.new("RGB", (16, 12), color) for color in ("red", "green", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=[100, 100, 100], loop=0)
    document = ImageDocument.from_image(read_image(source), source)
    old_first = document.animation_frames[0]

    document.apply_transform(lambda frame: Image.eval(frame, lambda value: 255 - value))

    assert document.undo_stack[-1].image is old_first
    assert document.image is document.animation_frames[0]
    assert document.undo()
    assert document.animation_frames[0] is old_first
    document.close()


def test_animation_reduction_scales_selection_geometry(tmp_path: Path):
    from imagesuite.models import ImageDocument
    from imagesuite.utils import read_image

    source = tmp_path / "geometry.gif"
    frames = [Image.new("RGB", (100, 80), color) for color in ("white", "red", "green", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=[100] * 4, loop=0)
    document = ImageDocument.from_image(read_image(source), source)
    document.selection = RectMask(20, 10, 80, 70)
    document.face_masks = [RectMask(10, 20, 50, 60)]
    document.lasso_points = [(10, 10), (90, 10), (50, 70)]

    summary = document.apply_transform(lambda frame: frame, budget_pixels=8_000)

    assert summary is not None and summary.output_size[0] < 100
    sx = summary.output_size[0] / 100
    sy = summary.output_size[1] / 80
    assert document.selection == RectMask(round(20 * sx), round(10 * sy), round(80 * sx), round(70 * sy))
    assert document.face_masks[0] == RectMask(round(10 * sx), round(20 * sy), round(50 * sx), round(60 * sy))
    assert document.lasso_points[0] == (round(10 * sx), round(10 * sy))
    document.close()
