from pathlib import Path
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PySide6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from imagesuite.main_window import MainWindow
from imagesuite.editor.workspace import EditorWorkspace
from imagesuite.models import RectMask


def test_main_window_constructs(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    (tmp_path / "portable.flag").touch()
    window = MainWindow(tmp_path)
    assert window.stack.count() == 5
    assert [button.text() for button in window.nav_buttons] == ["Edit", "Enhance", "Organize"]
    window.close()


def test_editor_workspace_constructs():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    assert workspace.canvas is not None
    assert workspace.tool_tabs.count() == 4
    assert workspace.advanced_widgets
    assert all(widget.isHidden() for widget in workspace.advanced_widgets)
    workspace.close()


def test_simplified_secondary_workspaces(tmp_path: Path):
    from imagesuite.jobs import JobManager
    from imagesuite.similarity.workspace import SimilarityWorkspace
    from imagesuite.upscale.workspace import UpscaleWorkspace

    app = QApplication.instance() or QApplication([])
    upscale = UpscaleWorkspace(JobManager(), tmp_path / "models", tmp_path / "outputs")
    similarity = SimilarityWorkspace(JobManager())
    assert upscale.workflow_preset.currentText() == "Custom"
    assert similarity.scan_options.isHidden()
    assert similarity.log.isHidden()
    upscale.close()
    similarity.close()


def test_quick_text_preview_can_move_resize_and_apply():
    from PIL import Image

    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (640, 480), "white"), dirty=True)
    workspace.canvas.set_mode("text")
    workspace.quick_text_edit.setText("Drag me")
    workspace.quick_text_anchor = (320, 240)
    workspace.preview_quick_text()

    assert workspace.canvas.text_overlay_box is not None
    original_box = workspace.canvas.text_overlay_box.copy()
    original_size = workspace.text_size.value()

    workspace._text_transform_changed(360, 260, original_size + 20)
    assert workspace.quick_text_anchor == (360, 260)
    assert workspace.text_size.value() == original_size + 20
    assert workspace.canvas.text_overlay_box is not None
    assert workspace.canvas.text_overlay_box.width > original_box.width

    before = workspace.document.image.copy()
    workspace.apply_quick_text()
    assert workspace.canvas.text_overlay_box is None
    assert workspace.document.image.tobytes() != before.tobytes()
    workspace.close()


def test_effect_settings_live_preview_and_apply_correct_kind():
    from PIL import Image
    from PySide6.QtTest import QTest

    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = Image.new("RGBA", (80, 60), "white")
    workspace.add_document(source, dirty=False)
    workspace.document.selection = RectMask(10, 10, 40, 40)

    workspace.tool_tabs.setCurrentIndex(2)
    workspace.brightness.setValue(-50)
    QTest.qWait(120)
    assert workspace._preview_kind == "adjustments"

    workspace.tool_tabs.setCurrentIndex(0)
    workspace.effect_combo.setCurrentText("Black Redaction")
    QTest.qWait(140)
    assert workspace._preview_kind == "effect"
    workspace.apply_effect()
    assert workspace.document.image.getpixel((20, 20))[:3] == (0, 0, 0)
    assert workspace.document.image.getpixel((60, 50))[:3] == (255, 255, 255)
    workspace.close()


def test_exception_reports_cross_threads_through_qt(monkeypatch):
    import threading
    from PySide6.QtTest import QTest
    from imagesuite import diagnostics

    app = QApplication.instance() or QApplication([])
    reports: list[str] = []
    monkeypatch.setattr(diagnostics, "_show_exception_dialog", reports.append)
    bridge = diagnostics._ExceptionBridge(app)
    worker = threading.Thread(target=lambda: bridge.reportReady.emit("worker failure"))
    worker.start()
    worker.join()
    QTest.qWait(20)
    assert reports == ["worker failure"]


def test_upscale_workers_are_visible_and_support_fifty(tmp_path: Path):
    from imagesuite.jobs import JobManager
    from imagesuite.upscale.workspace import UpscaleWorkspace

    app = QApplication.instance() or QApplication([])
    workspace = UpscaleWorkspace(JobManager(), tmp_path / "models", tmp_path / "outputs")
    assert workspace.max_workers.maximum() == 50
    assert not workspace.max_workers.isHidden()
    workspace.max_workers.setValue(50)
    assert workspace.settings().max_workers == 50
    workspace.method.setCurrentText("AI model")
    assert not workspace.max_workers.isEnabled()
    workspace.close()


def test_text_watermark_built_in_and_custom_presets(tmp_path: Path):
    from imagesuite.jobs import JobManager
    from imagesuite.upscale.workspace import UpscaleWorkspace

    app = QApplication.instance() or QApplication([])
    workspace = UpscaleWorkspace(JobManager(), tmp_path / "models", tmp_path / "outputs")
    workspace.watermark_presets_path = tmp_path / "text_watermark_presets.json"

    workspace.watermark_preset.setCurrentText("Diagonal Proof")
    assert workspace.text_enable.isChecked()
    assert workspace.text_value.text() == "PROOF"
    assert workspace.text_rotation.value() == -28.0

    workspace.text_value.setText("My mark")
    workspace.font_size.setValue(77)
    workspace.custom_watermark_presets["My preset"] = workspace._current_text_watermark_preset()
    workspace._save_custom_watermark_presets()
    workspace._refresh_text_watermark_presets("My preset")
    workspace.apply_text_watermark_preset("My preset")
    assert workspace.text_value.text() == "My mark"
    assert workspace.font_size.value() == 77
    assert workspace.watermark_presets_path.exists()
    workspace.close()


def test_upscale_animation_export_controls_round_trip(tmp_path: Path):
    from imagesuite.jobs import JobManager
    from imagesuite.upscale.workspace import UpscaleWorkspace

    app = QApplication.instance() or QApplication([])
    workspace = UpscaleWorkspace(JobManager(), tmp_path / "models", tmp_path / "outputs")
    workspace.resize_advanced.setChecked(True)
    workspace.format.setCurrentText("MP4")
    workspace.animation_fps.setValue(24)
    workspace.video_bitrate.setValue(2500)
    settings = workspace.settings()
    assert settings.output_format == "MP4"
    assert settings.animation_fps == 24
    assert settings.video_bitrate_kbps == 2500
    workspace.format.setCurrentText("GIF")
    workspace.gif_colors.setValue(64)
    workspace.gif_dither.setChecked(False)
    settings = workspace.settings()
    assert settings.gif_colors == 64
    assert not settings.gif_dither
    workspace.close()


def test_animation_scrubber_and_loop_bounds_work():
    from imagesuite.utils import read_image, save_animation
    from PIL import Image
    import tempfile

    app = QApplication.instance() or QApplication([])
    frames = [Image.new("RGBA", (40, 24), color) for color in ("red", "green", "blue", "yellow")]
    durations = [50, 50, 50, 50]
    path = Path(tempfile.gettempdir()) / "imagesuite_test_loop.gif"
    save_animation(frames, durations, path)
    workspace = EditorWorkspace()
    workspace.add_document(read_image(path), path, dirty=False)
    workspace.anim_loop_start.setValue(2)
    workspace.anim_loop_end.setValue(3)
    workspace.jump_animation_start()
    assert workspace._gif_preview_index == 1
    workspace.step_animation_frame(1)
    assert workspace._gif_preview_index == 2
    workspace.step_animation_frame(1)
    assert workspace._gif_preview_index == 1
    workspace.close()
    path.unlink(missing_ok=True)


def test_ai_profiles_and_controls_are_visible_in_ai_mode(tmp_path: Path):
    from imagesuite.jobs import JobManager
    from imagesuite.upscale.workspace import UpscaleWorkspace

    app = QApplication.instance() or QApplication([])
    workspace = UpscaleWorkspace(JobManager(), tmp_path / "models", tmp_path / "outputs")
    workspace.method.setCurrentText("AI model")
    workspace.ai_profile.setCurrentText("Low memory")
    assert not workspace.ai_profile.isHidden()
    assert workspace.tile.value() == 192
    assert workspace.ai_preview_size.value() == 384
    assert workspace.ai_oom_recovery.isChecked()
    settings = workspace.settings()
    assert settings.ai_precision == "Auto"
    assert settings.ai_preview_max_side == 384
    workspace.close()


def test_ai_install_is_available_inside_enhance(tmp_path: Path):
    from imagesuite.jobs import JobManager
    from imagesuite.upscale.workspace import UpscaleWorkspace

    app = QApplication.instance() or QApplication([])
    launcher = tmp_path / "ImageSuite.bat"
    launcher.write_text("@echo off\n", encoding="utf-8")
    workspace = UpscaleWorkspace(JobManager(), tmp_path / "models", tmp_path / "outputs", tmp_path)
    assert workspace.install_ai_button.text() == "Install / Repair AI"
    assert workspace.base_dir == tmp_path
    workspace.close()


def test_fast_resize_preset_uses_the_true_no_finishing_path(tmp_path: Path):
    from imagesuite.jobs import JobManager
    from imagesuite.upscale.workspace import UpscaleWorkspace

    app = QApplication.instance() or QApplication([])
    workspace = UpscaleWorkspace(JobManager(), tmp_path / "models", tmp_path / "outputs")
    workspace.apply_workflow_preset("Fast resize")
    settings = workspace.settings()
    assert settings.scale_factor == 2.0
    assert settings.method == "Bicubic"
    assert settings.output_format == "JPEG"
    assert settings.sharpen == 0
    assert settings.denoise == 0
    assert settings.contrast == 1
    assert settings.brightness == 1
    assert settings.saturation == 1
    workspace.close()


def test_target_edge_controls_offer_hard_soft_and_custom_modes():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    assert workspace.target_edge_preset.currentText() == "Soft transition"
    assert workspace.target_feather_slider.value() == 16
    assert workspace.target_padding_slider.value() == 8

    workspace.target_edge_preset.setCurrentText("Hard edge")
    assert workspace.target_feather_slider.value() == 0
    assert workspace.target_padding_slider.value() == 0

    workspace.target_feather_slider.setValue(23)
    assert workspace.target_edge_preset.currentText() == "Custom"
    workspace.close()


def test_ascii_effect_exposes_contour_control():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.effect_combo.setCurrentText("ASCII Art")
    assert not workspace.effect_parameter_rows["edge"].isHidden()
    assert workspace.effect_parameter_labels["edge"].text() == "Contour strength"
    assert workspace.effect_parameter_sliders["edge"].value() == 76
    workspace.close()
