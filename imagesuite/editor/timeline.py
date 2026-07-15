from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QWidget


class RangeTimeline(QWidget):
    """Compact video-editor style timeline with in/out handles and a playhead."""

    rangeChanged = Signal(int, int)
    playheadChanged = Signal(int)

    HANDLE_WIDTH = 10.0
    MIN_RANGE_MS = 20

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._duration_ms = 1000
        self._in_ms = 0
        self._out_ms = 1000
        self._playhead_ms = 0
        self._thumbnails: list[QPixmap] = []
        self._drag_mode: str | None = None
        self._drag_origin_x = 0.0
        self._drag_origin_range = (0, 1000)
        self.setMinimumHeight(118)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Video clip timeline")
        self.setToolTip("Drag the blue handles to choose the clip. Click or drag elsewhere to seek.")

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

    @property
    def in_ms(self) -> int:
        return self._in_ms

    @property
    def out_ms(self) -> int:
        return self._out_ms

    @property
    def playhead_ms(self) -> int:
        return self._playhead_ms

    def set_duration(self, duration_ms: int) -> None:
        duration = max(self.MIN_RANGE_MS, int(duration_ms))
        self._duration_ms = duration
        self._in_ms = min(self._in_ms, duration - self.MIN_RANGE_MS)
        self._out_ms = min(max(self._out_ms, self._in_ms + self.MIN_RANGE_MS), duration)
        self._playhead_ms = min(max(self._playhead_ms, 0), duration)
        self.update()

    def set_range(self, start_ms: int, end_ms: int, *, emit: bool = False) -> None:
        start = max(0, min(int(start_ms), self._duration_ms - self.MIN_RANGE_MS))
        end = max(start + self.MIN_RANGE_MS, min(int(end_ms), self._duration_ms))
        if (start, end) == (self._in_ms, self._out_ms):
            return
        self._in_ms, self._out_ms = start, end
        self._playhead_ms = min(max(self._playhead_ms, start), end)
        self.update()
        if emit:
            self.rangeChanged.emit(start, end)

    def set_playhead(self, position_ms: int, *, emit: bool = False) -> None:
        position = max(0, min(int(position_ms), self._duration_ms))
        if position == self._playhead_ms:
            return
        self._playhead_ms = position
        self.update()
        if emit:
            self.playheadChanged.emit(position)

    def set_thumbnails(self, thumbnails: list[QPixmap]) -> None:
        self._thumbnails = [thumbnail for thumbnail in thumbnails if not thumbnail.isNull()]
        self.update()

    def _content_rect(self) -> QRectF:
        return QRectF(12.0, 8.0, max(1.0, self.width() - 24.0), max(1.0, self.height() - 28.0))

    def _value_to_x(self, value_ms: int) -> float:
        rect = self._content_rect()
        return rect.left() + rect.width() * max(0.0, min(1.0, value_ms / self._duration_ms))

    def _x_to_value(self, x: float) -> int:
        rect = self._content_rect()
        ratio = (x - rect.left()) / max(1.0, rect.width())
        return round(max(0.0, min(1.0, ratio)) * self._duration_ms)

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self._content_rect()
        painter.fillRect(rect, QColor(28, 31, 38))

        if self._thumbnails:
            width = rect.width() / len(self._thumbnails)
            for index, thumbnail in enumerate(self._thumbnails):
                cell = QRectF(rect.left() + index * width, rect.top(), width + 1.0, rect.height())
                painter.drawPixmap(cell.toRect(), thumbnail, thumbnail.rect())
        else:
            painter.setPen(QColor(135, 142, 155))
            painter.drawText(rect, Qt.AlignCenter, "Loading timeline preview…")

        in_x = self._value_to_x(self._in_ms)
        out_x = self._value_to_x(self._out_ms)
        painter.fillRect(QRectF(rect.left(), rect.top(), max(0.0, in_x - rect.left()), rect.height()), QColor(0, 0, 0, 155))
        painter.fillRect(QRectF(out_x, rect.top(), max(0.0, rect.right() - out_x), rect.height()), QColor(0, 0, 0, 155))

        accent = QColor(51, 153, 255)
        painter.setPen(QPen(accent, 2.0))
        painter.drawRect(QRectF(in_x, rect.top(), max(1.0, out_x - in_x), rect.height()))
        painter.fillRect(QRectF(in_x - self.HANDLE_WIDTH / 2, rect.top(), self.HANDLE_WIDTH, rect.height()), accent)
        painter.fillRect(QRectF(out_x - self.HANDLE_WIDTH / 2, rect.top(), self.HANDLE_WIDTH, rect.height()), accent)

        play_x = self._value_to_x(self._playhead_ms)
        painter.setPen(QPen(QColor(255, 92, 92), 2.0))
        painter.drawLine(QPointF(play_x, rect.top() - 3.0), QPointF(play_x, rect.bottom() + 3.0))
        painter.setBrush(QColor(255, 92, 92))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygonF([
            QPointF(play_x - 5.0, rect.top() - 4.0),
            QPointF(play_x + 5.0, rect.top() - 4.0),
            QPointF(play_x, rect.top() + 3.0),
        ]))

        painter.setPen(QColor(175, 182, 194))
        painter.drawText(QRectF(rect.left(), rect.bottom() + 5.0, rect.width(), 18.0), Qt.AlignLeft, self._format_ms(self._in_ms))
        painter.drawText(QRectF(rect.left(), rect.bottom() + 5.0, rect.width(), 18.0), Qt.AlignRight, self._format_ms(self._out_ms))

    @staticmethod
    def _format_ms(value_ms: int) -> str:
        total_seconds = max(0, int(value_ms)) / 1000
        minutes = int(total_seconds // 60)
        seconds = total_seconds - minutes * 60
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours:d}:{minutes:02d}:{seconds:05.2f}"
        return f"{minutes:d}:{seconds:05.2f}"

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus(Qt.MouseFocusReason)
        x = event.position().x()
        in_x = self._value_to_x(self._in_ms)
        out_x = self._value_to_x(self._out_ms)
        tolerance = self.HANDLE_WIDTH + 5.0
        if abs(x - in_x) <= tolerance:
            self._drag_mode = "in"
        elif abs(x - out_x) <= tolerance:
            self._drag_mode = "out"
        elif in_x < x < out_x and event.position().y() >= self.height() - 36:
            self._drag_mode = "range"
            self._drag_origin_x = x
            self._drag_origin_range = (self._in_ms, self._out_ms)
        else:
            self._drag_mode = "seek"
            self.set_playhead(self._x_to_value(x), emit=True)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if not self._drag_mode:
            return super().mouseMoveEvent(event)
        value = self._x_to_value(event.position().x())
        if self._drag_mode == "in":
            start = min(value, self._out_ms - self.MIN_RANGE_MS)
            self.set_range(start, self._out_ms, emit=True)
        elif self._drag_mode == "out":
            end = max(value, self._in_ms + self.MIN_RANGE_MS)
            self.set_range(self._in_ms, end, emit=True)
        elif self._drag_mode == "range":
            origin_start, origin_end = self._drag_origin_range
            delta = self._x_to_value(event.position().x()) - self._x_to_value(self._drag_origin_x)
            length = origin_end - origin_start
            start = max(0, min(origin_start + delta, self._duration_ms - length))
            self.set_range(start, start + length, emit=True)
        else:
            self.set_playhead(value, emit=True)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._drag_mode = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        step = max(10, self._duration_ms // 1000)
        if event.key() in {Qt.Key_Left, Qt.Key_Right}:
            direction = -1 if event.key() == Qt.Key_Left else 1
            if event.modifiers() & Qt.ShiftModifier:
                self.set_range(self._in_ms, self._out_ms + direction * step, emit=True)
            else:
                self.set_playhead(self._playhead_ms + direction * step, emit=True)
            event.accept()
            return
        super().keyPressEvent(event)
