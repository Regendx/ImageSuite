from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

from imagesuite.editor.workspace import EditorWorkspace
from imagesuite.jobs import JobManager
from imagesuite.main_window import MainWindow
from imagesuite.similarity.workspace import SimilarityWorkspace


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_single_path_routes_to_editor_and_enhance_queue(tmp_path: Path):
    _app()
    (tmp_path / "portable.flag").touch()
    source = tmp_path / "image.png"
    Image.new("RGB", (16, 16), "red").save(source)
    window = MainWindow(tmp_path)

    window.route_paths([source])
    assert len(window.editor.documents) == 1
    assert window.stack.currentWidget() is window.editor

    window.add_to_enhance([source])
    assert window.upscale.queue_paths() == [source]
    assert window.stack.currentWidget() is window.upscale
    window.close()


def test_last_quick_text_can_be_reopened_before_another_edit():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (320, 240), "white"), dirty=False)
    workspace.choose_mode("text")
    workspace.quick_text_edit.setText("Editable")
    workspace.quick_text_anchor = (160, 120)
    before = workspace.document.image.copy()

    workspace.apply_quick_text()
    assert workspace.document.image.tobytes() != before.tobytes()
    workspace.reedit_last_text()

    assert workspace.document.image.tobytes() == before.tobytes()
    assert workspace.canvas.preview_image is not None
    assert workspace.quick_text_edit.text() == "Editable"
    workspace.close()


def test_undo_last_move_restores_files(tmp_path: Path):
    _app()
    original = tmp_path / "source" / "image.png"
    moved = tmp_path / "removed" / "image.png"
    moved.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), "blue").save(moved)

    workspace = SimilarityWorkspace(JobManager())
    workspace.last_move = [(str(original), str(moved))]
    workspace.undo_move_action.setEnabled(True)
    workspace.undo_last_move()

    assert original.exists()
    assert not moved.exists()
    assert not workspace.undo_move_action.isEnabled()
    workspace.close()


def test_recovery_is_written_once_and_removed_after_save(tmp_path: Path):
    _app()
    source = tmp_path / "source.png"
    Image.new("RGB", (24, 24), "white").save(source)
    workspace = EditorWorkspace()
    workspace.recovery_dir = tmp_path / "recovery"
    workspace.recovery_dir.mkdir()
    workspace.failed_recovery_dir = tmp_path / "recovery_failed"
    workspace.add_document(Image.new("RGBA", (24, 24), "red"), source, dirty=True)
    document = workspace.document
    assert document is not None

    workspace._autosave()
    assert workspace.wait_for_recovery()
    image_path = workspace.recovery_dir / f"{document.id}.png"
    metadata_path = workspace.recovery_dir / f"{document.id}.json"
    assert image_path.exists() and metadata_path.exists()
    first_mtime = image_path.stat().st_mtime_ns

    workspace._autosave()
    assert workspace.wait_for_recovery()
    assert image_path.stat().st_mtime_ns == first_mtime

    assert workspace.save()
    assert not image_path.exists()
    assert not metadata_path.exists()
    workspace.finalize_close()
    workspace.close()


def test_malformed_recovery_is_quarantined(tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _app()
    workspace = EditorWorkspace()
    workspace.recovery_dir = tmp_path / "recovery"
    workspace.recovery_dir.mkdir()
    workspace.failed_recovery_dir = tmp_path / "recovery_failed"
    image_path = workspace.recovery_dir / "broken.png"
    metadata_path = workspace.recovery_dir / "broken.json"
    Image.new("RGB", (8, 8), "white").save(image_path)
    metadata_path.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok)

    workspace._offer_recovery()

    assert not image_path.exists()
    assert not metadata_path.exists()
    assert len(list(workspace.failed_recovery_dir.iterdir())) == 2
    workspace.finalize_close()
    workspace.close()


def test_orphan_recovery_image_restores_as_untitled(tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _app()
    workspace = EditorWorkspace()
    workspace.recovery_dir = tmp_path / "recovery"
    workspace.recovery_dir.mkdir()
    workspace.failed_recovery_dir = tmp_path / "recovery_failed"
    image_path = workspace.recovery_dir / "orphan.png"
    Image.new("RGB", (13, 9), "purple").save(image_path)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)

    workspace._offer_recovery()

    assert len(workspace.documents) == 1
    assert workspace.document is not None
    assert workspace.document.path is None
    assert workspace.document.dirty
    assert workspace.document.id == "orphan"
    workspace.finalize_close()
    workspace.close()


def test_standalone_editor_close_stops_timers_and_recovery_executor():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (12, 12), "white"), dirty=False)
    assert workspace.autosave_timer.isActive()

    workspace.close()

    assert workspace._finalized
    assert not workspace.autosave_timer.isActive()
    assert workspace._recovery_executor._shutdown


def test_animated_recovery_preserves_frames_and_timing(tmp_path: Path):
    from imagesuite.utils import read_image

    _app()
    source = tmp_path / "source.gif"
    frames = [Image.new("RGB", (12, 10), color) for color in ("red", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=[300, 700], loop=2)

    workspace = EditorWorkspace()
    workspace.recovery_dir = tmp_path / "recovery"
    workspace.recovery_dir.mkdir()
    workspace.failed_recovery_dir = tmp_path / "recovery_failed"
    workspace.add_document(read_image(source), source, dirty=True)
    document = workspace.document
    assert document is not None and document.is_animated

    workspace._autosave()
    assert workspace.wait_for_recovery()
    gif_path = workspace.recovery_dir / f"{document.id}.gif"
    assert gif_path.exists()
    recovered = read_image(gif_path)
    from imagesuite.models import ImageDocument
    recovered_document = ImageDocument.from_image(recovered)
    assert recovered_document.frame_count == 2
    assert recovered_document.animation_duration_ms == 1000
    assert recovered_document.animation_loop == 2
    workspace.finalize_close()
    workspace.close()


def test_upscale_worker_can_use_fifty_non_ai_workers_when_planner_allows_it(tmp_path: Path, monkeypatch):
    import concurrent.futures
    from imagesuite.upscale import workspace as upscale_workspace
    from imagesuite.upscale.engine import UpscaleSettings, WorkerPlan

    _app()
    seen: list[int] = []
    real_executor = concurrent.futures.ThreadPoolExecutor

    def executor(max_workers: int, **kwargs):
        seen.append(max_workers)
        return real_executor(max_workers=max_workers, **kwargs)

    monkeypatch.setattr(upscale_workspace, "ThreadPoolExecutor", executor)
    monkeypatch.setattr(
        upscale_workspace,
        "process_file",
        lambda path, output, settings, progress=None: output / path.name,
    )
    files = [tmp_path / f"image-{index}.png" for index in range(60)]
    worker = upscale_workspace.UpscaleWorker(
        files,
        tmp_path / "output",
        UpscaleSettings(method="Nearest", max_workers=50),
        plan=WorkerPlan(50, 50, 1, 1024**4, "requested limit"),
    )
    worker.run()
    assert seen == [50]


def test_job_manager_bounds_long_session_history():
    from imagesuite.jobs import JobManager

    manager = JobManager()
    for index in range(250):
        manager.create(f"Job {index}", "Test")
    assert len(manager.jobs) == 200
    assert manager.jobs[0].name == "Job 249"


def test_custom_animation_export_does_not_replace_open_document(tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QDialog
    from imagesuite.editor.workspace import AnimationExportDialog
    from imagesuite.models import ImageDocument
    from imagesuite.utils import read_image, save_animation

    _app()
    source = tmp_path / "source.gif"
    target = tmp_path / "clip.gif"
    frames = [Image.new("RGBA", (12, 10), color) for color in ("red", "green", "blue")]
    save_animation(frames, [200, 200, 200], source)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source), source, dirty=True)
    document = workspace.document
    assert document is not None and document.dirty

    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), "Animated GIF (*.gif)"))
    monkeypatch.setattr(AnimationExportDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(
        AnimationExportDialog,
        "options",
        lambda self: {
            "start_ms": 100,
            "duration_ms": 300,
            "output_duration_ms": 900,
            "gif_colors": 128,
            "gif_dither": False,
            "gif_optimize": False,
            "fps": 0,
            "bitrate_kbps": 0,
        },
    )

    assert workspace.save_as()
    assert target.exists()
    assert document.path == source
    assert document.dirty
    exported = ImageDocument.from_image(read_image(target))
    assert exported.animation_duration_ms == 900
    exported.close()
    workspace.finalize_close()
    workspace.close()


def test_unedited_mp4_can_export_directly_from_source_video(tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QDialog
    from imagesuite.editor import workspace as workspace_module
    from imagesuite.editor.workspace import AnimationExportDialog
    from imagesuite.utils import read_image, save_animation

    _app()
    source = tmp_path / "source.mp4"
    target = tmp_path / "clip.mp4"
    frames = [Image.new("RGBA", (16, 12), color) for color in ("red", "green", "blue", "white", "black", "yellow")]
    save_animation(frames, [100] * len(frames), source, fps=10)

    workspace = EditorWorkspace()
    workspace.add_document(
        read_image(source, video_options={"start_ms": 100, "duration_ms": 500, "target_fps": 5, "max_side": 16}),
        source,
    )
    document = workspace.document
    assert document is not None and document.direct_video_source == source.resolve()

    calls: list[tuple[Path, Path, int, int]] = []

    def fake_direct(source_path, target_path, *, start_ms, duration_ms, fps=0, bitrate_kbps=0, preserve_audio=True):
        calls.append((Path(source_path), Path(target_path), start_ms, duration_ms))
        Path(target_path).write_bytes(b"video")

    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), "MP4 video (*.mp4)"))
    monkeypatch.setattr(AnimationExportDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(
        AnimationExportDialog,
        "options",
        lambda self: {
            "start_ms": 100,
            "duration_ms": 300,
            "output_duration_ms": 300,
            "gif_colors": 256,
            "gif_dither": True,
            "gif_optimize": False,
            "fps": 0,
            "bitrate_kbps": 0,
            "direct_video": True,
        },
    )
    monkeypatch.setattr(workspace_module, "export_video_segment", fake_direct)
    monkeypatch.setattr(workspace_module, "save_animation", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rendered path used")))

    assert workspace.save_as()
    assert calls == [(source.resolve(), target, 200, 300)]
    assert document.path == source
    workspace.finalize_close()
    workspace.close()


def test_edited_video_export_preserves_source_audio_via_rendered_path(tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QDialog
    from imagesuite.editor import workspace as workspace_module
    from imagesuite.editor.workspace import AnimationExportDialog
    from imagesuite.utils import read_image, save_animation as real_save_animation

    _app()
    source = tmp_path / "source.mp4"
    target = tmp_path / "edited.mp4"
    frames = [Image.new("RGBA", (16, 12), color) for color in ("red", "green", "blue", "white")]
    real_save_animation(frames, [100] * len(frames), source, fps=10)

    workspace = EditorWorkspace()
    workspace.add_document(
        read_image(source, video_options={"start_ms": 100, "duration_ms": 300, "target_fps": 10, "max_side": 16}),
        source,
    )
    document = workspace.document
    assert document is not None
    document.commit_frames([frame.copy() for frame in document.animation_frames], durations=list(document.frame_durations))
    assert document.direct_video_source is None
    assert document.source_video == source.resolve()

    captured: dict[str, object] = {}

    def fake_save(frames, durations, path, **kwargs):
        captured.update(kwargs)
        Path(path).write_bytes(b"rendered")

    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), "MP4 video (*.mp4)"))
    monkeypatch.setattr(AnimationExportDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(
        AnimationExportDialog,
        "options",
        lambda self: {
            "start_ms": 0,
            "duration_ms": document.animation_duration_ms,
            "output_duration_ms": document.animation_duration_ms,
            "gif_colors": 256,
            "gif_dither": True,
            "gif_optimize": False,
            "fps": 0,
            "bitrate_kbps": 0,
            "direct_video": False,
            "preserve_audio": True,
        },
    )
    monkeypatch.setattr(workspace_module, "save_animation", fake_save)

    assert workspace.save_as()
    assert captured["audio_source"] == source.resolve()
    assert captured["audio_start_ms"] == 100
    assert captured["audio_duration_ms"] == document.animation_duration_ms
    workspace.finalize_close()
    workspace.close()


def test_closing_tabs_detaches_closed_document_and_keeps_background_selection():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (20, 20), "red"))
    first = workspace.document
    workspace.add_document(Image.new("RGBA", (20, 20), "green"))
    second = workspace.document
    workspace.add_document(Image.new("RGBA", (20, 20), "blue"))
    third = workspace.document
    assert first is not None and second is not None and third is not None

    workspace.close_document(0)
    assert workspace.document is third
    assert workspace.canvas.document is third

    workspace.close_document(1)
    assert workspace.document is second
    assert workspace.canvas.document is second
    before = second.image_revision
    workspace.effect_combo.setCurrentText("Pixelate")
    workspace.apply_effect()
    assert second.image_revision > before
    workspace.finalize_close()
    workspace.close()


def test_closing_background_tab_preserves_active_preview_state():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (24, 18), "red"))
    workspace.add_document(Image.new("RGBA", (24, 18), "green"))
    active = workspace.document
    assert active is not None
    preview = Image.new("RGBA", (24, 18), "blue")
    workspace.canvas.preview_image = preview
    workspace._preview_kind = "effect"
    workspace._requested_preview_kind = "effect"

    workspace.close_document(0)

    assert workspace.document is active
    assert workspace.canvas.document is active
    assert workspace.canvas.preview_image is preview
    assert workspace._preview_kind == "effect"
    workspace.finalize_close()
    workspace.close()


def test_animation_recovery_uses_owned_frame_copies(tmp_path: Path):
    from imagesuite.models import ANIMATION_DURATIONS_KEY, ANIMATION_FRAMES_KEY

    _app()
    workspace = EditorWorkspace()
    workspace.recovery_dir = tmp_path
    frames = [Image.new("RGBA", (12, 10), color) for color in ("red", "green", "blue")]
    image = frames[0].copy()
    image.info[ANIMATION_FRAMES_KEY] = frames
    image.info[ANIMATION_DURATIONS_KEY] = [100, 100, 100]
    workspace.add_document(image, dirty=True)
    document = workspace.document
    assert document is not None
    original_ids = {id(frame) for frame in document.animation_frames}
    captured: list[Image.Image] = []

    def fake_recovery(image, image_path, meta_path, payload, animation_frames=None, frame_durations=None, animation_loop=0):
        captured.extend(animation_frames or [])
        for frame in animation_frames or []:
            frame.close()

    workspace._write_recovery = fake_recovery
    workspace._autosave()
    for future in workspace._recovery_futures.values():
        future.result(timeout=5)
    assert captured
    assert not original_ids.intersection(id(frame) for frame in captured)
    assert document.image.getpixel((0, 0))
    workspace.finalize_close()
    workspace.close()


def test_decoder_owned_images_are_released_only_when_explicitly_consumed():
    import pytest

    _app()
    workspace = EditorWorkspace()
    source = Image.new("RGBA", (14, 10), "purple")
    workspace.add_document(source, consume=True)
    with pytest.raises(ValueError, match="closed image"):
        source.getpixel((0, 0))
    assert workspace.document is not None
    assert workspace.document.image.getpixel((0, 0))[:3] == (128, 0, 128)
    workspace.finalize_close()
    workspace.close()


def test_animation_playback_reuses_timing_tables(monkeypatch):
    import time
    from imagesuite.models import ANIMATION_DURATIONS_KEY, ANIMATION_FRAMES_KEY

    _app()
    workspace = EditorWorkspace()
    frames = [Image.new("RGBA", (10, 8), color) for color in ("red", "green", "blue", "white")]
    image = frames[0].copy()
    image.info[ANIMATION_FRAMES_KEY] = frames
    image.info[ANIMATION_DURATIONS_KEY] = [40, 60, 80, 100]
    workspace.add_document(image)
    monkeypatch.setattr(workspace, "_show_animation_frame", lambda *args, **kwargs: None)
    workspace._gif_playing = True
    workspace._gif_playback_started = time.perf_counter()
    workspace._advance_gif_preview()
    first_table = workspace._gif_frame_ends
    workspace._advance_gif_preview()
    assert workspace._gif_frame_ends is first_table
    assert workspace._gif_frame_ends == [40, 100, 180, 280]
    workspace._stop_gif_preview()
    workspace.finalize_close()
    workspace.close()


def test_close_finalizes_workspace_even_when_never_shown():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (12, 10), "red"))
    workspace._effect_preview_timer.start()

    assert workspace.close()

    assert workspace._finalized
    assert not workspace._effect_preview_timer.isActive()
    assert not workspace.documents
    assert workspace.canvas.document is None
