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
