from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication

from imagesuite.editor.workspace import EditorWorkspace


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _workspace() -> EditorWorkspace:
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (640, 480), "white"), dirty=False)
    return workspace


def test_character_spacing_and_rotation_change_text_bounds():
    workspace = _workspace()
    workspace.choose_mode("text")
    workspace.quick_text_edit.setText("WIDE\nTEXT")
    workspace.quick_text_anchor = (320, 240)
    workspace.text_character_spacing.setValue(0)
    workspace.text_rotation.setValue(0)
    first = workspace._render_text_layer()
    assert first is not None
    first_layer, first_size = first

    workspace.text_character_spacing.setValue(18)
    workspace.text_rotation.setValue(30)
    second = workspace._render_text_layer()
    assert second is not None
    second_layer, second_size = second

    assert second_size != first_size
    assert second_size[0] > first_size[0]
    first_layer.close()
    second_layer.close()
    workspace.close()


def test_extended_text_state_reopens_after_apply():
    workspace = _workspace()
    workspace.choose_mode("text")
    workspace.quick_text_edit.setText("Editable\nText")
    workspace.quick_text_anchor = (300, 220)
    workspace.text_rotation.setValue(-18)
    workspace.text_opacity.setValue(72)
    workspace.text_bold_check.setChecked(True)
    workspace.text_italic_check.setChecked(True)
    workspace.text_character_spacing.setValue(6)
    workspace.text_line_spacing.setValue(19)
    workspace.text_shadow_x.setValue(9)
    workspace.text_shadow_y.setValue(-4)
    workspace.text_shadow_blur.setValue(8)
    workspace.text_corner_radius.setValue(17)

    workspace.apply_quick_text()
    workspace.reedit_last_text()

    assert workspace.quick_text_edit.text() == "Editable\nText"
    assert workspace.text_rotation.value() == -18
    assert workspace.text_opacity.value() == 72
    assert workspace.text_bold_check.isChecked()
    assert workspace.text_italic_check.isChecked()
    assert workspace.text_character_spacing.value() == 6
    assert workspace.text_line_spacing.value() == 19
    assert workspace.text_shadow_x.value() == 9
    assert workspace.text_shadow_y.value() == -4
    assert workspace.text_shadow_blur.value() == 8
    assert workspace.text_corner_radius.value() == 17
    assert workspace.canvas.preview_image is not None
    workspace.close()


def test_sticker_preview_can_move_resize_apply_and_reedit():
    workspace = _workspace()
    before = workspace.document.image.copy()
    workspace.choose_mode("sticker")
    workspace.sticker_combo.setCurrentText("★")
    workspace.sticker_anchor = (200, 160)
    workspace.sticker_size.setValue(110)
    workspace.sticker_rotation.setValue(24)
    workspace.sticker_opacity.setValue(80)
    workspace.preview_sticker()

    assert workspace.canvas.preview_image is not None
    assert workspace.canvas.text_overlay_box is not None

    workspace._text_transform_changed(360, 280, 145)
    assert workspace.sticker_anchor == (360, 280)
    assert workspace.sticker_size.value() == 145

    workspace.apply_sticker()
    assert workspace.document.image.tobytes() != before.tobytes()

    workspace.reedit_last_sticker()
    assert workspace.sticker_anchor == (360, 280)
    assert workspace.sticker_size.value() == 145
    assert workspace.sticker_rotation.value() == 24
    assert workspace.sticker_opacity.value() == 80
    assert workspace.canvas.preview_image is not None
    before.close()
    workspace.close()


def test_sticker_opacity_applies_to_entire_layer():
    workspace = _workspace()
    workspace.choose_mode("sticker")
    workspace.sticker_combo.setCurrentText("★")
    workspace.sticker_opacity.setValue(35)
    workspace.sticker_shadow_check.setChecked(True)
    workspace.sticker_outline_check.setChecked(True)
    rendered = workspace._render_sticker_layer()
    assert rendered is not None
    layer, _size = rendered
    assert layer.getchannel("A").getextrema()[1] <= 90
    layer.close()
    workspace.close()


def test_multiline_text_applies_to_every_animation_frame(tmp_path):
    from imagesuite.utils import read_image

    _app()
    source = tmp_path / "animated.gif"
    frames = [Image.new("RGB", (160, 100), color) for color in ("navy", "purple")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=[100, 100], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source), source, dirty=False)
    workspace.choose_mode("text")
    workspace.quick_text_edit.setText("A\nB")
    workspace.quick_text_anchor = (80, 50)
    originals = [frame.tobytes() for frame in workspace.document.animation_frames]
    workspace.apply_quick_text()

    assert workspace.document.is_animated
    assert all(frame.tobytes() != original for frame, original in zip(workspace.document.animation_frames, originals))
    workspace.close()


def test_text_wrap_width_creates_multiple_visual_lines():
    workspace = _workspace()
    workspace.choose_mode("text")
    workspace.quick_text_edit.setText("This is a long line that should wrap into several visual rows")
    workspace.text_wrap_width.setValue(180)
    layout = workspace._text_layout(workspace.quick_text_edit.text(), workspace.text_size.value(), 1.0)
    assert len(layout["lines"]) >= 2
    assert max(layout["widths"]) <= 185
    workspace.close()


def test_transparent_image_sticker_can_preview_and_apply(tmp_path):
    workspace = _workspace()
    sticker_path = tmp_path / "sticker.png"
    sticker = Image.new("RGBA", (100, 60), (0, 0, 0, 0))
    for x in range(20, 80):
        for y in range(10, 50):
            sticker.putpixel((x, y), (255, 0, 0, 220))
    sticker.save(sticker_path)
    sticker.close()

    workspace.choose_mode("sticker")
    workspace.sticker_image_path = str(sticker_path)
    workspace.sticker_source_label.setText(sticker_path.name)
    workspace.sticker_anchor = (320, 240)
    workspace.sticker_size.setValue(140)
    workspace.sticker_rotation.setValue(-15)
    workspace.preview_sticker()
    assert workspace.canvas.preview_image is not None
    before = workspace.document.image.tobytes()
    workspace.apply_sticker()
    assert workspace.document.image.tobytes() != before
    workspace.close()


def test_switching_sticker_category_returns_to_symbol_mode(tmp_path):
    workspace = _workspace()
    sticker_path = tmp_path / "sticker.png"
    Image.new("RGBA", (32, 32), "red").save(sticker_path)
    workspace.sticker_image_path = str(sticker_path)
    workspace.sticker_source_label.setText(sticker_path.name)
    workspace.sticker_category.setCurrentText("Marks")
    assert workspace.sticker_image_path == ""
    assert workspace.sticker_source_label.text() == "Emoji / symbol"
    workspace.close()
