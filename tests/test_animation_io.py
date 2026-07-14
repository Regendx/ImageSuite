from __future__ import annotations

from pathlib import Path

from PIL import Image

from imagesuite.utils import read_image, save_animation


def _make_frames(count: int = 4, size: tuple[int, int] = (24, 18)):
    frames = []
    for index in range(count):
        frames.append(Image.new("RGBA", size, ((index * 40) % 255, 40, 200, 255)))
    return frames


def test_long_gif_is_reduced_instead_of_rejected(tmp_path: Path):
    frames = _make_frames(100)
    durations = [1000] * 100  # 100 seconds
    source = tmp_path / "long.gif"
    save_animation(frames, durations, source)
    image = read_image(source)
    reduced_frames = image.info.get("_imagesuite_animation_frames", [])
    reduced_durations = image.info.get("_imagesuite_animation_durations", [])
    assert len(reduced_frames) < len(frames)
    assert sum(reduced_durations) == sum(durations)


def test_mp4_and_webm_round_trip_as_animated_media(tmp_path: Path):
    frames = _make_frames(3)
    durations = [100, 100, 100]
    for suffix in (".mp4", ".webm"):
        target = tmp_path / f"roundtrip{suffix}"
        save_animation(frames, durations, target)
        image = read_image(target)
        loaded_frames = image.info.get("_imagesuite_animation_frames", [])
        loaded_durations = image.info.get("_imagesuite_animation_durations", [])
        assert len(loaded_frames) >= 2
        assert sum(loaded_durations) > 0
