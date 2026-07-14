from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QApplication

from imagesuite.editor.workspace import EditorWorkspace
from imagesuite.jobs import JobManager
from imagesuite.models import RectMask
from imagesuite.similarity.engine import FileFingerprint, SimilarImage, SimilarityGroup
from imagesuite.similarity.workspace import SimilarityWorkspace
from imagesuite.upscale.workspace import UpscaleWorkspace


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_canvas_keyboard_navigation_and_zoom():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (800, 600), "white"), dirty=False)
    workspace.choose_mode("face")
    workspace.document.face_masks.append(RectMask(10, 10, 100, 100))
    workspace.canvas.selected_face_index = 0

    assert workspace.canvas._keyboard_transform_active(Qt.Key_Right, Qt.ShiftModifier)
    assert workspace.document.face_masks[0].left == 20
    assert workspace.canvas._keyboard_transform_active(Qt.Key_Down, Qt.AltModifier | Qt.ShiftModifier)
    assert workspace.document.face_masks[0].bottom == 110

    old_zoom = workspace.canvas.zoom
    workspace.canvas.zoom_by(1.2, QPointF(100, 100))
    assert workspace.canvas.zoom > old_zoom
    workspace.canvas.fit_to_window()
    assert workspace.canvas.fit_mode
    workspace.close()


def test_text_can_resize_from_any_corner():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (640, 480), "white"), dirty=False)
    workspace.choose_mode("text")
    workspace.quick_text_edit.setText("Resize me")
    workspace.quick_text_anchor = (320, 240)
    workspace.preview_quick_text()
    original = workspace.canvas.text_overlay_box.copy()
    original_size = workspace.text_size.value()

    point = QPointF(original.left, original.top)
    assert workspace.canvas._start_text_drag(point)
    workspace.canvas._drag_text(QPointF(original.left - 40, original.top - 30))
    QApplication.processEvents()
    assert workspace.text_size.value() > original_size
    assert workspace.quick_text_anchor != (320, 240)
    workspace.close()


def test_duplicate_open_activates_existing_tab(tmp_path: Path):
    _app()
    source = tmp_path / "same.png"
    Image.new("RGB", (32, 32), "red").save(source)
    workspace = EditorWorkspace()
    workspace.load_paths([source, source])
    assert len(workspace.documents) == 1
    assert workspace.active_index == 0
    workspace.close()


def test_upscale_queue_keyboard_helpers(tmp_path: Path):
    _app()
    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (16, 16), (index, index, index)).save(path)
        paths.append(path)
    workspace = UpscaleWorkspace(JobManager(), tmp_path / "models", tmp_path / "outputs")
    workspace._append(paths)
    assert workspace.files.count() == 3
    workspace.files.clearSelection()
    workspace.files.setCurrentRow(1)
    workspace.move_queue_selection(-1)
    assert Path(workspace.files.item(0).data(Qt.UserRole)).name == "1.png"
    workspace.remove_selected_files()
    assert workspace.files.count() == 2
    workspace.close()


def _fingerprint(path: Path, size: int) -> FileFingerprint:
    return FileFingerprint(
        path=str(path), size_bytes=size, width=100, height=100, mtime=1.0,
        a_hash=0, d_hash=0, color_hist=(0.0,) * 24, sharpness=1.0,
    )


def test_similarity_group_keyboard_navigation(tmp_path: Path):
    _app()
    p1, p2, p3, p4 = [tmp_path / f"{i}.png" for i in range(4)]
    for path in (p1, p2, p3, p4):
        Image.new("RGB", (8, 8), "white").save(path)
    group1 = SimilarityGroup(1, _fingerprint(p1, 10), [SimilarImage(_fingerprint(p1, 10)), SimilarImage(_fingerprint(p2, 11))])
    group2 = SimilarityGroup(2, _fingerprint(p3, 12), [SimilarImage(_fingerprint(p3, 12)), SimilarImage(_fingerprint(p4, 13))])
    workspace = SimilarityWorkspace(JobManager())
    workspace.groups = [group1, group2]
    workspace.group_list.addItems(["Group 1", "Group 2"])
    workspace.group_list.setCurrentRow(0)
    workspace.navigate_group(1)
    assert workspace.group_list.currentRow() == 1
    workspace.toggle_selected_check()
    assert group2.members[0].checked
    workspace.close()


def test_keyboard_face_move_can_be_undone():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (200, 160), "white"), dirty=False)
    workspace.choose_mode("face")
    workspace.document.face_masks.append(RectMask(10, 10, 50, 50))
    workspace.canvas.selected_face_index = 0

    assert workspace.canvas._keyboard_transform_active(Qt.Key_Right, Qt.ShiftModifier)
    assert workspace.document.face_masks[0] == RectMask(20, 10, 60, 50)

    workspace.undo()
    assert workspace.document.face_masks[0] == RectMask(10, 10, 50, 50)
    workspace.redo()
    assert workspace.document.face_masks[0] == RectMask(20, 10, 60, 50)
    workspace.close()


def test_mouse_created_rectangle_lasso_and_face_can_be_undone():
    from PySide6.QtCore import QPointF
    from PySide6.QtTest import QTest

    app = _app()
    workspace = EditorWorkspace()
    workspace.resize(1200, 800)
    workspace.show()
    app.processEvents()
    workspace.add_document(Image.new("RGBA", (400, 300), "white"), dirty=False)
    app.processEvents()
    canvas = workspace.canvas

    def widget_point(x: int, y: int):
        return canvas._image_to_widget(QPointF(x, y)).toPoint()

    workspace.choose_mode("select")
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, widget_point(20, 20))
    QTest.mouseMove(canvas, widget_point(120, 100), 10)
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, widget_point(120, 100))
    assert workspace.document.selection == RectMask(20, 20, 120, 100)
    workspace.undo()
    assert workspace.document.selection is None

    workspace.choose_mode("lasso")
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, widget_point(30, 30))
    QTest.mouseMove(canvas, widget_point(110, 35), 10)
    QTest.mouseMove(canvas, widget_point(70, 120), 10)
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, widget_point(70, 120))
    assert workspace.document.lasso_points
    workspace.undo()
    assert workspace.document.lasso_points == []

    workspace.choose_mode("face")
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, widget_point(40, 40))
    QTest.mouseMove(canvas, widget_point(140, 150), 10)
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, widget_point(140, 150))
    assert workspace.document.face_masks == [RectMask(40, 40, 140, 150)]
    workspace.undo()
    assert workspace.document.face_masks == []
    workspace.close()


def test_selection_resizes_from_top_left_handle():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (400, 300), "white"), dirty=False)
    workspace.choose_mode("select")
    workspace.document.selection = RectMask(100, 80, 240, 200)

    assert workspace.canvas._mask_handle_hit(workspace.document.selection, QPointF(100, 80)) == "tl"
    workspace.canvas._start_mask_drag("selection", None, workspace.document.selection, QPointF(100, 80))
    workspace.canvas._drag_mask(QPointF(70, 50))
    assert workspace.document.selection == RectMask(70, 50, 240, 200)
    workspace.close()


def test_text_resizes_from_edge_handle_without_jumping():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (640, 480), "white"), dirty=False)
    workspace.choose_mode("text")
    workspace.quick_text_edit.setText("Edge resize")
    workspace.quick_text_anchor = (320, 240)
    workspace.preview_quick_text()
    box = workspace.canvas.text_overlay_box.copy()
    original_center_y = (box.top + box.bottom) / 2
    original_size = workspace.text_size.value()

    point = QPointF(box.left, original_center_y)
    assert workspace.canvas._text_handle_hit(point) == "l"
    assert workspace.canvas._start_text_drag(point)
    workspace.canvas._drag_text(QPointF(box.left - 50, original_center_y))
    QApplication.processEvents()
    assert workspace.text_size.value() > original_size
    assert abs(workspace.quick_text_anchor[1] - original_center_y) <= 1
    workspace.close()


def test_empty_canvas_mouse_move_clears_stale_interaction_state():
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent

    _app()
    workspace = EditorWorkspace()
    canvas = workspace.canvas
    canvas.dragging = True
    canvas.panning = True
    event = QMouseEvent(
        QEvent.MouseMove,
        QPointF(10, 10),
        QPointF(10, 10),
        Qt.NoButton,
        Qt.NoButton,
        Qt.NoModifier,
    )
    canvas.mouseMoveEvent(event)
    assert canvas.document is None
    assert not canvas.dragging
    assert not canvas.panning
    workspace.close()


def test_switching_documents_clears_transient_mouse_state():
    _app()
    workspace = EditorWorkspace()
    workspace.add_document(Image.new("RGBA", (100, 100), "white"), dirty=False)
    workspace.add_document(Image.new("RGBA", (100, 100), "black"), dirty=False)
    workspace._activate(0)
    workspace.canvas.dragging = True
    workspace.canvas.mask_drag_kind = "selection"
    workspace.canvas.brush_before = Image.new("RGBA", (100, 100), "red")

    workspace._activate(1)

    assert not workspace.canvas.dragging
    assert workspace.canvas.mask_drag_kind is None
    assert workspace.canvas.brush_before is None
    workspace.close()


def test_similarity_group_loads_only_visible_thumbnails(tmp_path: Path, monkeypatch):
    from PySide6.QtGui import QPixmap

    app = _app()
    workspace = SimilarityWorkspace(JobManager())
    workspace.resize(1100, 700)
    workspace.show()
    app.processEvents()

    members = []
    for index in range(160):
        path = tmp_path / f"item_{index}.png"
        members.append(SimilarImage(_fingerprint(path, 100 + index)))
    workspace.groups = [SimilarityGroup(1, members[0].fp, members)]
    workspace.group_list.addItem("Large group")

    calls: list[str] = []
    monkeypatch.setattr(workspace, "_thumbnail", lambda path, max_side=160: (calls.append(path) or QPixmap(16, 16)))
    workspace.group_list.setCurrentRow(0)
    workspace._thumbnail_load_timer.stop()
    calls.clear()
    workspace._load_visible_thumbnails()

    assert 0 < len(calls) < len(members)
    workspace.close()
