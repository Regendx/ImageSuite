from __future__ import annotations

import json
import math
import os
import threading
from bisect import bisect_right
from collections import OrderedDict
from pathlib import Path
import time
from typing import Callable, Optional
from concurrent.futures import Future, ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from PIL.ImageQt import ImageQt, fromqimage
from PySide6.QtCore import QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPixmap, QShortcut, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTextEdit,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from imagesuite.editor import effects
from imagesuite.editor.canvas import ImageCanvas
from imagesuite.editor.timeline import RangeTimeline
from imagesuite.models import ANIMATION_FRAMES_KEY, ImageDocument, RectMask
from imagesuite.diagnostics import log_warning, show_operation_error
_RECOVERY_SAVE_LOCK = threading.Lock()

from imagesuite.utils import (
    IMAGE_FILE_FILTER,
    AnimationReadCancelled,
    atomic_write_text,
    app_data_dir,
    choose_animation_reduction,
    expand_image_paths,
    export_video_segment,
    open_folder,
    probe_video,
    read_image,
    read_video_timeline_thumbnails,
    retime_animation_durations,
    save_animation,
    slice_animation,
    save_image,
    unique_destination,
)


class MultilineTextEdit(QTextEdit):
    """QTextEdit with the old single-line accessor kept for compatibility."""

    def text(self) -> str:
        return self.toPlainText()


def _human_file_size(value: int) -> str:
    size = float(max(0, value))
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or suffix == "TB":
            return f"{size:.0f} {suffix}" if suffix == "B" else f"{size:.1f} {suffix}"
        size /= 1024
    return f"{size:.1f} TB"


class VideoImportDialog(QDialog):
    """Video-editor style source preview and in/out range chooser."""

    def __init__(self, source: Path, info: dict[str, int | float], parent=None) -> None:
        super().__init__(parent)
        self.source = source
        self.info = info
        self._duration_ms = max(20, int(info.get("duration_ms", 0) or 0))
        self._range_syncing = False
        self._thumbnail_cancel = threading.Event()
        self._thumbnail_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-filmstrip")
        self._thumbnail_future: Future | None = None
        self.setWindowTitle("Open video")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(720, 620)
        self.resize(920, 760)

        root = QVBoxLayout(self)
        heading = QLabel(f"Choose the part of {source.name} to edit", objectName="Brand")
        heading.setWordWrap(True)
        root.addWidget(heading)
        details = QLabel(
            f"Source: {int(info['width'])}×{int(info['height'])} · "
            f"{float(info['fps']):.2f} fps · {self._format_ms(self._duration_ms)} · "
            f"{_human_file_size(int(info['file_size']))}",
            objectName="Muted",
        )
        details.setWordWrap(True)
        root.addWidget(details)

        self.preview_stack = QStackedWidget()
        self.poster_label = QLabel("Preparing video preview…", objectName="Muted")
        self.poster_label.setAlignment(Qt.AlignCenter)
        self.poster_label.setMinimumHeight(300)
        self.poster_label.setFrameShape(QFrame.StyledPanel)
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(300)
        self.video_widget.setAspectRatioMode(Qt.KeepAspectRatio)
        self.preview_stack.addWidget(self.poster_label)
        self.preview_stack.addWidget(self.video_widget)
        self.preview_stack.setCurrentWidget(self.poster_label)
        root.addWidget(self.preview_stack, 1)
        self.audio_output: QAudioOutput | None = None
        self.player: QMediaPlayer | None = None
        if source.exists():
            self.audio_output = QAudioOutput(self)
            self.audio_output.setVolume(0.65)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio_output)
            self.player.setVideoOutput(self.video_widget)
            self.player.setSource(QUrl.fromLocalFile(str(source.resolve())))
            self.player.positionChanged.connect(self._player_position_changed)
            self.player.durationChanged.connect(self._player_duration_changed)
            self.player.playbackStateChanged.connect(self._playback_state_changed)
            self.player.errorOccurred.connect(self._player_error)

        playback_row = QHBoxLayout()
        self.play_button = QPushButton("▶ Play")
        self.play_button.clicked.connect(self._toggle_playback)
        playback_row.addWidget(self.play_button)
        set_in = QPushButton("Set In")
        set_in.clicked.connect(self._set_in_at_playhead)
        playback_row.addWidget(set_in)
        set_out = QPushButton("Set Out")
        set_out.clicked.connect(self._set_out_at_playhead)
        playback_row.addWidget(set_out)
        select_all = QPushButton("Select full video")
        select_all.clicked.connect(self._select_all)
        playback_row.addWidget(select_all)
        playback_row.addStretch(1)
        self.position_label = QLabel("0:00.00 / " + self._format_ms(self._duration_ms), objectName="Muted")
        playback_row.addWidget(self.position_label)
        root.addLayout(playback_row)

        self.timeline = RangeTimeline()
        self.timeline.set_duration(self._duration_ms)
        self.timeline.set_range(0, self._duration_ms)
        self.timeline.rangeChanged.connect(self._timeline_range_changed)
        self.timeline.playheadChanged.connect(self._seek_player)
        root.addWidget(self.timeline)

        form = QFormLayout()
        self.start_seconds = QDoubleSpinBox()
        self.start_seconds.setDecimals(3)
        self.start_seconds.setSingleStep(0.25)
        self.start_seconds.setSuffix(" s")
        self.start_seconds.setRange(0.0, max(0.0, self._duration_ms / 1000 - 0.02))
        form.addRow("Precise start", self.start_seconds)

        self.duration_seconds = QDoubleSpinBox()
        self.duration_seconds.setDecimals(3)
        self.duration_seconds.setSingleStep(0.25)
        self.duration_seconds.setSuffix(" s")
        self.duration_seconds.setRange(0.02, self._duration_ms / 1000)
        self.duration_seconds.setValue(self._duration_ms / 1000)
        form.addRow("Precise duration", self.duration_seconds)

        self.target_fps = QSpinBox()
        self.target_fps.setRange(1, 60)
        self.target_fps.setSuffix(" fps")
        self.target_fps.setValue(max(1, min(12, round(float(info["fps"])))))
        self.target_fps.setToolTip("This controls the editable proxy, not direct source-video export quality.")
        form.addRow("Editing proxy frame rate", self.target_fps)

        self.max_side = QSpinBox()
        self.max_side.setRange(0, 7680)
        self.max_side.setSpecialValueText("Original")
        self.max_side.setSuffix(" px")
        source_side = max(int(info["width"]), int(info["height"]))
        self.max_side.setValue(1280 if source_side > 1280 else 0)
        self.max_side.setToolTip("Caps the editable proxy only. Direct MP4/WebM export still uses the original source.")
        form.addRow("Editing proxy maximum edge", self.max_side)
        root.addLayout(form)

        note = QLabel(
            "There is no fixed clip-length limit. Long selections are opened as a sparse, memory-bounded editing proxy. "
            "Unedited MP4/WebM export continues to read the original video directly at full resolution with audio.",
            objectName="Muted",
        )
        note.setWordWrap(True)
        root.addWidget(note)
        self.estimate = QLabel(objectName="Muted")
        self.estimate.setWordWrap(True)
        root.addWidget(self.estimate)

        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Open).setText("Open selected range")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.start_seconds.valueChanged.connect(self._precision_range_changed)
        self.duration_seconds.valueChanged.connect(self._precision_range_changed)
        self.target_fps.valueChanged.connect(self._update_estimate)
        self.max_side.valueChanged.connect(self._update_estimate)
        self._precision_range_changed()
        self._start_filmstrip_load()

    @staticmethod
    def _format_ms(value_ms: int) -> str:
        total_seconds = max(0, int(value_ms)) / 1000
        minutes = int(total_seconds // 60)
        seconds = total_seconds - minutes * 60
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours:d}:{minutes:02d}:{seconds:05.2f}"
        return f"{minutes:d}:{seconds:05.2f}"

    def _start_filmstrip_load(self) -> None:
        if not self.source.exists():
            return
        self._thumbnail_future = self._thumbnail_executor.submit(
            read_video_timeline_thumbnails,
            self.source,
            duration_ms=self._duration_ms,
            count=10,
            cancel=self._thumbnail_cancel.is_set,
        )
        self._thumbnail_poll = QTimer(self)
        self._thumbnail_poll.setInterval(80)
        self._thumbnail_poll.timeout.connect(self._poll_filmstrip)
        self._thumbnail_poll.start()

    def _poll_filmstrip(self) -> None:
        future = self._thumbnail_future
        if future is None or not future.done():
            return
        self._thumbnail_poll.stop()
        try:
            images = future.result()
            pixmaps = [QPixmap.fromImage(ImageQt(image)) for image in images]
            self.timeline.set_thumbnails(pixmaps)
            if pixmaps:
                self.poster_label.setText("")
                self.poster_label.setPixmap(
                    pixmaps[0].scaled(760, 430, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            for image in images:
                image.close()
        except Exception:
            pass

    def _player_duration_changed(self, duration_ms: int) -> None:
        if duration_ms <= 0 or abs(duration_ms - self._duration_ms) < 20:
            return
        old_full_selection = self.timeline.in_ms == 0 and self.timeline.out_ms == self._duration_ms
        self._duration_ms = max(20, int(duration_ms))
        self.timeline.set_duration(self._duration_ms)
        if old_full_selection:
            self.timeline.set_range(0, self._duration_ms)
            self.duration_seconds.setValue(self._duration_ms / 1000)
        self.start_seconds.setMaximum(max(0.0, self._duration_ms / 1000 - 0.02))
        self._precision_range_changed()

    def _player_position_changed(self, position_ms: int) -> None:
        self.timeline.set_playhead(position_ms)
        self.position_label.setText(f"{self._format_ms(position_ms)} / {self._format_ms(self._duration_ms)}")
        if self.player is not None and self.player.playbackState() == QMediaPlayer.PlayingState and position_ms >= self.timeline.out_ms:
            self.player.pause()
            self.player.setPosition(self.timeline.in_ms)

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_button.setText("❚❚ Pause" if state == QMediaPlayer.PlayingState else "▶ Play")

    def _player_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        self.preview_stack.setCurrentWidget(self.poster_label)
        if self.poster_label.pixmap().isNull():
            self.poster_label.setText("Video playback preview is unavailable. The filmstrip and trim handles still work.")
        self.play_button.setEnabled(False)
        self.play_button.setToolTip(message or "Qt could not play this source video on the current system.")

    def _toggle_playback(self) -> None:
        if self.player is None:
            return
        self.preview_stack.setCurrentWidget(self.video_widget)
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            return
        position = self.player.position()
        if position < self.timeline.in_ms or position >= self.timeline.out_ms:
            self.player.setPosition(self.timeline.in_ms)
        self.player.play()

    def _seek_player(self, position_ms: int) -> None:
        if self.player is not None:
            self.preview_stack.setCurrentWidget(self.video_widget)
            self.player.setPosition(max(0, min(position_ms, self._duration_ms)))

    def _set_in_at_playhead(self) -> None:
        self.timeline.set_range(min(self.timeline.playhead_ms, self.timeline.out_ms - 20), self.timeline.out_ms, emit=True)

    def _set_out_at_playhead(self) -> None:
        self.timeline.set_range(self.timeline.in_ms, max(self.timeline.playhead_ms, self.timeline.in_ms + 20), emit=True)

    def _select_all(self) -> None:
        self.timeline.set_range(0, self._duration_ms, emit=True)
        self.timeline.set_playhead(0, emit=True)

    def _timeline_range_changed(self, start_ms: int, end_ms: int) -> None:
        if self._range_syncing:
            return
        self._range_syncing = True
        try:
            self.start_seconds.blockSignals(True)
            self.duration_seconds.blockSignals(True)
            self.start_seconds.setValue(start_ms / 1000)
            self.duration_seconds.setMaximum(max(0.02, (self._duration_ms - start_ms) / 1000))
            self.duration_seconds.setValue((end_ms - start_ms) / 1000)
        finally:
            self.start_seconds.blockSignals(False)
            self.duration_seconds.blockSignals(False)
            self._range_syncing = False
        self._update_estimate()

    def _precision_range_changed(self) -> None:
        if self._range_syncing:
            return
        self._range_syncing = True
        try:
            start_ms = round(self.start_seconds.value() * 1000)
            remaining_ms = max(20, self._duration_ms - start_ms)
            self.duration_seconds.setMaximum(remaining_ms / 1000)
            duration_ms = min(remaining_ms, max(20, round(self.duration_seconds.value() * 1000)))
            self.timeline.set_range(start_ms, start_ms + duration_ms)
        finally:
            self._range_syncing = False
        self._update_estimate()

    def _update_estimate(self) -> None:
        duration_ms = max(20, self.timeline.out_ms - self.timeline.in_ms)
        requested_frames = max(2, math.ceil(duration_ms * self.target_fps.value() / 1000))
        width, height = int(self.info["width"]), int(self.info["height"])
        try:
            stride, budget_scale, _reduced = choose_animation_reduction(requested_frames, duration_ms, width, height)
            cap = self.max_side.value()
            side_scale = min(1.0, cap / max(width, height)) if cap > 0 and max(width, height) else 1.0
            scale = min(budget_scale, side_scale)
            proxy_frames = max(2, math.ceil(requested_frames / stride))
            proxy_width = max(1, round(width * scale))
            proxy_height = max(1, round(height * scale))
            decoded_bytes = proxy_frames * proxy_width * proxy_height * 4
            proxy_fps = self.target_fps.value() / stride
            self.estimate.setText(
                f"Selected {self._format_ms(duration_ms)} · editing proxy about {proxy_frames:,} frames at "
                f"{proxy_fps:.2f} fps and {proxy_width}×{proxy_height} · roughly "
                f"{_human_file_size(decoded_bytes * 2)} for current + original proxy frames before history."
            )
        except Exception as exc:
            self.estimate.setText(str(exc))

    def options(self) -> dict[str, object]:
        return {
            "start_ms": self.timeline.in_ms,
            "duration_ms": self.timeline.out_ms - self.timeline.in_ms,
            "target_fps": self.target_fps.value(),
            "max_side": self.max_side.value() or None,
        }

    def done(self, result: int) -> None:  # type: ignore[override]
        if self.player is not None:
            self.player.stop()
        self._thumbnail_cancel.set()
        if hasattr(self, "_thumbnail_poll"):
            self._thumbnail_poll.stop()
        self._thumbnail_executor.shutdown(wait=False, cancel_futures=True)
        super().done(result)


class AnimationExportDialog(QDialog):
    """Visual in/out timeline and format options for animation/video export."""

    def __init__(self, document: ImageDocument, suffix: str, parent=None) -> None:
        super().__init__(parent)
        self.document = document
        self.suffix = suffix.lower()
        self.total_ms = max(20, document.animation_duration_ms)
        self.total_seconds = self.total_ms / 1000
        self._range_syncing = False
        self._preview_started_at = 0.0
        self._frame_starts: list[int] = []
        cursor = 0
        for duration in document.frame_durations:
            self._frame_starts.append(cursor)
            cursor += max(10, int(duration or 100))
        self.setWindowTitle("Video export" if self.suffix in {".mp4", ".webm"} else "Animation export")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(720, 620)
        self.resize(920, 760)

        root = QVBoxLayout(self)
        title = QLabel("Choose the export range on the timeline", objectName="Brand")
        title.setWordWrap(True)
        root.addWidget(title)
        info = QLabel(
            f"Current document: {document.frame_count:,} frames · {self._format_ms(self.total_ms)} · "
            f"{document.image.width}×{document.image.height}",
            objectName="Muted",
        )
        root.addWidget(info)

        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(250)
        self.preview_label.setFrameShape(QFrame.StyledPanel)
        root.addWidget(self.preview_label, 1)

        playback_row = QHBoxLayout()
        self.preview_play_button = QPushButton("▶ Play selection")
        self.preview_play_button.clicked.connect(self._toggle_preview)
        playback_row.addWidget(self.preview_play_button)
        set_in = QPushButton("Set In")
        set_in.clicked.connect(self._set_in_at_playhead)
        playback_row.addWidget(set_in)
        set_out = QPushButton("Set Out")
        set_out.clicked.connect(self._set_out_at_playhead)
        playback_row.addWidget(set_out)
        select_all = QPushButton("Select all")
        select_all.clicked.connect(self._select_all)
        playback_row.addWidget(select_all)
        playback_row.addStretch(1)
        self.preview_position_label = QLabel(objectName="Muted")
        playback_row.addWidget(self.preview_position_label)
        root.addLayout(playback_row)

        self.timeline = RangeTimeline()
        self.timeline.set_duration(self.total_ms)
        self.timeline.set_range(0, self.total_ms)
        self.timeline.set_thumbnails(self._document_thumbnails())
        self.timeline.rangeChanged.connect(self._timeline_range_changed)
        self.timeline.playheadChanged.connect(self._show_preview_at)
        root.addWidget(self.timeline)

        form = QFormLayout()
        self.start_seconds = QDoubleSpinBox()
        self.start_seconds.setDecimals(3)
        self.start_seconds.setSingleStep(0.10)
        self.start_seconds.setSuffix(" s")
        self.start_seconds.setRange(0.0, max(0.0, self.total_seconds - 0.02))
        form.addRow("Precise start", self.start_seconds)

        self.clip_seconds = QDoubleSpinBox()
        self.clip_seconds.setDecimals(3)
        self.clip_seconds.setSingleStep(0.10)
        self.clip_seconds.setSuffix(" s")
        self.clip_seconds.setRange(0.02, self.total_seconds)
        self.clip_seconds.setValue(self.total_seconds)
        form.addRow("Precise range duration", self.clip_seconds)

        self.output_seconds = QDoubleSpinBox()
        self.output_seconds.setDecimals(3)
        self.output_seconds.setSingleStep(0.10)
        self.output_seconds.setSuffix(" s")
        self.output_seconds.setRange(0.02, max(1_000_000.0, self.total_seconds))
        self.output_seconds.setValue(self.total_seconds)
        self.output_seconds.setToolTip("Changes GIF playback speed without changing the selected source frames.")
        if self.suffix == ".gif":
            form.addRow("Exported GIF duration", self.output_seconds)

        self.gif_colors = QSpinBox()
        self.gif_colors.setRange(2, 256)
        self.gif_colors.setValue(256)
        if self.suffix == ".gif":
            form.addRow("GIF colors", self.gif_colors)
        self.gif_dither = QCheckBox("Dither colors")
        self.gif_dither.setChecked(True)
        if self.suffix == ".gif":
            form.addRow("GIF palette", self.gif_dither)
        self.gif_optimize = QCheckBox("Optimize GIF file size (slower)")
        if self.suffix == ".gif":
            form.addRow("Compression", self.gif_optimize)

        self.video_fps = QSpinBox()
        self.video_fps.setRange(0, 60)
        self.video_fps.setSpecialValueText("Automatic")
        self.video_fps.setSuffix(" fps")
        self.video_bitrate = QSpinBox()
        self.video_bitrate.setRange(0, 100000)
        self.video_bitrate.setSpecialValueText("Automatic")
        self.video_bitrate.setSuffix(" kbps")
        self.direct_video = QCheckBox("Use original video directly (fast, full resolution, preserves audio)")
        direct_source = document.direct_video_source
        self.direct_video.setEnabled(direct_source is not None)
        self.direct_video.setChecked(direct_source is not None)
        if direct_source is None:
            self.direct_video.setToolTip(
                "Direct source export is available only while an imported MP4/WebM still matches its original pixels."
            )
        self.preserve_audio = QCheckBox("Preserve source audio when available")
        source_video = document.source_video
        self.preserve_audio.setEnabled(source_video is not None)
        self.preserve_audio.setChecked(source_video is not None)
        if source_video is None:
            self.preserve_audio.setToolTip("This document was not imported from an available MP4/WebM source.")
        else:
            self.preserve_audio.setToolTip(
                "For edited video, ImageSuite renders the new frames first and then attaches the matching source-audio segment."
            )
        if self.suffix in {".mp4", ".webm"}:
            form.addRow("Video source", self.direct_video)
            form.addRow("Audio", self.preserve_audio)
            form.addRow("Video frame rate", self.video_fps)
            form.addRow("Video bitrate", self.video_bitrate)
        root.addLayout(form)

        self.summary = QLabel(objectName="Muted")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Export selected range")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(30)
        self._preview_timer.timeout.connect(self._preview_tick)
        self.start_seconds.valueChanged.connect(self._precision_range_changed)
        self.clip_seconds.valueChanged.connect(self._precision_range_changed)
        self.output_seconds.valueChanged.connect(self._update_summary)
        self.direct_video.toggled.connect(self._update_summary)
        self.preserve_audio.toggled.connect(self._update_summary)
        self._show_preview_at(0)
        self._update_summary()

    @staticmethod
    def _format_ms(value_ms: int) -> str:
        return RangeTimeline._format_ms(value_ms)

    def _document_thumbnails(self) -> list[QPixmap]:
        frames = self.document.animation_frames
        if not frames:
            return []
        count = min(14, len(frames))
        indices = [round(index * (len(frames) - 1) / max(1, count - 1)) for index in range(count)]
        pixmaps: list[QPixmap] = []
        for index in indices:
            frame = frames[index]
            preview = frame.copy() if frame.mode == "RGBA" else frame.convert("RGBA")
            try:
                preview.thumbnail((160, 90), Image.Resampling.LANCZOS)
                pixmaps.append(QPixmap.fromImage(ImageQt(preview)))
            finally:
                preview.close()
        return pixmaps

    def _frame_index_at(self, position_ms: int) -> int:
        if not self._frame_starts:
            return 0
        return max(0, min(len(self._frame_starts) - 1, bisect_right(self._frame_starts, position_ms) - 1))

    def _show_preview_at(self, position_ms: int) -> None:
        position = max(0, min(int(position_ms), self.total_ms))
        self.timeline.set_playhead(position)
        frame = self.document.animation_frames[self._frame_index_at(position)]
        preview = frame.copy() if frame.mode == "RGBA" else frame.convert("RGBA")
        try:
            preview.thumbnail((760, 390), Image.Resampling.LANCZOS)
            self.preview_label.setPixmap(QPixmap.fromImage(ImageQt(preview)))
        finally:
            preview.close()
        self.preview_position_label.setText(f"{self._format_ms(position)} / {self._format_ms(self.total_ms)}")

    def _toggle_preview(self) -> None:
        if self._preview_timer.isActive():
            self._stop_preview()
            return
        position = self.timeline.playhead_ms
        if position < self.timeline.in_ms or position >= self.timeline.out_ms:
            position = self.timeline.in_ms
            self._show_preview_at(position)
        self._preview_started_at = time.monotonic() - (position - self.timeline.in_ms) / 1000
        self._preview_timer.start()
        self.preview_play_button.setText("❚❚ Pause")

    def _stop_preview(self) -> None:
        self._preview_timer.stop()
        self.preview_play_button.setText("▶ Play selection")

    def _preview_tick(self) -> None:
        position = self.timeline.in_ms + round((time.monotonic() - self._preview_started_at) * 1000)
        if position >= self.timeline.out_ms:
            self._stop_preview()
            self._show_preview_at(self.timeline.in_ms)
            return
        self._show_preview_at(position)

    def _set_in_at_playhead(self) -> None:
        self.timeline.set_range(min(self.timeline.playhead_ms, self.timeline.out_ms - 20), self.timeline.out_ms, emit=True)

    def _set_out_at_playhead(self) -> None:
        self.timeline.set_range(self.timeline.in_ms, max(self.timeline.playhead_ms, self.timeline.in_ms + 20), emit=True)

    def _select_all(self) -> None:
        self.timeline.set_range(0, self.total_ms, emit=True)
        self._show_preview_at(0)

    def _timeline_range_changed(self, start_ms: int, end_ms: int) -> None:
        if self._range_syncing:
            return
        self._range_syncing = True
        try:
            self.start_seconds.blockSignals(True)
            self.clip_seconds.blockSignals(True)
            self.start_seconds.setValue(start_ms / 1000)
            self.clip_seconds.setMaximum(max(0.02, (self.total_ms - start_ms) / 1000))
            self.clip_seconds.setValue((end_ms - start_ms) / 1000)
            if self.suffix == ".gif":
                self.output_seconds.setValue((end_ms - start_ms) / 1000)
        finally:
            self.start_seconds.blockSignals(False)
            self.clip_seconds.blockSignals(False)
            self._range_syncing = False
        if not start_ms <= self.timeline.playhead_ms <= end_ms:
            self._show_preview_at(start_ms)
        self._update_summary()

    def _precision_range_changed(self) -> None:
        if self._range_syncing:
            return
        self._range_syncing = True
        try:
            start_ms = round(self.start_seconds.value() * 1000 / 10) * 10
            remaining_ms = max(20, self.total_ms - start_ms)
            self.clip_seconds.setMaximum(remaining_ms / 1000)
            clip_ms = min(remaining_ms, max(20, round(self.clip_seconds.value() * 1000 / 10) * 10))
            self.timeline.set_range(start_ms, start_ms + clip_ms)
            if self.suffix == ".gif":
                self.output_seconds.setValue(clip_ms / 1000)
        finally:
            self._range_syncing = False
        self._update_summary()

    # Compatibility entrypoints kept for older automation/tests.
    def _range_changed(self) -> None:
        self._precision_range_changed()

    def _clip_changed(self) -> None:
        self._precision_range_changed()

    def _update_summary(self) -> None:
        try:
            frames, durations = slice_animation(
                self.document.animation_frames,
                self.document.frame_durations,
                start_ms=self.timeline.in_ms,
                duration_ms=self.timeline.out_ms - self.timeline.in_ms,
            )
            minimum = max(0.02, len(frames) / 100)
            if self.suffix == ".gif":
                self.output_seconds.setMinimum(minimum)
            target = self.output_seconds.value() if self.suffix == ".gif" else sum(durations) / 1000
            if self.suffix in {".mp4", ".webm"} and self.direct_video.isChecked():
                source = self.document.direct_video_source
                audio_text = "with source audio when present" if self.preserve_audio.isChecked() else "without audio"
                self.summary.setText(
                    f"Direct FFmpeg export from {source.name if source else 'the original video'}: "
                    f"{sum(durations) / 1000:.2f}s at original resolution {audio_text}. "
                    "The timeline maps directly to the source; no GIF or proxy-frame conversion is used."
                )
            elif self.suffix in {".mp4", ".webm"}:
                audio_text = (
                    "The matching source-audio segment will be attached after rendering."
                    if self.preserve_audio.isChecked()
                    else "The export will be silent."
                )
                self.summary.setText(
                    f"Rendered video export: {len(frames):,} editable proxy frames · {target:.2f}s. "
                    f"Frames encode straight to video; {audio_text}"
                )
            else:
                self.summary.setText(
                    f"Export selection: {len(frames):,} frames · source {sum(durations) / 1000:.2f}s · "
                    f"output {target:.2f}s."
                )
        except Exception as exc:
            self.summary.setText(str(exc))

    def options(self) -> dict[str, object]:
        clip_ms = self.timeline.out_ms - self.timeline.in_ms
        return {
            "start_ms": self.timeline.in_ms,
            "duration_ms": clip_ms,
            "output_duration_ms": round(self.output_seconds.value() * 1000 / 10) * 10 if self.suffix == ".gif" else clip_ms,
            "gif_colors": self.gif_colors.value(),
            "gif_dither": self.gif_dither.isChecked(),
            "gif_optimize": self.gif_optimize.isChecked(),
            "fps": self.video_fps.value(),
            "bitrate_kbps": self.video_bitrate.value(),
            "direct_video": self.direct_video.isChecked() and self.direct_video.isEnabled(),
            "preserve_audio": self.preserve_audio.isChecked() and self.preserve_audio.isEnabled(),
        }

    def done(self, result: int) -> None:  # type: ignore[override]
        self._stop_preview()
        super().done(result)


class EditorWorkspace(QWidget):
    LIVE_PREVIEW_MAX_PIXELS = 1_000_000
    LIVE_ADJUSTMENT_PREVIEW_MAX_PIXELS = 650_000
    LIVE_CREATIVE_PREVIEW_MAX_PIXELS = 450_000
    LIVE_ANIMATION_PREVIEW_MAX_PIXELS = 360_000
    LIVE_ANIMATION_CACHE_BYTES = 48 * 1024 * 1024

    STICKER_CATEGORIES = {
        "Faces": ["😀", "😎", "😂", "🤣", "😍", "🥰", "😘", "🤔", "😮", "😱", "😭", "😡", "🤯", "🥳", "🤖", "👻"],
        "Reactions": ["❤️", "💔", "🔥", "✨", "⭐", "💥", "💯", "👍", "👎", "👏", "🙏", "👀", "💀", "🎉", "🚀", "💡"],
        "Marks": ["✅", "❌", "❗", "❓", "⚠️", "🚫", "🔒", "🔓", "📌", "📍", "➡️", "⬅️", "⬆️", "⬇️", "⭕", "❎"],
        "Speech": ["💬", "🗨️", "🗯️", "💭", "📢", "🔔", "🔕", "🎵", "🎤", "📞", "✉️", "📨"],
        "Objects": ["📷", "🎮", "💻", "📱", "🎁", "🏆", "🎯", "💎", "🔑", "🛡️", "⚡", "☀️", "🌙", "☁️", "🌈", "🍕"],
    }

    CENSOR_EFFECTS = [
        "Soft Blur", "Privacy Blur", "Directional Blur", "Pixelate", "Mosaic",
        "Frosted Glass", "Faceted Glass", "Encrypted Tiles", "Prism Split", "Wave Scramble",
        "Black Redaction", "White Redaction", "Noise Redaction", "Marker Scribble",
        "Redaction Tape", "Halftone Dots", "Barcode Redaction", "Ordered Dither",
        "Glitch Blocks", "CRT Distortion", "Silhouette", "Comic Cutout",
        "Thermal Map", "Photocopy", "ASCII Art", "Blueprint", "Neon Edges",
        "Topographic Lines",
    ]
    EFFECT_PARAMETER_KEYS = ("amount", "size", "softness", "detail", "angle", "edge", "phase")
    # Each effect owns its parameter meanings. Irrelevant sliders are hidden,
    # never left disabled in the UI. Tuple: label, minimum, maximum, default, suffix.
    EFFECT_PARAMETERS = {
        "Soft Blur": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Radius", 1, 80, 12, " px"),
            "softness": ("Detail destruction", 0, 100, 10, "%"),
            "detail": ("Grain", 0, 100, 0, "%"),
        },
        "Privacy Blur": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Radius", 2, 100, 24, " px"),
            "softness": ("Downsample", 0, 100, 72, "%"),
            "detail": ("Grain", 0, 100, 12, "%"),
        },
        "Directional Blur": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Length", 1, 120, 28, " px"),
            "softness": ("Samples", 2, 21, 11, ""),
            "detail": ("Grain", 0, 100, 0, "%"),
            "angle": ("Angle", -90, 90, 0, "°"),
        },
        "Pixelate": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Block size", 2, 120, 20, " px"),
            "softness": ("Edge softness", 0, 100, 0, "%"),
            "detail": ("Color levels", 2, 256, 256, ""),
            "angle": ("Grid strength", 0, 100, 0, "%"),
        },
        "Mosaic": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Tile size", 3, 120, 24, " px"),
            "softness": ("Pre-blur", 0, 100, 18, "%"),
            "detail": ("Grid opacity", 0, 100, 45, "%"),
            "angle": ("Grid width", 1, 8, 1, " px"),
        },
        "Frosted Glass": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Blur radius", 1, 60, 12, " px"),
            "softness": ("Refraction", 0, 100, 38, "%"),
            "detail": ("Grain", 0, 100, 28, "%"),
            "angle": ("Distortion scale", 2, 40, 10, " px"),
        },
        "Faceted Glass": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Facet size", 8, 120, 30, " px"),
            "softness": ("Irregularity", 0, 100, 45, "%"),
            "detail": ("Edge strength", 0, 100, 28, "%"),
            "angle": ("Skew", -100, 100, 0, "%"),
        },
        "Encrypted Tiles": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Tile size", 4, 100, 24, " px"),
            "softness": ("Shuffle", 0, 100, 84, "%"),
            "detail": ("Tile rotation", 0, 100, 55, "%"),
            "angle": ("Color shift", 0, 80, 24, "%"),
        },
        "Prism Split": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Separation", 1, 80, 14, " px"),
            "softness": ("Softness", 0, 100, 8, "%"),
            "detail": ("Saturation", 0, 100, 28, "%"),
            "angle": ("Angle", -180, 180, 0, "°"),
        },
        "Wave Scramble": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Wavelength", 8, 160, 44, " px"),
            "softness": ("Amplitude", 0, 100, 24, " px"),
            "detail": ("Complexity", 1, 6, 2, ""),
            "angle": ("Direction", -90, 90, 0, "°"),
        },
        "Black Redaction": {
            "amount": ("Opacity", 0, 100, 100, "%"),
            "size": ("Texture scale", 2, 80, 20, " px"),
            "softness": ("Texture", 0, 100, 0, "%"),
            "detail": ("Grain", 0, 100, 0, "%"),
            "angle": ("Texture angle", -90, 90, 0, "°"),
        },
        "White Redaction": {
            "amount": ("Opacity", 0, 100, 100, "%"),
            "size": ("Texture scale", 2, 80, 20, " px"),
            "softness": ("Texture", 0, 100, 0, "%"),
            "detail": ("Grain", 0, 100, 0, "%"),
            "angle": ("Texture angle", -90, 90, 0, "°"),
        },
        "Noise Redaction": {
            "amount": ("Mix", 0, 100, 92, "%"),
            "size": ("Grain size", 1, 24, 3, " px"),
            "softness": ("Softness", 0, 100, 5, "%"),
            "detail": ("Color amount", 0, 100, 45, "%"),
            "angle": ("Contrast", 0, 100, 72, "%"),
        },
        "Marker Scribble": {
            "amount": ("Opacity", 0, 100, 96, "%"),
            "size": ("Stroke width", 3, 100, 20, " px"),
            "softness": ("Feather", 0, 100, 8, "%"),
            "detail": ("Density", 10, 100, 72, "%"),
            "angle": ("Direction", -180, 180, 0, "°"),
        },
        "Redaction Tape": {
            "amount": ("Opacity", 0, 100, 100, "%"),
            "size": ("Crease spacing", 6, 100, 22, " px"),
            "softness": ("Softness", 0, 100, 8, "%"),
            "detail": ("Texture", 0, 100, 58, "%"),
            "angle": ("Angle", -180, 180, 0, "°"),
        },
        "Halftone Dots": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Cell size", 3, 60, 12, " px"),
            "softness": ("Dot softness", 0, 100, 15, "%"),
            "detail": ("Contrast", 0, 100, 72, "%"),
            "angle": ("Screen angle", -90, 90, 15, "°"),
        },
        "Barcode Redaction": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Bar width", 2, 60, 8, " px"),
            "softness": ("Softness", 0, 100, 0, "%"),
            "detail": ("Contrast", 0, 100, 78, "%"),
            "angle": ("Angle", -90, 90, 0, "°"),
        },
        "Ordered Dither": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Dot scale", 1, 12, 3, " px"),
            "softness": ("Levels", 2, 8, 2, ""),
            "detail": ("Contrast", 0, 100, 68, "%"),
            "angle": ("Pattern angle", -90, 90, 0, "°"),
        },
        "Glitch Blocks": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Block size", 3, 80, 14, " px"),
            "softness": ("Displacement", 1, 120, 36, " px"),
            "detail": ("Density", 1, 100, 68, "%"),
            "angle": ("Direction", -180, 180, 0, "°"),
        },
        "CRT Distortion": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Scanline spacing", 2, 30, 7, " px"),
            "softness": ("Bloom", 0, 100, 22, "%"),
            "detail": ("Signal noise", 0, 100, 28, "%"),
            "angle": ("RGB separation", 0, 60, 8, " px"),
        },
        "Silhouette": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Smoothing", 0, 60, 8, " px"),
            "softness": ("Threshold", 0, 100, 50, "%"),
            "detail": ("Contrast", 0, 100, 78, "%"),
            "angle": ("Tone balance", 0, 100, 50, "%"),
        },
        "Comic Cutout": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Color levels", 2, 16, 5, ""),
            "softness": ("Edge width", 1, 7, 2, " px"),
            "detail": ("Edge strength", 0, 100, 72, "%"),
            "angle": ("Saturation", -100, 100, 32, "%"),
        },
        "Thermal Map": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Smoothing", 0, 30, 3, " px"),
            "softness": ("Contrast", 0, 100, 72, "%"),
            "detail": ("Palette shift", 0, 100, 0, "%"),
            "angle": ("Edge detail", 0, 100, 58, "%"),
        },
        "Photocopy": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Grain size", 1, 16, 2, " px"),
            "softness": ("Threshold", 0, 100, 42, "%"),
            "detail": ("Edge strength", 0, 100, 50, "%"),
            "angle": ("Ink spread", 0, 100, 8, "%"),
        },
        "ASCII Art": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Cell size", 5, 28, 10, " px"),
            "softness": ("Contrast", 0, 100, 58, "%"),
            "detail": ("Charset density", 0, 100, 62, "%"),
            "angle": ("Color preservation", 0, 100, 72, "%"),
            "edge": ("Contour strength", 0, 100, 76, "%"),
            "phase": ("Tone polarity", 0, 100, 0, "%"),
        },
        "Blueprint": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Grid spacing", 8, 64, 20, " px"),
            "softness": ("Line softness", 0, 100, 14, "%"),
            "detail": ("Edge contrast", 0, 100, 72, "%"),
            "angle": ("Grid opacity", 0, 100, 28, "%"),
        },
        "Neon Edges": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Blur radius", 0, 40, 8, " px"),
            "softness": ("Edge strength", 0, 100, 74, "%"),
            "detail": ("Glow", 0, 100, 64, "%"),
            "angle": ("Hue shift", -180, 180, 0, "°"),
        },
        "Topographic Lines": {
            "amount": ("Mix", 0, 100, 100, "%"),
            "size": ("Contour step", 6, 40, 16, ""),
            "softness": ("Smoothing", 0, 100, 12, "%"),
            "detail": ("Contrast", 0, 100, 68, "%"),
            "angle": ("Line width", 1, 8, 2, " px"),
        },
    }
    EFFECT_ALIASES = {
        "Blur": "Soft Blur", "Deep Blur": "Privacy Blur", "Black": "Black Redaction",
        "White": "White Redaction", "Noise": "Noise Redaction", "Halftone": "Halftone Dots",
        "Glitch": "Glitch Blocks", "Glass Tiles": "Encrypted Tiles",
    }
    EFFECT_DESCRIPTIONS = {
        "Soft Blur": "Smooth blur with adjustable detail destruction and grain.",
        "Privacy Blur": "Aggressive downsample-and-blur designed to remove facial and text detail.",
        "Directional Blur": "Long adjustable streaking with a true direction control.",
        "Pixelate": "Adjust block size, smoothing, color quantization, and grid strength.",
        "Mosaic": "Average-color tiles with pre-blur and fully adjustable grid styling.",
        "Frosted Glass": "Blur, refraction, and grain combined into a tunable privacy glass.",
        "Faceted Glass": "Irregular polygon facets with adjustable edges and skew.",
        "Encrypted Tiles": "Scrambles, rotates, and color-shifts individual image tiles.",
        "Prism Split": "Directional RGB separation with adjustable softness and saturation.",
        "Wave Scramble": "Non-wrapping wave displacement with direction and harmonic complexity.",
        "Black Redaction": "Opaque black redaction with adjustable independent texture, grain, and angle.",
        "White Redaction": "Opaque white redaction with adjustable independent texture, grain, and angle.",
        "Noise Redaction": "Deterministic monochrome-to-color noise with adjustable grain and contrast.",
        "Marker Scribble": "Directional hand-drawn strokes with width, feathering, and density controls.",
        "Redaction Tape": "Rotatable textured tape with adjustable creases and softness.",
        "Halftone Dots": "Print-screen dots with adjustable angle, contrast, and edge softness.",
        "Barcode Redaction": "Brightness-driven bars with adjustable orientation and contrast.",
        "Ordered Dither": "Bayer-style dithering with levels, scale, contrast, and angle.",
        "Glitch Blocks": "Directional rectangular displacement without edge wrapping.",
        "CRT Distortion": "Scanlines, RGB separation, bloom, and deterministic signal noise.",
        "Silhouette": "Two-tone stencil with adjustable smoothing, threshold, and tone balance.",
        "Comic Cutout": "Posterized color with adjustable ink edges and saturation.",
        "Thermal Map": "False-color mapping with palette shift and edge-detail control.",
        "Photocopy": "High-contrast copier texture with grain, threshold, edges, and ink spread.",
        "ASCII Art": "Font-calibrated character art with a rich luminance ramp, hue-preserving color, adjustable dark-to-light density polarity, stable RGB precision, and contours that detect brightness and color boundaries.",
        "Blueprint": "White/cyan technical drawing lines over a classic blueprint grid.",
        "Neon Edges": "Glowing edge extraction on a dark background with adjustable hue.",
        "Topographic Lines": "Contour-line stylization that resembles a printed terrain map.",
    }


    statusChanged = Signal(str)
    documentAvailabilityChanged = Signal(bool)
    pathsOpened = Signal(list)
    sendToEnhance = Signal(list)
    recoveryFinished = Signal(str, int, bool, str)

    def __init__(self) -> None:
        super().__init__()
        self.documents: list[ImageDocument] = []
        self.active_index = -1
        self._switching = False
        self.history_depth = 30
        self.history_memory_mb = 512
        self.preserve_metadata = True
        data_dir = app_data_dir()
        self.recovery_dir = data_dir / "recovery"
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        self.transfer_dir = data_dir / "transfers"
        self.transfer_dir.mkdir(parents=True, exist_ok=True)
        self.failed_recovery_dir = data_dir / "recovery_failed"
        self._recovery_revision: dict[str, int] = {}
        self._recovery_futures: dict[str, Future] = {}
        self._recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="imagesuite-recovery")
        self._finalized = False
        self.recoveryFinished.connect(self._recovery_finished)
        self._cleanup_transfers()
        self.setAcceptDrops(True)
        self.quick_text_anchor: Optional[tuple[int, int]] = None
        self.sticker_anchor: Optional[tuple[int, int]] = None
        self.sticker_image_path = ""
        self.quick_text_data_path = data_dir / "quick_text.json"
        self.recent_texts: list[str] = []
        self.tool_buttons: dict[str, QToolButton] = {}
        self.effect_chain_list: Optional[QListWidget] = None
        self.effect_parameter_rows: dict[str, QWidget] = {}
        self.effect_parameter_labels: dict[str, QLabel] = {}
        self.effect_parameter_values: dict[str, QLabel] = {}
        self.effect_parameter_sliders: dict[str, QSlider] = {}
        self.effect_parameter_suffixes: dict[str, str] = {}
        self._loading_effect_spec = False
        self._loading_target_edge = False
        self.advanced_widgets: list[QWidget] = []
        self.shortcuts: list[QShortcut] = []
        self.focus_sensitive_shortcuts: list[tuple[QShortcut, str]] = []
        self._last_text_edit: dict[str, dict[str, object]] = {}
        self._last_sticker_edit: dict[str, dict[str, object]] = {}
        self._last_sticker_box: Optional[RectMask] = None
        self._pending_animation_notice = ""
        self._font_cache: dict[tuple[object, ...], ImageFont.ImageFont] = {}
        self._preview_kind: Optional[str] = None
        self._requested_preview_kind: Optional[str] = None
        self._preview_source_cache_key: Optional[tuple[str, int, int, int]] = None
        self._preview_source_cache: Optional[tuple[Image.Image, float, float]] = None
        self._effect_preview_timer = QTimer(self)
        self._effect_preview_timer.setSingleShot(True)
        self._effect_preview_timer.setInterval(55)
        self._effect_preview_timer.timeout.connect(self.preview_effect)
        self._adjustment_preview_timer = QTimer(self)
        self._adjustment_preview_timer.setSingleShot(True)
        self._adjustment_preview_timer.setInterval(60)
        self._adjustment_preview_timer.timeout.connect(self.preview_adjustments)
        self._creative_preview_timer = QTimer(self)
        self._creative_preview_timer.setSingleShot(True)
        self._creative_preview_timer.setInterval(75)
        self._creative_preview_timer.timeout.connect(self.preview_selected_creative)
        self._text_preview_timer = QTimer(self)
        self._text_preview_timer.setSingleShot(True)
        self._text_preview_timer.setInterval(35)
        self._text_preview_timer.timeout.connect(self.preview_quick_text)
        self._sticker_preview_timer = QTimer(self)
        self._sticker_preview_timer.setSingleShot(True)
        self._sticker_preview_timer.setInterval(35)
        self._sticker_preview_timer.timeout.connect(self.preview_sticker)
        self._gif_preview_timer = QTimer(self)
        self._gif_preview_timer.setSingleShot(True)
        self._gif_preview_timer.timeout.connect(self._advance_gif_preview)
        self._gif_playing = False
        self._gif_preview_index = 0
        self._gif_playback_started = 0.0
        self._gif_frame_ends: list[int] = []
        self._gif_frame_ends_key: Optional[tuple[str, int, int, int]] = None
        self._animation_frame_starts: list[int] = []
        self._animation_frame_starts_key: Optional[tuple[str, int, int]] = None
        self._gif_source_cache: OrderedDict[tuple[str, int, int, int, int], tuple[Image.Image, float, float, int]] = OrderedDict()
        self._gif_source_cache_bytes = 0
        self._animation_controls_updating = False
        self._loop_preview_start = 0
        self._loop_preview_end = 0
        self._load_quick_text_data()
        self._build_ui()
        self._bind_shortcuts()
        QApplication.instance().focusChanged.connect(self._update_shortcut_states)
        self._update_shortcut_states(None, QApplication.focusWidget())
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self._autosave)
        self.autosave_timer.start(30_000)
        QTimer.singleShot(350, self._offer_recovery)

    @property
    def document(self) -> Optional[ImageDocument]:
        if 0 <= self.active_index < len(self.documents):
            return self.documents[self.active_index]
        return None

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        toolbar = QFrame(objectName="TopBar")
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(9, 7, 9, 7)
        row.setSpacing(5)
        for text, callback, shortcut in [
            ("Open", self.open_images, "Ctrl+O"),
            ("Save", self.save, "Ctrl+S"),
            ("Undo", self.undo, "Ctrl+Z"),
            ("Redo", self.redo, "Ctrl+Y"),
        ]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            button.setToolTip(shortcut)
            row.addWidget(button)
        file_view = QToolButton()
        file_view.setText("More")
        file_view.setPopupMode(QToolButton.InstantPopup)
        file_view.setToolTip("Paste, Save As, and view controls")
        file_view_menu = QMenu(file_view)
        for text, callback, shortcut in [
            ("Paste image", self.paste_image, "Ctrl+V"),
            ("Save As…", self.save_as, "Ctrl+Shift+S"),
            ("Copy image", self.copy_image, ""),
            ("Play/Pause animation", self.toggle_gif_preview, ""),
            ("Fit image", self.fit, "F"),
            ("Actual size (100%)", self.actual_size, "1"),
        ]:
            action = file_view_menu.addAction(text)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
        file_view.setMenu(file_view_menu)
        row.addWidget(file_view)
        row.addSpacing(8)
        self.before_check = QCheckBox("Before")
        self.before_check.toggled.connect(self._toggle_before)
        row.addWidget(self.before_check)
        self.compare_check = QCheckBox("Compare")
        self.compare_check.toggled.connect(self._toggle_compare)
        row.addWidget(self.compare_check)
        self.compare_slider = QSlider(Qt.Horizontal)
        self.compare_slider.setRange(5, 95)
        self.compare_slider.setValue(50)
        self.compare_slider.setMaximumWidth(140)
        self.compare_slider.valueChanged.connect(self._set_compare_split)
        row.addWidget(self.compare_slider)
        row.addStretch(1)
        self.tool_label = QLabel("Rectangle", objectName="Muted")
        self.tool_label.setToolTip("Current editor tool")
        row.addWidget(self.tool_label)
        self.zoom_label = QLabel("100%", objectName="Muted")
        self.zoom_label.setMinimumWidth(54)
        self.zoom_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.zoom_label)
        root.addWidget(toolbar)

        # Create the canvas before constructing the sidebar. Several sidebar
        # controls bind directly to canvas methods and properties while their
        # tabs are being built. Creating it later caused an AttributeError at
        # startup before the main window could be shown.
        self.canvas = ImageCanvas()
        self.canvas.statusChanged.connect(self._status)
        self.canvas.documentChanged.connect(self._document_changed)
        self.canvas.previewTargetChanged.connect(self._preview_target_changed)
        self.canvas.canvasClicked.connect(self._canvas_clicked)
        self.canvas.textTransformChanged.connect(self._text_transform_changed)
        self.canvas.textApplyRequested.connect(self._apply_active_annotation)
        self.canvas.zoomChanged.connect(self._zoom_changed)
        self.canvas.cursorPositionChanged.connect(self._cursor_position_changed)
        self.canvas.brushSizeChanged.connect(self._canvas_brush_size_changed)
        self.canvas.filesDropped.connect(self.load_dropped_paths)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.editor_splitter = QSplitter(Qt.Horizontal)
        self.editor_splitter.setChildrenCollapsible(False)
        self.editor_splitter.setHandleWidth(6)
        sidebar = QFrame(objectName="SideBar")
        sidebar.setMinimumWidth(280)
        sidebar.setMaximumWidth(720)
        self.sidebar = sidebar
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 10, 8, 8)
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("QUICKFX", objectName="Brand"))
        title_row.addStretch(1)
        title_row.addWidget(QLabel("PySide6", objectName="SubBrand"))
        sidebar_layout.addLayout(title_row)
        self.advanced_toggle = QCheckBox("Show advanced controls")
        self.advanced_toggle.setToolTip("Reveal brushes, detailed text styling, and less common actions")
        self.advanced_toggle.toggled.connect(self._set_advanced_visible)
        sidebar_layout.addWidget(self.advanced_toggle)
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_tabs = QTabWidget()
        self.tool_tabs.setAccessibleName("Editor tools")
        self.tool_tabs.addTab(self._build_censor_tab(), "Censor")
        self.tool_tabs.addTab(self._build_annotate_tab(), "Annotate")
        self.tool_tabs.addTab(self._build_adjust_tab(), "Adjust")
        self.tool_tabs.addTab(self._build_transform_tab(), "Transform")
        self.tool_tabs.currentChanged.connect(self._tool_tab_changed)
        sidebar_layout.addWidget(self.tool_tabs, 1)
        self.editor_splitter.addWidget(sidebar)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(6, 6, 6, 0)
        center_layout.setSpacing(4)
        self.document_tabs = QTabBar()
        self.document_tabs.setAccessibleName("Open image tabs")
        self.document_tabs.setTabsClosable(True)
        self.document_tabs.currentChanged.connect(self._tab_changed)
        self.document_tabs.tabCloseRequested.connect(self.close_document)
        self.document_tabs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.document_tabs.customContextMenuRequested.connect(self._show_document_menu)
        center_layout.addWidget(self.document_tabs)
        center_layout.addWidget(self.canvas, 1)
        nav_bar = QFrame(objectName="TopBar")
        nav_row = QHBoxLayout(nav_bar)
        nav_row.setContentsMargins(8, 3, 8, 3)
        self.navigation_hint = QLabel("Space/center/right-drag: pan  •  Wheel: zoom  •  Alt+wheel: tool size  •  Arrows: nudge  •  Alt+arrows: resize", objectName="Muted")
        self.navigation_hint.setToolTip("Double-click the image to toggle Fit and 100%. Ctrl+arrow pans the canvas. Shift makes movement larger.")
        nav_row.addWidget(self.navigation_hint, 1)
        self.anim_prev_button = QToolButton()
        self.anim_prev_button.setText("◀")
        self.anim_prev_button.setToolTip("Previous frame (PageUp)")
        self.anim_prev_button.setVisible(False)
        self.anim_prev_button.clicked.connect(lambda: self.step_animation_frame(-1))
        nav_row.addWidget(self.anim_prev_button)
        self.anim_scrub = QSlider(Qt.Horizontal)
        self.anim_scrub.setRange(1, 1)
        self.anim_scrub.setVisible(False)
        self.anim_scrub.setMinimumWidth(240)
        self.anim_scrub.setMaximumWidth(520)
        self.anim_scrub.setToolTip("Scrub through the video/animation timeline")
        self.anim_scrub.valueChanged.connect(self._scrub_animation_frame)
        nav_row.addWidget(self.anim_scrub)
        self.anim_next_button = QToolButton()
        self.anim_next_button.setText("▶")
        self.anim_next_button.setToolTip("Next frame (PageDown)")
        self.anim_next_button.setVisible(False)
        self.anim_next_button.clicked.connect(lambda: self.step_animation_frame(1))
        nav_row.addWidget(self.anim_next_button)
        self.gif_frame_label = QLabel("", objectName="Muted")
        self.gif_frame_label.setVisible(False)
        nav_row.addWidget(self.gif_frame_label)
        loop_label = QLabel("Loop", objectName="Muted")
        loop_label.setVisible(False)
        self.loop_preview_label = loop_label
        nav_row.addWidget(loop_label)
        self.anim_loop_start = QSpinBox()
        self.anim_loop_start.setRange(1, 1)
        self.anim_loop_start.setVisible(False)
        self.anim_loop_start.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.anim_loop_start.setMaximumWidth(54)
        self.anim_loop_start.setToolTip("Loop preview start frame")
        self.anim_loop_start.valueChanged.connect(self._loop_preview_changed)
        nav_row.addWidget(self.anim_loop_start)
        self.anim_loop_end = QSpinBox()
        self.anim_loop_end.setRange(1, 1)
        self.anim_loop_end.setVisible(False)
        self.anim_loop_end.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.anim_loop_end.setMaximumWidth(54)
        self.anim_loop_end.setToolTip("Loop preview end frame")
        self.anim_loop_end.valueChanged.connect(self._loop_preview_changed)
        nav_row.addWidget(self.anim_loop_end)
        self.gif_play_button = QPushButton("▶ Play animation live")
        self.gif_play_button.setCheckable(True)
        self.gif_play_button.setVisible(False)
        self.gif_play_button.setToolTip("Play the animation while the current effect, chain, selection, text, or corrections update live (Ctrl+Space)")
        self.gif_play_button.clicked.connect(self.toggle_gif_preview)
        nav_row.addWidget(self.gif_play_button)
        self.coordinate_label = QLabel("", objectName="Muted")
        self.coordinate_label.setMinimumWidth(110)
        self.coordinate_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        nav_row.addWidget(self.coordinate_label)
        center_layout.addWidget(nav_bar)
        self.editor_splitter.addWidget(center)
        self.editor_splitter.setStretchFactor(0, 0)
        self.editor_splitter.setStretchFactor(1, 1)
        self.editor_splitter.setSizes([360, 1140])
        body.addWidget(self.editor_splitter)
        root.addLayout(body, 1)
        self._set_advanced_visible(False)

    def _register_advanced(self, widget: QWidget) -> QWidget:
        self.advanced_widgets.append(widget)
        return widget

    def _set_advanced_visible(self, visible: bool) -> None:
        for widget in self.advanced_widgets:
            widget.setVisible(visible)

    def _scroll_panel(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(7, 8, 7, 12)
        layout.setSpacing(8)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        return page, layout

    def _section(self, layout: QVBoxLayout, title: str) -> None:
        label = QLabel(title, objectName="SectionTitle")
        layout.addWidget(label)

    def _tool_button(self, text: str, mode: str) -> QToolButton:
        shortcuts = {
            "select": "R", "lasso": "L", "face": "C", "text": "T", "sticker": "S",
            "arrow": "A", "box": "X", "pan": "P", "clone": "K", "heal": "H",
            "brush_blur": "G", "brush_pixel": "J", "brush_mosaic": "M", "brush_black": "Shift+B",
        }
        button = QToolButton()
        shortcut = shortcuts.get(mode, "")
        button.setText(f"{text}  {shortcut}" if shortcut else text)
        button.setToolTip(f"{text} tool" + (f" ({shortcut})" if shortcut else ""))
        button.setCheckable(True)
        button.clicked.connect(lambda checked=False, m=mode, b=button: self._choose_mode(m, b))
        self.tool_group.addButton(button)
        self.tool_buttons[mode] = button
        if mode == "select":
            button.setChecked(True)
        return button

    def _tool_grid(self, layout: QVBoxLayout, items: list[tuple[str, str]]) -> None:
        holder = QWidget()
        grid = QHBoxLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(5)
        for text, mode in items:
            grid.addWidget(self._tool_button(text, mode))
        layout.addWidget(holder)

    def _labeled_slider(self, layout: QVBoxLayout, label: str, minimum: int, maximum: int, value: int, callback: Callable[[int], None]) -> QSlider:
        line = QHBoxLayout()
        line.addWidget(QLabel(label, objectName="Muted"))
        value_label = QLabel(str(value), objectName="Muted")
        line.addStretch(1)
        line.addWidget(value_label)
        layout.addLayout(line)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        slider.valueChanged.connect(lambda v: (value_label.setText(str(v)), callback(v)))
        layout.addWidget(slider)
        return slider

    def _effect_parameter_slider(self, layout: QVBoxLayout, key: str) -> QSlider:
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(3)
        line = QHBoxLayout()
        label = QLabel(objectName="Muted")
        value_label = QLabel(objectName="Muted")
        line.addWidget(label)
        line.addStretch(1)
        line.addWidget(value_label)
        holder_layout.addLayout(line)
        slider = QSlider(Qt.Horizontal)
        slider.valueChanged.connect(lambda value, parameter=key: self._effect_parameter_changed(parameter, value))
        holder_layout.addWidget(slider)
        layout.addWidget(holder)
        self.effect_parameter_rows[key] = holder
        self.effect_parameter_labels[key] = label
        self.effect_parameter_values[key] = value_label
        self.effect_parameter_sliders[key] = slider
        return slider

    def _target_edge_preset_changed(self, name: str) -> None:
        presets = {
            "Hard edge": (0, 0),
            "Soft transition": (16, 8),
            "Seamless blend": (32, 16),
        }
        if name not in presets or not hasattr(self, "target_feather_slider"):
            return
        self._loading_target_edge = True
        try:
            feather, padding = presets[name]
            self.target_feather_slider.setValue(feather)
            self.target_padding_slider.setValue(padding)
        finally:
            self._loading_target_edge = False
        self._preview_target_changed()

    def _target_edge_value_changed(self, _value: int) -> None:
        if self._loading_target_edge or not hasattr(self, "target_edge_preset"):
            return
        self.target_edge_preset.blockSignals(True)
        self.target_edge_preset.setCurrentText("Custom")
        self.target_edge_preset.blockSignals(False)
        self._preview_target_changed()

    def _target_feather(self, scale: float = 1.0) -> int:
        if not hasattr(self, "target_feather_slider"):
            return 0
        return max(0, round(self.target_feather_slider.value() * max(0.001, scale)))

    def _target_padding(self, scale: float = 1.0) -> int:
        if not hasattr(self, "target_padding_slider"):
            return 0
        return max(0, round(self.target_padding_slider.value() * max(0.001, scale)))

    def _build_censor_tab(self) -> QWidget:
        page, layout = self._scroll_panel()
        self._section(layout, "1. Choose an area")
        self._tool_grid(layout, [("Rectangle", "select"), ("Lasso", "lasso"), ("Faces", "face")])
        self.target_edge_preset = QComboBox()
        self.target_edge_preset.addItems(["Hard edge", "Soft transition", "Seamless blend", "Custom"])
        self.target_edge_preset.setToolTip("Controls how effects blend at rectangle, lasso, and face-circle boundaries.")
        layout.addWidget(self.target_edge_preset)
        self.target_feather_slider = self._labeled_slider(
            layout, "Transition width", 0, 96, 16, self._target_edge_value_changed
        )
        self.target_padding_slider = self._labeled_slider(
            layout, "Coverage padding", 0, 64, 8, self._target_edge_value_changed
        )
        edge_hint = QLabel(
            "Padding keeps the selected area fully affected while the transition fades outside it. "
            "For face circles, padding expands the protected face area instead.",
            objectName="Muted",
        )
        edge_hint.setWordWrap(True)
        layout.addWidget(edge_hint)
        self.target_edge_preset.currentTextChanged.connect(self._target_edge_preset_changed)
        self.target_edge_preset.setCurrentText("Soft transition")

        self._section(layout, "2. Choose an effect")
        self.effect_combo = QComboBox()
        self.effect_combo.addItems(self.CENSOR_EFFECTS)
        self.effect_combo.currentTextChanged.connect(self._effect_choice_changed)
        self.effect_combo.setMaxVisibleItems(18)
        layout.addWidget(self.effect_combo)
        self.effect_description = QLabel(objectName="Muted")
        self.effect_description.setWordWrap(True)
        layout.addWidget(self.effect_description)
        privacy_hint = QLabel("For sensitive information, use Black/White Redaction, Redaction Tape, or Noise Redaction at high strength. Blur and pixelation can leave recognizable structure.", objectName="Muted")
        privacy_hint.setWordWrap(True)
        layout.addWidget(privacy_hint)
        self._section(layout, "Fine controls")
        for parameter in self.EFFECT_PARAMETER_KEYS:
            self._effect_parameter_slider(layout, parameter)
        reset_effect = QPushButton("Reset effect settings")
        reset_effect.clicked.connect(self._reset_current_effect)
        layout.addWidget(reset_effect)
        # Compatibility aliases used by the brush tools and older tests. They
        # point to the currently relabeled dynamic controls rather than dead
        # effect-specific sliders.
        self.effect_strength_slider = self.effect_parameter_sliders["amount"]
        self.effect_pattern_slider = self.effect_parameter_sliders["size"]
        self.blur_slider = self.effect_parameter_sliders["size"]
        self.pixel_slider = self.effect_parameter_sliders["size"]
        self.mosaic_slider = self.effect_parameter_sliders["size"]

        self._section(layout, "3. Combine effects")
        self.effect_preset_combo = QComboBox()
        self.effect_preset_combo.addItems([
            "Custom chain", "Maximum privacy", "Face anonymizer", "Deep pixel mosaic",
            "Frosted privacy", "Faceted privacy", "Encrypted glass", "Document redaction",
            "Whiteout", "Printed censor", "Barcode concealment", "Digital scramble",
            "Prism interference", "Anonymous silhouette", "Photocopy mask",
            "Thermal interference", "ASCII mask", "Blueprint concealment",
            "Neon wireframe", "Topographic concealment",
        ])
        self.effect_preset_combo.currentTextChanged.connect(self._load_effect_preset)
        layout.addWidget(self.effect_preset_combo)
        chain_hint = QLabel("Add multiple effects to build a censorship chain. When the chain is empty, the current effect is used by itself.", objectName="Muted")
        chain_hint.setWordWrap(True)
        layout.addWidget(chain_hint)
        chain_row = QHBoxLayout()
        add_chain = QPushButton("Add current")
        add_chain.clicked.connect(self._add_current_effect_to_chain)
        remove_chain = QPushButton("Remove selected")
        remove_chain.clicked.connect(self._remove_selected_effect_from_chain)
        clear_chain = QPushButton("Clear chain")
        clear_chain.clicked.connect(self._clear_effect_chain)
        chain_row.addWidget(add_chain)
        chain_row.addWidget(remove_chain)
        chain_row.addWidget(clear_chain)
        layout.addLayout(chain_row)
        self.effect_chain_list = QListWidget()
        self.effect_chain_list.setMaximumHeight(120)
        self.effect_chain_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.effect_chain_list.itemSelectionChanged.connect(self._update_button_states)
        self.effect_chain_list.currentItemChanged.connect(self._select_chain_effect)
        self.effect_chain_list.model().rowsMoved.connect(lambda *_: self._effect_chain_reordered())
        layout.addWidget(self.effect_chain_list)
        self._effect_choice_changed(self.effect_combo.currentText())

        self._section(layout, "4. Apply")
        effect_row = QHBoxLayout()
        apply_btn = QPushButton("Apply effect", objectName="Accent")
        apply_btn.clicked.connect(self.apply_effect)
        cancel = QPushButton("Cancel preview")
        cancel.clicked.connect(self.cancel_preview)
        effect_row.addWidget(apply_btn, 1)
        effect_row.addWidget(cancel)
        layout.addLayout(effect_row)

        more = QToolButton()
        more.setText("More censor actions")
        more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(more)
        menu.addAction("Apply outside face circles", self.apply_outside_faces)
        menu.addAction("Add black bar in rectangle", self.black_bar)
        menu.addSeparator()
        menu.addAction("Clear all masks", self.canvas.clear_masks)
        menu.addAction("Duplicate selected face", self.duplicate_face)
        menu.addAction("Delete selected face", self.delete_face)
        more.setMenu(menu)
        layout.addWidget(more)

        advanced = self._register_advanced(QFrame())
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 6, 0, 0)
        advanced_layout.setSpacing(8)
        self._section(advanced_layout, "Brushes and repair")
        self._tool_grid(advanced_layout, [("Blur", "brush_blur"), ("Pixel", "brush_pixel"), ("Mosaic", "brush_mosaic"), ("Black", "brush_black")])
        self._tool_grid(advanced_layout, [("Clone", "clone"), ("Heal", "heal")])
        self.brush_slider = self._labeled_slider(advanced_layout, "Brush size", 8, 400, 60, lambda v: setattr(self.canvas, "brush_size", v))
        brush_hint = QLabel("Hold Space or use the middle mouse button to pan. A separate Pan tool is no longer necessary.", objectName="Muted")
        brush_hint.setWordWrap(True)
        advanced_layout.addWidget(brush_hint)
        layout.addWidget(advanced)
        layout.addStretch(1)
        return page

    def _build_annotate_tab(self) -> QWidget:
        page, layout = self._scroll_panel()
        self._section(layout, "Choose annotation")
        self._tool_grid(layout, [("Text", "text"), ("Sticker", "sticker"), ("Arrow", "arrow"), ("Box", "box")])

        self._section(layout, "Text")
        quick_hint = QLabel("Type directly here, then click the image. Drag to move; handles resize; double-click or Ctrl+Enter applies.", objectName="Muted")
        quick_hint.setWordWrap(True)
        layout.addWidget(quick_hint)
        self.quick_text_edit = MultilineTextEdit()
        self.quick_text_edit.setPlainText("Text")
        self.quick_text_edit.setPlaceholderText("Type multiline text…")
        self.quick_text_edit.setMaximumHeight(90)
        self.quick_text_edit.textChanged.connect(self._refresh_quick_text_preview)
        self.quick_text_edit.setToolTip("Multiline text editor. Ctrl+Enter applies the live text overlay.")
        layout.addWidget(self.quick_text_edit)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Preset", objectName="Muted"))
        self.text_style_combo = QComboBox()
        self.text_style_combo.addItems(["Caption", "Title", "Subtitle", "Meme", "Label", "Quote", "Lower third", "Watermark"])
        self.text_style_combo.currentTextChanged.connect(self._apply_quick_text_style)
        style_row.addWidget(self.text_style_combo, 1)
        layout.addLayout(style_row)
        self.text_size = self._labeled_slider(layout, "Font size", 8, 600, 48, lambda _v: self._refresh_quick_text_preview())
        self.text_rotation = self._labeled_slider(layout, "Rotation", -180, 180, 0, lambda _v: self._refresh_quick_text_preview())
        self.text_opacity = self._labeled_slider(layout, "Opacity", 0, 100, 100, lambda _v: self._refresh_quick_text_preview())

        action_row = QHBoxLayout()
        apply_button = QPushButton("Apply text", objectName="Accent")
        apply_button.clicked.connect(self.apply_quick_text)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancel_quick_text)
        more = QToolButton()
        more.setText("More")
        more.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(more)
        more_menu.addAction("Place in center", self.place_quick_text_center)
        more_menu.addAction("Re-edit last text", self.reedit_last_text)
        more.setMenu(more_menu)
        action_row.addWidget(apply_button, 1)
        action_row.addWidget(cancel)
        action_row.addWidget(more)
        layout.addLayout(action_row)

        self._section(layout, "Stickers")
        sticker_hint = QLabel("Choose or paste any emoji/symbol, click the image, then move, resize, rotate, or fade it before applying.", objectName="Muted")
        sticker_hint.setWordWrap(True)
        layout.addWidget(sticker_hint)
        sticker_picker = QHBoxLayout()
        self.sticker_category = QComboBox()
        self.sticker_category.addItems(list(self.STICKER_CATEGORIES))
        self.sticker_category.currentTextChanged.connect(self._update_sticker_category)
        self.sticker_combo = QComboBox()
        self.sticker_combo.setEditable(True)
        self.sticker_combo.setInsertPolicy(QComboBox.NoInsert)
        self.sticker_combo.currentTextChanged.connect(self._sticker_symbol_changed)
        sticker_picker.addWidget(self.sticker_category)
        sticker_picker.addWidget(self.sticker_combo, 1)
        layout.addLayout(sticker_picker)
        sticker_source_row = QHBoxLayout()
        self.sticker_source_label = QLabel("Emoji / symbol", objectName="Muted")
        choose_sticker_image = QPushButton("Use image…")
        choose_sticker_image.clicked.connect(self.choose_sticker_image)
        clear_sticker_image = QPushButton("Use symbol")
        clear_sticker_image.clicked.connect(self.clear_sticker_image)
        sticker_source_row.addWidget(self.sticker_source_label, 1)
        sticker_source_row.addWidget(choose_sticker_image)
        sticker_source_row.addWidget(clear_sticker_image)
        layout.addLayout(sticker_source_row)
        self.sticker_size = self._labeled_slider(layout, "Sticker size", 24, 600, 96, lambda _v: self._refresh_sticker_preview())
        self.sticker_rotation = self._labeled_slider(layout, "Sticker rotation", -180, 180, 0, lambda _v: self._refresh_sticker_preview())
        self.sticker_opacity = self._labeled_slider(layout, "Sticker opacity", 0, 100, 100, lambda _v: self._refresh_sticker_preview())
        sticker_toggles = QHBoxLayout()
        self.sticker_shadow_check = QCheckBox("Shadow")
        self.sticker_shadow_check.setChecked(True)
        self.sticker_outline_check = QCheckBox("Outline")
        for widget in (self.sticker_shadow_check, self.sticker_outline_check):
            widget.toggled.connect(self._refresh_sticker_preview)
            sticker_toggles.addWidget(widget)
        layout.addLayout(sticker_toggles)
        sticker_actions = QHBoxLayout()
        apply_sticker = QPushButton("Apply sticker", objectName="Accent")
        apply_sticker.clicked.connect(self.apply_sticker)
        cancel_sticker = QPushButton("Cancel")
        cancel_sticker.clicked.connect(self.cancel_sticker)
        sticker_more = QToolButton()
        sticker_more.setText("More")
        sticker_more.setPopupMode(QToolButton.InstantPopup)
        sticker_menu = QMenu(sticker_more)
        sticker_menu.addAction("Place in center", self.place_sticker_center)
        sticker_menu.addAction("Re-edit last sticker", self.reedit_last_sticker)
        sticker_more.setMenu(sticker_menu)
        sticker_actions.addWidget(apply_sticker, 1)
        sticker_actions.addWidget(cancel_sticker)
        sticker_actions.addWidget(sticker_more)
        layout.addLayout(sticker_actions)
        self._update_sticker_category(self.sticker_category.currentText())

        advanced = self._register_advanced(QFrame())
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 6, 0, 0)
        advanced_layout.setSpacing(8)
        self._section(advanced_layout, "Detailed text control")

        font_row = QHBoxLayout()
        self.text_font_label = QLabel("Default font", objectName="Muted")
        choose_font = QPushButton("Choose font…")
        choose_font.clicked.connect(self.choose_quick_text_font)
        reset_font = QPushButton("Reset")
        reset_font.clicked.connect(self.reset_quick_text_font)
        font_row.addWidget(self.text_font_label, 1)
        font_row.addWidget(choose_font)
        font_row.addWidget(reset_font)
        advanced_layout.addLayout(font_row)
        self.text_font_path = ""

        format_row = QHBoxLayout()
        self.text_bold_check = QCheckBox("Bold")
        self.text_italic_check = QCheckBox("Italic")
        self.text_alignment_combo = QComboBox()
        self.text_alignment_combo.addItems(["left", "center", "right"])
        self.text_alignment_combo.setCurrentText("center")
        for widget in (self.text_bold_check, self.text_italic_check):
            widget.toggled.connect(self._refresh_quick_text_preview)
            format_row.addWidget(widget)
        self.text_alignment_combo.currentTextChanged.connect(self._refresh_quick_text_preview)
        format_row.addWidget(self.text_alignment_combo, 1)
        advanced_layout.addLayout(format_row)

        self.text_character_spacing = self._labeled_slider(advanced_layout, "Character spacing", -5, 40, 0, lambda _v: self._refresh_quick_text_preview())
        self.text_line_spacing = self._labeled_slider(advanced_layout, "Line spacing", 0, 100, 8, lambda _v: self._refresh_quick_text_preview())
        self.text_wrap_width = self._labeled_slider(advanced_layout, "Wrap width (0 = off)", 0, 2000, 0, lambda _v: self._refresh_quick_text_preview())

        toggles = QHBoxLayout()
        self.text_background_check = QCheckBox("Background")
        self.text_outline_check = QCheckBox("Outline")
        self.text_outline_check.setChecked(True)
        self.text_shadow_check = QCheckBox("Shadow")
        self.text_shadow_check.setChecked(True)
        for widget in (self.text_background_check, self.text_outline_check, self.text_shadow_check):
            widget.toggled.connect(self._refresh_quick_text_preview)
            toggles.addWidget(widget)
        advanced_layout.addLayout(toggles)

        self.text_color = "#ffffff"
        self.text_background_color = "#000000"
        self.text_outline_color = "#000000"
        self.text_shadow_color = "#000000"
        color_row = QHBoxLayout()
        for label, callback in [
            ("Text", self.choose_text_color),
            ("Background", self.choose_text_background_color),
            ("Outline", self.choose_text_outline_color),
            ("Shadow", self.choose_text_shadow_color),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            color_row.addWidget(button)
        advanced_layout.addLayout(color_row)
        self.text_outline_width = self._labeled_slider(advanced_layout, "Outline width", 0, 20, 2, lambda _v: self._refresh_quick_text_preview())
        self.text_shadow_x = self._labeled_slider(advanced_layout, "Shadow X", -50, 50, 3, lambda _v: self._refresh_quick_text_preview())
        self.text_shadow_y = self._labeled_slider(advanced_layout, "Shadow Y", -50, 50, 3, lambda _v: self._refresh_quick_text_preview())
        self.text_shadow_blur = self._labeled_slider(advanced_layout, "Shadow blur", 0, 40, 4, lambda _v: self._refresh_quick_text_preview())
        self.text_shadow_opacity = self._labeled_slider(advanced_layout, "Shadow opacity", 0, 100, 70, lambda _v: self._refresh_quick_text_preview())
        self.text_background_opacity = self._labeled_slider(advanced_layout, "Background opacity", 0, 100, 75, lambda _v: self._refresh_quick_text_preview())
        self.text_padding = self._labeled_slider(advanced_layout, "Background padding", 0, 80, 10, lambda _v: self._refresh_quick_text_preview())
        self.text_corner_radius = self._labeled_slider(advanced_layout, "Corner radius", 0, 80, 8, lambda _v: self._refresh_quick_text_preview())

        recent_row = QHBoxLayout()
        self.recent_text_combo = QComboBox()
        self.recent_text_combo.addItems(self.recent_texts)
        use_recent = QPushButton("Use recent")
        use_recent.clicked.connect(self.use_recent_text)
        recent_row.addWidget(self.recent_text_combo, 1)
        recent_row.addWidget(use_recent)
        advanced_layout.addLayout(recent_row)

        self._section(advanced_layout, "Arrow and box")
        color_btn = QPushButton("Choose arrow / box color")
        color_btn.clicked.connect(self.choose_color)
        advanced_layout.addWidget(color_btn)
        self.color_label = QLabel(self.canvas.shape_color, objectName="Muted")
        advanced_layout.addWidget(self.color_label)
        self.line_width = self._labeled_slider(advanced_layout, "Line width", 1, 40, 6, lambda v: setattr(self.canvas, "shape_width", v))
        layout.addWidget(advanced)
        layout.addStretch(1)
        return page

    def _build_adjust_tab(self) -> QWidget:
        page, layout = self._scroll_panel()
        self._section(layout, "Corrections")
        hint = QLabel("Move a slider to preview. Apply once the image looks right.", objectName="Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.brightness = self._labeled_slider(layout, "Brightness", -100, 100, 0, lambda _v: self._schedule_adjustment_preview())
        self.contrast = self._labeled_slider(layout, "Contrast", -100, 100, 0, lambda _v: self._schedule_adjustment_preview())
        self.saturation = self._labeled_slider(layout, "Saturation", -100, 100, 0, lambda _v: self._schedule_adjustment_preview())
        self.sharpness = self._labeled_slider(layout, "Sharpness", -100, 200, 0, lambda _v: self._schedule_adjustment_preview())
        row = QHBoxLayout()
        apply_btn = QPushButton("Apply corrections", objectName="Accent")
        apply_btn.clicked.connect(self.apply_adjustments)
        reset = QPushButton("Reset")
        reset.clicked.connect(self.reset_adjustments)
        row.addWidget(apply_btn, 1)
        row.addWidget(reset)
        layout.addLayout(row)

        self._section(layout, "One-click looks")
        effect_row = QHBoxLayout()
        self.creative_combo = QComboBox()
        self.creative_combo.addItems(["Auto enhance", "Grayscale", "Invert", "Vignette", "Glow", "Posterize", "Sketch", "Cinematic"])
        self.creative_combo.currentTextChanged.connect(lambda _text: self._schedule_creative_preview())
        creative_apply = QPushButton("Apply", objectName="Accent")
        creative_apply.clicked.connect(self.apply_selected_creative)
        effect_row.addWidget(self.creative_combo, 1)
        effect_row.addWidget(creative_apply)
        layout.addLayout(effect_row)
        self.creative_strength = self._labeled_slider(layout, "Effect strength", 5, 100, 40, lambda _v: self._schedule_creative_preview())

        self._section(layout, "Presets")
        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["Strong blur", "Strong pixelate", "Manga mosaic", "Clean enhance", "Cinematic look"])
        apply_preset = QPushButton("Apply", objectName="Accent")
        apply_preset.clicked.connect(self.apply_preset)
        preset_more = QToolButton()
        preset_more.setText("More")
        preset_more.setPopupMode(QToolButton.InstantPopup)
        preset_menu = QMenu(preset_more)
        preset_menu.addAction("Batch process this preset…", self.batch_process)
        preset_more.setMenu(preset_menu)
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(apply_preset)
        preset_row.addWidget(preset_more)
        layout.addLayout(preset_row)
        layout.addStretch(1)
        return page

    def _build_transform_tab(self) -> QWidget:
        page, layout = self._scroll_panel()
        self._section(layout, "Transform image")
        hint = QLabel("Choose an operation, then run it. Rectangle or lasso selections are used automatically when relevant.", objectName="Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.transform_combo = QComboBox()
        self.transform_combo.addItems([
            "Crop to selection", "Resize…", "Rotate…", "Flip horizontal",
            "Flip vertical", "Add cinematic bars", "Extend animation duration…",
            "Reset to original",
        ])
        layout.addWidget(self.transform_combo)
        self.transform_apply = QPushButton("Run transform", objectName="Accent")
        self.transform_apply.clicked.connect(self.apply_selected_transform)
        layout.addWidget(self.transform_apply)
        self._section(layout, "View")
        view_hint = QLabel("Wheel zooms around the pointer. Hold Space and drag, or use middle/right-drag, to pan. Double-click toggles Fit/100%. Hold B for the original.", objectName="Muted")
        view_hint.setWordWrap(True)
        layout.addWidget(view_hint)
        layout.addStretch(1)
        return page

    def _creative_transform(self, scale: float = 1.0) -> Callable[[Image.Image], Image.Image]:
        name = self.creative_combo.currentText()
        strength = self.creative_strength.value()
        return {
            "Auto enhance": effects.auto_enhance,
            "Grayscale": self._grayscale_rgba,
            "Invert": self._invert_rgba,
            "Vignette": lambda im: effects.vignette(im, strength),
            "Glow": lambda im: effects.glow(im, strength, scale=scale),
            "Posterize": lambda im: self._posterize(im, strength),
            "Sketch": lambda im: effects.sketch(im, strength, scale=scale),
            "Cinematic": lambda im: effects.cinematic(im, strength),
        }[name]

    def preview_selected_creative(self) -> None:
        if (
            not self.document
            or self.tool_tabs.currentIndex() != 2
            or self.canvas.mode in {"text", "sticker"}
            or self._requested_preview_kind not in {None, "creative"}
        ):
            return
        if self.document.is_animated and self._gif_playing:
            self._advance_gif_preview()
            self._status(f"Live GIF creative effect: {self.creative_combo.currentText().lower()}")
            return
        preview = self._render_live_preview(
            lambda image, scale: self._creative_transform(scale)(image),
            max_pixels=self.LIVE_CREATIVE_PREVIEW_MAX_PIXELS,
        )
        if preview is None:
            return
        self.canvas.preview_image = preview
        self._preview_kind = "creative"
        self._requested_preview_kind = "creative"
        self.canvas.update()
        self._status(f"Live preview: {self.creative_combo.currentText().lower()} — saved output remains full resolution")

    def apply_selected_creative(self) -> None:
        name = self.creative_combo.currentText()
        self._commit_transform(self._creative_transform(), f"Applied {name.lower()}")

    def apply_selected_transform(self) -> None:
        name = self.transform_combo.currentText()
        if name == "Crop to selection":
            self.crop()
        elif name == "Resize…":
            self.resize_image()
        elif name == "Rotate…":
            self.rotate_image()
        elif name == "Flip horizontal":
            self._commit_transform(ImageOps.mirror, "Flipped horizontally", clear_masks=True)
        elif name == "Flip vertical":
            self._commit_transform(ImageOps.flip, "Flipped vertically", clear_masks=True)
        elif name == "Add cinematic bars":
            self.cinematic_bars()
        elif name == "Extend animation duration…":
            self.extend_animation_duration()
        elif name == "Reset to original":
            self.reset_original()

    def _set_canvas_mode_only(self, mode: str) -> None:
        """Activate an existing tool without navigating to another task tab."""
        button = self.tool_buttons.get(mode)
        if button is not None:
            button.setChecked(True)
            self.tool_label.setText(button.text().split("  ", 1)[0])
        self.canvas.set_mode(mode)
        self.canvas.setFocus()

    def _tool_tab_changed(self, index: int) -> None:
        # A visible task panel must never leave an incompatible hidden tool
        # active. That made controls look broken (for example Adjust while the
        # canvas was still in Text/Brush mode). Reuse the existing modes rather
        # than adding a second panel-state system.
        censor_modes = {
            "select", "lasso", "face", "clone", "heal",
            "brush_blur", "brush_pixel", "brush_mosaic", "brush_black",
        }
        annotate_modes = {"text", "sticker", "arrow", "box"}
        area_modes = {"select", "lasso"}
        if index == 0 and self.canvas.mode not in censor_modes:
            self._set_canvas_mode_only("select")
        elif index == 1 and self.canvas.mode not in annotate_modes:
            self._set_canvas_mode_only("text")
            self.preview_quick_text()
        elif index in {2, 3} and self.canvas.mode not in area_modes:
            self._set_canvas_mode_only("select")

        preview_kind = self._requested_preview_kind or self._preview_kind
        expected_tab = {"effect": 0, "text": 1, "sticker": 1, "adjustments": 2, "creative": 2}.get(preview_kind)
        if expected_tab is not None and expected_tab != index:
            self.cancel_preview(False)
            self.canvas.update()

    def _choose_mode(self, mode: str, source: QToolButton) -> None:
        self._set_canvas_mode_only(mode)
        censor_modes = {
            "select", "lasso", "face", "clone", "heal",
            "brush_blur", "brush_pixel", "brush_mosaic", "brush_black",
        }
        annotate_modes = {"text", "sticker", "arrow", "box"}
        if mode in censor_modes:
            self.tool_tabs.setCurrentIndex(0)
        elif mode in annotate_modes:
            self.tool_tabs.setCurrentIndex(1)

        direct_modes = {"arrow", "box", "clone", "heal", "brush_blur", "brush_pixel", "brush_mosaic", "brush_black"}
        if mode in direct_modes and (self._preview_kind is not None or self._requested_preview_kind is not None):
            self.cancel_preview(False)
            self.canvas.update()

        brush_effect = {
            "brush_blur": "Soft Blur",
            "brush_pixel": "Pixelate",
            "brush_mosaic": "Mosaic",
            "brush_black": "Black Redaction",
        }.get(mode)
        if brush_effect is not None:
            self.effect_combo.setCurrentText(brush_effect)

        if mode == "text":
            self.advanced_toggle.setChecked(True)
            self.preview_quick_text()
        elif mode == "sticker":
            self.preview_sticker()
            self.advanced_toggle.setChecked(True)
        elif mode in direct_modes:
            self.advanced_toggle.setChecked(True)
        elif mode in {"select", "lasso", "face"} and (self._requested_preview_kind or self._preview_kind) == "effect":
            # Re-render against the newly selected target type. In particular,
            # entering Faces must not leave a rectangle preview on screen.
            self._schedule_effect_preview()
        elif self.canvas.text_overlay_box is not None:
            self._text_preview_timer.stop()
            self._sticker_preview_timer.stop()
            self.canvas.clear_text_overlay()
            self.canvas.preview_image = None
            self._preview_kind = None
            self._requested_preview_kind = None
            self.quick_text_anchor = None
            self.sticker_anchor = None
            if self._gif_playing:
                self._advance_gif_preview()
            else:
                self.canvas.update()
        self.canvas.setFocus()

    def choose_mode(self, mode: str) -> None:
        button = self.tool_buttons.get(mode)
        if button is not None:
            self._choose_mode(mode, button)
        elif mode == "pan":
            self.canvas.set_mode("pan")
            self.tool_label.setText("Pan")
            self.canvas.setFocus()

    def _add_shortcut(self, sequence: str, callback: Callable[[], None], focus_policy: str = "always") -> None:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(callback)
        self.shortcuts.append(shortcut)
        if focus_policy != "always":
            self.focus_sensitive_shortcuts.append((shortcut, focus_policy))

    @staticmethod
    def _is_typing_widget(widget: Optional[QWidget]) -> bool:
        return isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)) or (isinstance(widget, QComboBox) and widget.isEditable())

    @staticmethod
    def _is_control_widget(widget: Optional[QWidget]) -> bool:
        return EditorWorkspace._is_typing_widget(widget) or isinstance(widget, (QComboBox, QSlider))

    def _update_shortcut_states(self, _old: Optional[QWidget], new: Optional[QWidget]) -> None:
        typing = self._is_typing_widget(new)
        control = self._is_control_widget(new)
        for shortcut, policy in self.focus_sensitive_shortcuts:
            shortcut.setEnabled(not typing if policy == "typing" else not control)

    def _bind_shortcuts(self) -> None:
        # File open/save are owned by MainWindow to avoid duplicate activation.
        for sequence, callback, policy in [
            ("Ctrl+V", self.paste_image, "typing"),
            ("Ctrl+Z", self.undo, "typing"), ("Ctrl+Y", self.redo, "typing"),
            ("Ctrl+Shift+Z", self.redo, "typing"), ("Ctrl+W", self.close_active, "typing"),
            ("F", self.fit, "control"), ("0", self.fit, "control"), ("1", self.actual_size, "control"),
            ("Escape", self.cancel_active_preview, "always"),
            ("Backspace", self.clear_faces, "typing"), ("Delete", self.delete_selected_item, "typing"),
            ("Ctrl+D", self.duplicate_face, "control"),
            ("Ctrl+Return", self._apply_active_annotation, "always"),
            ("Ctrl+Space", self.toggle_gif_preview, "always"),
            ("PageUp", lambda: self.step_animation_frame(-1), "always"), ("PageDown", lambda: self.step_animation_frame(1), "always"),
            ("Home", self.jump_animation_start, "always"), ("End", self.jump_animation_end, "always"),
            ("Ctrl++", self.zoom_in, "control"), ("Ctrl+=", self.zoom_in, "control"), ("Ctrl+-", self.zoom_out, "control"),
            ("Ctrl+Tab", self.next_document, "always"), ("Ctrl+Shift+Tab", self.previous_document, "always"),
            ("Alt+1", lambda: self.tool_tabs.setCurrentIndex(0), "always"),
            ("Alt+2", lambda: self.tool_tabs.setCurrentIndex(1), "always"),
            ("Alt+3", lambda: self.tool_tabs.setCurrentIndex(2), "always"),
            ("Alt+4", lambda: self.tool_tabs.setCurrentIndex(3), "always"),
            ("[", lambda: self.adjust_brush_size(-5), "control"), ("]", lambda: self.adjust_brush_size(5), "control"),
            ("R", lambda: self.choose_mode("select"), "control"),
            ("L", lambda: self.choose_mode("lasso"), "control"),
            ("C", lambda: self.choose_mode("face"), "control"),
            ("T", lambda: self.choose_mode("text"), "control"),
            ("S", lambda: self.choose_mode("sticker"), "control"),
            ("A", lambda: self.choose_mode("arrow"), "control"),
            ("X", lambda: self.choose_mode("box"), "control"),
            ("P", lambda: self.choose_mode("pan"), "control"),
            ("K", lambda: self.choose_mode("clone"), "control"),
            ("H", lambda: self.choose_mode("heal"), "control"),
            ("G", lambda: self.choose_mode("brush_blur"), "control"),
            ("J", lambda: self.choose_mode("brush_pixel"), "control"),
            ("M", lambda: self.choose_mode("brush_mosaic"), "control"),
            ("Shift+B", lambda: self.choose_mode("brush_black"), "control"),
            ("?", self.show_navigation_help, "control"),
        ]:
            self._add_shortcut(sequence, callback, policy)

    def next_document(self) -> None:
        if self.documents:
            self.document_tabs.setCurrentIndex((self.active_index + 1) % len(self.documents))

    def previous_document(self) -> None:
        if self.documents:
            self.document_tabs.setCurrentIndex((self.active_index - 1) % len(self.documents))

    def adjust_brush_size(self, delta: int) -> None:
        value = max(self.brush_slider.minimum(), min(self.brush_slider.maximum(), self.brush_slider.value() + delta))
        self.brush_slider.setValue(value)
        self.canvas.brush_size = value
        self._status(f"Brush size {value}px")

    def show_navigation_help(self) -> None:
        QMessageBox.information(
            self,
            "Editor navigation",
            "Mouse\n"
            "• Wheel: zoom around pointer\n"
            "• Space + left-drag, middle-drag, or right-drag: pan\n"
            "• Shift + wheel: horizontal pan\n"
            "• Alt + wheel: resize active text or brush\n"
            "• Double-click image: toggle Fit / 100%\n\n"
            "Keyboard\n"
            "• R/L/C/T/S/A/X/P: rectangle, lasso, circles, text, sticker, arrow, box, pan\n"
            "• G/J/M/Shift+B: blur, pixel, mosaic, black brushes\n"
            "• K/H: clone/heal\n"
            "• Arrows: move active text, face, or selection; Shift = 10 px\n"
            "• Alt + arrows: resize active item\n"
            "• Ctrl + arrows: pan canvas\n"
            "• Tab / Shift+Tab: cycle face circles\n"
            "• [ / ]: brush size\n"
            "• Ctrl+Tab: next image tab\n"
            "• Ctrl+Space: play/pause animation with the current live preview\n"
            "• PageUp/PageDown: previous/next animation frame · Home/End: jump to loop bounds\n"
            "• Hold B: original image\n"
            "• ?: show this guide",
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_B and not event.isAutoRepeat() and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier)):
            self.canvas.show_original = True
            self.canvas.update()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_B and not event.isAutoRepeat():
            self.canvas.show_original = self.before_check.isChecked()
            self.canvas.update()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        self.load_dropped_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def open_images(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Open images", "", IMAGE_FILE_FILTER)
        self.load_paths([Path(n) for n in names])

    def _video_import_options(self, path: Path) -> Optional[dict[str, object]]:
        try:
            info = probe_video(path)
        except Exception as exc:
            show_operation_error(self, "Open video", "The video metadata could not be read.", str(exc))
            return None
        dialog = VideoImportDialog(path, info, self)
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.options()

    def _read_editor_path(self, path: Path) -> Optional[Image.Image]:
        if path.suffix.lower() not in {".mp4", ".webm"}:
            return read_image(path)
        options = self._video_import_options(path)
        if options is None:
            return None
        progress_dialog = QProgressDialog("Streaming the selected video segment…", "Cancel", 0, 100, self)
        progress_dialog.setWindowTitle("Import video")
        progress_dialog.setWindowModality(Qt.WindowModal)
        progress_dialog.setMinimumDuration(250)
        progress_dialog.setAutoClose(False)
        progress_dialog.setAutoReset(False)
        progress_dialog.setMinimumWidth(460)

        def update_progress(done: int, total: int) -> bool:
            progress_dialog.setMaximum(max(1, total))
            progress_dialog.setValue(min(done, max(1, total)))
            QApplication.processEvents()
            return not progress_dialog.wasCanceled()

        try:
            return read_image(path, video_options=options, progress=update_progress)
        finally:
            progress_dialog.close()

    def load_paths(self, paths: list[Path]) -> None:
        failures: list[str] = []
        existing: dict[Path, int] = {}
        for index, doc in enumerate(self.documents):
            if doc.path:
                try:
                    existing[doc.path.resolve()] = index
                except OSError:
                    existing[doc.path] = index
        opened = 0
        opened_paths: list[str] = []
        for path in paths:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in existing:
                self.document_tabs.setCurrentIndex(existing[resolved])
                self._status(f"Already open: {path.name}")
                continue
            try:
                image = self._read_editor_path(path)
                if image is None:
                    continue
                self.add_document(image, path, consume=True)
                existing[resolved] = len(self.documents) - 1
                opened += 1
                opened_paths.append(str(path))
            except AnimationReadCancelled:
                self._status(f"Cancelled video import: {path.name}")
            except Exception as exc:
                failures.append(f"{path.name}: {exc}")
        if opened_paths:
            self.pathsOpened.emit(opened_paths)
        if opened > 1:
            self._status(f"Opened {opened} images")
        if failures:
            QMessageBox.warning(self, "Open images", "Some images could not be opened:\n\n" + "\n".join(failures[:12]))

    def _update_animation_tool_availability(self, doc: Optional[ImageDocument]) -> None:
        animated = bool(doc and doc.is_animated)
        for mode in ("brush_blur", "brush_pixel", "brush_mosaic", "brush_black", "clone", "heal"):
            button = self.tool_buttons.get(mode)
            if button is not None:
                button.setEnabled(not animated)
                button.setToolTip(
                    "Brush, clone, and heal tools are unavailable for animations; use rectangle, lasso, or face effects."
                    if animated
                    else button.toolTip().split(" — ")[0]
                )
        animated_widgets = [self.gif_play_button, self.gif_frame_label, self.anim_prev_button, self.anim_next_button, self.anim_scrub, self.loop_preview_label, self.anim_loop_start, self.anim_loop_end]
        for widget in animated_widgets:
            widget.setVisible(animated)
            widget.setEnabled(animated)
        if animated and doc is not None:
            count = max(1, doc.frame_count)
            self._animation_controls_updating = True
            self.anim_scrub.setRange(1, count)
            self.anim_loop_start.setRange(1, count)
            self.anim_loop_end.setRange(1, count)
            self.anim_loop_start.setValue(1)
            self.anim_loop_end.setValue(count)
            self._animation_controls_updating = False
            self._show_animation_frame(min(self._gif_preview_index, count - 1), update_slider=True)
        else:
            self.gif_frame_label.clear()
            self.gif_play_button.setChecked(False)
            self.gif_play_button.setText("▶ Play animation live")

    def add_document(
        self,
        image: Image.Image,
        path: Optional[Path] = None,
        dirty: bool = False,
        *,
        consume: bool = False,
    ) -> None:
        raw_frames = image.info.get(ANIMATION_FRAMES_KEY, [])
        owned_inputs = [image, *(raw_frames if isinstance(raw_frames, list) else [])]
        try:
            doc = ImageDocument.from_image(image, path)
        finally:
            if consume:
                # Decoder/clipboard paths transfer ownership to the workspace.
                # Public callers and tests may still reuse images passed to
                # add_document(), so consumption must remain explicit.
                seen: set[int] = set()
                for source in owned_inputs:
                    if id(source) in seen:
                        continue
                    seen.add(id(source))
                    try:
                        source.close()
                    except Exception:
                        pass
        doc.max_history = self.history_depth
        doc.max_history_bytes = self.history_memory_mb * 1024 * 1024
        if dirty:
            doc.mark_unsaved()
        self.documents.append(doc)
        self.document_tabs.addTab(doc.display_name)
        self.document_tabs.setCurrentIndex(len(self.documents) - 1)
        self._activate(len(self.documents) - 1)
        if doc.is_animated:
            self._status(
                f"Opened {path.name if path else 'animation'} — {doc.frame_count} frames, "
                f"{doc.animation_duration_ms / 1000:.2f}s; Play previews edits live, and Save As chooses export range/duration"
            )
        else:
            self._status(f"Opened {path.name if path else 'clipboard image'}")

    def _show_document_menu(self, position) -> None:
        index = self.document_tabs.tabAt(position)
        if index < 0:
            return
        self.document_tabs.setCurrentIndex(index)
        self._activate(index)
        doc = self.document
        if not doc:
            return
        menu = QMenu(self)
        menu.addAction("Save", self.save)
        menu.addAction("Save As…", self.save_as)
        menu.addAction("Send to Enhance", self.send_current_to_enhance)
        menu.addAction("Copy image", self.copy_image)
        if doc.path:
            menu.addAction("Copy path", lambda: QApplication.clipboard().setText(str(doc.path)))
            menu.addAction("Open containing folder", lambda: open_folder(doc.path.parent))
        menu.addSeparator()
        menu.addAction("Close", lambda: self.close_document(index))
        menu.exec(self.document_tabs.mapToGlobal(position))

    def send_current_to_enhance(self) -> None:
        doc = self.document
        if not doc:
            return
        if doc.path and not doc.dirty and doc.path.suffix.lower() not in {".mp4", ".webm"}:
            self.sendToEnhance.emit([str(doc.path)])
            return
        target = self.transfer_dir / f"send_{doc.id}{(doc.path.suffix if doc.path and doc.path.suffix.lower() in {'.gif', '.mp4', '.webm'} else '.gif') if doc.is_animated else '.png'}"
        try:
            if doc.is_animated:
                direct_source = doc.direct_video_source if target.suffix.lower() in {".mp4", ".webm"} else None
                if direct_source is not None:
                    export_video_segment(
                        direct_source,
                        target,
                        start_ms=doc.direct_video_start_ms,
                        duration_ms=doc.direct_video_duration_ms or doc.animation_duration_ms,
                        preserve_audio=True,
                    )
                else:
                    save_animation(
                        doc.animation_frames,
                        doc.frame_durations,
                        target,
                        loop=doc.animation_loop,
                        audio_source=doc.source_video if target.suffix.lower() in {".mp4", ".webm"} else None,
                        audio_start_ms=doc.direct_video_start_ms,
                        audio_duration_ms=doc.animation_duration_ms,
                    )
            else:
                save_image(doc.image, target)
        except Exception as exc:
            show_operation_error(self, "Send to Enhance", "The image could not be prepared for Enhance.", str(exc))
            return
        self.sendToEnhance.emit([str(target)])

    def copy_image(self) -> None:
        doc = self.document
        if doc:
            QApplication.clipboard().setImage(QImage(ImageQt(doc.image)))
            self._status("Copied image to clipboard")

    def paste_image(self) -> None:
        from PySide6.QtWidgets import QApplication
        mime = QApplication.clipboard().mimeData()
        if mime.hasImage():
            qimage = QApplication.clipboard().image().convertToFormat(QImage.Format_RGBA8888)
            image = fromqimage(qimage).convert("RGBA")
            self.add_document(image, dirty=True, consume=True)
            return
        if mime.hasUrls():
            self.load_paths([Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()])
            return
        QMessageBox.information(self, "Paste", "The clipboard does not contain an image or image file.")

    def _activate(self, index: int) -> None:
        self._stop_gif_preview(clear_canvas=False)
        self._stop_preview_timers()
        self._clear_preview_source_cache()
        self._clear_gif_source_cache()
        self._clear_animation_timing_cache()
        self._preview_kind = None
        self._requested_preview_kind = None
        if not (0 <= index < len(self.documents)):
            self.active_index = -1
            self.canvas.set_document(None)
            self._update_animation_tool_availability(None)
            self.documentAvailabilityChanged.emit(False)
            return
        self.active_index = index
        self.quick_text_anchor = None
        self.sticker_anchor = None
        self._last_sticker_box = None
        self.canvas.set_document(self.documents[index])
        self._update_animation_tool_availability(self.documents[index])
        QTimer.singleShot(0, lambda: self._zoom_changed(self.canvas.zoom))
        self.canvas.setFocus()
        self.documentAvailabilityChanged.emit(True)
        self._update_tab_labels()

    def _tab_changed(self, index: int) -> None:
        if not self._switching:
            self._activate(index)

    def _update_button_states(self) -> None:
        self.documentAvailabilityChanged.emit(self.document is not None)

    def _update_tab_labels(self) -> None:
        for i, doc in enumerate(self.documents):
            self.document_tabs.setTabText(i, doc.display_name)

    def close_active(self) -> None:
        if self.active_index >= 0:
            self.close_document(self.active_index)

    def close_document(self, index: int) -> None:
        if not (0 <= index < len(self.documents)):
            return
        requested_active = self.active_index
        doc = self.documents[index]
        if doc.dirty:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Close image")
            box.setText(f"Save changes to '{doc.display_name.rstrip(' *')}' before closing?")
            box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            box.setDefaultButton(QMessageBox.Save)
            result = box.exec()
            if result == QMessageBox.Cancel:
                return
            if result == QMessageBox.Save:
                self.document_tabs.setCurrentIndex(index)
                self._activate(index)
                self.save()
                if doc.dirty:
                    return
                if requested_active != index and 0 <= requested_active < len(self.documents):
                    self.document_tabs.setCurrentIndex(requested_active)
                    self._activate(requested_active)
            elif result == QMessageBox.Discard:
                self._delete_recovery(doc)
        previous_active = requested_active
        closing_active = index == previous_active
        if closing_active:
            next_index = min(index, len(self.documents) - 2)
        elif index < previous_active:
            next_index = previous_active - 1
        else:
            next_index = previous_active
        self._switching = True
        try:
            if closing_active:
                self._stop_gif_preview(clear_canvas=False)
                self._stop_preview_timers()
                self._clear_preview_source_cache()
                self._clear_gif_source_cache()
                self._clear_animation_timing_cache()
                # Detach before ImageDocument.close() releases Pillow storage.
                # QTabBar.removeTab() can repaint or emit currentChanged while
                # the close is in progress, so the canvas must not retain the
                # soon-to-be-closed document even briefly.
                self.canvas.set_document(None)
                self.active_index = -1
            del self.documents[index]
            self.document_tabs.removeTab(index)
            self.document_tabs.setCurrentIndex(next_index)
        finally:
            self._switching = False
        doc.close()
        if closing_active:
            self._activate(next_index)
        else:
            self.active_index = next_index
            self._update_tab_labels()
            self._update_button_states()

    def save(self) -> bool:
        doc = self.document
        if not doc:
            return False
        if not doc.path:
            return self.save_as()
        try:
            if doc.is_animated:
                direct_source = doc.direct_video_source if doc.path.suffix.lower() in {".mp4", ".webm"} else None
                if direct_source is not None:
                    try:
                        same_file = direct_source.resolve() == doc.path.resolve()
                    except OSError:
                        same_file = direct_source == doc.path
                    if not same_file:
                        export_video_segment(
                            direct_source,
                            doc.path,
                            start_ms=doc.direct_video_start_ms,
                            duration_ms=doc.direct_video_duration_ms or doc.animation_duration_ms,
                            preserve_audio=True,
                        )
                else:
                    source_video = doc.source_video if doc.path.suffix.lower() in {".mp4", ".webm"} else None
                    save_animation(
                        doc.animation_frames,
                        doc.frame_durations,
                        doc.path,
                        loop=doc.animation_loop,
                        audio_source=source_video,
                        audio_start_ms=doc.direct_video_start_ms,
                        audio_duration_ms=doc.animation_duration_ms,
                    )
            else:
                save_image(doc.image, doc.path, metadata=doc.metadata if self.preserve_metadata else {})
            doc.mark_saved()
            self._update_tab_labels()
            self._delete_recovery(doc)
            self._status(f"Saved {doc.path.name}")
            return True
        except Exception as exc:
            show_operation_error(self, "Save failed", "The image was not saved. The open document is unchanged.", str(exc))
            return False

    def save_as(self) -> bool:
        doc = self.document
        if not doc:
            return False
        animated_suffix = doc.path.suffix.lower() if doc.path and doc.path.suffix.lower() in {".gif", ".mp4", ".webm"} else ".gif"
        default_name = f"animation{animated_suffix}" if doc.is_animated else "image.png"
        start = str(doc.path or Path.home() / default_name)
        file_filter = "Animated GIF (*.gif);;MP4 video (*.mp4);;WebM video (*.webm)" if doc.is_animated else "PNG (*.png);;JPEG (*.jpg);;WebP (*.webp);;BMP (*.bmp);;TIFF (*.tiff);;GIF (*.gif)"
        name, _ = QFileDialog.getSaveFileName(self, "Save image as", start, file_filter)
        if not name:
            return False
        path = Path(name)
        if not path.suffix:
            path = path.with_suffix(animated_suffix if doc.is_animated else ".png")
        if doc.is_animated and path.suffix.lower() not in {".gif", ".mp4", ".webm"}:
            QMessageBox.information(self, "Animated export", "Animated documents can be saved as GIF, MP4, or WebM files.")
            return False
        try:
            customized_export = False
            if doc.is_animated:
                export_dialog = AnimationExportDialog(doc, path.suffix, self)
                if export_dialog.exec() != QDialog.Accepted:
                    return False
                options = export_dialog.options()
                direct_video = bool(options.get("direct_video", False)) and path.suffix.lower() in {".mp4", ".webm"}
                if direct_video:
                    direct_source = doc.direct_video_source
                    if direct_source is None:
                        raise ValueError("The original video is no longer available for direct export.")
                    export_video_segment(
                        direct_source,
                        path,
                        start_ms=doc.direct_video_start_ms + int(options["start_ms"]),
                        duration_ms=int(options["duration_ms"]),
                        fps=int(options["fps"]),
                        bitrate_kbps=int(options["bitrate_kbps"]),
                        preserve_audio=bool(options.get("preserve_audio", True)),
                    )
                else:
                    export_frames, export_durations = slice_animation(
                        doc.animation_frames,
                        doc.frame_durations,
                        start_ms=int(options["start_ms"]),
                        duration_ms=int(options["duration_ms"]),
                    )
                    if path.suffix.lower() == ".gif":
                        export_durations = retime_animation_durations(
                            export_durations,
                            int(options["output_duration_ms"]),
                        )
                    save_animation(
                        export_frames,
                        export_durations,
                        path,
                        loop=doc.animation_loop,
                        fps=int(options["fps"]),
                        bitrate_kbps=int(options["bitrate_kbps"]),
                        gif_colors=int(options["gif_colors"]),
                        gif_dither=bool(options["gif_dither"]),
                        gif_optimize=bool(options["gif_optimize"]),
                        audio_source=(
                            doc.source_video
                            if path.suffix.lower() in {".mp4", ".webm"}
                            and bool(options.get("preserve_audio", False))
                            else None
                        ),
                        audio_start_ms=doc.direct_video_start_ms + int(options["start_ms"]),
                        audio_duration_ms=int(options["duration_ms"]),
                    )
                customized_export = (
                    int(options["start_ms"]) != 0
                    or int(options["duration_ms"]) != doc.animation_duration_ms
                    or int(options["output_duration_ms"]) != doc.animation_duration_ms
                )
            else:
                save_image(doc.image, path, metadata=doc.metadata if self.preserve_metadata else {})
            if not customized_export:
                doc.path = path
                doc.mark_saved()
                self._update_tab_labels()
                self._delete_recovery(doc)
                self._status(f"Saved {path.name}")
            else:
                self._status(f"Exported animation clip to {path.name}; the open document was not replaced")
            return True
        except Exception as exc:
            show_operation_error(self, "Save As failed", "The image was not saved. The open document is unchanged.", str(exc))
            return False

    def undo(self) -> None:
        if self.document and self.document.undo():
            self.cancel_preview(False)
            self._sync_selected_face()
            self._document_changed()
            self._status("Undo")

    def redo(self) -> None:
        if self.document and self.document.redo():
            self.cancel_preview(False)
            self._sync_selected_face()
            self._document_changed()
            self._status("Redo")

    def fit(self) -> None:
        self.canvas.fit_to_window()

    def actual_size(self) -> None:
        self.canvas.actual_size()

    def zoom_in(self) -> None:
        if self.document:
            self.canvas.zoom_by(1.2)
            self._status(f"Zoom {self.canvas.zoom * 100:.0f}%")

    def zoom_out(self) -> None:
        if self.document:
            self.canvas.zoom_by(1 / 1.2)
            self._status(f"Zoom {self.canvas.zoom * 100:.0f}%")

    def _zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"{zoom * 100:.0f}%")

    def _cursor_position_changed(self, x: int, y: int) -> None:
        self.coordinate_label.setText("" if x < 0 or y < 0 else f"x {x}  y {y}")

    def _canvas_brush_size_changed(self, value: int) -> None:
        if self.brush_slider.value() != value:
            self.brush_slider.blockSignals(True)
            self.brush_slider.setValue(value)
            self.brush_slider.blockSignals(False)

    def load_dropped_paths(self, raw_paths: list[str]) -> None:
        self.load_paths(expand_image_paths(raw_paths))


    def cancel_active_preview(self) -> None:
        if self.canvas.mode == "text" and (self.canvas.preview_image is not None or self.quick_text_anchor is not None):
            self.cancel_quick_text()
        elif self.canvas.mode == "sticker" and (self.canvas.preview_image is not None or self.sticker_anchor is not None):
            self.cancel_sticker()
        else:
            self.clear_selection()
            self.cancel_preview(False)

    def delete_selected_item(self) -> None:
        if self.canvas.mode == "face" and self.document and self.document.face_masks:
            self.delete_face()
        elif self.canvas.mode == "text" and self.canvas.text_overlay_box:
            self.cancel_quick_text()
        elif self.canvas.mode == "sticker" and self.canvas.text_overlay_box:
            self.cancel_sticker()
        else:
            self.clear_selection()

    def _toggle_before(self, checked: bool) -> None:
        self.canvas.show_original = checked
        self.canvas.update()

    def _toggle_compare(self, checked: bool) -> None:
        self.canvas.compare_enabled = checked
        self.canvas.update()

    def _set_compare_split(self, value: int) -> None:
        self.canvas.compare_split = value
        self.canvas.update()

    def _status(self, text: str) -> None:
        if self._pending_animation_notice:
            text = f"{text} · {self._pending_animation_notice}"
            self._pending_animation_notice = ""
        self.statusChanged.emit(text)
        self.zoom_label.setText(f"{self.canvas.zoom * 100:.0f}%")

    def _document_changed(self) -> None:
        self._update_tab_labels()
        self._clear_preview_source_cache()
        self._clear_gif_source_cache()
        self._clear_animation_timing_cache()
        doc = self.document
        if doc and doc.is_animated and self.anim_scrub.maximum() != doc.frame_count:
            self._update_animation_tool_availability(doc)
        self.canvas.update()
        self._preview_target_changed()

    def _preview_target_changed(self) -> None:
        """Refresh previews when rectangle, lasso, or face geometry changes."""
        kind = self._requested_preview_kind or self._preview_kind
        if kind == "effect":
            self._schedule_effect_preview()
        elif kind == "adjustments":
            self._schedule_adjustment_preview()
        elif kind == "creative":
            self._schedule_creative_preview()
        elif self.tool_tabs.currentIndex() == 0 and self.document:
            # The first area gesture should immediately show the currently
            # selected effect. Previously live preview did not begin until a
            # slider/combo changed at least once, which felt randomly broken.
            has_target = (
                self.canvas.preview_selection() is not None
                or len(self.document.lasso_points) >= 3
                or (self.canvas.mode == "face" and bool(self.document.face_masks))
            )
            if has_target:
                self._schedule_effect_preview()

    def _sync_selected_face(self) -> None:
        faces = self.document.face_masks if self.document else []
        if not faces:
            self.canvas.selected_face_index = None
        elif self.canvas.selected_face_index is None:
            self.canvas.selected_face_index = len(faces) - 1 if self.canvas.mode == "face" else None
        else:
            self.canvas.selected_face_index = min(self.canvas.selected_face_index, len(faces) - 1)

    def clear_selection(self) -> None:
        if self.document and (self.document.selection or self.document.lasso_points):
            self.document.push_mask_undo()
            self.document.selection = None
            self.document.lasso_points.clear()
            self._document_changed()

    def clear_faces(self) -> None:
        if self.document and self.document.face_masks:
            self.document.push_mask_undo()
            self.document.face_masks.clear()
            self.canvas.selected_face_index = None
            self._document_changed()

    def duplicate_face(self) -> None:
        if self.document and self.document.face_masks:
            index = self.canvas.selected_face_index if self.canvas.selected_face_index is not None else len(self.document.face_masks) - 1
            face = self.document.face_masks[index]
            self.document.push_mask_undo()
            self.document.face_masks.append(RectMask(face.left + 12, face.top + 12, face.right + 12, face.bottom + 12))
            self.canvas.selected_face_index = len(self.document.face_masks) - 1
            self._document_changed()

    def delete_face(self) -> None:
        if self.document and self.document.face_masks:
            index = self.canvas.selected_face_index if self.canvas.selected_face_index is not None else len(self.document.face_masks) - 1
            self.document.push_mask_undo()
            self.document.face_masks.pop(index)
            self.canvas.selected_face_index = min(index, len(self.document.face_masks) - 1) if self.document.face_masks else None
            self._document_changed()

    def _current_animation_range(self, doc: Optional[ImageDocument] = None) -> tuple[int, int]:
        doc = doc or self.document
        if not doc or not doc.is_animated:
            return 0, 0
        count = doc.frame_count
        start = max(0, min(count - 1, self.anim_loop_start.value() - 1 if hasattr(self, "anim_loop_start") else 0))
        end = max(start, min(count - 1, self.anim_loop_end.value() - 1 if hasattr(self, "anim_loop_end") else count - 1))
        return start, end

    def _clear_animation_timing_cache(self) -> None:
        self._gif_frame_ends.clear()
        self._gif_frame_ends_key = None
        self._animation_frame_starts.clear()
        self._animation_frame_starts_key = None

    def _frame_starts_for(self, doc: ImageDocument) -> list[int]:
        key = (doc.id, doc.image_revision, doc.frame_count)
        if key == self._animation_frame_starts_key:
            return self._animation_frame_starts
        starts: list[int] = []
        elapsed = 0
        for duration in doc.frame_durations:
            starts.append(elapsed)
            elapsed += max(10, int(duration or 100))
        self._animation_frame_starts = starts
        self._animation_frame_starts_key = key
        return starts

    def _sync_animation_widgets(self, index: int, count: int) -> None:
        self._animation_controls_updating = True
        if hasattr(self, "anim_scrub"):
            self.anim_scrub.setValue(index + 1)
        if hasattr(self, "gif_frame_label"):
            start, end = self._current_animation_range()
            loop_text = f" · loop {start + 1}-{end + 1}" if count > 1 and (start != 0 or end != count - 1) else ""
            doc = self.document
            elapsed_ms = self._frame_starts_for(doc)[index] if doc and doc.is_animated else 0
            total_ms = doc.animation_duration_ms if doc and doc.is_animated else 0
            self.gif_frame_label.setText(
                f"{RangeTimeline._format_ms(elapsed_ms)} / {RangeTimeline._format_ms(total_ms)} · "
                f"frame {index + 1}/{count}{loop_text}"
            )
        self._animation_controls_updating = False

    def _show_animation_frame(self, index: int, *, update_slider: bool = True) -> None:
        doc = self.document
        if not doc or not doc.is_animated:
            return
        index = max(0, min(doc.frame_count - 1, index))
        self._gif_preview_index = index
        if update_slider:
            self._sync_animation_widgets(index, doc.frame_count)
        frame = doc.animation_frames[index]
        if self._requested_preview_kind is None:
            self.canvas.animation_original_image = frame
            self.canvas.preview_image = frame.copy()
            self._preview_kind = "animation"
        else:
            self.canvas.preview_image = self._render_gif_frame(frame, index)
            self._preview_kind = self._requested_preview_kind or "animation"
        if self._requested_preview_kind == "text" and self.canvas.mode == "text":
            self.canvas.set_text_overlay(getattr(self, "_last_quick_text_box", None), self.text_size.value())
        elif self._requested_preview_kind == "sticker" and self.canvas.mode == "sticker":
            self.canvas.set_text_overlay(self._last_sticker_box, self.sticker_size.value())
        self.canvas.update()

    def _scrub_animation_frame(self, value: int) -> None:
        if self._animation_controls_updating:
            return
        doc = self.document
        if not doc or not doc.is_animated:
            return
        if self._gif_playing:
            self._stop_gif_preview(clear_canvas=False)
        self._show_animation_frame(value - 1, update_slider=True)
        self._status(f"Frame {value}/{doc.frame_count}")

    def _loop_preview_changed(self, _value: int) -> None:
        if self._animation_controls_updating:
            return
        doc = self.document
        if not doc or not doc.is_animated:
            return
        self._animation_controls_updating = True
        start = min(self.anim_loop_start.value(), self.anim_loop_end.value())
        end = max(self.anim_loop_start.value(), self.anim_loop_end.value())
        self.anim_loop_start.setValue(start)
        self.anim_loop_end.setValue(end)
        self._animation_controls_updating = False
        self._gif_frame_ends.clear()
        self._gif_frame_ends_key = None
        start_index, end_index = self._current_animation_range(doc)
        if self._gif_preview_index < start_index or self._gif_preview_index > end_index:
            self._show_animation_frame(start_index, update_slider=True)
        else:
            self._sync_animation_widgets(self._gif_preview_index, doc.frame_count)
        if self._gif_playing:
            self._gif_playback_started = time.perf_counter()
        self._status(f"Loop preview set to frames {start_index + 1}-{end_index + 1}")

    def step_animation_frame(self, delta: int) -> None:
        doc = self.document
        if not doc or not doc.is_animated:
            return
        if self._gif_playing:
            self._stop_gif_preview(clear_canvas=False)
        start, end = self._current_animation_range(doc)
        span = max(1, end - start + 1)
        current = self._gif_preview_index if start <= self._gif_preview_index <= end else start
        next_index = start + ((current - start + delta) % span)
        self._show_animation_frame(next_index, update_slider=True)

    def jump_animation_start(self) -> None:
        doc = self.document
        if doc and doc.is_animated:
            if self._gif_playing:
                self._stop_gif_preview(clear_canvas=False)
            self._show_animation_frame(self._current_animation_range(doc)[0], update_slider=True)

    def jump_animation_end(self) -> None:
        doc = self.document
        if doc and doc.is_animated:
            if self._gif_playing:
                self._stop_gif_preview(clear_canvas=False)
            self._show_animation_frame(self._current_animation_range(doc)[1], update_slider=True)

    def toggle_gif_preview(self) -> None:
        doc = self.document
        if not doc or not doc.is_animated:
            self._status("The current document is not an animation")
            return
        if self._gif_playing:
            kind = self._requested_preview_kind
            self._stop_gif_preview(clear_canvas=True)
            self._render_static_preview(kind)
            self._status("GIF preview paused")
            return

        self._stop_preview_timers()
        self._gif_playing = True
        start, end = self._current_animation_range(doc)
        if not (start <= self._gif_preview_index <= end):
            self._gif_preview_index = start
        self._gif_playback_started = time.perf_counter()
        self._gif_frame_ends = []
        if self._requested_preview_kind is None and self.tool_tabs.currentIndex() == 0:
            self._requested_preview_kind = "effect"
        if hasattr(self, "gif_play_button"):
            self.gif_play_button.blockSignals(True)
            self.gif_play_button.setChecked(True)
            self.gif_play_button.setText("❚❚ Pause animation")
            self.gif_play_button.blockSignals(False)
        self._advance_gif_preview()
        start, end = self._current_animation_range(doc)
        self._status(
            f"Playing animation with live {self._gif_live_label()} — "
            f"frames {start + 1}-{end + 1} of {doc.frame_count}, {doc.animation_duration_ms / 1000:.2f}s total"
        )

    def _stop_gif_preview(self, *, clear_canvas: bool = False) -> None:
        self._gif_preview_timer.stop()
        self._gif_playing = False
        self._gif_frame_ends.clear()
        self._gif_frame_ends_key = None
        if hasattr(self, "gif_play_button"):
            self.gif_play_button.blockSignals(True)
            self.gif_play_button.setChecked(False)
            self.gif_play_button.setText("▶ Play animation live")
            self.gif_play_button.blockSignals(False)
        self.canvas.animation_original_image = None
        if clear_canvas:
            self.canvas.preview_image = None
            self._preview_kind = None
            self.canvas.update()

    def _clear_preview_source_cache(self) -> None:
        cached = self._preview_source_cache
        self._preview_source_cache_key = None
        self._preview_source_cache = None
        if cached is not None:
            source, scale_x, scale_y = cached
            # A 1:1 cache entry aliases a document/history frame and is not owned
            # by the preview cache. Only downsampled preview copies are closed.
            if scale_x < 0.999 or scale_y < 0.999:
                close = getattr(source, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        pass

    def _clear_gif_source_cache(self) -> None:
        for source, scale_x, scale_y, _bytes in self._gif_source_cache.values():
            if scale_x < 0.999 or scale_y < 0.999:
                close = getattr(source, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        pass
        self._gif_source_cache.clear()
        self._gif_source_cache_bytes = 0

    def _gif_live_label(self) -> str:
        return {
            "effect": "effect",
            "adjustments": "corrections",
            "creative": "creative effect",
            "text": "text",
            "sticker": "sticker",
        }.get(self._requested_preview_kind, "animation")

    def _render_static_preview(self, kind: Optional[str]) -> None:
        if kind == "effect":
            self.preview_effect()
        elif kind == "adjustments":
            self.preview_adjustments()
        elif kind == "creative":
            self.preview_selected_creative()
        elif kind == "text":
            self.preview_quick_text()
        elif kind == "sticker":
            self.preview_sticker()
        else:
            self.canvas.preview_image = None
            self._preview_kind = None
            self.canvas.update()

    def _gif_source(self, frame: Image.Image, index: int, max_pixels: int) -> tuple[Image.Image, float, float]:
        doc = self.document
        if not doc:
            return frame, 1.0, 1.0
        pixel_limit = max(1, int(max_pixels))
        key = (doc.id, doc.image_revision, index, id(frame), pixel_limit)
        cached = self._gif_source_cache.pop(key, None)
        if cached is not None:
            self._gif_source_cache[key] = cached
            return cached[0], cached[1], cached[2]
        width, height = frame.size
        scale = min(1.0, (pixel_limit / max(1, width * height)) ** 0.5)
        preview_width = max(1, round(width * scale))
        preview_height = max(1, round(height * scale))
        source = frame if scale == 1.0 else frame.resize((preview_width, preview_height), Image.Resampling.BOX)
        byte_size = preview_width * preview_height * max(1, len(source.getbands()))
        self._gif_source_cache[key] = (source, preview_width / width, preview_height / height, byte_size)
        self._gif_source_cache_bytes += byte_size
        while self._gif_source_cache and self._gif_source_cache_bytes > self.LIVE_ANIMATION_CACHE_BYTES:
            _, removed = self._gif_source_cache.popitem(last=False)
            self._gif_source_cache_bytes -= removed[3]
        return source, preview_width / width, preview_height / height

    def _render_gif_frame(self, frame: Image.Image, index: int) -> Image.Image:
        doc = self.document
        if not doc:
            return frame
        kind = self._requested_preview_kind
        max_pixels = self.LIVE_ANIMATION_PREVIEW_MAX_PIXELS
        if kind == "creative":
            max_pixels = min(max_pixels, self.LIVE_CREATIVE_PREVIEW_MAX_PIXELS)
        elif kind == "adjustments":
            max_pixels = min(max_pixels, self.LIVE_ADJUSTMENT_PREVIEW_MAX_PIXELS)
        source, scale_x, scale_y = self._gif_source(frame, index, max_pixels)
        self.canvas.animation_original_image = source
        scale = min(scale_x, scale_y)

        if kind == "text" and self.canvas.mode == "text":
            if self.quick_text_anchor is None:
                self.quick_text_anchor = (doc.image.width // 2, doc.image.height // 2)
            return self._quick_text_image(source, scale) or source.copy()
        if kind == "sticker" and self.canvas.mode == "sticker":
            if self.sticker_anchor is None:
                self.sticker_anchor = (doc.image.width // 2, doc.image.height // 2)
            return self._sticker_image(source, scale) or source.copy()

        if kind == "adjustments" and self.tool_tabs.currentIndex() == 2:
            transform = lambda image: effects.adjustments(
                image, self.brightness.value(), self.contrast.value(), self.saturation.value(), self.sharpness.value()
            )
        elif kind == "creative" and self.tool_tabs.currentIndex() == 2:
            transform = self._creative_transform(scale)
        elif kind == "effect" and self.tool_tabs.currentIndex() == 0:
            transform = self._composed_effect_transform(scale)
        else:
            return source.copy()

        if kind == "effect" and self.canvas.mode == "face":
            faces = [self._scaled_mask(face, scale_x, scale_y) for face in doc.face_masks]
            valid_faces = [face for face in faces if face is not None]
            return effects.apply_outside_faces(source, transform, valid_faces, feather=self._target_feather(scale), padding=self._target_padding(scale)) if valid_faces else source.copy()
        selection = self._scaled_mask(self.canvas.preview_selection(), scale_x, scale_y)
        lasso = [(round(x * scale_x), round(y * scale_y)) for x, y in doc.lasso_points]
        return effects.apply_to_target(source, transform, selection, lasso, feather=self._target_feather(scale), padding=self._target_padding(scale))

    def _advance_gif_preview(self) -> None:
        doc = self.document
        if not self._gif_playing or not doc or not doc.is_animated:
            self._stop_gif_preview()
            return
        start, end = self._current_animation_range(doc)
        timing_key = (doc.id, doc.image_revision, start, end)
        if timing_key != self._gif_frame_ends_key:
            frame_ends: list[int] = []
            total = 0
            for value in doc.frame_durations[start:end + 1]:
                total += max(10, int(value or 100))
                frame_ends.append(total)
            if not frame_ends:
                frame_ends = list(range(100, 100 * max(1, end - start + 1) + 1, 100))
                total = frame_ends[-1]
            self._gif_frame_ends = frame_ends
            self._gif_frame_ends_key = timing_key
        frame_ends = self._gif_frame_ends
        total = frame_ends[-1] if frame_ends else 100
        elapsed_ms = int((time.perf_counter() - self._gif_playback_started) * 1000) % max(1, total)
        local_index = min(len(frame_ends) - 1, bisect_right(frame_ends, elapsed_ms))
        index = start + local_index
        self._show_animation_frame(index, update_slider=True)

        elapsed_after_render = int((time.perf_counter() - self._gif_playback_started) * 1000) % max(1, total)
        next_local = min(len(frame_ends) - 1, bisect_right(frame_ends, elapsed_after_render))
        next_boundary = frame_ends[next_local]
        delay = next_boundary - elapsed_after_render
        self._gif_preview_timer.start(max(10, int(delay if delay > 0 else 10)))

    def _preview_timers(self) -> dict[str, QTimer]:
        return {
            "effect": self._effect_preview_timer,
            "adjustments": self._adjustment_preview_timer,
            "creative": self._creative_preview_timer,
            "text": self._text_preview_timer,
            "sticker": self._sticker_preview_timer,
        }

    def _stop_preview_timers(self, except_kind: Optional[str] = None) -> None:
        for kind, timer in self._preview_timers().items():
            if kind != except_kind:
                timer.stop()

    def _schedule_preview(self, kind: str) -> None:
        if not self.document:
            return
        if kind not in {"text", "sticker"} and self.canvas.mode in {"text", "sticker"}:
            self.choose_mode("select")
        timer = self._preview_timers()[kind]
        self._requested_preview_kind = kind
        self._stop_preview_timers(kind)
        # A checked Before toggle made working previews appear broken. Any user
        # change to an effect explicitly returns the canvas to the edited view.
        if self.before_check.isChecked():
            self.before_check.blockSignals(True)
            self.before_check.setChecked(False)
            self.before_check.blockSignals(False)
        self.canvas.show_original = False
        # Do not restart an active timer. This is a throttle, not a trailing-edge
        # debounce: continuous slider movement therefore produces regular live
        # frames while still coalescing bursts of valueChanged signals.
        if not timer.isActive():
            timer.start()

    @staticmethod
    def _scaled_mask(mask: Optional[RectMask], scale_x: float, scale_y: float) -> Optional[RectMask]:
        if mask is None:
            return None
        left = math.floor(mask.left * scale_x)
        top = math.floor(mask.top * scale_y)
        if mask.width <= 0 or mask.height <= 0:
            # Preserve an empty target. Returning None would mean the whole
            # image, while forcing +1px would create a misleading stray edit.
            return RectMask(left, top, left, top)
        right = max(left + 1, math.ceil(mask.right * scale_x))
        bottom = max(top + 1, math.ceil(mask.bottom * scale_y))
        return RectMask(left, top, right, bottom)

    def _live_preview_source(self, max_pixels: Optional[int] = None) -> Optional[tuple[Image.Image, float, float]]:
        doc = self.document
        if not doc:
            return None
        pixel_limit = max(1, int(max_pixels or self.LIVE_PREVIEW_MAX_PIXELS))
        key = (doc.id, doc.image_revision, id(doc.image), pixel_limit)
        if key == self._preview_source_cache_key and self._preview_source_cache is not None:
            return self._preview_source_cache
        width, height = doc.image.size
        scale = min(1.0, (pixel_limit / max(1, width * height)) ** 0.5)
        preview_width = max(1, round(width * scale))
        preview_height = max(1, round(height * scale))
        source = doc.image if scale == 1.0 else doc.image.resize((preview_width, preview_height), Image.Resampling.BOX)
        result = (source, preview_width / width, preview_height / height)
        # Deliberate one-entry cache: repeated previews are fast without retaining
        # a downsample for every open document. The revision/object key prevents
        # stale pixels after undo, redo, transforms, or in-place brush commits.
        self._preview_source_cache_key = key
        self._preview_source_cache = result
        return result

    def _render_live_preview(
        self,
        transform: Callable[[Image.Image, float], Image.Image],
        *,
        outside_faces: bool = False,
        max_pixels: Optional[int] = None,
    ) -> Optional[Image.Image]:
        """Render a bounded display preview without changing saved quality."""
        doc = self.document
        preview_source = self._live_preview_source(max_pixels)
        if not doc or preview_source is None:
            return None
        source, scale_x, scale_y = preview_source
        scaled_transform = lambda image: transform(image, min(scale_x, scale_y))
        if outside_faces:
            faces = [self._scaled_mask(face, scale_x, scale_y) for face in doc.face_masks]
            valid_faces = [face for face in faces if face is not None]
            return effects.apply_outside_faces(source, scaled_transform, valid_faces, feather=self._target_feather(min(scale_x, scale_y)), padding=self._target_padding(min(scale_x, scale_y))) if valid_faces else source.copy()
        selection = self._scaled_mask(self.canvas.preview_selection(), scale_x, scale_y)
        lasso = [(round(x * scale_x), round(y * scale_y)) for x, y in doc.lasso_points]
        return effects.apply_to_target(source, scaled_transform, selection, lasso, feather=self._target_feather(min(scale_x, scale_y)), padding=self._target_padding(min(scale_x, scale_y)))

    EFFECT_PRESETS = {
        "Maximum privacy": [
            {"name": "Privacy Blur", "amount": 100, "size": 34, "softness": 92, "detail": 15},
            {"name": "Noise Redaction", "amount": 100, "size": 4, "softness": 2, "detail": 35, "angle": 90},
            {"name": "Marker Scribble", "amount": 100, "size": 24, "softness": 5, "detail": 82, "angle": -8},
        ],
        "Face anonymizer": [
            {"name": "Privacy Blur", "amount": 100, "size": 28, "softness": 84, "detail": 8},
            {"name": "Pixelate", "amount": 82, "size": 26, "softness": 12, "detail": 12, "angle": 15},
        ],
        "Deep pixel mosaic": [
            {"name": "Pixelate", "amount": 100, "size": 34, "softness": 0, "detail": 8, "angle": 20},
            {"name": "Mosaic", "amount": 72, "size": 24, "softness": 32, "detail": 58, "angle": 2},
        ],
        "Frosted privacy": [
            {"name": "Privacy Blur", "amount": 72, "size": 20, "softness": 70, "detail": 0},
            {"name": "Frosted Glass", "amount": 100, "size": 10, "softness": 52, "detail": 36, "angle": 9},
        ],
        "Faceted privacy": [
            {"name": "Privacy Blur", "amount": 55, "size": 16, "softness": 55, "detail": 0},
            {"name": "Faceted Glass", "amount": 100, "size": 34, "softness": 60, "detail": 36, "angle": 18},
        ],
        "Encrypted glass": [
            {"name": "Encrypted Tiles", "amount": 100, "size": 22, "softness": 94, "detail": 68, "angle": 32},
            {"name": "Prism Split", "amount": 42, "size": 8, "softness": 12, "detail": 35, "angle": 12},
        ],
        "Document redaction": [
            {"name": "Black Redaction", "amount": 100, "size": 20, "softness": 10, "detail": 8, "angle": 0},
            {"name": "Redaction Tape", "amount": 58, "size": 24, "softness": 4, "detail": 42, "angle": 0},
        ],
        "Whiteout": [
            {"name": "White Redaction", "amount": 100, "size": 20, "softness": 4, "detail": 3, "angle": 0},
        ],
        "Printed censor": [
            {"name": "Halftone Dots", "amount": 100, "size": 11, "softness": 10, "detail": 82, "angle": 15},
            {"name": "Marker Scribble", "amount": 70, "size": 14, "softness": 8, "detail": 52, "angle": -5},
        ],
        "Barcode concealment": [
            {"name": "Privacy Blur", "amount": 65, "size": 16, "softness": 68, "detail": 0},
            {"name": "Barcode Redaction", "amount": 100, "size": 7, "softness": 2, "detail": 84, "angle": 0},
        ],
        "Digital scramble": [
            {"name": "Encrypted Tiles", "amount": 88, "size": 18, "softness": 78, "detail": 48, "angle": 18},
            {"name": "Glitch Blocks", "amount": 76, "size": 12, "softness": 42, "detail": 72, "angle": 0},
            {"name": "CRT Distortion", "amount": 52, "size": 6, "softness": 18, "detail": 22, "angle": 7},
        ],
        "Prism interference": [
            {"name": "Wave Scramble", "amount": 72, "size": 48, "softness": 20, "detail": 3, "angle": 12},
            {"name": "Prism Split", "amount": 82, "size": 16, "softness": 8, "detail": 45, "angle": -12},
        ],
        "Anonymous silhouette": [
            {"name": "Privacy Blur", "amount": 62, "size": 18, "softness": 70, "detail": 0},
            {"name": "Silhouette", "amount": 100, "size": 8, "softness": 48, "detail": 88, "angle": 45},
        ],
        "Photocopy mask": [
            {"name": "Photocopy", "amount": 100, "size": 3, "softness": 54, "detail": 82, "angle": 28},
            {"name": "Marker Scribble", "amount": 42, "size": 10, "softness": 2, "detail": 35, "angle": 0},
        ],
        "Thermal interference": [
            {"name": "Thermal Map", "amount": 100, "size": 3, "softness": 84, "detail": 18, "angle": 68},
            {"name": "Glitch Blocks", "amount": 48, "size": 10, "softness": 24, "detail": 38, "angle": 0},
        ],
        "ASCII mask": [
            {"name": "Privacy Blur", "amount": 36, "size": 14, "softness": 62, "detail": 0},
            {"name": "ASCII Art", "amount": 100, "size": 9, "softness": 66, "detail": 76, "angle": 72, "edge": 84, "phase": 0},
        ],
        "Blueprint concealment": [
            {"name": "Blueprint", "amount": 100, "size": 22, "softness": 16, "detail": 76, "angle": 34},
            {"name": "Halftone Dots", "amount": 22, "size": 9, "softness": 0, "detail": 58, "angle": 15},
        ],
        "Neon wireframe": [
            {"name": "Neon Edges", "amount": 100, "size": 9, "softness": 82, "detail": 72, "angle": -22},
            {"name": "Prism Split", "amount": 26, "size": 7, "softness": 4, "detail": 18, "angle": -10},
        ],
        "Topographic concealment": [
            {"name": "Topographic Lines", "amount": 100, "size": 14, "softness": 14, "detail": 74, "angle": 2},
            {"name": "Privacy Blur", "amount": 28, "size": 10, "softness": 40, "detail": 0},
        ],
    }

    def _default_effect_spec(self, name: str) -> dict[str, int | str]:
        name = self.EFFECT_ALIASES.get(name, name)
        parameters = self.EFFECT_PARAMETERS.get(name, self.EFFECT_PARAMETERS["Soft Blur"])
        spec: dict[str, int | str] = {"name": name}
        for key in self.EFFECT_PARAMETER_KEYS:
            definition = parameters.get(key)
            spec[key] = int(definition[3]) if definition else 0
        return spec

    def _normalize_effect_spec(self, value: object) -> dict[str, int | str]:
        if not isinstance(value, dict):
            return self._default_effect_spec(self.EFFECT_ALIASES.get(str(value), str(value)))
        raw_name = str(value.get("name", self.effect_combo.currentText()))
        name = self.EFFECT_ALIASES.get(raw_name, raw_name)
        spec = self._default_effect_spec(name)
        for key in self.EFFECT_PARAMETER_KEYS:
            if key in value:
                spec[key] = int(value[key])
        # Compatibility with RC4-RC6 saved/text-only chain values.
        if "strength" in value and "amount" not in value:
            spec["amount"] = int(value["strength"])
        legacy_size = value.get("pattern")
        if name in {"Soft Blur", "Privacy Blur"}:
            legacy_size = value.get("blur", legacy_size)
        elif name == "Pixelate":
            legacy_size = value.get("pixel", legacy_size)
        elif name == "Mosaic":
            legacy_size = value.get("mosaic", legacy_size)
        if legacy_size is not None and "size" not in value:
            spec["size"] = int(legacy_size)
        # Keep legacy aliases in stored chain data so upgrades and third-party
        # presets from RC4-RC6 remain readable while the UI uses the richer
        # generic parameter model.
        spec["strength"] = int(spec["amount"])
        spec["pattern"] = int(spec["size"])
        spec["blur"] = int(spec["size"])
        spec["pixel"] = int(spec["size"])
        spec["mosaic"] = int(spec["size"])
        return spec

    def _current_effect_spec(self) -> dict[str, int | str]:
        spec: dict[str, int | str] = {"name": self.effect_combo.currentText()}
        for key in self.EFFECT_PARAMETER_KEYS:
            spec[key] = self.effect_parameter_sliders[key].value()
        return self._normalize_effect_spec(spec)

    def _effect_item_label(self, spec: dict[str, int | str]) -> str:
        spec = self._normalize_effect_spec(spec)
        name = str(spec["name"])
        parameters = self.EFFECT_PARAMETERS.get(name, {})
        details = []
        for key in self.EFFECT_PARAMETER_KEYS:
            definition = parameters.get(key)
            if not definition:
                continue
            label, _minimum, _maximum, _default, suffix = definition
            if key == "amount" and int(spec[key]) == 100:
                continue
            details.append(f"{label} {spec[key]}{suffix}")
            if len(details) == 2:
                break
        return f"{name} · {' · '.join(details)}" if details else name

    def _make_effect_item(self, value: object) -> QListWidgetItem:
        spec = self._normalize_effect_spec(value)
        item = QListWidgetItem(self._effect_item_label(spec))
        item.setData(Qt.UserRole, spec)
        return item

    def _item_effect_spec(self, item: Optional[QListWidgetItem]) -> Optional[dict[str, int | str]]:
        if item is None:
            return None
        stored = item.data(Qt.UserRole)
        if isinstance(stored, dict):
            return self._normalize_effect_spec(stored)
        return self._normalize_effect_spec(item.text().split(" · ", 1)[0])

    def _set_effect_parameter_value_text(self, key: str, value: int) -> None:
        suffix = self.effect_parameter_suffixes.get(key, "")
        self.effect_parameter_values[key].setText(f"{value}{suffix}")

    def _update_effect_controls(self, name: str) -> None:
        parameters = self.EFFECT_PARAMETERS.get(name, {})
        for key in self.EFFECT_PARAMETER_KEYS:
            row = self.effect_parameter_rows[key]
            definition = parameters.get(key)
            visible = definition is not None
            if name in {"Black Redaction", "White Redaction"} and key in {"size", "angle"}:
                texture_slider = self.effect_parameter_sliders.get("softness")
                visible = visible and texture_slider is not None and texture_slider.value() > 0
            row.setVisible(visible)
            if definition is None:
                continue
            label, minimum, maximum, default, suffix = definition
            slider = self.effect_parameter_sliders[key]
            slider.blockSignals(True)
            slider.setRange(int(minimum), int(maximum))
            if not (minimum <= slider.value() <= maximum):
                slider.setValue(int(default))
            slider.blockSignals(False)
            self.effect_parameter_labels[key].setText(str(label))
            self.effect_parameter_suffixes[key] = str(suffix)
            self._set_effect_parameter_value_text(key, slider.value())
        self.effect_description.setText(self.EFFECT_DESCRIPTIONS.get(name, ""))

    def _load_effect_spec_controls(self, value: object) -> None:
        spec = self._normalize_effect_spec(value)
        self._loading_effect_spec = True
        self.effect_combo.blockSignals(True)
        for slider in self.effect_parameter_sliders.values():
            slider.blockSignals(True)
        try:
            self.effect_combo.setCurrentText(str(spec["name"]))
            self._update_effect_controls(str(spec["name"]))
            for key in self.EFFECT_PARAMETER_KEYS:
                slider = self.effect_parameter_sliders[key]
                slider.setValue(max(slider.minimum(), min(slider.maximum(), int(spec[key]))))
                self._set_effect_parameter_value_text(key, slider.value())
            size = int(spec["size"])
            self.canvas.blur_radius = float(size)
            self.canvas.pixel_size = max(2, size)
            self.canvas.mosaic_size = max(3, size)
            self._update_effect_controls(str(spec["name"]))
        finally:
            self.effect_combo.blockSignals(False)
            for slider in self.effect_parameter_sliders.values():
                slider.blockSignals(False)
            self._loading_effect_spec = False

    def _reset_current_effect(self) -> None:
        name = self.effect_combo.currentText()
        spec = self._default_effect_spec(name)
        self._load_effect_spec_controls(spec)
        item = self.effect_chain_list.currentItem() if self.effect_chain_list else None
        if item is not None:
            item.setData(Qt.UserRole, self._normalize_effect_spec(spec))
            item.setText(self._effect_item_label(spec))
            self.effect_preset_combo.blockSignals(True)
            self.effect_preset_combo.setCurrentText("Custom chain")
            self.effect_preset_combo.blockSignals(False)
        self._schedule_effect_preview()
        self._status(f"Reset {name.lower()} settings")

    def _load_effect_preset(self, name: str) -> None:
        if name == "Custom chain" or self.effect_chain_list is None:
            return
        self.effect_chain_list.clear()
        for spec in self.EFFECT_PRESETS.get(name, []):
            self.effect_chain_list.addItem(self._make_effect_item(spec))
        if self.effect_chain_list.count():
            self.effect_chain_list.setCurrentRow(0)
        self._schedule_effect_preview()
        self._status(f"Loaded censor preset: {name}")

    def _select_chain_effect(self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]) -> None:
        spec = self._item_effect_spec(current)
        if spec:
            self._load_effect_spec_controls(spec)

    def _effect_chain_reordered(self) -> None:
        self.effect_preset_combo.blockSignals(True)
        self.effect_preset_combo.setCurrentText("Custom chain")
        self.effect_preset_combo.blockSignals(False)
        self._schedule_effect_preview()

    def _effect_chain_specs(self) -> list[dict[str, int | str]]:
        if self.effect_chain_list is None:
            return []
        return [
            spec for index in range(self.effect_chain_list.count())
            if (spec := self._item_effect_spec(self.effect_chain_list.item(index))) is not None
        ]

    def _effect_chain_names(self) -> list[str]:
        return [str(spec["name"]) for spec in self._effect_chain_specs()]

    def _active_effect_specs(self) -> list[dict[str, int | str]]:
        specs = self._effect_chain_specs()
        return specs if specs else [self._current_effect_spec()]

    def _active_effect_names(self) -> list[str]:
        return [str(spec["name"]) for spec in self._active_effect_specs()]

    def _effect_summary(self, names: Optional[list[str]] = None) -> str:
        return " + ".join(item.lower() for item in (names or self._active_effect_names()))

    def _add_current_effect_to_chain(self) -> None:
        if self.effect_chain_list is None:
            return
        if self.effect_chain_list.count() >= 6:
            self._status("Censor chains are limited to 6 effects for responsive previews")
            return
        item = self._make_effect_item(self._current_effect_spec())
        self.effect_chain_list.addItem(item)
        self.effect_chain_list.setCurrentItem(item)
        self.effect_preset_combo.blockSignals(True)
        self.effect_preset_combo.setCurrentText("Custom chain")
        self.effect_preset_combo.blockSignals(False)
        self._update_button_states()
        self._schedule_effect_preview()
        self._status(f"Added {self.effect_combo.currentText().lower()} to the censor chain")

    def _remove_selected_effect_from_chain(self) -> None:
        if self.effect_chain_list is None:
            return
        row = self.effect_chain_list.currentRow()
        if row < 0:
            return
        self.effect_chain_list.takeItem(row)
        self.effect_preset_combo.blockSignals(True)
        self.effect_preset_combo.setCurrentText("Custom chain")
        self.effect_preset_combo.blockSignals(False)
        self._update_button_states()
        self._schedule_effect_preview()

    def _clear_effect_chain(self) -> None:
        if self.effect_chain_list is None or self.effect_chain_list.count() == 0:
            return
        self.effect_chain_list.clear()
        self.effect_preset_combo.blockSignals(True)
        self.effect_preset_combo.setCurrentText("Custom chain")
        self.effect_preset_combo.blockSignals(False)
        self._update_button_states()
        self._schedule_effect_preview()
        self._status("Censor effect chain cleared")

    def _effect_choice_changed(self, name: str) -> None:
        if not name:
            return
        if self._loading_effect_spec:
            self._update_effect_controls(name)
            return
        current = self.effect_chain_list.currentItem() if self.effect_chain_list else None
        current_spec = self._item_effect_spec(current)
        if current_spec and current_spec["name"] != name:
            self.effect_chain_list.setCurrentRow(-1)
        self._load_effect_spec_controls(self._default_effect_spec(name))
        brush_effect = {
            "brush_blur": "Soft Blur", "brush_pixel": "Pixelate",
            "brush_mosaic": "Mosaic", "brush_black": "Black Redaction",
        }.get(self.canvas.mode)
        if brush_effect is not None and brush_effect != name:
            self.choose_mode("select")
        self._schedule_effect_preview()

    def _schedule_effect_preview(self) -> None:
        if self.canvas.mode in {"clone", "heal", "brush_blur", "brush_pixel", "brush_mosaic", "brush_black"}:
            return
        self._schedule_preview("effect")

    def _schedule_adjustment_preview(self) -> None:
        if self.canvas.mode not in {"select", "lasso"}:
            self._set_canvas_mode_only("select")
        self._schedule_preview("adjustments")

    def _schedule_creative_preview(self) -> None:
        if self.canvas.mode not in {"select", "lasso"}:
            self._set_canvas_mode_only("select")
        self._schedule_preview("creative")

    def _effect_parameter_changed(self, key: str, value: int) -> None:
        self._set_effect_parameter_value_text(key, value)
        name = self.effect_combo.currentText()
        if key == "size":
            if name in {"Soft Blur", "Privacy Blur", "Directional Blur"}:
                self.canvas.blur_radius = float(value)
            elif name == "Pixelate":
                self.canvas.pixel_size = value
            elif name == "Mosaic":
                self.canvas.mosaic_size = value
        if key == "softness" and name in {"Black Redaction", "White Redaction"}:
            self._update_effect_controls(name)
        if not self._loading_effect_spec and self.effect_chain_list is not None:
            item = self.effect_chain_list.currentItem()
            spec = self._item_effect_spec(item)
            if item is not None and spec is not None:
                spec[key] = int(value)
                spec = self._normalize_effect_spec(spec)
                item.setData(Qt.UserRole, spec)
                item.setText(self._effect_item_label(spec))
                self.effect_preset_combo.blockSignals(True)
                self.effect_preset_combo.setCurrentText("Custom chain")
                self.effect_preset_combo.blockSignals(False)
        self._schedule_effect_preview()

    def _effect_param_changed(self, attribute: str, value) -> None:
        # Compatibility for old callers: route legacy names into the dynamic
        # parameter model instead of maintaining five separate slider systems.
        key = {
            "effect_strength": "amount", "effect_pattern": "size",
            "blur_radius": "size", "pixel_size": "size", "mosaic_size": "size",
        }.get(attribute)
        if key:
            slider = self.effect_parameter_sliders[key]
            slider.setValue(max(slider.minimum(), min(slider.maximum(), int(round(value)))))

    def _effect_transform(self, value: object, scale: float = 1.0) -> Callable[[Image.Image], Image.Image]:
        spec = self._normalize_effect_spec(value if isinstance(value, dict) else self._default_effect_spec(str(value)))
        name = str(spec["name"])
        amount = int(spec["amount"])
        size = int(spec["size"])
        softness = int(spec["softness"])
        detail = int(spec["detail"])
        angle = int(spec["angle"])
        edge = int(spec["edge"])
        phase = int(spec["phase"])
        if name == "Soft Blur":
            return lambda im: effects.privacy_blur(im, size, softness // 3, detail, amount, scale=scale)
        if name == "Privacy Blur":
            return lambda im: effects.privacy_blur(im, size, softness, detail, amount, scale=scale)
        if name == "Directional Blur":
            return lambda im: effects.directional_blur(im, size, softness, angle, amount, detail, scale=scale)
        if name == "Pixelate":
            return lambda im: effects.pixelate_tuned(im, max(2, round(size * scale)), softness, detail, angle, amount)
        if name == "Mosaic":
            return lambda im: effects.mosaic_tuned(im, size, softness, detail, angle, amount, scale=scale)
        if name == "Frosted Glass":
            return lambda im: effects.frosted_glass_tuned(im, size, softness, detail, angle, amount, preview_scale=scale)
        if name == "Faceted Glass":
            return lambda im: effects.faceted_glass(im, size, softness, detail, angle, amount, scale=scale)
        if name == "Encrypted Tiles":
            return lambda im: effects.encrypted_tiles(im, max(4, round(size * scale)), softness, detail, angle, amount)
        if name == "Prism Split":
            return lambda im: effects.prism_split(im, size, angle, softness, detail, amount, scale=scale)
        if name == "Wave Scramble":
            return lambda im: effects.wave_scramble(im, size, softness, detail, angle, amount, scale=scale)
        if name == "Black Redaction":
            return lambda im: effects.solid_redaction(im, (0, 0, 0), amount, size, softness, detail, angle, scale=scale)
        if name == "White Redaction":
            return lambda im: effects.solid_redaction(im, (255, 255, 255), amount, size, softness, detail, angle, scale=scale)
        if name == "Noise Redaction":
            return lambda im: effects.noise_redaction_tuned(im, amount, size, softness, detail, angle)
        if name == "Marker Scribble":
            return lambda im: effects.marker_scribble_tuned(im, amount, max(2, round(size * scale)), softness, detail, angle)
        if name == "Redaction Tape":
            return lambda im: effects.redaction_tape_tuned(im, amount, max(4, round(size * scale)), softness, detail, angle)
        if name == "Halftone Dots":
            return lambda im: effects.halftone_tuned(im, amount, max(3, round(size * scale)), softness, detail, angle)
        if name == "Barcode Redaction":
            return lambda im: effects.barcode_redaction(im, amount, max(2, round(size * scale)), softness, detail, angle)
        if name == "Ordered Dither":
            return lambda im: effects.ordered_dither(im, amount, max(1, round(size * scale)), softness, detail, angle)
        if name == "Glitch Blocks":
            return lambda im: effects.glitch_blocks_tuned(im, amount, max(3, round(size * scale)), max(1, round(softness * scale)), detail, angle)
        if name == "CRT Distortion":
            return lambda im: effects.crt_distortion_tuned(im, amount, size, softness, detail, angle, scale=scale)
        if name == "Silhouette":
            return lambda im: effects.silhouette_tuned(im, amount, size, softness, detail, angle, scale=scale)
        if name == "Comic Cutout":
            return lambda im: effects.comic_cutout_tuned(im, amount, size, softness, detail, angle)
        if name == "Thermal Map":
            return lambda im: effects.thermal_map_tuned(im, amount, size, softness, detail, angle)
        if name == "Photocopy":
            return lambda im: effects.photocopy_redaction(im, amount, size, softness, detail, angle)
        if name == "ASCII Art":
            return lambda im: effects.ascii_art_tuned(im, amount, max(5, round(size * scale)), softness, detail, angle, edge, phase)
        if name == "Blueprint":
            return lambda im: effects.blueprint_tuned(im, amount, max(8, round(size * scale)), softness, detail, angle)
        if name == "Neon Edges":
            return lambda im: effects.neon_edges_tuned(im, amount, max(0, round(size * scale)), softness, detail, angle)
        if name == "Topographic Lines":
            return lambda im: effects.topographic_lines_tuned(im, amount, size, softness, detail, angle)
        return lambda im: im.copy()

    def _composed_effect_transform(self, scale: float = 1.0) -> Callable[[Image.Image], Image.Image]:
        return effects.compose_transforms(self._effect_transform(spec, scale) for spec in self._active_effect_specs())

    def preview_effect(self) -> None:
        doc = self.document
        direct_modes = {"clone", "heal", "brush_blur", "brush_pixel", "brush_mosaic", "brush_black"}
        if (
            not doc
            or self.tool_tabs.currentIndex() != 0
            or self.canvas.mode in {"text", "sticker"}
            or self.canvas.mode in direct_modes
            or self._requested_preview_kind not in {None, "effect"}
        ):
            return
        names = self._active_effect_names()
        if doc.is_animated and self._gif_playing:
            self._advance_gif_preview()
            self._status(f"Live GIF effect: {self._effect_summary(names)}")
            return
        preview = self._render_live_preview(
            lambda image, scale: self._composed_effect_transform(scale)(image),
            outside_faces=self.canvas.mode == "face",
        )
        if preview is None:
            return
        self.canvas.preview_image = preview
        self._preview_kind = "effect"
        self._requested_preview_kind = "effect"
        self.canvas.update()
        self._status(f"Live preview: {self._effect_summary(names)} — saved output remains full resolution")

    def apply_effect(self) -> None:
        doc = self.document
        if not doc:
            return
        self._effect_preview_timer.stop()
        names = self._active_effect_names()
        if self.canvas.mode == "face":
            self.apply_outside_faces()
            return
        # Recompute from the current controls. A pending live-preview timer may
        # still represent an older slider value when Apply is clicked quickly.
        self._commit_transform(self._composed_effect_transform(), f"Applied {self._effect_summary(names)}")

    def cancel_preview(self, render: bool = True) -> None:
        self._stop_preview_timers()
        self.canvas.preview_image = None
        self._preview_kind = None
        self._requested_preview_kind = None
        if self.canvas.text_overlay_box is not None:
            self.canvas.clear_text_overlay()
            self.quick_text_anchor = None
            self.sticker_anchor = None
            self._last_sticker_box = None
        if self._gif_playing:
            self._advance_gif_preview()
        elif render:
            self.canvas.update()
        if render:
            self._status("Preview cancelled — masks unchanged")

    def apply_outside_faces(self) -> None:
        doc = self.document
        if not doc:
            return
        if not doc.face_masks:
            QMessageBox.information(self, "Face circles", "Draw one or more face circles first.")
            return
        names = self._active_effect_names()
        if not self._apply_document_transform(
            lambda frame: effects.apply_outside_faces(frame, self._composed_effect_transform(), doc.face_masks, feather=self._target_feather(), padding=self._target_padding()),
            label=f"Applying {self._effect_summary(names)}",
        ):
            return
        self.cancel_preview(False)
        self._document_changed()
        self._status(f"Applied {self._effect_summary(names)} outside face circles")

    def black_bar(self) -> None:
        doc = self.document
        if not doc or not doc.selection:
            QMessageBox.information(self, "Black bar", "Draw a rectangle selection first.")
            return
        def add_bar(frame: Image.Image) -> Image.Image:
            edited = frame.copy()
            ImageDraw.Draw(edited).rectangle(doc.selection.box, fill=(0, 0, 0, 255))
            return edited
        if not self._apply_document_transform(add_bar, label="Adding black bar"):
            return
        self.cancel_preview(False)
        self._document_changed()

    def preview_adjustments(self) -> None:
        if (
            not self.document
            or self.tool_tabs.currentIndex() != 2
            or self.canvas.mode in {"text", "sticker"}
            or self._requested_preview_kind not in {None, "adjustments"}
        ):
            return
        if self.document.is_animated and self._gif_playing:
            self._advance_gif_preview()
            self._status("Live GIF corrections")
            return
        preview = self._render_live_preview(
            lambda image, _scale: effects.adjustments(
                image, self.brightness.value(), self.contrast.value(), self.saturation.value(), self.sharpness.value()
            ),
            max_pixels=self.LIVE_ADJUSTMENT_PREVIEW_MAX_PIXELS,
        )
        if preview is None:
            return
        self.canvas.preview_image = preview
        self._preview_kind = "adjustments"
        self._requested_preview_kind = "adjustments"
        self.canvas.update()
        self._status("Live preview: corrections — saved output remains full resolution")

    def apply_adjustments(self) -> None:
        if not self.document:
            return
        transform = lambda image: effects.adjustments(
            image, self.brightness.value(), self.contrast.value(), self.saturation.value(), self.sharpness.value()
        )
        self._commit_transform(transform, "Applied image adjustments")
        self.reset_adjustments()

    def reset_adjustments(self) -> None:
        for slider in (self.brightness, self.contrast, self.saturation, self.sharpness):
            slider.setValue(0)
        self.cancel_preview(False)
        self.canvas.update()

    def _apply_document_transform(
        self,
        transform: Callable[[Image.Image], Image.Image],
        *,
        clear_masks: bool = False,
        label: str = "Applying edit",
    ) -> bool:
        doc = self.document
        if not doc:
            return False
        if not doc.is_animated:
            doc.apply_transform(transform, clear_masks=clear_masks)
            return True

        progress = QProgressDialog(f"{label} to animation…", "Cancel", 0, doc.frame_count, self)
        progress.setWindowTitle("Processing animation")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(250)
        summary = doc.apply_transform(
            transform,
            clear_masks=clear_masks,
            progress=lambda done, total: (progress.setMaximum(total), progress.setValue(done), QApplication.processEvents()),
            cancelled=progress.wasCanceled,
        )
        if summary is not None and summary.cancelled:
            self._status("Animation edit cancelled — document unchanged")
            return False
        progress.setValue(progress.maximum())
        if summary is not None and summary.reduced:
            self._pending_animation_notice = summary.description
        return True

    def _commit_transform(self, transform: Callable[[Image.Image], Image.Image], status: str, clear_masks: bool = False) -> None:
        doc = self.document
        if not doc:
            return
        try:
            frame_transform = (
                (lambda frame: transform(frame.copy()))
                if clear_masks
                else (lambda frame: effects.apply_to_target(frame, transform, doc.selection, doc.lasso_points, feather=self._target_feather(), padding=self._target_padding()))
            )
            if not self._apply_document_transform(frame_transform, clear_masks=clear_masks, label=status):
                return
            self.cancel_preview(False)
            self._document_changed()
            self._status(status)
        except Exception as exc:
            QMessageBox.critical(self, "Image effect", str(exc))

    @staticmethod
    def _grayscale_rgba(image: Image.Image) -> Image.Image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        out = ImageOps.grayscale(rgba).convert("RGBA")
        out.putalpha(alpha)
        return out

    @staticmethod
    def _invert_rgba(image: Image.Image) -> Image.Image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        out = ImageOps.invert(rgba.convert("RGB")).convert("RGBA")
        out.putalpha(alpha)
        return out

    @staticmethod
    def _posterize(image: Image.Image, strength: int) -> Image.Image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        bits = max(2, min(7, 8 - round(strength / 20)))
        out = ImageOps.posterize(rgba.convert("RGB"), bits).convert("RGBA")
        out.putalpha(alpha)
        return out

    @staticmethod
    def _preset_transform(name: str) -> Callable[[Image.Image], Image.Image]:
        mapping: dict[str, Callable[[Image.Image], Image.Image]] = {
            "Strong blur": lambda im: im.filter(ImageFilter.GaussianBlur(18)),
            "Strong pixelate": lambda im: effects.pixelate(im, 32),
            "Manga mosaic": lambda im: effects.mosaic(im, 20),
            "Clean enhance": effects.auto_enhance,
            "Cinematic look": lambda im: effects.cinematic(im, 32),
        }
        return mapping[name]

    def apply_preset(self) -> None:
        name = self.preset_combo.currentText()
        self._commit_transform(self._preset_transform(name), f"Applied preset: {name}")

    def batch_process(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Choose images", "", IMAGE_FILE_FILTER)
        if not names:
            return
        output = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if not output:
            return
        transform = self._preset_transform(self.preset_combo.currentText())
        success, failures = 0, []
        for raw in names:
            path = Path(raw)
            try:
                out = transform(read_image(path))
                target = unique_destination(output, f"{path.stem}_qfx{path.suffix}")
                save_image(out, target)
                success += 1
            except Exception as exc:
                failures.append(f"{path.name}: {exc}")
        QMessageBox.information(self, "Batch process", f"Saved {success}/{len(names)} images." + ("\n\n" + "\n".join(failures[:10]) if failures else ""))

    def choose_color(self) -> None:
        color = QColorDialog.getColor(QColor(self.canvas.shape_color), self, "Choose annotation color")
        if color.isValid():
            self.canvas.shape_color = color.name()
            self.color_label.setText(color.name())

    def _font(
        self,
        size: int,
        emoji: bool = False,
        *,
        font_path: str = "",
        bold: bool = False,
        italic: bool = False,
    ) -> ImageFont.ImageFont:
        normalized_path = str(Path(font_path).resolve()) if font_path and Path(font_path).is_file() else ""
        key = (max(1, int(size)), bool(emoji), normalized_path, bool(bold), bool(italic))
        cached = self._font_cache.get(key)
        if cached is not None:
            return cached
        candidates: list[Path | str] = []
        if normalized_path:
            candidates.append(normalized_path)
        if os.name == "nt":
            fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
            if emoji:
                candidates.extend([fonts / "seguiemj.ttf", fonts / "seguisym.ttf"])
            else:
                if bold and italic:
                    candidates.extend([fonts / "segoeuiz.ttf", fonts / "arialbi.ttf"])
                elif bold:
                    candidates.extend([fonts / "segoeuib.ttf", fonts / "arialbd.ttf"])
                elif italic:
                    candidates.extend([fonts / "segoeuii.ttf", fonts / "ariali.ttf"])
                candidates.extend([fonts / "segoeui.ttf", fonts / "arial.ttf"])
        elif not emoji:
            if bold and italic:
                candidates.extend(["DejaVuSans-BoldOblique.ttf", "LiberationSans-BoldItalic.ttf"])
            elif bold:
                candidates.extend(["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"])
            elif italic:
                candidates.extend(["DejaVuSans-Oblique.ttf", "LiberationSans-Italic.ttf"])
        if not emoji:
            candidates.extend(["DejaVuSans.ttf", "LiberationSans-Regular.ttf"])
        font: ImageFont.ImageFont
        for candidate in candidates:
            try:
                font = ImageFont.truetype(str(candidate), key[0])
                break
            except Exception:
                continue
        else:
            font = ImageFont.load_default()
        if len(self._font_cache) >= 64:
            self._font_cache.pop(next(iter(self._font_cache)))
        self._font_cache[key] = font
        return font

    def _quick_text_value(self) -> str:
        return self.quick_text_edit.toPlainText()

    def _set_quick_text_value(self, value: str) -> None:
        self.quick_text_edit.blockSignals(True)
        self.quick_text_edit.setPlainText(value)
        self.quick_text_edit.blockSignals(False)
        self._refresh_quick_text_preview()

    def choose_quick_text_font(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Choose text font", "", "Fonts (*.ttf *.otf *.ttc)")
        if not name:
            return
        self.text_font_path = name
        self.text_font_label.setText(Path(name).name)
        self._font_cache.clear()
        self._refresh_quick_text_preview()

    def reset_quick_text_font(self) -> None:
        self.text_font_path = ""
        self.text_font_label.setText("Default font")
        self._font_cache.clear()
        self._refresh_quick_text_preview()

    def _update_sticker_category(self, name: str) -> None:
        current = self.sticker_combo.currentText().strip() if hasattr(self, "sticker_combo") else ""
        values = self.STICKER_CATEGORIES.get(name, self.STICKER_CATEGORIES["Faces"])
        self.sticker_combo.blockSignals(True)
        self.sticker_combo.clear()
        self.sticker_combo.addItems(values)
        if current and current in values:
            self.sticker_combo.setCurrentText(current)
        self.sticker_combo.blockSignals(False)
        if self.sticker_image_path:
            self.sticker_image_path = ""
            if hasattr(self, "sticker_source_label"):
                self.sticker_source_label.setText("Emoji / symbol")
        self._refresh_sticker_preview()

    def _sticker_symbol_changed(self, _value: str) -> None:
        if self.sticker_image_path:
            self.sticker_image_path = ""
            if hasattr(self, "sticker_source_label"):
                self.sticker_source_label.setText("Emoji / symbol")
        self._refresh_sticker_preview()

    def choose_sticker_image(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Choose sticker image", "", "Sticker images (*.png *.webp *.jpg *.jpeg)")
        if not name:
            return
        try:
            with Image.open(name) as raw:
                raw.verify()
        except Exception as exc:
            show_operation_error(self, "Sticker could not be opened", "Choose a valid PNG, WebP, or JPEG image.", str(exc))
            return
        self.sticker_image_path = name
        self.sticker_source_label.setText(Path(name).name)
        self._refresh_sticker_preview()

    def clear_sticker_image(self) -> None:
        self.sticker_image_path = ""
        self.sticker_source_label.setText("Emoji / symbol")
        self._refresh_sticker_preview()

    def _canvas_clicked(self, x: float, y: float, mode: str) -> None:
        doc = self.document
        if not doc:
            return
        if mode == "text":
            self.quick_text_anchor = (round(x), round(y))
            self.preview_quick_text()
        elif mode == "sticker":
            self.sticker_anchor = (round(x), round(y))
            self.preview_sticker()

    def _load_quick_text_data(self) -> None:
        try:
            if self.quick_text_data_path.exists():
                payload = json.loads(self.quick_text_data_path.read_text(encoding="utf-8"))
                values = payload.get("recent_texts", []) if isinstance(payload, dict) else []
                if isinstance(values, list):
                    self.recent_texts = [str(value) for value in values if str(value).strip()][:10]
        except Exception:
            self.recent_texts = []

    def _save_quick_text_data(self) -> None:
        try:
            atomic_write_text(self.quick_text_data_path, json.dumps({"recent_texts": self.recent_texts}, indent=2))
        except Exception:
            pass

    def _choose_quick_text_color(self, current: str, title: str) -> Optional[str]:
        color = QColorDialog.getColor(QColor(current), self, title)
        return color.name() if color.isValid() else None

    def choose_text_color(self) -> None:
        chosen = self._choose_quick_text_color(self.text_color, "Choose text color")
        if chosen:
            self.text_color = chosen
            self._refresh_quick_text_preview()

    def choose_text_background_color(self) -> None:
        chosen = self._choose_quick_text_color(self.text_background_color, "Choose text background color")
        if chosen:
            self.text_background_color = chosen
            self._refresh_quick_text_preview()

    def choose_text_outline_color(self) -> None:
        chosen = self._choose_quick_text_color(self.text_outline_color, "Choose text outline color")
        if chosen:
            self.text_outline_color = chosen
            self._refresh_quick_text_preview()

    def choose_text_shadow_color(self) -> None:
        chosen = self._choose_quick_text_color(self.text_shadow_color, "Choose text shadow color")
        if chosen:
            self.text_shadow_color = chosen
            self._refresh_quick_text_preview()

    def _apply_quick_text_style(self, name: str) -> None:
        if not hasattr(self, "text_background_check"):
            return
        styles = {
            "Caption": dict(size=48, alignment="center", rotation=0, opacity=100, background=False, background_opacity=75, outline=True, outline_width=2, shadow=True, shadow_blur=4, shadow_opacity=70, padding=10, radius=8, bold=False, italic=False, char_spacing=0, line_spacing=8),
            "Title": dict(size=76, alignment="center", rotation=0, opacity=100, background=False, background_opacity=75, outline=True, outline_width=3, shadow=True, shadow_blur=6, shadow_opacity=75, padding=12, radius=10, bold=True, italic=False, char_spacing=1, line_spacing=10),
            "Subtitle": dict(size=36, alignment="center", rotation=0, opacity=100, background=True, background_opacity=70, outline=False, outline_width=0, shadow=False, shadow_blur=0, shadow_opacity=0, padding=12, radius=8, bold=False, italic=False, char_spacing=0, line_spacing=7),
            "Meme": dict(size=64, alignment="center", rotation=0, opacity=100, background=False, background_opacity=75, outline=True, outline_width=5, shadow=False, shadow_blur=0, shadow_opacity=0, padding=10, radius=0, bold=True, italic=False, char_spacing=0, line_spacing=8),
            "Label": dict(size=30, alignment="left", rotation=0, opacity=100, background=True, background_opacity=85, outline=False, outline_width=0, shadow=False, shadow_blur=0, shadow_opacity=0, padding=10, radius=8, bold=True, italic=False, char_spacing=0, line_spacing=6),
            "Quote": dict(size=46, alignment="center", rotation=0, opacity=100, background=True, background_opacity=58, outline=False, outline_width=0, shadow=True, shadow_blur=7, shadow_opacity=55, padding=22, radius=18, bold=False, italic=True, char_spacing=0, line_spacing=14),
            "Lower third": dict(size=38, alignment="left", rotation=0, opacity=100, background=True, background_opacity=82, outline=False, outline_width=0, shadow=True, shadow_blur=4, shadow_opacity=45, padding=14, radius=4, bold=True, italic=False, char_spacing=0, line_spacing=8),
            "Watermark": dict(size=54, alignment="center", rotation=-25, opacity=32, background=False, background_opacity=0, outline=False, outline_width=0, shadow=False, shadow_blur=0, shadow_opacity=0, padding=4, radius=0, bold=True, italic=False, char_spacing=3, line_spacing=8),
        }
        style = styles.get(name, styles["Caption"])
        controls = [
            (self.text_size, style["size"]), (self.text_rotation, style["rotation"]), (self.text_opacity, style["opacity"]),
            (self.text_background_opacity, style["background_opacity"]), (self.text_outline_width, style["outline_width"]),
            (self.text_shadow_blur, style["shadow_blur"]), (self.text_shadow_opacity, style["shadow_opacity"]),
            (self.text_padding, style["padding"]), (self.text_corner_radius, style["radius"]),
            (self.text_character_spacing, style["char_spacing"]), (self.text_line_spacing, style["line_spacing"]),
            (self.text_wrap_width, int(style.get("wrap_width", 0))),
        ]
        for control, value in controls:
            control.setValue(value)
        self.text_alignment_combo.setCurrentText(style["alignment"])
        self.text_background_check.setChecked(style["background"])
        self.text_outline_check.setChecked(style["outline"])
        self.text_shadow_check.setChecked(style["shadow"])
        self.text_bold_check.setChecked(style["bold"])
        self.text_italic_check.setChecked(style["italic"])
        self._refresh_quick_text_preview()
        self._status(f"Text preset: {name}")

    @staticmethod
    def _multiply_alpha(image: Image.Image, opacity: int) -> Image.Image:
        if opacity >= 100:
            return image
        alpha = image.getchannel("A").point(lambda value: round(value * max(0, opacity) / 100))
        image.putalpha(alpha)
        return image

    @staticmethod
    def _rotated_size(width: int, height: int, angle: float) -> tuple[int, int]:
        radians = math.radians(angle % 180)
        cos_value = abs(math.cos(radians))
        sin_value = abs(math.sin(radians))
        return max(1, math.ceil(width * cos_value + height * sin_value)), max(1, math.ceil(width * sin_value + height * cos_value))

    @staticmethod
    def _line_width(draw: ImageDraw.ImageDraw, line: str, font: ImageFont.ImageFont, character_spacing: int, stroke_width: int) -> float:
        if not line:
            return 0.0
        if character_spacing == 0:
            bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
            return float(max(0, bbox[2] - bbox[0]))
        widths = [float(draw.textlength(char, font=font)) for char in line]
        return max(0.0, sum(widths) + character_spacing * max(0, len(line) - 1) + stroke_width * 2)

    @staticmethod
    def _draw_line(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        line: str,
        *,
        font: ImageFont.ImageFont,
        fill,
        character_spacing: int,
        stroke_width: int = 0,
        stroke_fill=None,
    ) -> None:
        if character_spacing == 0:
            draw.text(xy, line, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
            return
        x, y = xy
        for char in line:
            draw.text((x, y), char, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
            x += float(draw.textlength(char, font=font)) + character_spacing

    def _wrapped_text_lines(
        self,
        text: str,
        draw: ImageDraw.ImageDraw,
        font: ImageFont.ImageFont,
        character_spacing: int,
        stroke_width: int,
        max_width: int,
    ) -> list[str]:
        paragraphs = text.splitlines() or [""]
        if max_width <= 0:
            return paragraphs
        lines: list[str] = []
        for paragraph in paragraphs:
            if not paragraph:
                lines.append("")
                continue
            words = paragraph.split(" ")
            current = ""
            for word in words:
                candidate = word if not current else f"{current} {word}"
                if self._line_width(draw, candidate, font, character_spacing, stroke_width) <= max_width:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                    current = ""
                if self._line_width(draw, word, font, character_spacing, stroke_width) <= max_width:
                    current = word
                    continue
                chunk = ""
                for char in word:
                    candidate_chunk = chunk + char
                    if chunk and self._line_width(draw, candidate_chunk, font, character_spacing, stroke_width) > max_width:
                        lines.append(chunk)
                        chunk = char
                    else:
                        chunk = candidate_chunk
                current = chunk
            lines.append(current)
        return lines or [""]

    def _text_layout(self, text: str, size: int, scale: float) -> dict[str, object]:
        draw_size = max(1, round(size * scale))
        font = self._font(
            draw_size,
            font_path=self.text_font_path,
            bold=self.text_bold_check.isChecked(),
            italic=self.text_italic_check.isChecked(),
        )
        character_spacing = round(self.text_character_spacing.value() * scale)
        line_spacing = max(0, round(self.text_line_spacing.value() * scale))
        outline = max(0, round((self.text_outline_width.value() if self.text_outline_check.isChecked() else 0) * scale))
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        wrap_width = max(0, round(self.text_wrap_width.value() * scale))
        lines = self._wrapped_text_lines(text, probe, font, character_spacing, outline, wrap_width)
        widths = [self._line_width(probe, line, font, character_spacing, outline) for line in lines]
        bbox = probe.textbbox((0, 0), "Ag", font=font, stroke_width=outline)
        line_top = int(bbox[1])
        line_height = max(1, bbox[3] - bbox[1])
        block_width = max(1, math.ceil(max(widths, default=1.0)))
        block_height = max(1, line_height * len(lines) + line_spacing * max(0, len(lines) - 1))
        return {
            "font": font, "lines": lines, "widths": widths, "line_top": line_top, "line_height": line_height,
            "block_width": block_width, "block_height": block_height,
            "character_spacing": character_spacing, "line_spacing": line_spacing, "outline": outline,
        }

    def _render_text_layer(self, render_scale: float = 1.0) -> tuple[Image.Image, tuple[int, int]] | None:
        text = self._quick_text_value()
        if not text.strip():
            return None
        scale = max(0.001, float(render_scale))
        full_layout = self._text_layout(text, self.text_size.value(), 1.0)
        layout = full_layout if scale == 1.0 else self._text_layout(text, self.text_size.value(), scale)
        padding = max(0, round(self.text_padding.value() * scale))
        outline = int(layout["outline"])
        shadow_x = round(self.text_shadow_x.value() * scale) if self.text_shadow_check.isChecked() else 0
        shadow_y = round(self.text_shadow_y.value() * scale) if self.text_shadow_check.isChecked() else 0
        shadow_blur = max(0, round(self.text_shadow_blur.value() * scale)) if self.text_shadow_check.isChecked() else 0
        margin = max(outline + 2, padding + 2, abs(shadow_x) + shadow_blur * 2 + 2, abs(shadow_y) + shadow_blur * 2 + 2)
        width = int(layout["block_width"]) + padding * 2 + margin * 2
        height = int(layout["block_height"]) + padding * 2 + margin * 2
        layer = Image.new("RGBA", (max(1, width), max(1, height)), (0, 0, 0, 0))
        x0 = margin + padding
        y0 = margin + padding
        alignment = self.text_alignment_combo.currentText()

        if self.text_background_check.isChecked():
            background = QColor(self.text_background_color)
            alpha = round(255 * self.text_background_opacity.value() / 100)
            radius = max(0, round(self.text_corner_radius.value() * scale))
            ImageDraw.Draw(layer, "RGBA").rounded_rectangle(
                (margin, margin, margin + int(layout["block_width"]) + padding * 2, margin + int(layout["block_height"]) + padding * 2),
                radius=radius,
                fill=(background.red(), background.green(), background.blue(), alpha),
            )

        def draw_text_lines(target: Image.Image, fill, offset_x: int = 0, offset_y: int = 0, stroke_fill=None) -> None:
            draw = ImageDraw.Draw(target, "RGBA")
            y = y0 + offset_y
            for line, line_width in zip(layout["lines"], layout["widths"]):
                if alignment == "center":
                    x = x0 + (int(layout["block_width"]) - float(line_width)) / 2
                elif alignment == "right":
                    x = x0 + int(layout["block_width"]) - float(line_width)
                else:
                    x = x0
                self._draw_line(
                    draw, (x + offset_x, y - int(layout["line_top"])), line, font=layout["font"], fill=fill,
                    character_spacing=int(layout["character_spacing"]), stroke_width=outline, stroke_fill=stroke_fill,
                )
                y += int(layout["line_height"]) + int(layout["line_spacing"])

        if self.text_shadow_check.isChecked() and self.text_shadow_opacity.value() > 0:
            mask = Image.new("L", layer.size, 0)
            draw_mask = ImageDraw.Draw(mask)
            y = y0 + shadow_y
            for line, line_width in zip(layout["lines"], layout["widths"]):
                if alignment == "center":
                    x = x0 + (int(layout["block_width"]) - float(line_width)) / 2
                elif alignment == "right":
                    x = x0 + int(layout["block_width"]) - float(line_width)
                else:
                    x = x0
                self._draw_line(
                    draw_mask, (x + shadow_x, y - int(layout["line_top"])), line, font=layout["font"], fill=255,
                    character_spacing=int(layout["character_spacing"]), stroke_width=outline, stroke_fill=255,
                )
                y += int(layout["line_height"]) + int(layout["line_spacing"])
            if shadow_blur:
                mask = mask.filter(ImageFilter.GaussianBlur(shadow_blur))
            shadow_color = QColor(self.text_shadow_color)
            shadow_alpha = mask.point(lambda value: round(value * self.text_shadow_opacity.value() / 100))
            shadow_layer = Image.new("RGBA", layer.size, (shadow_color.red(), shadow_color.green(), shadow_color.blue(), 0))
            shadow_layer.putalpha(shadow_alpha)
            layer.alpha_composite(shadow_layer)
            shadow_layer.close()
            shadow_alpha.close()
            mask.close()

        outline_color = QColor(self.text_outline_color)
        text_color = QColor(self.text_color)
        draw_text_lines(
            layer,
            (text_color.red(), text_color.green(), text_color.blue(), 255),
            stroke_fill=(outline_color.red(), outline_color.green(), outline_color.blue(), 255),
        )
        layer = self._multiply_alpha(layer, self.text_opacity.value())
        angle = self.text_rotation.value()
        if angle:
            unrotated = layer
            layer = unrotated.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=True)
            unrotated.close()

        full_padding = self.text_padding.value()
        full_outline = int(full_layout["outline"])
        full_shadow_x = self.text_shadow_x.value() if self.text_shadow_check.isChecked() else 0
        full_shadow_y = self.text_shadow_y.value() if self.text_shadow_check.isChecked() else 0
        full_shadow_blur = self.text_shadow_blur.value() if self.text_shadow_check.isChecked() else 0
        full_margin = max(full_outline + 2, full_padding + 2, abs(full_shadow_x) + full_shadow_blur * 2 + 2, abs(full_shadow_y) + full_shadow_blur * 2 + 2)
        full_width = int(full_layout["block_width"]) + full_padding * 2 + full_margin * 2
        full_height = int(full_layout["block_height"]) + full_padding * 2 + full_margin * 2
        full_rotated = self._rotated_size(full_width, full_height, angle)
        return layer, full_rotated

    def _quick_text_image(self, base: Image.Image, render_scale: float = 1.0) -> Optional[Image.Image]:
        if not self.quick_text_anchor:
            return None
        rendered = self._render_text_layer(render_scale)
        if rendered is None:
            return None
        layer, full_size = rendered
        scale = max(0.001, float(render_scale))
        anchor_x, anchor_y = self.quick_text_anchor
        full_left = round(anchor_x - full_size[0] / 2)
        full_top = round(anchor_y - full_size[1] / 2)
        self._last_quick_text_box = RectMask(full_left, full_top, full_left + full_size[0], full_top + full_size[1])
        result = base.convert("RGBA").copy()
        draw_anchor_x = round(anchor_x * scale)
        draw_anchor_y = round(anchor_y * scale)
        left = round(draw_anchor_x - layer.width / 2)
        top = round(draw_anchor_y - layer.height / 2)
        result.alpha_composite(layer, (left, top))
        layer.close()
        return result

    def _render_sticker_layer(self, render_scale: float = 1.0) -> tuple[Image.Image, tuple[int, int]] | None:
        sticker = self.sticker_combo.currentText().strip()
        if not sticker and not self.sticker_image_path:
            return None
        scale = max(0.001, float(render_scale))
        size = max(1, round(self.sticker_size.value() * scale))
        outline_width = max(1, round(size / 28)) if self.sticker_outline_check.isChecked() else 0
        shadow_blur = max(1, round(size / 18)) if self.sticker_shadow_check.isChecked() else 0
        shadow_offset = max(1, round(size / 16)) if self.sticker_shadow_check.isChecked() else 0
        margin = max(4, outline_width * 2 + 2, shadow_blur * 2 + shadow_offset + 2)
        if self.sticker_image_path and Path(self.sticker_image_path).is_file():
            with Image.open(self.sticker_image_path) as raw:
                source = raw.convert("RGBA")
                source.thumbnail((size, size), Image.Resampling.LANCZOS)
                sticker_image = source.copy()
            layer = Image.new("RGBA", (sticker_image.width + margin * 2, sticker_image.height + margin * 2), (0, 0, 0, 0))
            layer.alpha_composite(sticker_image, (margin, margin))
            sticker_image.close()
        else:
            font = self._font(size, emoji=True)
            probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            try:
                bbox = probe.textbbox((0, 0), sticker, font=font, embedded_color=True)
            except TypeError:
                bbox = probe.textbbox((0, 0), sticker, font=font)
            width = max(1, bbox[2] - bbox[0])
            height = max(1, bbox[3] - bbox[1])
            layer = Image.new("RGBA", (width + margin * 2, height + margin * 2), (0, 0, 0, 0))
            draw = ImageDraw.Draw(layer)
            xy = (margin - bbox[0], margin - bbox[1])
            try:
                draw.text(xy, sticker, font=font, embedded_color=True)
            except Exception:
                draw.text(xy, sticker, font=font, fill=self.canvas.shape_color)
        alpha = layer.getchannel("A")
        if self.sticker_outline_check.isChecked():
            kernel = max(3, outline_width * 2 + 1)
            if kernel % 2 == 0:
                kernel += 1
            expanded = alpha.filter(ImageFilter.MaxFilter(kernel))
            outline_alpha = Image.eval(expanded, lambda value: value)
            outline_layer = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            outline_layer.putalpha(outline_alpha)
            outline_color = Image.new("RGBA", layer.size, (255, 255, 255, 255))
            outline_color.putalpha(outline_alpha)
            outline_color.alpha_composite(layer)
            previous_layer = layer
            layer = outline_color
            previous_layer.close()
            alpha.close()
            expanded.close()
            outline_alpha.close()
            alpha = layer.getchannel("A")
        if self.sticker_shadow_check.isChecked():
            shadow_mask = alpha.filter(ImageFilter.GaussianBlur(shadow_blur))
            shifted = Image.new("L", layer.size, 0)
            shifted.paste(shadow_mask, (shadow_offset, shadow_offset))
            shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            shadow.putalpha(shifted.point(lambda value: round(value * 0.65)))
            composed = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            composed.alpha_composite(shadow)
            composed.alpha_composite(layer)
            previous_layer = layer
            layer = composed
            previous_layer.close()
            shadow.close()
            shifted.close()
            shadow_mask.close()
        layer = self._multiply_alpha(layer, self.sticker_opacity.value())
        angle = self.sticker_rotation.value()
        if angle:
            unrotated = layer
            layer = unrotated.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=True)
            unrotated.close()
        alpha.close()
        full_scale = self.sticker_size.value() / max(1, size)
        full_width = max(1, round(layer.width * full_scale))
        full_height = max(1, round(layer.height * full_scale))
        return layer, (full_width, full_height)

    def _sticker_image(self, base: Image.Image, render_scale: float = 1.0) -> Optional[Image.Image]:
        if not self.sticker_anchor:
            return None
        rendered = self._render_sticker_layer(render_scale)
        if rendered is None:
            return None
        layer, full_size = rendered
        anchor_x, anchor_y = self.sticker_anchor
        full_left = round(anchor_x - full_size[0] / 2)
        full_top = round(anchor_y - full_size[1] / 2)
        self._last_sticker_box = RectMask(full_left, full_top, full_left + full_size[0], full_top + full_size[1])
        scale = max(0.001, float(render_scale))
        result = base.convert("RGBA").copy()
        left = round(anchor_x * scale - layer.width / 2)
        top = round(anchor_y * scale - layer.height / 2)
        result.alpha_composite(layer, (left, top))
        layer.close()
        return result

    def preview_quick_text(self) -> None:
        doc = self.document
        if not doc:
            return
        self._requested_preview_kind = "text"
        self._stop_preview_timers()
        if self.quick_text_anchor is None:
            self.quick_text_anchor = (doc.image.width // 2, doc.image.height // 2)
        if doc.is_animated and self._gif_playing:
            self._advance_gif_preview()
            self._status("Text live on animation — drag or resize while it plays")
            return
        preview_source = self._live_preview_source()
        if preview_source is None:
            return
        source, scale_x, scale_y = preview_source
        preview = self._quick_text_image(source, min(scale_x, scale_y))
        if preview is None:
            self.cancel_quick_text()
            return
        self.canvas.preview_image = preview
        self._preview_kind = "text"
        self._requested_preview_kind = "text"
        self.canvas.set_text_overlay(getattr(self, "_last_quick_text_box", None), self.text_size.value())
        self.canvas.update()
        self._status("Text active — drag, resize, rotate from controls, then Ctrl+Enter or double-click to apply")

    def preview_sticker(self) -> None:
        doc = self.document
        if not doc:
            return
        self._requested_preview_kind = "sticker"
        self._stop_preview_timers()
        if self.sticker_anchor is None:
            self.sticker_anchor = (doc.image.width // 2, doc.image.height // 2)
        if doc.is_animated and self._gif_playing:
            self._advance_gif_preview()
            self._status("Sticker live on animation — drag or resize while it plays")
            return
        preview_source = self._live_preview_source()
        if preview_source is None:
            return
        source, scale_x, scale_y = preview_source
        preview = self._sticker_image(source, min(scale_x, scale_y))
        if preview is None:
            self.cancel_sticker()
            return
        self.canvas.preview_image = preview
        self._preview_kind = "sticker"
        self._requested_preview_kind = "sticker"
        self.canvas.set_text_overlay(self._last_sticker_box, self.sticker_size.value())
        self.canvas.update()
        self._status("Sticker active — drag, resize, rotate or fade, then apply")

    def _apply_active_annotation(self) -> None:
        if self.canvas.mode == "sticker":
            self.apply_sticker()
        else:
            self.apply_quick_text()

    def _text_transform_changed(self, center_x: float, center_y: float, font_size: int) -> None:
        if self.canvas.mode == "text":
            self._text_preview_timer.stop()
            self.quick_text_anchor = (round(center_x), round(center_y))
            if self.text_size.value() != font_size:
                self.text_size.blockSignals(True)
                self.text_size.setValue(font_size)
                self.text_size.blockSignals(False)
            self.preview_quick_text()
        elif self.canvas.mode == "sticker":
            self._sticker_preview_timer.stop()
            self.sticker_anchor = (round(center_x), round(center_y))
            if self.sticker_size.value() != font_size:
                self.sticker_size.blockSignals(True)
                self.sticker_size.setValue(font_size)
                self.sticker_size.blockSignals(False)
            self.preview_sticker()

    def _refresh_quick_text_preview(self, *_args) -> None:
        if getattr(self, "canvas", None) and self.canvas.mode == "text" and self.quick_text_anchor is not None:
            self._schedule_preview("text")

    def _refresh_sticker_preview(self, *_args) -> None:
        if getattr(self, "canvas", None) and self.canvas.mode == "sticker" and self.sticker_anchor is not None:
            self._schedule_preview("sticker")

    def _quick_text_state(self) -> dict[str, object]:
        return {
            "text": self._quick_text_value(), "anchor": self.quick_text_anchor, "style": self.text_style_combo.currentText(),
            "size": self.text_size.value(), "rotation": self.text_rotation.value(), "opacity": self.text_opacity.value(),
            "alignment": self.text_alignment_combo.currentText(), "font_path": self.text_font_path,
            "bold": self.text_bold_check.isChecked(), "italic": self.text_italic_check.isChecked(),
            "character_spacing": self.text_character_spacing.value(), "line_spacing": self.text_line_spacing.value(),
            "wrap_width": self.text_wrap_width.value(),
            "background": self.text_background_check.isChecked(), "background_opacity": self.text_background_opacity.value(),
            "outline": self.text_outline_check.isChecked(), "outline_width": self.text_outline_width.value(),
            "shadow": self.text_shadow_check.isChecked(), "shadow_x": self.text_shadow_x.value(), "shadow_y": self.text_shadow_y.value(),
            "shadow_blur": self.text_shadow_blur.value(), "shadow_opacity": self.text_shadow_opacity.value(),
            "padding": self.text_padding.value(), "corner_radius": self.text_corner_radius.value(),
            "text_color": self.text_color, "background_color": self.text_background_color,
            "outline_color": self.text_outline_color, "shadow_color": self.text_shadow_color,
        }

    def _sticker_state(self) -> dict[str, object]:
        return {
            "sticker": self.sticker_combo.currentText(), "category": self.sticker_category.currentText(),
            "image_path": self.sticker_image_path,
            "anchor": self.sticker_anchor, "size": self.sticker_size.value(), "rotation": self.sticker_rotation.value(),
            "opacity": self.sticker_opacity.value(), "shadow": self.sticker_shadow_check.isChecked(),
            "outline": self.sticker_outline_check.isChecked(),
        }

    def reedit_last_text(self) -> None:
        doc = self.document
        entry = self._last_text_edit.get(doc.id) if doc else None
        if not doc or not entry or len(doc.undo_stack) != entry["undo_depth"] or doc.redo_stack:
            self._status("The last text can only be re-edited before another change")
            return
        if not doc.undo():
            return
        doc.redo_stack.clear()
        state = entry["state"]
        self._set_quick_text_value(str(state.get("text", "")))
        self.text_style_combo.blockSignals(True)
        self.text_style_combo.setCurrentText(str(state.get("style", "Caption")))
        self.text_style_combo.blockSignals(False)
        self.text_size.setValue(int(state.get("size", 48)))
        self.text_rotation.setValue(int(state.get("rotation", 0)))
        self.text_opacity.setValue(int(state.get("opacity", 100)))
        self.text_alignment_combo.setCurrentText(str(state.get("alignment", "center")))
        self.text_font_path = str(state.get("font_path", ""))
        self.text_font_label.setText(Path(self.text_font_path).name if self.text_font_path else "Default font")
        self.text_bold_check.setChecked(bool(state.get("bold", False)))
        self.text_italic_check.setChecked(bool(state.get("italic", False)))
        self.text_character_spacing.setValue(int(state.get("character_spacing", 0)))
        self.text_line_spacing.setValue(int(state.get("line_spacing", 8)))
        self.text_wrap_width.setValue(int(state.get("wrap_width", 0)))
        self.text_background_check.setChecked(bool(state.get("background", False)))
        self.text_background_opacity.setValue(int(state.get("background_opacity", 75)))
        self.text_outline_check.setChecked(bool(state.get("outline", True)))
        self.text_outline_width.setValue(int(state.get("outline_width", 2)))
        self.text_shadow_check.setChecked(bool(state.get("shadow", True)))
        self.text_shadow_x.setValue(int(state.get("shadow_x", 3)))
        self.text_shadow_y.setValue(int(state.get("shadow_y", 3)))
        self.text_shadow_blur.setValue(int(state.get("shadow_blur", 4)))
        self.text_shadow_opacity.setValue(int(state.get("shadow_opacity", 70)))
        self.text_padding.setValue(int(state.get("padding", 10)))
        self.text_corner_radius.setValue(int(state.get("corner_radius", 8)))
        self.text_color = str(state.get("text_color", "#ffffff"))
        self.text_background_color = str(state.get("background_color", "#000000"))
        self.text_outline_color = str(state.get("outline_color", "#000000"))
        self.text_shadow_color = str(state.get("shadow_color", "#000000"))
        anchor = state.get("anchor")
        self.quick_text_anchor = tuple(anchor) if anchor else None
        self.choose_mode("text")
        self.preview_quick_text()
        self._last_text_edit.pop(doc.id, None)
        self._document_changed()
        self._status("Last text reopened for editing")

    def reedit_last_sticker(self) -> None:
        doc = self.document
        entry = self._last_sticker_edit.get(doc.id) if doc else None
        if not doc or not entry or len(doc.undo_stack) != entry["undo_depth"] or doc.redo_stack:
            self._status("The last sticker can only be re-edited before another change")
            return
        if not doc.undo():
            return
        doc.redo_stack.clear()
        state = entry["state"]
        self.sticker_category.setCurrentText(str(state.get("category", "Faces")))
        self.sticker_combo.setCurrentText(str(state.get("sticker", "😀")))
        self.sticker_image_path = str(state.get("image_path", ""))
        self.sticker_source_label.setText(Path(self.sticker_image_path).name if self.sticker_image_path else "Emoji / symbol")
        self.sticker_size.setValue(int(state.get("size", 96)))
        self.sticker_rotation.setValue(int(state.get("rotation", 0)))
        self.sticker_opacity.setValue(int(state.get("opacity", 100)))
        self.sticker_shadow_check.setChecked(bool(state.get("shadow", True)))
        self.sticker_outline_check.setChecked(bool(state.get("outline", False)))
        anchor = state.get("anchor")
        self.sticker_anchor = tuple(anchor) if anchor else None
        self.choose_mode("sticker")
        self.preview_sticker()
        self._last_sticker_edit.pop(doc.id, None)
        self._document_changed()
        self._status("Last sticker reopened for editing")

    def apply_quick_text(self) -> None:
        doc = self.document
        if not doc or self.canvas.mode != "text":
            return
        rendered = self._quick_text_image(doc.image)
        if rendered is None:
            return
        state = self._quick_text_state()
        if doc.is_animated:
            def render_text(frame: Image.Image) -> Image.Image:
                frame_rendered = self._quick_text_image(frame)
                if frame_rendered is None:
                    raise ValueError("Text could not be rendered.")
                return frame_rendered
            if not self._apply_document_transform(render_text, label="Applying text"):
                return
        else:
            doc.commit(rendered)
        self._last_text_edit[doc.id] = {"undo_depth": len(doc.undo_stack), "state": state}
        text = self._quick_text_value().strip()
        if text:
            self.recent_texts = [text] + [item for item in self.recent_texts if item != text]
            self.recent_texts = self.recent_texts[:10]
            self.recent_text_combo.clear()
            self.recent_text_combo.addItems(self.recent_texts)
            self._save_quick_text_data()
        self.cancel_quick_text(clear_status=False)
        self._document_changed()
        if self._gif_playing:
            self._advance_gif_preview()
        self._status("Text applied")

    def apply_sticker(self) -> None:
        doc = self.document
        if not doc or self.canvas.mode != "sticker":
            return
        rendered = self._sticker_image(doc.image)
        if rendered is None:
            return
        state = self._sticker_state()
        if doc.is_animated:
            def render_sticker(frame: Image.Image) -> Image.Image:
                frame_rendered = self._sticker_image(frame)
                if frame_rendered is None:
                    raise ValueError("Sticker could not be rendered.")
                return frame_rendered
            if not self._apply_document_transform(render_sticker, label="Applying sticker"):
                return
        else:
            doc.commit(rendered)
        self._last_sticker_edit[doc.id] = {"undo_depth": len(doc.undo_stack), "state": state}
        self.cancel_sticker(clear_status=False)
        self._document_changed()
        if self._gif_playing:
            self._advance_gif_preview()
        self._status("Sticker applied")

    def cancel_quick_text(self, *, clear_status: bool = True) -> None:
        self._text_preview_timer.stop()
        if self.canvas.mode == "text" or self._preview_kind == "text":
            self.canvas.preview_image = None
            self._preview_kind = None
            self._requested_preview_kind = None
            self.canvas.clear_text_overlay()
        self.quick_text_anchor = None
        if self._gif_playing:
            self._advance_gif_preview()
        else:
            self.canvas.update()
        if clear_status:
            self._status("Text preview cancelled")

    def cancel_sticker(self, *, clear_status: bool = True) -> None:
        self._sticker_preview_timer.stop()
        if self.canvas.mode == "sticker" or self._preview_kind == "sticker":
            self.canvas.preview_image = None
            self._preview_kind = None
            self._requested_preview_kind = None
            self.canvas.clear_text_overlay()
        self.sticker_anchor = None
        self._last_sticker_box = None
        if self._gif_playing:
            self._advance_gif_preview()
        else:
            self.canvas.update()
        if clear_status:
            self._status("Sticker preview cancelled")

    def place_quick_text_center(self) -> None:
        doc = self.document
        if not doc:
            return
        self.quick_text_anchor = (doc.image.width // 2, doc.image.height // 2)
        self.preview_quick_text()

    def place_sticker_center(self) -> None:
        doc = self.document
        if not doc:
            return
        self.sticker_anchor = (doc.image.width // 2, doc.image.height // 2)
        self.preview_sticker()

    def open_advanced_text_dialog(self) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "Text", "Text", self._quick_text_value())
        if ok:
            self._set_quick_text_value(text)
            self.preview_quick_text()

    def use_recent_text(self) -> None:
        text = self.recent_text_combo.currentText()
        if text:
            self._set_quick_text_value(text)
            self.preview_quick_text()


    def crop(self) -> None:
        doc = self.document
        if not doc:
            return
        if doc.lasso_points:
            xs, ys = zip(*doc.lasso_points)
            box = (max(0, min(xs)), max(0, min(ys)), min(doc.image.width, max(xs) + 1), min(doc.image.height, max(ys) + 1))
            def crop_lasso(frame: Image.Image) -> Image.Image:
                frame_mask = Image.new("L", frame.size, 0)
                ImageDraw.Draw(frame_mask).polygon(doc.lasso_points, fill=255)
                frame_isolated = Image.new("RGBA", frame.size, (0, 0, 0, 0))
                frame_isolated.paste(frame, (0, 0), frame_mask)
                return frame_isolated.crop(box)
            if not self._apply_document_transform(crop_lasso, clear_masks=True, label="Cropping GIF"):
                return
        elif doc.selection:
            selection_box = doc.selection.box
            if not self._apply_document_transform(lambda frame: frame.crop(selection_box), clear_masks=True, label="Cropping GIF"):
                return
        else:
            QMessageBox.information(self, "Crop", "Draw a rectangle or lasso first.")
            return
        self.canvas.fit_to_window()
        self._document_changed()

    def resize_image(self) -> None:
        doc = self.document
        if not doc:
            return
        width, ok = QInputDialog.getInt(self, "Resize", "Width", doc.image.width, 1, 32768)
        if not ok:
            return
        height, ok = QInputDialog.getInt(self, "Resize", "Height", doc.image.height, 1, 32768)
        if not ok:
            return
        if not self._apply_document_transform(
            lambda frame: frame.resize((width, height), Image.Resampling.LANCZOS),
            clear_masks=True,
            label="Resizing GIF",
        ):
            return
        self.canvas.fit_to_window()
        self._document_changed()

    def rotate_image(self) -> None:
        doc = self.document
        if not doc:
            return
        degrees, ok = QInputDialog.getDouble(self, "Rotate", "Degrees clockwise", 90, -360, 360, 1)
        if ok:
            if not self._apply_document_transform(
                lambda frame: effects.rotate_keep(frame, degrees),
                clear_masks=True,
                label="Rotating GIF",
            ):
                return
            self.canvas.fit_to_window()
            self._document_changed()

    def cinematic_bars(self) -> None:
        doc = self.document
        if not doc:
            return
        height, ok = QInputDialog.getInt(self, "Cinematic bars", "Top and bottom bar height", max(10, doc.image.height // 12), 1, max(1, doc.image.height // 2))
        if not ok:
            return
        def add_bars(frame: Image.Image) -> Image.Image:
            edited = frame.copy()
            draw = ImageDraw.Draw(edited)
            draw.rectangle((0, 0, frame.width, height), fill=(0, 0, 0, 255))
            draw.rectangle((0, frame.height - height, frame.width, frame.height), fill=(0, 0, 0, 255))
            return edited
        if self._apply_document_transform(add_bars, label="Adding cinematic bars"):
            self._document_changed()

    def extend_animation_duration(self) -> None:
        doc = self.document
        if not doc or not doc.is_animated:
            QMessageBox.information(
                self,
                "Extend animation",
                "Open an animated GIF, MP4, or WebM file first.",
            )
            return

        current_seconds = doc.animation_duration_ms / 1000
        maximum_seconds = 1_000_000_000.0

        target_seconds, accepted = QInputDialog.getDouble(
            self,
            "Extend animation duration",
            f"New total duration in seconds\nCurrent duration: {current_seconds:.2f}s",
            current_seconds + max(1.0, current_seconds),
            current_seconds + 0.01,
            maximum_seconds,
            2,
        )
        if not accepted:
            return

        start, end = self._current_animation_range(doc)
        custom_range = start != 0 or end != doc.frame_count - 1
        repeat_label = "Repeat current preview loop" if custom_range else "Repeat full animation"
        mode_label, accepted = QInputDialog.getItem(
            self,
            "Extend animation duration",
            "Fill the added time with:",
            [repeat_label, "Hold final frame"],
            0,
            False,
        )
        if not accepted:
            return

        self._stop_gif_preview(clear_canvas=False)
        try:
            added_frames = doc.extend_animation(
                round(target_seconds * 1000),
                mode="hold" if mode_label == "Hold final frame" else "repeat",
                range_start=start,
                range_end=end,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Extend animation", str(exc))
            return

        self.cancel_preview(False)
        self._gif_preview_index = min(self._gif_preview_index, doc.frame_count - 1)
        self._document_changed()
        method = "held the final frame" if mode_label == "Hold final frame" else (
            f"repeated frames {start + 1}-{end + 1}"
        )
        frame_note = "" if added_frames == 0 else f", added {added_frames} frames"
        self._status(
            f"Extended animation to {doc.animation_duration_ms / 1000:.2f}s — {method}{frame_note}"
        )

    def reset_original(self) -> None:
        if self.document:
            self.document.reset()
            self._document_changed()

    @staticmethod
    def _write_recovery(
        image: Image.Image | None,
        image_path: Path,
        meta_path: Path,
        payload: dict[str, object],
        animation_frames: Optional[list[Image.Image]] = None,
        frame_durations: Optional[list[int]] = None,
        animation_loop: int = 0,
    ) -> None:
        try:
            # Pillow's native encoders are not consistently safe when several
            # editor instances finish recovery files at the same time. Keep
            # recovery encoding serialized while snapshots remain independent.
            with _RECOVERY_SAVE_LOCK:
                if animation_frames:
                    save_animation(animation_frames, frame_durations or [100] * len(animation_frames), image_path, loop=animation_loop)
                elif image is not None:
                    save_image(image, image_path, optimize=False)
                else:
                    raise ValueError("Recovery snapshot contains no image data.")
                atomic_write_text(meta_path, json.dumps(payload))
        finally:
            owned = ([image] if image is not None else []) + list(animation_frames or [])
            seen: set[int] = set()
            for frame in owned:
                if id(frame) in seen:
                    continue
                seen.add(id(frame))
                try:
                    frame.close()
                except Exception:
                    pass

    def _autosave(self) -> None:
        if self._finalized:
            return
        for doc in self.documents:
            if not doc.dirty:
                continue
            if self._recovery_revision.get(doc.id) == doc.image_revision:
                continue
            pending = self._recovery_futures.get(doc.id)
            if pending and not pending.done():
                continue
            image_path = self.recovery_dir / f"{doc.id}{'.gif' if doc.is_animated else '.png'}"
            meta_path = self.recovery_dir / f"{doc.id}.json"
            revision = doc.image_revision
            payload: dict[str, object] = {
                "path": str(doc.path) if doc.path else None,
                "name": doc.display_name,
                "revision": revision,
                "width": doc.image.width,
                "height": doc.image.height,
                "animated": doc.is_animated,
                "frames": doc.frame_count,
                "duration_ms": doc.animation_duration_ms,
            }
            animation_snapshot = [frame.copy() for frame in doc.animation_frames] if doc.is_animated else None
            still_snapshot = None if animation_snapshot else doc.image.copy()
            future = self._recovery_executor.submit(
                self._write_recovery,
                still_snapshot,
                image_path,
                meta_path,
                payload,
                animation_snapshot,
                list(doc.frame_durations) if doc.is_animated else None,
                doc.animation_loop,
            )
            self._recovery_futures[doc.id] = future

            def finished(done: Future, document_id: str = doc.id, saved_revision: int = revision) -> None:
                if self._finalized:
                    return
                try:
                    done.result()
                    self.recoveryFinished.emit(document_id, saved_revision, True, "")
                except Exception as exc:
                    if not self._finalized:
                        self.recoveryFinished.emit(document_id, saved_revision, False, str(exc))

            future.add_done_callback(finished)

    def _recovery_finished(self, document_id: str, revision: int, success: bool, error: str) -> None:
        self._recovery_futures.pop(document_id, None)
        doc = next((item for item in self.documents if item.id == document_id), None)
        if success and doc and doc.dirty:
            self._recovery_revision[document_id] = revision
            return
        if not success:
            if doc is None or not doc.dirty:
                return
            log_warning("Could not autosave document %s: %s", document_id, error)
            self._status("Recovery save failed — your document remains open")
            return
        for suffix in (".png", ".gif", ".json"):
            (self.recovery_dir / f"{document_id}{suffix}").unlink(missing_ok=True)

    def wait_for_recovery(self, timeout_ms: int = 7000) -> bool:
        self._autosave()
        deadline = time.monotonic() + timeout_ms / 1000
        while any(not future.done() for future in self._recovery_futures.values()) and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        QApplication.processEvents()
        return not any(not future.done() for future in self._recovery_futures.values())

    def _delete_recovery(self, doc: ImageDocument) -> None:
        self._recovery_revision.pop(doc.id, None)
        for suffix in (".png", ".gif", ".json"):
            try:
                (self.recovery_dir / f"{doc.id}{suffix}").unlink(missing_ok=True)
            except Exception as exc:
                log_warning("Could not remove recovery file for %s: %s", doc.display_name, exc)

    def _cleanup_transfers(self) -> None:
        cutoff = time.time() - 7 * 24 * 60 * 60
        paths: list[Path] = []
        for suffix in ("png", "gif", "mp4", "webm"):
            paths.extend(self.transfer_dir.glob(f"send_*.{suffix}"))
        for path in paths:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError as exc:
                log_warning("Could not clean transfer file %s: %s", path, exc)

    def _quarantine_recovery(self, image_path: Path, reason: Exception) -> None:
        self.failed_recovery_dir.mkdir(parents=True, exist_ok=True)
        log_warning("Could not restore recovery file %s: %s", image_path, reason)
        for path in (image_path, image_path.with_suffix(".json")):
            if not path.exists():
                continue
            try:
                target = unique_destination(self.failed_recovery_dir, path.name)
                path.replace(target)
            except OSError as exc:
                log_warning("Could not quarantine recovery file %s: %s", path, exc)

    def _offer_recovery(self) -> None:
        for meta_path in self.recovery_dir.glob("*.json"):
            if not any((self.recovery_dir / f"{meta_path.stem}{suffix}").exists() for suffix in (".png", ".gif")):
                try:
                    meta_path.unlink()
                except OSError as exc:
                    log_warning("Could not remove orphan recovery metadata %s: %s", meta_path, exc)
        # The image is written before its metadata. A crash between those two atomic
        # writes must still leave a recoverable untitled document rather than a ghost file.
        images = list(self.recovery_dir.glob("*.png")) + list(self.recovery_dir.glob("*.gif"))
        if not images:
            return
        result = QMessageBox.question(self, "Restore recovered images", f"ImageSuite found {len(images)} unsaved recovered image(s). Restore them?", QMessageBox.Yes | QMessageBox.No)
        if result == QMessageBox.Yes:
            failures: list[str] = []
            for image_path in images:
                try:
                    meta_path = image_path.with_suffix(".json")
                    meta: dict[str, object] = {}
                    if meta_path.exists():
                        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                        if not isinstance(loaded, dict):
                            raise ValueError("Recovery metadata is not an object")
                        meta = loaded
                    source = Path(str(meta["path"])) if meta.get("path") else None
                    self.add_document(read_image(image_path), source, dirty=True, consume=True)
                    # Keep the recovered identity until the user saves or discards it.
                    # An immediate second crash must not lose the only recovery copy.
                    doc = self.documents[-1]
                    doc.id = image_path.stem
                    doc.image_revision = max(1, int(meta.get("revision", 1)))
                    doc.next_revision = doc.image_revision
                    doc.saved_revision = 0
                    doc.dirty = True
                    self._recovery_revision[doc.id] = doc.image_revision
                except Exception as exc:
                    failures.append(f"{image_path.name}: {exc}")
                    self._quarantine_recovery(image_path, exc)
            if failures:
                QMessageBox.warning(
                    self,
                    "Some recovery files could not be restored",
                    "The damaged recovery files were moved to the recovery_failed folder.\n\n" + "\n".join(failures[:10]),
                )
        else:
            for path in self.recovery_dir.iterdir():
                try:
                    path.unlink()
                except Exception as exc:
                    log_warning("Could not remove recovery file %s: %s", path, exc)

    def set_history_limits(self, depth: int, memory_mb: int) -> None:
        self.history_depth = max(1, depth)
        self.history_memory_mb = max(64, memory_mb)
        for doc in self.documents:
            doc.max_history = self.history_depth
            doc.max_history_bytes = self.history_memory_mb * 1024 * 1024

    def apply_preferences(self, depth: int, memory_mb: int, autosave_seconds: int, preserve_metadata: bool) -> None:
        self.set_history_limits(depth, memory_mb)
        self.preserve_metadata = preserve_metadata
        self.autosave_timer.setInterval(max(10, autosave_seconds) * 1000)

    def prepare_close(self) -> bool:
        dirty_indexes = [index for index, doc in enumerate(self.documents) if doc.dirty]
        if not dirty_indexes:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Exit ImageSuite")
        box.setText(f"There are {len(dirty_indexes)} unsaved image(s).")
        box.setInformativeText("Save them now, or keep crash-recovery copies for the next launch.")
        save_all = box.addButton("Save all", QMessageBox.AcceptRole)
        recover = box.addButton("Exit and recover next launch", QMessageBox.DestructiveRole)
        cancel = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(save_all)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel:
            return False
        if clicked is recover:
            if self.wait_for_recovery():
                return True
            QMessageBox.critical(
                self,
                "Recovery is still being written",
                "ImageSuite could not finish creating the recovery copy yet. Keep the app open and try again.",
            )
            return False
        if clicked is save_all:
            original_index = self.active_index
            for index in dirty_indexes:
                self.document_tabs.setCurrentIndex(index)
                self._activate(index)
                if not self.save():
                    if 0 <= original_index < len(self.documents):
                        self.document_tabs.setCurrentIndex(original_index)
                        self._activate(original_index)
                    return False
            if 0 <= original_index < len(self.documents):
                self.document_tabs.setCurrentIndex(original_index)
                self._activate(original_index)
            return True
        return False

    def finalize_close(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        self.autosave_timer.stop()
        self._stop_gif_preview(clear_canvas=False)
        self._stop_preview_timers()
        self._clear_preview_source_cache()
        self._clear_gif_source_cache()
        self._recovery_executor.shutdown(wait=True, cancel_futures=True)
        self.canvas.set_document(None)
        for doc in self.documents:
            doc.close()
        self.documents.clear()

    def close(self) -> bool:  # type: ignore[override]
        # Qt does not always deliver closeEvent() for a widget that has never
        # been shown (common in embedding, tests, and cancelled startup). Make
        # cleanup deterministic so timers and Pillow buffers cannot survive and
        # affect a later editor instance.
        self.finalize_close()
        return super().close()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # EditorWorkspace is also used independently in tests and embedding.
        # Cleanup cannot rely solely on MainWindow.closeEvent.
        self.finalize_close()
        super().closeEvent(event)
