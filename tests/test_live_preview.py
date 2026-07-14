from __future__ import annotations

import time

from PIL import Image
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from imagesuite.editor import effects
from imagesuite.editor.workspace import EditorWorkspace
from imagesuite.models import RectMask


def _checker(width: int = 160, height: int = 120) -> Image.Image:
    image = Image.new("RGBA", (width, height), "white")
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            if (x // 4 + y // 4) % 2:
                pixels[x, y] = (0, 0, 0, 255)
    return image


def test_effect_preview_updates_during_continuous_slider_changes():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.effect_combo.setCurrentText("Pixelate")
    QTest.qWait(80)
    workspace.cancel_preview(False)

    preview_seen_before_drag_finished = False
    values = [4, 8, 12, 20, 28, 36]
    for index, value in enumerate(values):
        workspace.pixel_slider.setValue(value)
        QTest.qWait(20)
        if index < len(values) - 1 and workspace.canvas.preview_image is not None:
            preview_seen_before_drag_finished = True

    assert preview_seen_before_drag_finished
    assert workspace._preview_kind == "effect"
    workspace.close()


def test_apply_effect_uses_latest_value_even_with_pending_preview():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = _checker(96, 72)
    workspace.add_document(source, dirty=False)
    workspace.effect_combo.setCurrentText("Pixelate")
    workspace.pixel_slider.setValue(4)
    workspace.preview_effect()

    workspace.pixel_slider.setValue(32)
    workspace.apply_effect()

    expected = effects.pixelate(source, 32).convert("RGBA")
    assert workspace.document.image.tobytes() == expected.tobytes()
    workspace.close()


def test_last_requested_preview_wins_and_before_is_disabled():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.before_check.setChecked(True)

    workspace.brightness.setValue(-40)
    QTest.qWait(10)
    workspace.effect_combo.setCurrentText("Black Redaction")
    QTest.qWait(100)

    assert not workspace.before_check.isChecked()
    assert not workspace.canvas.show_original
    assert workspace._preview_kind == "effect"
    assert workspace.canvas.preview_image.getpixel((20, 20))[:3] == (0, 0, 0)
    workspace.close()


def test_large_live_preview_is_bounded_but_apply_remains_full_resolution():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = _checker(2000, 1200)
    workspace.add_document(source, dirty=False)
    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.preview_effect()

    preview = workspace.canvas.preview_image
    assert preview is not None
    assert preview.width * preview.height <= workspace.LIVE_PREVIEW_MAX_PIXELS + 3000
    assert preview.size != source.size

    workspace.apply_effect()
    assert workspace.document.image.size == source.size
    assert workspace.document.image.getpixel((1000, 600))[:3] == (0, 0, 0)
    workspace.close()


def test_scaled_preview_respects_rectangle_target():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = Image.new("RGBA", (2000, 1200), "white")
    workspace.add_document(source, dirty=False)
    workspace.document.selection = RectMask(400, 300, 1200, 900)
    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.preview_effect()

    preview = workspace.canvas.preview_image
    assert preview is not None
    sx = preview.width / source.width
    sy = preview.height / source.height
    assert preview.getpixel((round(800 * sx), round(600 * sy)))[:3] == (0, 0, 0)
    assert preview.getpixel((round(100 * sx), round(100 * sy)))[:3] == (255, 255, 255)
    workspace.close()


def test_empty_quick_text_clears_stale_overlay_and_preview_kind():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.choose_mode("text")
    workspace.quick_text_edit.setText("Visible")
    workspace.preview_quick_text()
    assert workspace.canvas.text_overlay_box is not None

    workspace.quick_text_edit.setText("")
    workspace.preview_quick_text()
    assert workspace.canvas.preview_image is None
    assert workspace.canvas.text_overlay_box is None
    assert workspace._preview_kind is None
    workspace.close()


def test_incomplete_lasso_targets_nothing_instead_of_crashing():
    source = _checker(40, 30)
    for points in [[(5, 5)], [(5, 5), (10, 10)]]:
        result = effects.apply_to_target(source, lambda image: Image.new("RGBA", image.size, "black"), None, points)
        assert result.tobytes() == source.tobytes()


def test_zero_size_selection_does_not_fall_back_to_whole_image():
    source = _checker(40, 30)
    result = effects.apply_to_target(
        source,
        lambda image: Image.new("RGBA", image.size, "black"),
        RectMask(10, 10, 10, 20),
        [],
    )
    assert result.tobytes() == source.tobytes()


def test_sketch_effect_uses_supported_pillow_operations():
    source = Image.effect_noise((80, 60), 80).convert("RGBA")
    result = effects.sketch(source, 40)
    assert result.mode == "RGBA"
    assert result.size == source.size
    assert result.tobytes() != source.tobytes()


def test_leaving_text_tab_allows_effect_preview_immediately():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.choose_mode("text")
    workspace.quick_text_edit.setText("Text")
    workspace.preview_quick_text()
    assert workspace.canvas.mode == "text"

    workspace.tool_tabs.setCurrentIndex(0)
    workspace.effect_combo.setCurrentText("Black Redaction")
    for _ in range(20):
        if workspace._preview_kind == "effect":
            break
        QTest.qWait(25)

    assert workspace.canvas.mode == "select"
    assert workspace._preview_kind == "effect"
    assert workspace.canvas.preview_image is not None
    workspace.close()


def test_face_mode_previews_and_applies_outside_protected_circles():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = Image.new("RGBA", (100, 80), "white")
    workspace.add_document(source, dirty=False)
    workspace.document.face_masks = [RectMask(30, 20, 70, 60)]
    workspace.choose_mode("face")
    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.preview_effect()

    preview = workspace.canvas.preview_image
    assert preview is not None
    assert preview.getpixel((50, 40))[:3] == (255, 255, 255)
    assert preview.getpixel((5, 5))[:3] == (0, 0, 0)

    workspace.apply_effect()
    assert workspace.document.image.getpixel((50, 40))[:3] == (255, 255, 255)
    assert workspace.document.image.getpixel((5, 5))[:3] == (0, 0, 0)
    workspace.close()


def test_creative_effects_preserve_transparency():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = Image.new("RGBA", (40, 30), (120, 80, 40, 0))
    source.putpixel((20, 15), (120, 80, 40, 128))
    workspace.add_document(source, dirty=False)

    for name in ["Grayscale", "Invert", "Vignette", "Glow", "Posterize", "Sketch", "Cinematic"]:
        workspace.creative_combo.setCurrentText(name)
        result = workspace._creative_transform()(source)
        assert result.getchannel("A").tobytes() == source.getchannel("A").tobytes(), name
    workspace.close()


def test_mosaic_grid_does_not_create_opacity():
    source = Image.new("RGBA", (64, 48), (120, 80, 40, 0))
    result = effects.mosaic(source, 16)
    assert result.getchannel("A").getbbox() is None


def test_switching_tabs_cancels_pending_preview_before_it_renders():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.effect_combo.setCurrentText("Black Redaction")
    assert workspace._effect_preview_timer.isActive()

    workspace.tool_tabs.setCurrentIndex(2)
    QTest.qWait(100)

    assert not workspace._effect_preview_timer.isActive()
    assert workspace.canvas.preview_image is None
    assert workspace._preview_kind is None
    assert workspace._requested_preview_kind is None
    workspace.close()


def test_live_preview_source_cache_reuses_only_current_revision():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(1800, 1000), dirty=False)

    first = workspace._live_preview_source()
    second = workspace._live_preview_source()
    assert first is second

    workspace.document.commit(Image.new("RGBA", workspace.document.image.size, "black"))
    third = workspace._live_preview_source()
    assert third is not first
    assert third[0].getpixel((0, 0))[:3] == (0, 0, 0)
    workspace.close()


def test_starting_lasso_replaces_rectangle_target():
    from PySide6.QtCore import QPoint, Qt

    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.resize(900, 700)
    workspace.show()
    workspace.add_document(_checker(200, 140), dirty=False)
    workspace.document.selection = RectMask(10, 10, 80, 80)
    workspace.choose_mode("lasso")
    QTest.qWait(20)

    QTest.mousePress(workspace.canvas, Qt.LeftButton, pos=QPoint(450, 350))
    assert workspace.document.selection is None
    assert len(workspace.document.lasso_points) == 1
    QTest.mouseRelease(workspace.canvas, Qt.LeftButton, pos=QPoint(450, 350))
    workspace.close()


def test_quick_text_font_cache_is_bounded_and_reuses_fonts():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    first = workspace._font(48)
    assert workspace._font(48) is first
    for size in range(1, 80):
        workspace._font(size)
    assert len(workspace._font_cache) <= 64
    workspace.close()


def test_brush_settings_do_not_trigger_full_image_preview():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.preview_effect()
    assert workspace.canvas.preview_image is not None

    workspace.choose_mode("brush_pixel")
    assert workspace.tool_tabs.currentIndex() == 0
    assert workspace.effect_combo.currentText() == "Pixelate"
    assert workspace.pixel_slider.isEnabled()
    assert workspace.canvas.preview_image is None

    workspace.pixel_slider.setValue(40)
    QTest.qWait(100)
    assert workspace.canvas.preview_image is None
    assert not workspace._effect_preview_timer.isActive()
    workspace.close()


def test_annotation_shortcuts_route_to_annotation_panel():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    for mode in ["text", "sticker", "arrow", "box"]:
        workspace.choose_mode(mode)
        assert workspace.tool_tabs.currentIndex() == 1
    workspace.close()


def test_changing_effect_while_brushing_returns_to_area_selection():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.choose_mode("brush_pixel")
    assert workspace.canvas.mode == "brush_pixel"

    workspace.effect_combo.setCurrentText("Soft Blur")
    assert workspace.canvas.mode == "select"
    for _ in range(12):
        if workspace._preview_kind == "effect":
            break
        QTest.qWait(20)
    assert workspace._preview_kind == "effect"
    workspace.close()


def test_switching_to_brush_cancels_pending_area_preview():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.effect_combo.setCurrentText("Soft Blur")
    workspace.blur_slider.setValue(workspace.blur_slider.value() + 1)
    assert workspace._effect_preview_timer.isActive()
    assert workspace._requested_preview_kind == "effect"

    workspace.choose_mode("brush_pixel")

    assert not workspace._effect_preview_timer.isActive()
    assert workspace._requested_preview_kind is None
    assert workspace._preview_kind is None
    assert workspace.canvas.preview_image is None
    assert workspace.effect_combo.currentText() == "Pixelate"
    workspace.close()


def test_first_rectangle_target_starts_effect_preview_without_slider_change():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.document.selection = RectMask(20, 20, 80, 70)

    workspace._document_changed()
    for _ in range(12):
        if workspace._preview_kind == "effect":
            break
        QTest.qWait(20)

    assert workspace._preview_kind == "effect"
    assert workspace.canvas.preview_image is not None
    workspace.close()


def test_task_tabs_replace_incompatible_hidden_tools():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)

    workspace.choose_mode("brush_blur")
    workspace.tool_tabs.setCurrentIndex(2)
    assert workspace.tool_tabs.currentIndex() == 2
    assert workspace.canvas.mode == "select"

    workspace.tool_tabs.setCurrentIndex(1)
    assert workspace.tool_tabs.currentIndex() == 1
    assert workspace.canvas.mode == "text"

    workspace.tool_tabs.setCurrentIndex(3)
    assert workspace.tool_tabs.currentIndex() == 3
    assert workspace.canvas.mode == "select"
    workspace.close()


def test_scaled_glow_preview_uses_scaled_blur_radius(monkeypatch):
    seen = []
    original = effects.glow

    def capture(image, strength, *, scale=1.0):
        seen.append(scale)
        return original(image, strength, scale=scale)

    monkeypatch.setattr(effects, "glow", capture)
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(2000, 1200), dirty=False)
    workspace.tool_tabs.setCurrentIndex(2)
    workspace.creative_combo.setCurrentText("Glow")
    workspace.preview_selected_creative()

    assert seen and 0 < seen[-1] < 1
    workspace.close()


def test_preview_callbacks_cannot_render_on_the_wrong_task_tab():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)

    workspace.tool_tabs.setCurrentIndex(3)
    workspace._requested_preview_kind = "effect"
    workspace.preview_effect()
    assert workspace.canvas.preview_image is None

    workspace._requested_preview_kind = "adjustments"
    workspace.preview_adjustments()
    assert workspace.canvas.preview_image is None

    workspace._requested_preview_kind = "creative"
    workspace.preview_selected_creative()
    assert workspace.canvas.preview_image is None
    workspace.close()


def test_switching_effect_target_mode_refreshes_target_semantics():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = _checker()
    workspace.add_document(source, dirty=False)
    workspace.document.selection = RectMask(10, 10, 80, 70)
    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.preview_effect()
    assert workspace.canvas.preview_image.getpixel((20, 20))[:3] == (0, 0, 0)

    workspace.choose_mode("face")
    for _ in range(12):
        QTest.qWait(20)
        if workspace.canvas.preview_image and workspace.canvas.preview_image.tobytes() == source.tobytes():
            break
    assert workspace.canvas.preview_image.tobytes() == source.tobytes()
    workspace.close()


def test_small_live_previews_match_the_exact_applied_output():
    app = QApplication.instance() or QApplication([])
    source = _checker(96, 72)
    selection = RectMask(10, 10, 70, 55)

    for name in ["Blur", "Pixelate", "Mosaic", "Black"]:
        workspace = EditorWorkspace()
        workspace.add_document(source, dirty=False)
        workspace.document.selection = selection.copy()
        workspace.effect_combo.setCurrentText(name)
        workspace.preview_effect()
        preview = workspace.canvas.preview_image.copy()
        workspace.apply_effect()
        assert preview.tobytes() == workspace.document.image.tobytes(), name
        workspace.close()

    workspace = EditorWorkspace()
    workspace.add_document(source, dirty=False)
    workspace.document.selection = selection.copy()
    workspace.tool_tabs.setCurrentIndex(2)
    for slider, value in zip(
        (workspace.brightness, workspace.contrast, workspace.saturation, workspace.sharpness),
        (-30, 10, 20, 40),
    ):
        slider.setValue(value)
    workspace.preview_adjustments()
    preview = workspace.canvas.preview_image.copy()
    workspace.apply_adjustments()
    assert preview.tobytes() == workspace.document.image.tobytes()
    workspace.close()

    for name in ["Auto enhance", "Grayscale", "Invert", "Vignette", "Glow", "Posterize", "Sketch", "Cinematic"]:
        workspace = EditorWorkspace()
        workspace.add_document(source, dirty=False)
        workspace.document.selection = selection.copy()
        workspace.tool_tabs.setCurrentIndex(2)
        workspace.creative_combo.setCurrentText(name)
        workspace.creative_strength.setValue(63)
        workspace.preview_selected_creative()
        preview = workspace.canvas.preview_image.copy()
        workspace.apply_selected_creative()
        assert preview.tobytes() == workspace.document.image.tobytes(), name
        workspace.close()


def test_expensive_live_previews_use_lower_bounded_sources():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(2400, 1600), dirty=False)
    workspace.tool_tabs.setCurrentIndex(2)

    workspace.brightness.setValue(30)
    workspace.preview_adjustments()
    assert workspace.canvas.preview_image.width * workspace.canvas.preview_image.height <= workspace.LIVE_ADJUSTMENT_PREVIEW_MAX_PIXELS + 4000

    workspace.creative_combo.setCurrentText("Cinematic")
    workspace.preview_selected_creative()
    assert workspace.canvas.preview_image.width * workspace.canvas.preview_image.height <= workspace.LIVE_CREATIVE_PREVIEW_MAX_PIXELS + 4000
    workspace.close()


def test_scaled_zero_size_selection_remains_empty():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = _checker(2000, 1200)
    workspace.add_document(source, dirty=False)
    workspace.document.selection = RectMask(400, 300, 400, 300)
    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.preview_effect()

    preview_source, _sx, _sy = workspace._live_preview_source()
    assert workspace.canvas.preview_image.tobytes() == preview_source.tobytes()
    workspace.close()


def test_real_mouse_rectangle_updates_effect_before_release():
    from PySide6.QtCore import QPoint, Qt

    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.resize(1100, 760)
    workspace.show()
    workspace.add_document(_checker(800, 600), dirty=False)
    QTest.qWait(40)
    canvas = workspace.canvas
    rect = canvas._image_rect
    start = QPoint(round(rect.left() + rect.width() * 0.2), round(rect.top() + rect.height() * 0.2))
    end = QPoint(round(rect.left() + rect.width() * 0.7), round(rect.top() + rect.height() * 0.7))

    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, start)
    for step in range(1, 9):
        point = QPoint(
            start.x() + (end.x() - start.x()) * step // 8,
            start.y() + (end.y() - start.y()) * step // 8,
        )
        QTest.mouseMove(canvas, point, delay=10)
        QApplication.processEvents()
    QTest.qWait(80)

    assert canvas.dragging
    assert workspace._preview_kind == "effect"
    assert canvas.preview_image is not None
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, end)
    workspace.close()


def test_censor_preset_loads_chain_and_live_preview_uses_it():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.effect_preset_combo.setCurrentText("Maximum privacy")
    QTest.qWait(120)

    assert workspace._effect_chain_names() == ["Privacy Blur", "Noise Redaction", "Marker Scribble"]
    assert workspace.canvas.preview_image is not None
    assert workspace._preview_kind == "effect"
    workspace.close()


def test_chain_order_is_used_when_applying_effects():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (40, 30), "red"), dirty=False)
    workspace.effect_chain_list.addItems(["Black Redaction", "White Redaction"])
    workspace.apply_effect()

    assert workspace.document.image.getpixel((10, 10))[:3] == (255, 255, 255)
    workspace.close()


def test_effect_selector_exposes_curated_censor_set():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    names = {workspace.effect_combo.itemText(index) for index in range(workspace.effect_combo.count())}
    assert names == set(workspace.CENSOR_EFFECTS)
    assert len(names) == 28
    assert {"Privacy Blur", "Directional Blur", "Faceted Glass", "Encrypted Tiles", "Barcode Redaction", "Photocopy"} <= names
    assert {"Wave", "Shred", "Checkerboard", "Emboss"}.isdisjoint(names)
    workspace.close()


def test_every_censor_selector_entry_builds_a_transform():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = _checker(64, 48)
    workspace.add_document(source, dirty=False)
    for index in range(workspace.effect_combo.count()):
        name = workspace.effect_combo.itemText(index)
        result = workspace._effect_transform(name)(source)
        assert result.mode == "RGBA", name
        assert result.size == source.size, name
    workspace.close()


def test_all_censor_presets_reference_known_effects_and_tuned_specs():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    known = {workspace.effect_combo.itemText(index) for index in range(workspace.effect_combo.count())}
    assert len(workspace.EFFECT_PRESETS) == 19
    for preset, chain in workspace.EFFECT_PRESETS.items():
        assert chain, preset
        assert len(chain) <= 6, preset
        for spec in chain:
            assert spec["name"] in known, (preset, spec)
            normalized = workspace._normalize_effect_spec(spec)
            for key, definition in workspace.EFFECT_PARAMETERS[spec["name"]].items():
                assert int(definition[1]) <= int(normalized[key]) <= int(definition[2]), (preset, key, normalized)
    workspace.close()


def test_chain_items_keep_independent_settings():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)

    workspace.effect_combo.setCurrentText("Glitch Blocks")
    workspace.effect_strength_slider.setValue(82)
    workspace.effect_pattern_slider.setValue(17)
    workspace._add_current_effect_to_chain()

    workspace.effect_combo.setCurrentText("Frosted Glass")
    workspace.effect_strength_slider.setValue(48)
    workspace._add_current_effect_to_chain()

    specs = workspace._effect_chain_specs()
    assert specs[0]["name"] == "Glitch Blocks"
    assert specs[0]["strength"] == 82
    assert specs[0]["pattern"] == 17
    assert specs[1]["name"] == "Frosted Glass"
    assert specs[1]["strength"] == 48

    workspace.effect_chain_list.setCurrentRow(0)
    assert workspace.effect_strength_slider.value() == 82
    assert workspace.effect_pattern_slider.value() == 17
    workspace.close()


def test_effect_chain_applies_outside_face_masks():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (80, 60), "red"), dirty=False)
    workspace.document.face_masks = [RectMask(20, 15, 60, 45)]
    workspace.effect_chain_list.addItems(["Black Redaction", "White Redaction"])
    workspace.choose_mode("face")
    workspace.apply_effect()

    assert workspace.document.image.getpixel((40, 30))[:3] == (255, 0, 0)
    assert workspace.document.image.getpixel((5, 5))[:3] == (255, 255, 255)
    workspace.close()


def test_editing_one_chain_item_does_not_change_siblings():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    first = workspace._make_effect_item({"name": "Glitch Blocks", "strength": 30, "pattern": 8})
    second = workspace._make_effect_item({"name": "Glitch Blocks", "strength": 90, "pattern": 22})
    workspace.effect_chain_list.addItem(first)
    workspace.effect_chain_list.addItem(second)

    workspace.effect_chain_list.setCurrentItem(first)
    workspace.effect_strength_slider.setValue(55)
    specs = workspace._effect_chain_specs()
    assert specs[0]["strength"] == 55
    assert specs[1]["strength"] == 90
    assert specs[1]["pattern"] == 22
    workspace.close()


def test_dynamic_effect_controls_never_leave_visible_dead_sliders():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    for name in workspace.CENSOR_EFFECTS:
        workspace.effect_combo.setCurrentText(name)
        definitions = workspace.EFFECT_PARAMETERS[name]
        for key in workspace.EFFECT_PARAMETER_KEYS:
            row = workspace.effect_parameter_rows[key]
            slider = workspace.effect_parameter_sliders[key]
            expected_visible = key in definitions
            if name in {"Black Redaction", "White Redaction"} and key in {"size", "angle"}:
                expected_visible = False
            assert row.isVisibleTo(workspace) == expected_visible, (name, key)
            if expected_visible:
                assert slider.isEnabled(), (name, key)
                assert workspace.effect_parameter_labels[key].text() == definitions[key][0]
    workspace.close()


def test_every_visible_effect_parameter_changes_output():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    width, height = 96, 72
    source = Image.new("RGBA", (width, height))
    pixels = source.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (x * 255 // (width - 1), y * 255 // (height - 1), (x * 7 + y * 11) % 256, 255)
    workspace.add_document(source, dirty=False)

    for name in workspace.CENSOR_EFFECTS:
        base = workspace._default_effect_spec(name)
        for key, definition in workspace.EFFECT_PARAMETERS[name].items():
            parameter_base = dict(base)
            if name in {"Black Redaction", "White Redaction"} and key in {"size", "angle"}:
                parameter_base["softness"] = 45
            baseline = workspace._effect_transform(parameter_base)(source).tobytes()
            _label, minimum, maximum, default, _suffix = definition
            candidates = [minimum, maximum, (minimum + maximum) // 2]
            assert any(
                candidate != default
                and workspace._effect_transform({**parameter_base, key: candidate})(source).tobytes() != baseline
                for candidate in candidates
            ), (name, key)
    workspace.close()


def test_chain_item_retains_all_dynamic_parameters():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.add_document(_checker(), dirty=False)
    workspace.effect_combo.setCurrentText("Prism Split")
    values = {"amount": 74, "size": 19, "softness": 26, "detail": 63, "angle": -31}
    for key, value in values.items():
        workspace.effect_parameter_sliders[key].setValue(value)
    workspace._add_current_effect_to_chain()

    workspace.effect_combo.setCurrentText("Photocopy")
    workspace._add_current_effect_to_chain()
    workspace.effect_chain_list.setCurrentRow(0)

    spec = workspace._effect_chain_specs()[0]
    for key, value in values.items():
        assert spec[key] == value
        assert workspace.effect_parameter_sliders[key].value() == value
    workspace.close()


def test_preview_and_apply_match_for_every_dynamic_censor_effect():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    source = _checker(72, 54)
    workspace.add_document(source, dirty=False)
    for name in workspace.CENSOR_EFFECTS:
        workspace.effect_chain_list.clear()
        workspace._load_effect_spec_controls(workspace._default_effect_spec(name))
        workspace.cancel_preview(False)
        workspace.preview_effect()
        expected = workspace._effect_transform(workspace._default_effect_spec(name))(source).convert("RGBA")
        assert workspace.canvas.preview_image.tobytes() == expected.tobytes(), name
        workspace.apply_effect()
        assert workspace.document.image.tobytes() == expected.tobytes(), name
        workspace.document.image = source.copy()
        workspace.document.undo_stack.clear()
        workspace.document.redo_stack.clear()
        workspace.cancel_preview(False)
    workspace.close()


def test_solid_redaction_reveals_texture_controls_only_when_useful():
    app = QApplication.instance() or QApplication([])
    workspace = EditorWorkspace()
    workspace.effect_combo.setCurrentText("Black Redaction")
    assert not workspace.effect_parameter_rows["size"].isVisibleTo(workspace)
    assert not workspace.effect_parameter_rows["angle"].isVisibleTo(workspace)

    workspace.effect_parameter_sliders["softness"].setValue(30)
    assert workspace.effect_parameter_rows["size"].isVisibleTo(workspace)
    assert workspace.effect_parameter_rows["angle"].isVisibleTo(workspace)
    workspace.close()


def test_animated_gif_effect_applies_to_every_frame_and_can_preview(tmp_path):
    from imagesuite.utils import read_image

    app = QApplication.instance() or QApplication([])
    source_path = tmp_path / "animation.gif"
    frames = [Image.new("RGB", (48, 32), color) for color in ("red", "green", "blue")]
    frames[0].save(source_path, save_all=True, append_images=frames[1:], duration=[40, 50, 60], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source_path), source_path, dirty=False)
    assert workspace.document is not None and workspace.document.is_animated
    assert not workspace.tool_buttons["brush_blur"].isEnabled()

    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.toggle_gif_preview()
    QTest.qWait(45)
    assert workspace._gif_playing
    assert workspace._preview_kind == "effect"
    assert workspace.canvas.preview_image is not None
    assert workspace.canvas.preview_image.getpixel((10, 10))[:3] == (0, 0, 0)

    workspace.effect_combo.setCurrentText("White Redaction")
    QTest.qWait(90)
    assert workspace._gif_playing
    assert workspace._gif_preview_timer.isActive()
    assert workspace.canvas.preview_image.getpixel((10, 10))[:3] == (255, 255, 255)
    workspace.toggle_gif_preview()
    assert not workspace._gif_playing

    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.apply_effect()
    assert all(frame.getpixel((10, 10))[:3] == (0, 0, 0) for frame in workspace.document.animation_frames)
    assert workspace.document.undo()
    assert workspace.document.animation_frames[0].getpixel((10, 10))[:3] == (255, 0, 0)
    assert workspace.document.animation_frames[1].getpixel((10, 10))[:3] == (0, 128, 0)
    workspace.close()


def test_animated_gif_live_effect_respects_selection_and_updates_while_playing(tmp_path):
    from imagesuite.utils import read_image

    app = QApplication.instance() or QApplication([])
    source_path = tmp_path / "selection-animation.gif"
    frames = [Image.new("RGB", (40, 24), color) for color in ("red", "green", "blue")]
    frames[0].save(source_path, save_all=True, append_images=frames[1:], duration=[80, 80, 80], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source_path), source_path, dirty=False)
    workspace.document.selection = RectMask(0, 0, 20, 24)
    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.toggle_gif_preview()
    QTest.qWait(70)

    preview = workspace.canvas.preview_image
    assert preview is not None
    assert preview.getpixel((5, 10))[:3] == (0, 0, 0)
    assert preview.getpixel((30, 10))[:3] != (0, 0, 0)

    workspace.effect_combo.setCurrentText("White Redaction")
    QTest.qWait(90)
    preview = workspace.canvas.preview_image
    assert preview is not None
    assert preview.getpixel((5, 10))[:3] == (255, 255, 255)
    assert preview.getpixel((30, 10))[:3] != (255, 255, 255)
    workspace.close()


def test_animated_gif_live_adjustments_continue_playback(tmp_path):
    from imagesuite.utils import read_image

    app = QApplication.instance() or QApplication([])
    source_path = tmp_path / "adjust-animation.gif"
    frames = [Image.new("RGB", (36, 20), color) for color in ((40, 40, 40), (80, 80, 80), (120, 120, 120))]
    frames[0].save(source_path, save_all=True, append_images=frames[1:], duration=[60, 60, 60], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source_path), source_path, dirty=False)
    workspace.tool_tabs.setCurrentIndex(2)
    workspace.brightness.setValue(60)
    workspace.toggle_gif_preview()
    QTest.qWait(80)

    assert workspace._gif_playing
    assert workspace._requested_preview_kind == "adjustments"
    first = workspace.canvas.preview_image.getpixel((10, 10))[0]
    workspace.brightness.setValue(-50)
    QTest.qWait(90)
    second = workspace.canvas.preview_image.getpixel((10, 10))[0]
    assert second < first
    assert workspace._gif_preview_timer.isActive()
    workspace.close()


def test_gif_play_button_controls_live_playback_from_any_editor_tab(tmp_path):
    from imagesuite.utils import read_image

    app = QApplication.instance() or QApplication([])
    source_path = tmp_path / "button-animation.gif"
    frames = [Image.new("RGB", (32, 20), color) for color in ("red", "blue")]
    frames[0].save(source_path, save_all=True, append_images=frames[1:], duration=[80, 80], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source_path), source_path, dirty=False)
    workspace.tool_tabs.setCurrentIndex(2)
    assert not workspace.gif_play_button.isHidden()

    workspace.gif_play_button.click()
    assert workspace._gif_playing
    assert workspace.gif_play_button.isChecked()
    workspace.gif_play_button.click()
    assert not workspace._gif_playing
    assert not workspace.gif_play_button.isChecked()
    workspace.close()


def test_gif_playback_uses_elapsed_time_to_skip_late_frames(tmp_path):
    from imagesuite.utils import read_image

    app = QApplication.instance() or QApplication([])
    source_path = tmp_path / "timing-animation.gif"
    frames = [Image.new("RGB", (20, 12), color) for color in ("red", "green", "blue")]
    frames[0].save(source_path, save_all=True, append_images=frames[1:], duration=[40, 40, 40], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source_path), source_path, dirty=False)
    workspace.toggle_gif_preview()
    workspace._gif_playback_started = time.perf_counter() - 0.09
    workspace._advance_gif_preview()
    assert workspace._gif_preview_index == 2
    workspace.close()


def test_cancel_live_effect_keeps_raw_gif_playing(tmp_path):
    from imagesuite.utils import read_image

    app = QApplication.instance() or QApplication([])
    source_path = tmp_path / "cancel-animation.gif"
    frames = [Image.new("RGB", (30, 18), color) for color in ("red", "green", "blue")]
    frames[0].save(source_path, save_all=True, append_images=frames[1:], duration=[70, 70, 70], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source_path), source_path, dirty=False)
    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.toggle_gif_preview()
    QTest.qWait(60)
    assert workspace.canvas.preview_image.getpixel((10, 10))[:3] == (0, 0, 0)

    workspace.cancel_preview()
    assert workspace._gif_playing
    assert workspace._requested_preview_kind is None
    assert workspace.canvas.preview_image.getpixel((10, 10))[:3] != (0, 0, 0)
    workspace.close()


def test_before_view_uses_current_raw_gif_frame_during_live_playback(tmp_path):
    from imagesuite.utils import read_image

    app = QApplication.instance() or QApplication([])
    source_path = tmp_path / "before-animation.gif"
    frames = [Image.new("RGB", (28, 16), color) for color in ("red", "blue")]
    frames[0].save(source_path, save_all=True, append_images=frames[1:], duration=[80, 80], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source_path), source_path, dirty=False)
    workspace.effect_combo.setCurrentText("Black Redaction")
    workspace.toggle_gif_preview()
    QTest.qWait(60)
    assert workspace.canvas.preview_image.getpixel((8, 8))[:3] == (0, 0, 0)

    workspace.canvas.show_original = True
    assert workspace.canvas.current_image().getpixel((8, 8))[:3] != (0, 0, 0)
    workspace.close()


def test_animated_gif_live_creative_effect_updates_while_playing(tmp_path):
    from imagesuite.utils import read_image

    app = QApplication.instance() or QApplication([])
    source_path = tmp_path / "creative-animation.gif"
    frames = [Image.new("RGB", (30, 18), color) for color in ((20, 40, 60), (60, 80, 100))]
    frames[0].save(source_path, save_all=True, append_images=frames[1:], duration=[90, 90], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source_path), source_path, dirty=False)
    workspace.tool_tabs.setCurrentIndex(2)
    workspace.creative_combo.setCurrentText("Invert")
    workspace.preview_selected_creative()
    workspace.toggle_gif_preview()
    QTest.qWait(70)

    assert workspace._gif_playing
    assert workspace._requested_preview_kind == "creative"
    raw = workspace.canvas.animation_original_image.getpixel((8, 8))[:3]
    shown = workspace.canvas.preview_image.getpixel((8, 8))[:3]
    assert shown == tuple(255 - channel for channel in raw)
    workspace.close()


def test_quick_text_updates_on_current_gif_frame_while_playing(tmp_path):
    from imagesuite.utils import read_image

    app = QApplication.instance() or QApplication([])
    source_path = tmp_path / "text-animation.gif"
    frames = [Image.new("RGB", (120, 70), color) for color in ("navy", "purple")]
    frames[0].save(source_path, save_all=True, append_images=frames[1:], duration=[100, 100], loop=0)

    workspace = EditorWorkspace()
    workspace.add_document(read_image(source_path), source_path, dirty=False)
    workspace.choose_mode("text")
    workspace.quick_text_anchor = (60, 35)
    workspace.quick_text_edit.setText("A")
    workspace.toggle_gif_preview()
    QTest.qWait(80)
    first = workspace.canvas.preview_image.tobytes()

    workspace.quick_text_edit.setText("LONGER")
    QTest.qWait(90)
    second = workspace.canvas.preview_image.tobytes()
    assert workspace._gif_playing
    assert workspace._requested_preview_kind == "text"
    assert first != second
    workspace.close()
