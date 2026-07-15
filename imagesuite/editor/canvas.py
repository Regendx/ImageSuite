from __future__ import annotations

from collections import OrderedDict
import math
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QWidget

from imagesuite.editor.effects import mosaic, pixelate
from imagesuite.models import ImageDocument, RectMask


class ImageCanvas(QWidget):
    HANDLE_SIZE = 16
    HANDLE_HIT_RADIUS = 20

    statusChanged = Signal(str)
    documentChanged = Signal()
    previewTargetChanged = Signal()
    canvasClicked = Signal(float, float, str)
    textTransformChanged = Signal(float, float, int)
    textApplyRequested = Signal()
    zoomChanged = Signal(float)
    cursorPositionChanged = Signal(int, int)
    brushSizeChanged = Signal(int)
    filesDropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.setAccessibleName("Image editor canvas")
        self.setAccessibleDescription("Displays the active image and supports selection, text, brush, zoom, and pan tools")
        self.document: Optional[ImageDocument] = None
        self.mode = "select"
        self.brush_size = 60
        self.blur_radius = 8.0
        self.pixel_size = 16
        self.mosaic_size = 22
        self.shape_width = 6
        self.shape_color = "#ff3030"
        self._preview_image: Optional[Image.Image] = None
        self._animation_original_image: Optional[Image.Image] = None
        self._pixmap_cache: OrderedDict[tuple[int, int, int, str], QPixmap] = OrderedDict()
        self._brush_mask_size = 0
        self._brush_mask_hard: Optional[Image.Image] = None
        self._brush_mask_soft: Optional[Image.Image] = None
        self._brush_black_patch: Optional[Image.Image] = None
        self.show_original = False
        self.before_hold_previous = False
        self.compare_enabled = False
        self.compare_split = 50
        self.fit_mode = True
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self.drag_start: Optional[QPointF] = None
        self.drag_current: Optional[QPointF] = None
        self.dragging = False
        self.panning = False
        self.space_pan_active = False
        self.last_mouse = QPoint()
        self.brush_before: Optional[Image.Image] = None
        self.brush_source: Optional[Image.Image] = None
        self.clone_source: Optional[tuple[int, int]] = None
        self.clone_offset: Optional[tuple[int, int]] = None
        self.selected_face_index: Optional[int] = None
        self.mask_drag_kind: Optional[str] = None
        self.mask_drag_index: Optional[int] = None
        self.mask_drag_start_image: Optional[QPointF] = None
        self.mask_drag_original: Optional[RectMask] = None
        self.mask_drag_handle: Optional[str] = None
        self.mask_drag_history_pushed = False
        self.text_overlay_box: Optional[RectMask] = None
        self.text_overlay_size = 48
        self.text_drag_mode: Optional[str] = None
        self.text_drag_start: Optional[QPointF] = None
        self.text_drag_original_box: Optional[RectMask] = None
        self.text_drag_original_size = 48
        self._image_rect = QRectF()
        self.setMinimumSize(420, 320)

    def set_document(self, document: Optional[ImageDocument]) -> None:
        self._reset_interaction_state()
        self.document = document
        self.preview_image = None
        self.animation_original_image = None
        self._invalidate_pixmap_cache()
        self.clear_text_overlay()
        self.fit_mode = True
        self.pan = QPointF()
        if document is not None and self.width() > 0 and self.height() > 0:
            self._image_rect = self._calculate_image_rect(document.image)
        self.update()
        self._emit_zoom()

    def _reset_interaction_state(self) -> None:
        """Drop transient mouse state when tabs/documents change mid-gesture."""
        self._rollback_incomplete_brush()
        self.drag_start = None
        self.drag_current = None
        self.dragging = False
        self.panning = False
        self.space_pan_active = False
        self.clone_offset = None
        self.mask_drag_kind = None
        self.mask_drag_index = None
        self.mask_drag_start_image = None
        self.mask_drag_original = None
        self.mask_drag_handle = None
        self.mask_drag_history_pushed = False
        self.text_drag_mode = None
        self.text_drag_start = None
        self.text_drag_original_box = None
        self._update_cursor()

    def set_mode(self, mode: str) -> None:
        if self.mode in {"text", "sticker"} and mode not in {"text", "sticker"}:
            self.text_drag_mode = None
        self.mode = mode
        self._update_cursor()
        self.statusChanged.emit(f"Tool: {mode.replace('_', ' ').title()}")

    def _update_cursor(self) -> None:
        if self.panning:
            cursor = Qt.ClosedHandCursor
        elif self.space_pan_active or self.mode == "pan":
            cursor = Qt.OpenHandCursor
        elif self.mode == "text":
            cursor = Qt.IBeamCursor
        elif self.mode == "sticker":
            cursor = Qt.PointingHandCursor
        else:
            cursor = Qt.CrossCursor
        self.setCursor(cursor)

    def _emit_zoom(self) -> None:
        self.zoomChanged.emit(float(self.zoom))

    def set_text_overlay(self, box: Optional[RectMask], font_size: int) -> None:
        self.text_overlay_box = box.copy() if box else None
        self.text_overlay_size = max(8, int(font_size))
        self.update()

    def clear_text_overlay(self) -> None:
        self.text_overlay_box = None
        self.text_drag_mode = None
        self.text_drag_start = None
        self.text_drag_original_box = None

    @property
    def preview_image(self) -> Optional[Image.Image]:
        return self._preview_image

    @preview_image.setter
    def preview_image(self, image: Optional[Image.Image]) -> None:
        previous = self._preview_image
        self._preview_image = image
        if previous is not image:
            self._discard_pixmap(previous)
        if previous is not None and previous is not image:
            protected = {id(self._animation_original_image)}
            if self.document is not None:
                protected.add(id(self.document.image))
                protected.add(id(self.document.original_image))
                protected.update(id(frame) for frame in self.document.animation_frames)
                protected.update(id(frame) for frame in self.document.original_animation_frames)
            if id(previous) not in protected:
                try:
                    previous.close()
                except Exception:
                    pass

    @property
    def animation_original_image(self) -> Optional[Image.Image]:
        return self._animation_original_image

    @animation_original_image.setter
    def animation_original_image(self, image: Optional[Image.Image]) -> None:
        if self._animation_original_image is not image:
            self._discard_pixmap(self._animation_original_image)
        self._animation_original_image = image

    def _invalidate_pixmap_cache(self) -> None:
        self._pixmap_cache.clear()

    def _discard_pixmap(self, image: Optional[Image.Image]) -> None:
        if image is None:
            return
        image_id = id(image)
        for key in [key for key in self._pixmap_cache if key[0] == image_id]:
            del self._pixmap_cache[key]

    def _close_brush_masks(self) -> None:
        for image in (self._brush_mask_hard, self._brush_mask_soft, self._brush_black_patch):
            if image is not None:
                image.close()
        self._brush_mask_hard = None
        self._brush_mask_soft = None
        self._brush_black_patch = None
        self._brush_mask_size = 0

    def _full_brush_mask(self, softened: bool = False) -> Image.Image:
        size = max(2, int(self.brush_size))
        if self._brush_mask_hard is None or self._brush_mask_size != size:
            self._close_brush_masks()
            radius = max(2, size // 2)
            diameter = radius * 2 + 1
            hard = Image.new("L", (diameter, diameter), 0)
            ImageDraw.Draw(hard).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
            self._brush_mask_hard = hard
            self._brush_mask_size = size
        if softened and self._brush_mask_soft is None:
            assert self._brush_mask_hard is not None
            self._brush_mask_soft = self._brush_mask_hard.filter(
                ImageFilter.GaussianBlur(max(1, size / 8))
            )
        mask = self._brush_mask_soft if softened else self._brush_mask_hard
        assert mask is not None
        return mask

    def _full_black_brush_patch(self) -> Image.Image:
        hard_mask = self._full_brush_mask(False)
        if self._brush_black_patch is None:
            self._brush_black_patch = Image.new("RGBA", hard_mask.size, (0, 0, 0, 255))
        return self._brush_black_patch

    def _rollback_incomplete_brush(self) -> None:
        """Restore an uncommitted in-place stroke when its document is left."""
        before = self.brush_before
        source = self.brush_source
        document = self.document
        if before is not None:
            if document is not None:
                modified = document.image
                document.image = before
                if modified is not before:
                    modified.close()
                self._invalidate_pixmap_cache()
            else:
                before.close()
        if source is not None and source is not before:
            source.close()
        self.brush_before = None
        self.brush_source = None

    def _text_handle_hit(self, point: QPointF) -> Optional[str]:
        if not self.text_overlay_box:
            return None
        tolerance = max(3.0, self.HANDLE_HIT_RADIUS / max(self.zoom, 0.05))
        box = self.text_overlay_box
        mid_x = (box.left + box.right) / 2
        mid_y = (box.top + box.bottom) / 2
        corners = {
            "tl": (box.left, box.top), "t": (mid_x, box.top), "tr": (box.right, box.top),
            "l": (box.left, mid_y), "r": (box.right, mid_y),
            "bl": (box.left, box.bottom), "b": (mid_x, box.bottom), "br": (box.right, box.bottom),
        }
        for name, (x, y) in corners.items():
            if abs(point.x() - x) <= tolerance and abs(point.y() - y) <= tolerance:
                return name
        return None

    def _start_text_drag(self, point: QPointF) -> bool:
        if not self.text_overlay_box:
            return False
        handle = self._text_handle_hit(point)
        if handle:
            self.text_drag_mode = f"resize_{handle}"
        elif self._contains(self.text_overlay_box, point):
            self.text_drag_mode = "move"
        else:
            return False
        self.text_drag_start = point
        self.text_drag_original_box = self.text_overlay_box.copy()
        self.text_drag_original_size = self.text_overlay_size
        self.dragging = True
        self.statusChanged.emit("Drag a handle to resize text" if self.text_drag_mode.startswith("resize_") else "Drag to move text")
        return True

    def _drag_text(self, point: QPointF) -> None:
        if not self.document or not self.text_drag_start or not self.text_drag_original_box or not self.text_drag_mode:
            return
        original = self.text_drag_original_box
        if self.text_drag_mode == "move":
            dx = point.x() - self.text_drag_start.x()
            dy = point.y() - self.text_drag_start.y()
            box = self._clamp_mask(RectMask(round(original.left + dx), round(original.top + dy), round(original.right + dx), round(original.bottom + dy)))
            self.text_overlay_box = box
            self.textTransformChanged.emit((box.left + box.right) / 2, (box.top + box.bottom) / 2, self.text_overlay_size)
        else:
            corner = self.text_drag_mode.removeprefix("resize_")
            base_width = max(1.0, float(original.width))
            base_height = max(1.0, float(original.height))
            opposite_x = original.right if "l" in corner else original.left if "r" in corner else (original.left + original.right) / 2
            opposite_y = original.bottom if "t" in corner else original.top if "b" in corner else (original.top + original.bottom) / 2
            width_scale = abs(point.x() - opposite_x) / base_width if corner in {"l", "r"} else None
            height_scale = abs(point.y() - opposite_y) / base_height if corner in {"t", "b"} else None
            if width_scale is None:
                width_scale = abs(point.x() - opposite_x) / base_width
            if height_scale is None:
                height_scale = abs(point.y() - opposite_y) / base_height
            scale = width_scale if corner in {"l", "r"} else height_scale if corner in {"t", "b"} else max(width_scale, height_scale)
            scale = max(0.15, min(8.0, scale))
            new_width = base_width * scale
            new_height = base_height * scale
            center_x = (original.left + original.right) / 2 if corner in {"t", "b"} else opposite_x + (new_width / 2 if "r" in corner else -new_width / 2)
            center_y = (original.top + original.bottom) / 2 if corner in {"l", "r"} else opposite_y + (new_height / 2 if "b" in corner else -new_height / 2)
            center_x = max(0, min(self.document.image.width, center_x))
            center_y = max(0, min(self.document.image.height, center_y))
            new_size = max(8, min(600, round(self.text_drag_original_size * scale)))
            self.text_overlay_size = new_size
            self.textTransformChanged.emit(center_x, center_y, new_size)
        self.update()

    def current_image(self) -> Optional[Image.Image]:
        if not self.document:
            return None
        if self.show_original:
            return self.animation_original_image or self.document.original_image
        return self.preview_image or self.document.image

    def _pixmap(self, image: Image.Image) -> QPixmap:
        key = (id(image), image.width, image.height, image.mode)
        cached = self._pixmap_cache.get(key)
        if cached is not None and not cached.isNull():
            self._pixmap_cache.move_to_end(key)
            return cached
        converted = image if image.mode == "RGBA" else image.convert("RGBA")
        try:
            qimage = QImage(ImageQt(converted)).copy()
        finally:
            if converted is not image:
                converted.close()
        pixmap = QPixmap.fromImage(qimage)
        self._pixmap_cache[key] = pixmap
        self._pixmap_cache.move_to_end(key)
        while len(self._pixmap_cache) > 2:
            self._pixmap_cache.popitem(last=False)
        return pixmap

    def _calculate_image_rect(self, image: Image.Image) -> QRectF:
        if self.fit_mode:
            scale = min(max(0.05, (self.width() - 30) / image.width), max(0.05, (self.height() - 30) / image.height))
            scale = min(scale, 1.0)
            self.zoom = scale
        scaled_w = image.width * self.zoom
        scaled_h = image.height * self.zoom
        x = (self.width() - scaled_w) / 2 + self.pan.x()
        y = (self.height() - scaled_h) / 2 + self.pan.y()
        return QRectF(x, y, scaled_w, scaled_h)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#13151a"))
        image = self.current_image()
        if image is None:
            painter.setPen(QColor("#8e96a3"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Drop images here, paste with Ctrl+V, or choose Open")
            return
        # Preview images may be downsampled for responsiveness. Geometry, zoom,
        # and hit testing must always remain in original document coordinates.
        self._image_rect = self._calculate_image_rect(self.document.image)
        painter.setRenderHint(
            QPainter.SmoothPixmapTransform,
            self.zoom < 1.0 or (self.document is not None and image.size != self.document.image.size),
        )
        if self.compare_enabled and not self.show_original and self.document:
            edited = self._pixmap(self.preview_image or self.document.image)
            original = self._pixmap(self.animation_original_image or self.document.original_image)
            split_x = self._image_rect.left() + self._image_rect.width() * self.compare_split / 100
            painter.save()
            painter.setClipRect(QRectF(self._image_rect.left(), self._image_rect.top(), split_x - self._image_rect.left(), self._image_rect.height()))
            painter.drawPixmap(self._image_rect, original, QRectF(original.rect()))
            painter.restore()
            painter.save()
            painter.setClipRect(QRectF(split_x, self._image_rect.top(), self._image_rect.right() - split_x, self._image_rect.height()))
            painter.drawPixmap(self._image_rect, edited, QRectF(edited.rect()))
            painter.restore()
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(int(split_x), int(self._image_rect.top()), int(split_x), int(self._image_rect.bottom()))
        else:
            pix = self._pixmap(image)
            painter.drawPixmap(self._image_rect, pix, QRectF(pix.rect()))
        self._draw_overlays(painter)

    def _draw_overlays(self, painter: QPainter) -> None:
        if not self.document:
            return
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor("#7e9aff"), 2, Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if self.document.selection:
            selection_rect = self._image_box_to_widget(self.document.selection)
            painter.drawRect(selection_rect)
            for handle in self._widget_handle_points(selection_rect).values():
                self._draw_handle(painter, handle, QColor("#7e9aff"))
        for i, face in enumerate(self.document.face_masks):
            selected = i == self.selected_face_index
            painter.setPen(QPen(QColor("#7e9aff" if selected else "#55d6be"), 3 if selected else 2, Qt.SolidLine if selected else Qt.DashLine))
            face_rect = self._image_box_to_widget(face)
            painter.drawEllipse(face_rect)
            if selected:
                for handle in self._widget_handle_points(face_rect).values():
                    self._draw_handle(painter, handle, QColor("#7e9aff"))
        if self.mode in {"text", "sticker"} and self.text_overlay_box:
            text_rect = self._image_box_to_widget(self.text_overlay_box)
            painter.setPen(QPen(QColor("#7e9aff"), 2, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(text_rect, 5, 5)
            for handle in self._widget_handle_points(text_rect).values():
                self._draw_handle(painter, handle, QColor("#7e9aff"))
            painter.setPen(QColor("#ffffff"))
            hint = QRectF(text_rect.left(), max(self._image_rect.top(), text_rect.top() - 24), max(150, text_rect.width()), 20)
            painter.fillRect(hint, QColor(15, 18, 24, 205))
            painter.drawText(hint.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, "Drag to move • handles resize • Alt+wheel changes size • double-click applies")
        if self.document.lasso_points:
            pts = [self._image_to_widget(QPointF(x, y)) for x, y in self.document.lasso_points]
            for a, b in zip(pts, pts[1:]):
                painter.drawLine(a, b)
            if len(pts) > 2:
                painter.drawLine(pts[-1], pts[0])
        if self.dragging and self.drag_start and self.drag_current and self.mode in {"select", "face", "box", "arrow"}:
            rect = QRectF(self.drag_start, self.drag_current).normalized()
            painter.setPen(QPen(QColor(self.shape_color if self.mode in {"box", "arrow"} else "#7e9aff"), 2, Qt.DashLine))
            if self.mode == "face":
                painter.drawEllipse(rect)
            elif self.mode == "arrow":
                painter.drawLine(self.drag_start, self.drag_current)
            else:
                painter.drawRect(rect)

    @staticmethod
    def _widget_handle_points(rect: QRectF) -> dict[str, QPointF]:
        return {
            "tl": rect.topLeft(), "t": QPointF(rect.center().x(), rect.top()), "tr": rect.topRight(),
            "l": QPointF(rect.left(), rect.center().y()), "r": QPointF(rect.right(), rect.center().y()),
            "bl": rect.bottomLeft(), "b": QPointF(rect.center().x(), rect.bottom()), "br": rect.bottomRight(),
        }

    def _draw_handle(self, painter: QPainter, point: QPointF, color: QColor) -> None:
        half = self.HANDLE_SIZE / 2
        painter.save()
        painter.setPen(QPen(QColor("#101218"), 2))
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(point.x() - half, point.y() - half, self.HANDLE_SIZE, self.HANDLE_SIZE), 3, 3)
        painter.restore()

    @staticmethod
    def _contains(box: RectMask, point: QPointF) -> bool:
        return box.left <= point.x() <= box.right and box.top <= point.y() <= box.bottom

    def _mask_handle_hit(self, box: RectMask, point: QPointF) -> Optional[str]:
        tolerance = max(3.0, self.HANDLE_HIT_RADIUS / max(self.zoom, 0.05))
        mid_x = (box.left + box.right) / 2
        mid_y = (box.top + box.bottom) / 2
        handles = {
            "tl": (box.left, box.top), "t": (mid_x, box.top), "tr": (box.right, box.top),
            "l": (box.left, mid_y), "r": (box.right, mid_y),
            "bl": (box.left, box.bottom), "b": (mid_x, box.bottom), "br": (box.right, box.bottom),
        }
        for name, (x, y) in handles.items():
            if abs(point.x() - x) <= tolerance and abs(point.y() - y) <= tolerance:
                return name
        return None

    def _start_mask_drag(self, kind: str, index: Optional[int], box: RectMask, point: QPointF) -> None:
        self.mask_drag_kind = kind
        self.mask_drag_index = index
        self.mask_drag_start_image = point
        self.mask_drag_original = box.copy()
        self.mask_drag_handle = self._mask_handle_hit(box, point)
        self.mask_drag_history_pushed = False
        self.dragging = True
        self.statusChanged.emit(f"{'Resize' if self.mask_drag_handle else 'Move'} {kind.replace('_', ' ')}")

    def _clamp_mask(self, box: RectMask) -> RectMask:
        assert self.document
        width, height = max(2, box.width), max(2, box.height)
        left = max(0, min(self.document.image.width - width, box.left))
        top = max(0, min(self.document.image.height - height, box.top))
        return RectMask(left, top, min(self.document.image.width, left + width), min(self.document.image.height, top + height))

    def _drag_mask(self, point: QPointF) -> None:
        if not self.document or not self.mask_drag_kind or not self.mask_drag_original or not self.mask_drag_start_image:
            return
        original = self.mask_drag_original
        if not self.mask_drag_history_pushed:
            self.document.push_mask_undo()
            self.mask_drag_history_pushed = True
        if self.mask_drag_handle:
            left, top, right, bottom = original.left, original.top, original.right, original.bottom
            handle = self.mask_drag_handle
            if "l" in handle:
                left = round(point.x())
            if "r" in handle:
                right = round(point.x())
            if "t" in handle:
                top = round(point.y())
            if "b" in handle:
                bottom = round(point.y())
            box = self._clamp_mask(RectMask(left, top, right, bottom).normalized())
        else:
            dx = round(point.x() - self.mask_drag_start_image.x())
            dy = round(point.y() - self.mask_drag_start_image.y())
            box = self._clamp_mask(RectMask(original.left + dx, original.top + dy, original.right + dx, original.bottom + dy))
        if self.mask_drag_kind == "selection":
            self.document.selection = box
            self.previewTargetChanged.emit()
        elif self.mask_drag_index is not None and 0 <= self.mask_drag_index < len(self.document.face_masks):
            self.document.face_masks[self.mask_drag_index] = box
            self.previewTargetChanged.emit()
        self.update()

    def preview_selection(self) -> Optional[RectMask]:
        """Return the provisional rectangle while it is being drawn."""
        if self.document and self.mode == "select" and self.dragging and self.drag_start and self.drag_current:
            a = self._widget_to_image(self.drag_start)
            b = self._widget_to_image(self.drag_current)
            box = RectMask(round(a.x()), round(a.y()), round(b.x()), round(b.y())).normalized()
            if box.width > 0 and box.height > 0:
                return box
        return self.document.selection if self.document else None

    def _image_to_widget(self, point: QPointF) -> QPointF:
        return QPointF(self._image_rect.left() + point.x() * self.zoom, self._image_rect.top() + point.y() * self.zoom)

    def _widget_to_image(self, point: QPointF, clamp: bool = True) -> QPointF:
        if not self.document or self.zoom <= 0:
            return QPointF()
        x = (point.x() - self._image_rect.left()) / self.zoom
        y = (point.y() - self._image_rect.top()) / self.zoom
        if clamp:
            x = max(0, min(self.document.image.width - 1, x))
            y = max(0, min(self.document.image.height - 1, y))
        return QPointF(x, y)

    def _image_box_to_widget(self, box: RectMask) -> QRectF:
        a = self._image_to_widget(QPointF(box.left, box.top))
        b = self._image_to_widget(QPointF(box.right, box.bottom))
        return QRectF(a, b).normalized()

    def set_zoom(self, zoom: float, anchor: Optional[QPointF] = None) -> None:
        if not self.document:
            return
        anchor = anchor or QPointF(self.width() / 2, self.height() / 2)
        old_image_point = self._widget_to_image(anchor, clamp=False)
        self.fit_mode = False
        self.zoom = max(0.05, min(12.0, float(zoom)))
        self._image_rect = self._calculate_image_rect(self.document.image)
        new_widget = self._image_to_widget(old_image_point)
        self.pan += anchor - new_widget
        self.update()
        self._emit_zoom()

    def zoom_by(self, factor: float, anchor: Optional[QPointF] = None) -> None:
        self.set_zoom(self.zoom * factor, anchor)

    def pan_by(self, dx: float, dy: float) -> None:
        if not self.document:
            return
        self.fit_mode = False
        self.pan += QPointF(dx, dy)
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        if not self.document:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        direction = 1 if delta > 0 else -1
        modifiers = event.modifiers()
        if modifiers & Qt.AltModifier:
            if self.mode in {"text", "sticker"} and self.text_overlay_box:
                new_size = max(8, min(600, self.text_overlay_size + direction * (5 if modifiers & Qt.ShiftModifier else 1)))
                center_x = (self.text_overlay_box.left + self.text_overlay_box.right) / 2
                center_y = (self.text_overlay_box.top + self.text_overlay_box.bottom) / 2
                self.textTransformChanged.emit(center_x, center_y, new_size)
                self.statusChanged.emit(f"Text size {new_size}px")
            elif self.mode.startswith("brush_") or self.mode in {"clone", "heal"}:
                step = 10 if modifiers & Qt.ShiftModifier else 2
                self.brush_size = max(2, min(800, self.brush_size + direction * step))
                self.brushSizeChanged.emit(self.brush_size)
                self.statusChanged.emit(f"Brush size {self.brush_size}px")
            event.accept()
            return
        if modifiers & Qt.ShiftModifier:
            self.pan_by(direction * 55, 0)
            self.statusChanged.emit("Panned horizontally")
            event.accept()
            return
        factor = 1.15 if direction > 0 else 1 / 1.15
        self.zoom_by(factor, event.position())
        self.statusChanged.emit(f"Zoom {self.zoom * 100:.0f}%")
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if not self.document:
            event.ignore()
            return
        self.setFocus()
        self.last_mouse = event.position().toPoint()
        if event.button() in {Qt.MiddleButton, Qt.RightButton} or self.space_pan_active or self.mode == "pan":
            self.panning = True
            self.fit_mode = False
            self._update_cursor()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.mode in {"text", "sticker"} and self.text_overlay_box:
            if self._start_text_drag(self._widget_to_image(event.position(), clamp=False)):
                return
        if event.button() != Qt.LeftButton or not self._image_rect.contains(event.position()):
            return
        image_point = self._widget_to_image(event.position())
        if self.mode == "select" and self.document.selection and (self._mask_handle_hit(self.document.selection, image_point) or self._contains(self.document.selection, image_point)):
            self._start_mask_drag("selection", None, self.document.selection, image_point)
            return
        if self.mode == "face":
            for index in range(len(self.document.face_masks) - 1, -1, -1):
                face = self.document.face_masks[index]
                if self._mask_handle_hit(face, image_point) or self._contains(face, image_point):
                    self.selected_face_index = index
                    self._start_mask_drag("face circle", index, face, image_point)
                    self.update()
                    return
        if self.mode == "text":
            self.canvasClicked.emit(image_point.x(), image_point.y(), self.mode)
            return
        if self.mode == "sticker":
            self.canvasClicked.emit(image_point.x(), image_point.y(), self.mode)
            return
        if self.mode == "clone" and (event.modifiers() & Qt.AltModifier):
            self.clone_source = (round(image_point.x()), round(image_point.y()))
            self.statusChanged.emit("Clone source selected; paint elsewhere")
            return
        if self.mode in {"brush_blur", "brush_pixel", "brush_mosaic", "brush_black", "clone", "heal"}:
            if self.document.is_animated:
                self.statusChanged.emit("Brush, clone, and heal tools are unavailable for animated GIFs; use area effects instead")
                return
            if self.mode in {"clone", "heal"} and self.clone_source is None:
                self.statusChanged.emit("Alt-click to choose a clone source first")
                return
            self._begin_brush((round(image_point.x()), round(image_point.y())))
            self.dragging = True
            return
        if self.mode == "lasso":
            self.document.push_mask_undo()
            self.document.selection = None
            self.document.lasso_points = [(round(image_point.x()), round(image_point.y()))]
            self.dragging = True
            self.previewTargetChanged.emit()
            self.update()
            return
        self.drag_start = event.position()
        self.drag_current = event.position()
        self.dragging = True

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if not self.document:
            event.ignore()
            return
        if self.mode in {"text", "sticker"} and self.text_overlay_box and event.button() == Qt.LeftButton:
            point = self._widget_to_image(event.position())
            if self._contains(self.text_overlay_box, point):
                self.textApplyRequested.emit()
                event.accept()
                return
        if event.button() == Qt.LeftButton and self.document and self._image_rect.contains(event.position()):
            if self.fit_mode:
                self.actual_size()
                self.statusChanged.emit("Actual size (100%)")
            else:
                self.fit_to_window()
                self.statusChanged.emit("Fit to window")
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        document = self.document
        if document is None:
            self.cursorPositionChanged.emit(-1, -1)
            if self.dragging or self.panning:
                self._reset_interaction_state()
            event.ignore()
            return
        if self.panning:
            delta = event.position() - QPointF(self.last_mouse)
            self.pan_by(delta.x(), delta.y())
            self.last_mouse = event.position().toPoint()
            event.accept()
            return
        if self._image_rect.contains(event.position()):
            pos = self._widget_to_image(event.position())
            self.cursorPositionChanged.emit(round(pos.x()), round(pos.y()))
        else:
            self.cursorPositionChanged.emit(-1, -1)
        if not self.dragging:
            if self._image_rect.contains(event.position()) or (self.mode in {"text", "sticker"} and self.text_overlay_box):
                ip = self._widget_to_image(event.position(), clamp=self.mode not in {"text", "sticker"})
                handle: Optional[str] = None
                active_box: Optional[RectMask] = None
                if self.mode in {"text", "sticker"} and self.text_overlay_box:
                    handle, active_box = self._text_handle_hit(ip), self.text_overlay_box
                elif self.mode == "select" and document.selection:
                    handle, active_box = self._mask_handle_hit(document.selection, ip), document.selection
                elif self.mode == "face" and self.selected_face_index is not None and 0 <= self.selected_face_index < len(document.face_masks):
                    active_box = document.face_masks[self.selected_face_index]
                    handle = self._mask_handle_hit(active_box, ip)
                if handle in {"tl", "br"}:
                    self.setCursor(Qt.SizeFDiagCursor)
                elif handle in {"tr", "bl"}:
                    self.setCursor(Qt.SizeBDiagCursor)
                elif handle in {"l", "r"}:
                    self.setCursor(Qt.SizeHorCursor)
                elif handle in {"t", "b"}:
                    self.setCursor(Qt.SizeVerCursor)
                elif active_box and self._contains(active_box, ip):
                    self.setCursor(Qt.SizeAllCursor)
                else:
                    self._update_cursor()
            return
        if self.text_drag_mode:
            self._drag_text(self._widget_to_image(event.position()))
            return
        if self.mask_drag_kind:
            self._drag_mask(self._widget_to_image(event.position()))
            return
        if self.mode == "lasso":
            p = self._widget_to_image(event.position())
            pt = (round(p.x()), round(p.y()))
            if not document.lasso_points or math.dist(pt, document.lasso_points[-1]) >= 2:
                document.lasso_points.append(pt)
                self.previewTargetChanged.emit()
                self.update()
            return
        if self.mode in {"brush_blur", "brush_pixel", "brush_mosaic", "brush_black", "clone", "heal"}:
            p = self._widget_to_image(event.position())
            self._paint_brush((round(p.x()), round(p.y())))
            return
        self.drag_current = event.position()
        if self.mode == "select":
            self.previewTargetChanged.emit()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self.panning:
            self.panning = False
            self._update_cursor()
            event.accept()
            return
        if event.button() != Qt.LeftButton or not self.dragging:
            return
        if not self.document:
            self._reset_interaction_state()
            event.ignore()
            return
        self.dragging = False
        if self.text_drag_mode:
            action = "resized" if self.text_drag_mode.startswith("resize_") else "moved"
            self.statusChanged.emit(f"{self.mode.title()} {action} — press Ctrl+Enter or double-click to apply")
            self.text_drag_mode = None
            self.text_drag_start = None
            self.text_drag_original_box = None
            self.update()
            return
        if self.mask_drag_kind:
            detail = "Resized" if self.mask_drag_handle else "Moved"
            self.statusChanged.emit(f"{detail} {self.mask_drag_kind}")
            self.mask_drag_kind = None
            self.mask_drag_index = None
            self.mask_drag_start_image = None
            self.mask_drag_original = None
            self.mask_drag_handle = None
            self.mask_drag_history_pushed = False
            self.documentChanged.emit()
            self.update()
            return
        if self.mode in {"brush_blur", "brush_pixel", "brush_mosaic", "brush_black", "clone", "heal"}:
            self._finish_brush()
            return
        if self.mode == "lasso":
            self.statusChanged.emit(f"Lasso created with {len(self.document.lasso_points)} points")
            self.documentChanged.emit()
            self.update()
            return
        if not self.drag_start:
            return
        end = event.position()
        a = self._widget_to_image(self.drag_start)
        b = self._widget_to_image(end)
        box = RectMask(round(a.x()), round(a.y()), round(b.x()), round(b.y())).normalized()
        if box.width < 2 or box.height < 2:
            self.drag_start = self.drag_current = None
            self.update()
            return
        if self.mode == "select":
            self.document.push_mask_undo()
            self.document.selection = box
            self.document.lasso_points.clear()
            self.statusChanged.emit(f"Selection {box.width} × {box.height}")
        elif self.mode == "face":
            self.document.push_mask_undo()
            self.document.face_masks.append(box)
            self.selected_face_index = len(self.document.face_masks) - 1
            self.statusChanged.emit(f"Face circle #{len(self.document.face_masks)} added")
        elif self.mode in {"box", "arrow"}:
            self._commit_shape(a, b, self.mode)
        self.drag_start = self.drag_current = None
        self.documentChanged.emit()
        self.update()

    def _commit_shape(self, a: QPointF, b: QPointF, mode: str) -> None:
        assert self.document
        xy = (round(a.x()), round(a.y()), round(b.x()), round(b.y()))

        def draw_shape(frame: Image.Image) -> Image.Image:
            edited = frame.copy()
            draw = ImageDraw.Draw(edited)
            if mode == "box":
                draw.rectangle(xy, outline=self.shape_color, width=max(1, self.shape_width))
            else:
                draw.line(xy, fill=self.shape_color, width=max(1, self.shape_width))
                angle = math.atan2(b.y() - a.y(), b.x() - a.x())
                length = max(12, self.shape_width * 4)
                for offset in (2.55, -2.55):
                    point = (b.x() + math.cos(angle + offset) * length, b.y() + math.sin(angle + offset) * length)
                    draw.line((b.x(), b.y(), point[0], point[1]), fill=self.shape_color, width=max(1, self.shape_width))
            return edited

        self.document.apply_transform(draw_shape)
        self._invalidate_pixmap_cache()
        self.documentChanged.emit()

    def _begin_brush(self, point: tuple[int, int]) -> None:
        assert self.document
        self.brush_before = self.document.image.copy()
        if self.mode == "brush_blur":
            self.brush_source = self.brush_before.filter(ImageFilter.GaussianBlur(max(0.1, self.blur_radius)))
        elif self.mode == "brush_pixel":
            self.brush_source = pixelate(self.brush_before, self.pixel_size)
        elif self.mode == "brush_mosaic":
            self.brush_source = mosaic(self.brush_before, self.mosaic_size)
        elif self.mode in {"clone", "heal"}:
            self.brush_source = self.brush_before.copy()
            assert self.clone_source
            self.clone_offset = (self.clone_source[0] - point[0], self.clone_source[1] - point[1])
        else:
            self.brush_source = None
        self.last_brush = point
        self._stamp(point)

    def _paint_brush(self, point: tuple[int, int]) -> None:
        last = getattr(self, "last_brush", point)
        distance = math.dist(last, point)
        steps = max(1, math.ceil(distance / max(1, self.brush_size * 0.18)))
        for i in range(1, steps + 1):
            t = i / steps
            p = (round(last[0] + (point[0] - last[0]) * t), round(last[1] + (point[1] - last[1]) * t))
            self._stamp(p)
        self.last_brush = point
        self.update()

    def _stamp(self, point: tuple[int, int]) -> None:
        assert self.document
        radius = max(2, self.brush_size // 2)
        x, y = point
        raw_left, raw_top = x - radius, y - radius
        box = (max(0, raw_left), max(0, raw_top), min(self.document.image.width, x + radius + 1), min(self.document.image.height, y + radius + 1))
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        full_mask = self._full_brush_mask(self.mode == "heal")
        if self.mode == "brush_black":
            self.document.image.paste(self._full_black_brush_patch(), (raw_left, raw_top), full_mask)
            self._invalidate_pixmap_cache()
            return
        full_box = (raw_left, raw_top, x + radius + 1, y + radius + 1)
        mask_is_crop = box != full_box
        mask = (
            full_mask.crop((box[0] - raw_left, box[1] - raw_top, box[2] - raw_left, box[3] - raw_top))
            if mask_is_crop
            else full_mask
        )
        try:
            if self.mode in {"clone", "heal"} and self.brush_source and self.clone_offset:
                destination = self.document.image.crop(box)
                patch = destination.copy()
                try:
                    ox, oy = self.clone_offset
                    source_box = (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy)
                    valid = (
                        max(0, source_box[0]), max(0, source_box[1]),
                        min(self.brush_source.width, source_box[2]), min(self.brush_source.height, source_box[3]),
                    )
                    if valid[2] <= valid[0] or valid[3] <= valid[1]:
                        return
                    crop = self.brush_source.crop(valid)
                    try:
                        patch.paste(crop, (valid[0] - source_box[0], valid[1] - source_box[1]))
                    finally:
                        crop.close()
                    if self.mode == "heal":
                        healed = Image.blend(destination, patch, 0.68)
                        patch.close()
                        patch = healed
                    self.document.image.paste(patch, (box[0], box[1]), mask)
                finally:
                    patch.close()
                    destination.close()
            elif self.brush_source:
                patch = self.brush_source.crop(box)
                try:
                    self.document.image.paste(patch, (box[0], box[1]), mask)
                finally:
                    patch.close()
            else:
                return
        finally:
            if mask_is_crop:
                mask.close()
        self._invalidate_pixmap_cache()

    def _finish_brush(self) -> None:
        if not self.document or self.brush_before is None:
            return
        before = self.brush_before
        source = self.brush_source
        self.document.commit_in_place(before)
        self._invalidate_pixmap_cache()
        self.brush_before = self.brush_source = None
        if source is not None and source is not before:
            source.close()
        self.documentChanged.emit()
        self.statusChanged.emit("Brush stroke applied")
        self.update()


    def _active_mask(self) -> tuple[str, Optional[int], Optional[RectMask]]:
        if self.mode in {"text", "sticker"} and self.text_overlay_box:
            return "text", None, self.text_overlay_box
        if self.selected_face_index is not None and self.document and 0 <= self.selected_face_index < len(self.document.face_masks):
            return "face", self.selected_face_index, self.document.face_masks[self.selected_face_index]
        if self.document and self.document.selection:
            return "selection", None, self.document.selection
        return "", None, None

    def _keyboard_transform_active(self, key: int, modifiers: Qt.KeyboardModifiers) -> bool:
        if not self.document:
            return False
        kind, index, box = self._active_mask()
        if not box:
            return False
        step = 10 if modifiers & Qt.ShiftModifier else 1
        dx = dy = 0
        if key == Qt.Key_Left:
            dx = -step
        elif key == Qt.Key_Right:
            dx = step
        elif key == Qt.Key_Up:
            dy = -step
        elif key == Qt.Key_Down:
            dy = step
        else:
            return False
        if modifiers & Qt.AltModifier:
            transformed = self._clamp_mask(RectMask(box.left, box.top, box.right + dx, box.bottom + dy).normalized())
        else:
            transformed = self._clamp_mask(RectMask(box.left + dx, box.top + dy, box.right + dx, box.bottom + dy))
        if kind == "text":
            self.text_overlay_box = transformed
            self.textTransformChanged.emit((transformed.left + transformed.right) / 2, (transformed.top + transformed.bottom) / 2, self.text_overlay_size)
        elif kind == "face" and index is not None:
            self.document.push_mask_undo()
            self.document.face_masks[index] = transformed
            self.documentChanged.emit()
        elif kind == "selection":
            self.document.push_mask_undo()
            self.document.selection = transformed
            self.documentChanged.emit()
        self.update()
        verb = "Resized" if modifiers & Qt.AltModifier else "Moved"
        self.statusChanged.emit(f"{verb} {kind} by {step}px")
        return True

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self.space_pan_active = True
            self._update_cursor()
            self.statusChanged.emit("Temporary pan — drag with the left mouse button")
            event.accept()
            return
        if event.key() == Qt.Key_B and not event.isAutoRepeat() and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier)):
            self.before_hold_previous = self.show_original
            self.show_original = True
            self.update()
            event.accept()
            return
        if event.modifiers() & Qt.ControlModifier and event.key() in {Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down}:
            step = 100 if event.modifiers() & Qt.ShiftModifier else 30
            dx = -step if event.key() == Qt.Key_Left else step if event.key() == Qt.Key_Right else 0
            dy = -step if event.key() == Qt.Key_Up else step if event.key() == Qt.Key_Down else 0
            self.pan_by(dx, dy)
            event.accept()
            return
        if self._keyboard_transform_active(event.key(), event.modifiers()):
            event.accept()
            return
        if self.mode in {"text", "sticker"} and self.text_overlay_box and event.key() in {Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus}:
            step = 10 if event.modifiers() & Qt.ShiftModifier else 1
            delta = step if event.key() in {Qt.Key_Plus, Qt.Key_Equal} else -step
            new_size = max(8, min(600, self.text_overlay_size + delta))
            center_x = (self.text_overlay_box.left + self.text_overlay_box.right) / 2
            center_y = (self.text_overlay_box.top + self.text_overlay_box.bottom) / 2
            self.textTransformChanged.emit(center_x, center_y, new_size)
            event.accept()
            return
        if event.key() == Qt.Key_Tab and self.document and self.document.face_masks:
            direction = -1 if event.modifiers() & Qt.ShiftModifier else 1
            current = self.selected_face_index if self.selected_face_index is not None else (-1 if direction > 0 else 0)
            self.selected_face_index = (current + direction) % len(self.document.face_masks)
            self.mode = "face"
            self._update_cursor()
            self.update()
            self.statusChanged.emit(f"Selected face circle #{self.selected_face_index + 1}")
            event.accept()
            return
        if event.key() in {Qt.Key_Home, Qt.Key_0} and not event.modifiers():
            self.fit_to_window()
            event.accept()
            return
        if event.key() == Qt.Key_1 and not event.modifiers():
            self.actual_size()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self.space_pan_active = False
            if self.panning:
                self.panning = False
            self._update_cursor()
            self.statusChanged.emit("Temporary pan released")
            event.accept()
            return
        if event.key() == Qt.Key_B and not event.isAutoRepeat():
            self.show_original = self.before_hold_previous
            self.update()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self.document and self.fit_mode:
            self._image_rect = self._calculate_image_rect(self.document.image)
            self._emit_zoom()

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def fit_to_window(self) -> None:
        self.fit_mode = True
        self.pan = QPointF()
        if self.document:
            self._image_rect = self._calculate_image_rect(self.document.image)
        self.update()
        self._emit_zoom()

    def actual_size(self) -> None:
        self.fit_mode = False
        self.zoom = 1.0
        self.pan = QPointF()
        self.update()
        self._emit_zoom()

    def clear_masks(self) -> None:
        if self.document and (self.document.selection or self.document.lasso_points or self.document.face_masks):
            self.document.push_mask_undo()
            self.document.selection = None
            self.document.lasso_points.clear()
            self.document.face_masks.clear()
            self.selected_face_index = None
            self.documentChanged.emit()
            self.update()
