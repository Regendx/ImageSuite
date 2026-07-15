from __future__ import annotations

from pathlib import Path

from PIL import Image

from imagesuite.models import (
    ANIMATION_DURATIONS_KEY,
    ANIMATION_FRAMES_KEY,
    ANIMATION_LOOP_KEY,
    ImageDocument,
)
from imagesuite.utils import (
    choose_animation_reduction,
    export_video_segment,
    probe_video,
    read_image,
    read_video_timeline_thumbnails,
    save_animation,
)


def _make_frames(count: int = 4, size: tuple[int, int] = (24, 18)):
    frames = []
    for index in range(count):
        frames.append(Image.new("RGBA", size, ((index * 40) % 255, 40, 200, 255)))
    return frames


def _make_document(durations: list[int] | None = None) -> ImageDocument:
    frames = _make_frames(3, (8, 6))
    image = frames[0].copy()
    image.info[ANIMATION_FRAMES_KEY] = frames
    image.info[ANIMATION_DURATIONS_KEY] = durations or [100, 200, 300]
    image.info[ANIMATION_LOOP_KEY] = 0
    return ImageDocument.from_image(image)


def test_animation_can_extend_by_repeating_selected_loop_and_undo():
    document = _make_document()

    added = document.extend_animation(1050, mode="repeat", range_start=1, range_end=2)

    assert added == 2
    assert document.frame_count == 5
    assert document.frame_durations == [100, 200, 300, 200, 250]
    assert document.animation_duration_ms == 1050
    assert document.animation_frames[3].getpixel((0, 0)) == document.original_animation_frames[1].getpixel((0, 0))
    assert document.undo()
    assert document.frame_count == 3
    assert document.animation_duration_ms == 600
    assert document.redo()
    assert document.frame_count == 5
    assert document.animation_duration_ms == 1050
    document.close()


def test_animation_can_extend_by_holding_final_frame_without_adding_frames():
    document = _make_document()

    added = document.extend_animation(1600, mode="hold")

    assert added == 0
    assert document.frame_count == 3
    assert document.frame_durations == [100, 200, 1300]
    assert document.animation_duration_ms == 1600
    document.close()


def test_animation_extension_preserves_exact_sub_frame_remainder():
    document = _make_document()

    document.extend_animation(605, mode="repeat")

    assert document.frame_count == 3
    assert document.frame_durations == [100, 200, 305]
    assert document.animation_duration_ms == 605
    document.close()


def test_animation_extension_refuses_unsafe_frame_count_without_changes():
    document = _make_document()

    try:
        document.extend_animation(1200, mode="repeat", max_frames=4)
    except ValueError as exc:
        assert "editable frames" in str(exc)
    else:
        raise AssertionError("Expected the frame safety limit to reject the extension")

    assert document.frame_count == 3
    assert document.animation_duration_ms == 600
    assert not document.undo_stack
    document.close()


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


def test_video_segment_import_seeks_limits_fps_and_scales(tmp_path: Path):
    from imagesuite.utils import probe_video, read_animated_video

    source = tmp_path / "large-source.mp4"
    source_frames = [Image.new("RGBA", (48, 32), (index * 8, index * 8, index * 8, 255)) for index in range(30)]
    save_animation(source_frames, [100] * len(source_frames), source, fps=10)
    for frame in source_frames:
        frame.close()

    info = probe_video(source)
    assert info["duration_ms"] == 3000
    assert info["width"] == 48
    assert info["height"] == 32

    frames, durations, _loop, reduced = read_animated_video(
        source,
        start_ms=1000,
        duration_ms=500,
        target_fps=5,
        max_side=24,
    )
    assert 2 <= len(frames) <= 3
    assert sum(durations) == 500
    assert max(frames[0].size) == 24
    assert frames[0].getpixel((0, 0))[0] >= 65
    assert reduced
    for frame in frames:
        frame.close()


def test_multi_hour_video_uses_proxy_instead_of_duration_rejection():
    source_frames = 30 * 60 * 60 * 2
    duration_ms = 2 * 60 * 60 * 1000

    stride, scale, reduced = choose_animation_reduction(source_frames, duration_ms, 1920, 1080)

    assert reduced
    assert (source_frames + stride - 1) // stride <= 900
    assert 0.2 <= scale <= 1.0


def test_video_timeline_filmstrip_uses_even_fast_seek_samples(tmp_path: Path):
    source = tmp_path / "timeline.mp4"
    frames = _make_frames(8, (64, 48))
    save_animation(frames, [125] * len(frames), source, fps=8)

    thumbnails = read_video_timeline_thumbnails(source, count=4, size=(120, 68))

    assert len(thumbnails) == 4
    assert all(thumbnail.size == (120, 68) for thumbnail in thumbnails)
    assert thumbnails[0].getpixel((60, 34)) != thumbnails[-1].getpixel((60, 34))
    for thumbnail in thumbnails:
        thumbnail.close()
    for frame in frames:
        frame.close()


def test_imported_video_keeps_direct_source_provenance_and_exports_without_gif(tmp_path: Path):
    source = tmp_path / "source.mp4"
    target = tmp_path / "direct.mp4"
    frames = _make_frames(12, (32, 24))
    save_animation(frames, [100] * len(frames), source, fps=10)
    source_info = probe_video(source)

    image = read_image(
        source,
        video_options={"start_ms": 200, "duration_ms": 800, "target_fps": 5, "max_side": 24},
    )
    document = ImageDocument.from_image(image, source)
    assert document.direct_video_source == source.resolve()
    assert document.direct_video_start_ms == 200
    assert document.direct_video_duration_ms == 800
    assert max(document.image.size) == 24

    export_video_segment(
        document.direct_video_source,
        target,
        start_ms=document.direct_video_start_ms + 100,
        duration_ms=400,
    )

    assert target.exists()
    assert not list(tmp_path.glob("*.gif"))
    exported = probe_video(target)
    assert 300 <= int(exported["duration_ms"]) <= 500
    assert (int(exported["width"]), int(exported["height"])) == (
        int(source_info["width"]),
        int(source_info["height"]),
    )
    document.close()


def test_animation_export_range_and_exact_gif_duration(tmp_path: Path):
    from imagesuite.utils import retime_animation_durations, slice_animation

    frames = _make_frames(4)
    selected, durations = slice_animation(
        frames,
        [100, 200, 300, 400],
        start_ms=150,
        duration_ms=500,
    )
    assert len(selected) == 3
    assert durations == [150, 300, 50]

    retimed = retime_animation_durations(durations, 1250)
    assert len(retimed) == 3
    assert sum(retimed) == 1250
    assert all(duration >= 10 and duration % 10 == 0 for duration in retimed)

    target = tmp_path / "exact-duration.gif"
    save_animation(selected, retimed, target)
    loaded = read_image(target)
    document = ImageDocument.from_image(loaded)
    assert document.animation_duration_ms == 1250
    document.close()


def test_rendered_video_can_remux_matching_source_audio(tmp_path: Path):
    import subprocess

    import imageio_ffmpeg

    source = tmp_path / "source-with-audio.mp4"
    target = tmp_path / "edited-with-audio.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    created = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=32x24:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=1",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr

    frames = _make_frames(10, (32, 24))
    save_animation(
        frames,
        [100] * len(frames),
        target,
        fps=10,
        audio_source=source,
        audio_start_ms=0,
        audio_duration_ms=1000,
    )
    probe = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(target), "-map", "0:a:0", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert target.exists()
    for frame in frames:
        frame.close()


def test_direct_video_export_can_explicitly_drop_audio(tmp_path: Path):
    import subprocess

    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = tmp_path / "source-audio.mp4"
    muted = tmp_path / "muted.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=32x24:r=10:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=0.5",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    export_video_segment(source, muted, duration_ms=500, preserve_audio=False)
    probe = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(muted), "-map", "0:a:0", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0
