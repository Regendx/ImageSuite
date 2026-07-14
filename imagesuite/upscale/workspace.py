from __future__ import annotations

from pathlib import Path
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
import subprocess
import time
import traceback
import zipfile
from threading import Lock

from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMenu, QMessageBox, QInputDialog,
    QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from imagesuite.jobs import JobManager, JobRecord
from imagesuite.diagnostics import show_operation_error
from imagesuite.upscale.engine import (
    UpscaleSettings,
    WorkerPlan,
    ai_environment_summary,
    available_models,
    plan_worker_count,
    prepare_ai_model,
    release_ai_model,
    process_file,
    process_image,
    validate_settings,
)
from imagesuite.utils import atomic_write_text, app_data_dir, expand_image_paths, open_folder, read_thumbnail, release_unused_memory, unique_destination


class QueueListWidget(QListWidget):
    pathsDropped = Signal(list)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self.pathsDropped.emit(paths)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class UpscaleWorker(QObject):
    progress = Signal(int, int, str)
    previewReady = Signal(object)
    finished = Signal(list, list)
    failed = Signal(str)

    def __init__(
        self,
        files: list[Path],
        output: Path,
        settings: UpscaleSettings,
        preview_only: bool = False,
        plan: WorkerPlan | None = None,
    ) -> None:
        super().__init__()
        self.files = files
        self.output = output
        self.settings = settings
        self.preview_only = preview_only
        self.cancelled = False
        self.failed_paths: list[Path] = []
        self.plan = plan
        self._progress_lock = Lock()
        self._last_detail_emit = 0.0

    def cancel(self) -> None:
        self.cancelled = True

    def _emit_detail(self, done: int, total: int, detail: str, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._progress_lock:
            if not force and now - self._last_detail_emit < 0.10:
                return
            self._last_detail_emit = now
        self.progress.emit(done, total, detail)

    def run(self) -> None:
        model_prepared = False
        try:
            validate_settings(self.settings)
            if self.settings.method == "AI model":
                prepare_ai_model(self.settings, lambda detail: self._emit_detail(0, max(1, len(self.files)), detail, force=True))
                model_prepared = True
                if self.cancelled:
                    self.finished.emit([], [])
                    return
            if self.preview_only:
                preview_side = self.settings.ai_preview_max_side if self.settings.method == "AI model" else 1200
                image = read_thumbnail(self.files[0], preview_side)
                try:
                    result = process_image(image, self.settings, lambda detail: self._emit_detail(0, 1, detail))
                    self.previewReady.emit(result.copy())
                    if result is not image:
                        result.close()
                finally:
                    image.close()
                self.finished.emit([], [])
                return

            outputs: list[Path] = []
            errors: list[str] = []
            total = len(self.files)
            plan = self.plan or plan_worker_count(self.files, self.settings)
            workers = min(total, plan.effective)

            def run_one(item: tuple[int, Path]) -> tuple[Path, Path | None]:
                index, path = item
                callback = None if workers > 1 else lambda detail: self._emit_detail(index - 1, total, f"{path.name}: {detail}")
                target = process_file(path, self.output, self.settings, callback)
                return path, target

            if workers == 1:
                for index, path in enumerate(self.files, 1):
                    if self.cancelled:
                        break
                    self._emit_detail(index - 1, total, path.name)
                    try:
                        _, target = run_one((index, path))
                        if target is not None:
                            outputs.append(target)
                    except Exception as exc:
                        self.failed_paths.append(path)
                        errors.append(f"{path}: {exc}")
                    self._emit_detail(index, total, path.name, force=index == total)
            else:
                iterator = iter(enumerate(self.files, 1))
                pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="imagesuite-enhance")
                pending: dict[object, tuple[int, Path]] = {}

                def submit_next() -> bool:
                    if self.cancelled:
                        return False
                    try:
                        item = next(iterator)
                    except StopIteration:
                        return False
                    pending[pool.submit(run_one, item)] = item
                    return True

                for _ in range(min(total, workers * 2)):
                    if not submit_next():
                        break
                completed = 0
                try:
                    while pending and not self.cancelled:
                        finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                        for future in finished:
                            _index, path = pending.pop(future)
                            try:
                                _, target = future.result()
                                if target is not None:
                                    outputs.append(target)
                            except Exception as exc:
                                self.failed_paths.append(path)
                                errors.append(f"{path}: {exc}")
                            completed += 1
                            self._emit_detail(completed, total, path.name, force=completed == total)
                            submit_next()
                finally:
                    for future in pending:
                        future.cancel()
                    # Running Pillow calls cannot be interrupted safely. Only the
                    # bounded active set must finish; the rest were never submitted.
                    pool.shutdown(wait=True, cancel_futures=True)
                    pending.clear()

            if not self.cancelled and self.settings.export_zip and outputs:
                zip_path = unique_destination(self.output.parent, f"{self.output.name}.zip")
                # Images are already compressed; deflating them again wastes CPU.
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
                    for output in outputs:
                        archive.write(output, arcname=Path(output).name)
                outputs.append(zip_path)
            self.finished.emit(outputs, errors)
        except Exception:
            self.failed.emit(traceback.format_exc())
        finally:
            if model_prepared:
                release_ai_model()
            release_unused_memory()


class UpscaleWorkspace(QWidget):
    TEXT_WATERMARK_PRESETS = {
        "Subtle Corner": {
            "font_size": 30, "rotation": 0.0, "font_color": "#FFFFFF", "opacity": 0.48,
            "anchor": "Bottom-Right", "x": 100.0, "y": 100.0, "margin_x": 18, "margin_y": 18,
            "outline": True, "outline_color": "#000000", "outline_width": 1,
            "shadow": True, "shadow_offset": 2, "shadow_opacity": 0.45,
            "background": False, "background_color": "#000000", "background_opacity": 0.35,
        },
        "Bold Copyright": {
            "font_size": 54, "rotation": 0.0, "font_color": "#FFFFFF", "opacity": 0.82,
            "anchor": "Bottom-Center", "x": 50.0, "y": 100.0, "margin_x": 12, "margin_y": 22,
            "outline": True, "outline_color": "#000000", "outline_width": 3,
            "shadow": True, "shadow_offset": 3, "shadow_opacity": 0.65,
            "background": False, "background_color": "#000000", "background_opacity": 0.45,
        },
        "Diagonal Proof": {
            "text": "PROOF", "font_size": 92, "rotation": -28.0, "font_color": "#FFFFFF", "opacity": 0.24,
            "anchor": "Center", "x": 50.0, "y": 50.0, "margin_x": 0, "margin_y": 0,
            "outline": True, "outline_color": "#000000", "outline_width": 2,
            "shadow": False, "shadow_offset": 0, "shadow_opacity": 0.0,
            "background": False, "background_color": "#000000", "background_opacity": 0.0,
        },
        "Caption Bar": {
            "font_size": 42, "rotation": 0.0, "font_color": "#FFFFFF", "opacity": 0.96,
            "anchor": "Bottom-Center", "x": 50.0, "y": 100.0, "margin_x": 18, "margin_y": 18,
            "outline": False, "outline_color": "#000000", "outline_width": 0,
            "shadow": False, "shadow_offset": 0, "shadow_opacity": 0.0,
            "background": True, "background_color": "#000000", "background_opacity": 0.62,
        },
        "Soft Center Mark": {
            "font_size": 70, "rotation": -18.0, "font_color": "#FFFFFF", "opacity": 0.20,
            "anchor": "Center", "x": 50.0, "y": 50.0, "margin_x": 0, "margin_y": 0,
            "outline": False, "outline_color": "#000000", "outline_width": 0,
            "shadow": True, "shadow_offset": 2, "shadow_opacity": 0.25,
            "background": False, "background_color": "#000000", "background_opacity": 0.0,
        },
    }
    statusChanged = Signal(str)
    pathsAdded = Signal(list)
    openInEditor = Signal(str)

    def __init__(self, jobs: JobManager, models_dir: Path, outputs_dir: Path, base_dir: Path | None = None) -> None:
        super().__init__()
        self.jobs = jobs
        self.models_dir = models_dir
        self.outputs_dir = outputs_dir
        self.base_dir = Path(base_dir) if base_dir is not None else models_dir.parent
        self.thread: QThread | None = None
        self.worker: UpscaleWorker | None = None
        self.job: JobRecord | None = None
        self._preview_pixmap = QPixmap()
        self.last_outputs: list[Path] = []
        self.last_output_count = 0
        self.last_failed_paths: list[Path] = []
        self.preserve_metadata = True
        self._loading_watermark_preset = False
        self.watermark_presets_path = app_data_dir() / "text_watermark_presets.json"
        self.custom_watermark_presets = self._load_custom_watermark_presets()
        self.setAcceptDrops(True)
        self._build_ui()
        self._bind_shortcuts()
        QApplication.instance().focusChanged.connect(self._update_shortcut_states)
        self._update_shortcut_states(None, QApplication.focusWidget())
        self.refresh_models()
        self._update_mode_controls()
        self._update_method_controls()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 8)
        header = QHBoxLayout()
        title = QLabel("UPSCALE & WATERMARK", objectName="Brand")
        header.addWidget(title)
        header.addStretch(1)
        add = QToolButton()
        add.setText("Add images")
        add.setPopupMode(QToolButton.MenuButtonPopup)
        add.clicked.connect(self.add_files)
        add_menu = QMenu(add)
        add_menu.addAction("Add image files…", self.add_files)
        add_menu.addAction("Add a whole folder…", self.add_folder)
        add.setMenu(add_menu)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self.remove_selected_files)
        manage = QToolButton()
        manage.setText("More")
        manage.setPopupMode(QToolButton.InstantPopup)
        manage_menu = QMenu(manage)
        manage_menu.addAction("Clear queue", self.clear_files)
        manage_menu.addAction("Move selected up", lambda: self.move_queue_selection(-1))
        manage_menu.addAction("Move selected down", lambda: self.move_queue_selection(1))
        manage.setMenu(manage_menu)
        header.addWidget(add)
        header.addWidget(remove)
        header.addWidget(manage)
        root.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 6, 0)
        self.queue_label = QLabel("Input queue · 0 images", objectName="SectionTitle")
        self.files = QueueListWidget()
        self.files.setAccessibleName("Enhance input queue")
        self.files.pathsDropped.connect(self._append_dropped_paths)
        self.files.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.files.setDragDropMode(QAbstractItemView.InternalMove)
        self.files.setDefaultDropAction(Qt.MoveAction)
        self.files.setAlternatingRowColors(True)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self._show_queue_menu)
        self.files.currentRowChanged.connect(self.show_source_preview)
        self.files.itemDoubleClicked.connect(lambda _item: self.generate_preview())
        left_layout.addWidget(self.queue_label)
        left_layout.addWidget(self.files, 1)
        queue_hint = QLabel("Drop files/folders here · Double-click to preview · Delete removes selected", objectName="Muted")
        queue_hint.setWordWrap(True)
        left_layout.addWidget(queue_hint)
        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.outputs_dir / "upscaled"))
        browse_out = QPushButton("Output")
        browse_out.clicked.connect(self.choose_output)
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(browse_out)
        left_layout.addLayout(out_row)
        splitter.addWidget(left)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.NoFrame)
        settings_host = QWidget()
        settings_layout = QVBoxLayout(settings_host)
        settings_layout.setContentsMargins(6, 0, 6, 8)
        self.settings_tabs = QTabWidget()
        self.settings_tabs.addTab(self._build_upscale_tab(), "Resize")
        self.settings_tabs.addTab(self._build_finish_tab(), "Quality")
        self.settings_tabs.addTab(self._build_text_watermark_tab(), "Text watermark")
        self.settings_tabs.addTab(self._build_image_watermark_tab(), "Image watermark")
        settings_layout.addWidget(self.settings_tabs)
        settings_scroll.setWidget(settings_host)
        splitter.addWidget(settings_scroll)

        preview_host = QWidget()
        preview_layout = QVBoxLayout(preview_host)
        preview_layout.setContentsMargins(6, 0, 0, 0)
        preview_layout.addWidget(QLabel("Preview", objectName="SectionTitle"))
        self.preview = QLabel("Select an input image")
        self.preview.setAccessibleName("Enhance preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(360, 320)
        self.preview.setStyleSheet("background:#13151a; border:1px solid #303540;")
        self.preview.setScaledContents(False)
        preview_layout.addWidget(self.preview, 1)
        preview_btn = QPushButton("Generate preview")
        preview_btn.setToolTip("Process only the selected image (Ctrl+P)")
        preview_btn.clicked.connect(self.generate_preview)
        preview_layout.addWidget(preview_btn)
        self.result_bar = QFrame(objectName="TopBar")
        result_layout = QVBoxLayout(self.result_bar)
        result_layout.setContentsMargins(8, 6, 8, 6)
        self.result_label = QLabel("", objectName="Muted")
        self.result_label.setWordWrap(True)
        result_layout.addWidget(self.result_label)
        result_actions = QHBoxLayout()
        open_result = QPushButton("Open in editor")
        open_result.clicked.connect(self.open_result_in_editor)
        reveal_result = QPushButton("Reveal")
        reveal_result.clicked.connect(self.reveal_result)
        output_folder = QPushButton("Output folder")
        output_folder.clicked.connect(lambda: open_folder(self.output_edit.text()))
        self.retry_failed_btn = QPushButton("Retry failed")
        self.retry_failed_btn.clicked.connect(self.retry_failed)
        self.retry_failed_btn.setVisible(False)
        result_actions.addWidget(open_result)
        result_actions.addWidget(reveal_result)
        result_actions.addWidget(output_folder)
        result_actions.addWidget(self.retry_failed_btn)
        result_layout.addLayout(result_actions)
        self.result_bar.setVisible(False)
        preview_layout.addWidget(self.result_bar)
        splitter.addWidget(preview_host)
        splitter.setSizes([330, 500, 420])
        root.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.status = QLabel("Ready", objectName="Muted")
        self.start_btn = QPushButton("Start queue", objectName="Accent")
        self.start_btn.setToolTip("Process the complete queue (Ctrl+Enter)")
        self.start_btn.clicked.connect(self.start_queue)
        self.cancel_btn = QPushButton("Cancel", objectName="Danger")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.setEnabled(False)
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.status)
        bottom.addWidget(self.start_btn)
        bottom.addWidget(self.cancel_btn)
        root.addLayout(bottom)

    def _bind_shortcuts(self) -> None:
        bindings = [
            ("Delete", self.remove_selected_files, "queue"),
            ("Ctrl+Return", self.start_queue, "always"),
            ("Ctrl+P", self.generate_preview, "always"),
            ("Escape", self.cancel, "always"),
            ("Ctrl+Shift+O", self.add_folder, "always"),
            ("Alt+Up", lambda: self.move_queue_selection(-1), "queue"),
            ("Alt+Down", lambda: self.move_queue_selection(1), "queue"),
        ]
        self._shortcuts: list[tuple[QShortcut, str]] = []
        for sequence, callback, policy in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append((shortcut, policy))

    def _update_shortcut_states(self, _old, new) -> None:
        queue_focus = new is self.files or (new is not None and self.files.isAncestorOf(new))
        typing = isinstance(new, (QLineEdit, QAbstractSpinBox)) or (isinstance(new, QComboBox) and new.isEditable())
        for shortcut, policy in self._shortcuts:
            if policy == "queue":
                shortcut.setEnabled(queue_focus)
            else:
                shortcut.setEnabled(not typing or shortcut.key().toString() in {"Ctrl+Return", "Escape"})

    def _update_mode_controls(self, *_args) -> None:
        target_mode = hasattr(self, "mode") and self.mode.currentText() == "Target size"
        if hasattr(self, "scale"):
            self.scale.setEnabled(not target_mode)
            self.target_w.setEnabled(target_mode)
            self.target_h.setEnabled(target_mode)

    def _update_method_controls(self, *_args) -> None:
        ai = hasattr(self, "method") and self.method.currentText() == "AI model"
        if hasattr(self, "model"):
            self.max_workers.setEnabled(not ai)
            self.max_workers.setToolTip(
                "AI models remain single-worker to avoid loading or competing for the same model/GPU memory."
                if ai else
                "Parallel non-AI image jobs. Values up to 50 are supported, but measured photo batches usually peak around 4–6 active workers. ImageSuite lowers the count automatically when more workers would be slower."
            )
            if hasattr(self, "resize_form"):
                for field in self.ai_fields:
                    self.resize_form.setRowVisible(field, ai)
                self._set_resize_advanced(self.resize_advanced.isChecked())
            self._update_ai_status_text()

    def _update_format_controls(self, *_args) -> None:
        if not hasattr(self, "format") or not hasattr(self, "resize_form"):
            return
        fmt = self.format.currentText()
        is_gif = fmt == "GIF"
        is_video = fmt in {"MP4", "WebM"}
        self.preserve_alpha.setEnabled(not is_video)
        self.preserve_alpha.setToolTip("Video exports flatten transparency onto black." if is_video else "Preserve transparency when the chosen format supports it.")
        self._set_resize_advanced(self.resize_advanced.isChecked())
        if is_video:
            self.status.setText("MP4/WebM export works for animated sources. Still images should use PNG, JPEG, WebP, TIFF, or GIF.")

    def _update_queue_label(self) -> None:
        count = self.files.count()
        self.queue_label.setText(f"Input queue · {count} image{'s' if count != 1 else ''}")

    def queue_paths(self) -> list[Path]:
        return [Path(self.files.item(i).data(Qt.UserRole)) for i in range(self.files.count())]

    def add_paths(self, paths: list[Path], *, select_first: bool = True) -> None:
        self._append(expand_image_paths(paths), select_first=select_first)

    def _show_queue_menu(self, position) -> None:
        item = self.files.itemAt(position)
        if item is not None and not item.isSelected():
            self.files.clearSelection()
            item.setSelected(True)
            self.files.setCurrentItem(item)
        menu = QMenu(self)
        preview = menu.addAction("Generate preview")
        edit = menu.addAction("Open in editor")
        reveal = menu.addAction("Open containing folder")
        copy_path = menu.addAction("Copy path")
        menu.addSeparator()
        remove = menu.addAction("Remove selected")
        selected = menu.exec(self.files.mapToGlobal(position))
        if selected is preview:
            self.generate_preview()
        elif selected is edit:
            path = self.selected_path()
            if path:
                self.openInEditor.emit(str(path))
        elif selected is reveal:
            path = self.selected_path()
            if path:
                open_folder(path.parent)
        elif selected is copy_path:
            path = self.selected_path()
            if path:
                QApplication.clipboard().setText(str(path))
        elif selected is remove:
            self.remove_selected_files()

    def remove_selected_files(self) -> None:
        rows = sorted({self.files.row(item) for item in self.files.selectedItems()}, reverse=True)
        for row in rows:
            self.files.takeItem(row)
        if rows:
            self.status.setText(f"Removed {len(rows)} queue item(s)")
            self.statusChanged.emit(self.status.text())
        self._update_queue_label()
        if self.files.count() == 0:
            self._preview_pixmap = QPixmap()
            self.preview.clear()
            self.preview.setText("Select an input image")

    def move_queue_selection(self, direction: int) -> None:
        selected = sorted({self.files.row(item) for item in self.files.selectedItems()})
        if not selected:
            return
        order = selected if direction < 0 else list(reversed(selected))
        moved_rows: list[int] = []
        for row in order:
            target = row + direction
            if not (0 <= target < self.files.count()) or target in selected:
                moved_rows.append(row)
                continue
            item = self.files.takeItem(row)
            self.files.insertItem(target, item)
            moved_rows.append(target)
        self.files.clearSelection()
        for row in moved_rows:
            if 0 <= row < self.files.count():
                self.files.item(row).setSelected(True)
        if moved_rows:
            self.files.setCurrentRow(moved_rows[0])

    def _append_dropped_paths(self, raw_paths: list[str]) -> None:
        self.add_paths([Path(path) for path in raw_paths])

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        self._append_dropped_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def _build_upscale_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.workflow_preset = QComboBox()
        self.workflow_preset.addItems(["Fast resize", "Best non-AI quality", "AI upscale", "Custom"])
        form.addRow("Quick setup", self.workflow_preset)

        self.mode = QComboBox(); self.mode.addItems(["Scale factor", "Target size"]); self.mode.currentTextChanged.connect(self._update_mode_controls)
        self.scale = QDoubleSpinBox(); self.scale.setRange(1, 8); self.scale.setValue(4); self.scale.setSingleStep(0.25)
        self.target_w = QSpinBox(); self.target_w.setRange(1, 32768); self.target_w.setValue(1920)
        self.target_h = QSpinBox(); self.target_h.setRange(1, 32768); self.target_h.setValue(1080)
        self.method = QComboBox(); self.method.addItems(["Lanczos", "Bicubic", "Bilinear", "Nearest", "AI model"]); self.method.currentTextChanged.connect(self._update_method_controls)
        self.model = QComboBox()
        refresh = QPushButton("Refresh"); refresh.clicked.connect(self.refresh_models)
        check_ai = QPushButton("Check AI"); check_ai.clicked.connect(self.check_ai_setup)
        self.install_ai_button = QPushButton("Install / Repair AI")
        self.install_ai_button.setToolTip("Open the optional AI installer using the main ImageSuite launcher.")
        self.install_ai_button.clicked.connect(self.install_ai_support)
        model_row = QWidget(); mrow = QHBoxLayout(model_row); mrow.setContentsMargins(0,0,0,0); mrow.addWidget(self.model,1); mrow.addWidget(refresh); mrow.addWidget(check_ai); mrow.addWidget(self.install_ai_button)
        self.model_row = model_row
        self.ai_profile = QComboBox(); self.ai_profile.addItems(["Balanced (recommended)", "Fast", "Low memory", "Maximum quality", "Custom"])
        self.ai_profile.currentTextChanged.connect(self.apply_ai_profile)
        self.device = QComboBox(); self.device.addItems(["Auto", "CUDA", "CPU", "DirectML"])
        self.ai_precision = QComboBox(); self.ai_precision.addItems(["Auto", "FP16", "FP32"])
        self.tile = QSpinBox(); self.tile.setRange(0, 4096); self.tile.setSingleStep(64); self.tile.setValue(0); self.tile.setSpecialValueText("Auto")
        self.tile.setToolTip("Auto chooses a safe tile from available GPU/RAM. Manual values are retried at smaller sizes after OOM when recovery is enabled.")
        self.ai_oom_recovery = QCheckBox("Automatically recover from GPU memory errors")
        self.ai_oom_recovery.setChecked(True)
        self.ai_preview_size = QSpinBox(); self.ai_preview_size.setRange(256, 1600); self.ai_preview_size.setValue(640); self.ai_preview_size.setSingleStep(64); self.ai_preview_size.setSuffix(" px")
        self.ai_preview_size.setToolTip("Maximum input side used by quick AI preview. Final batch output always uses the original source.")
        self.ai_status = QLabel("Choose an AI model, then use Check AI to inspect the backend.", objectName="AIStatus")
        self.ai_status.setWordWrap(True)
        self.ai_status.setAccessibleName("AI backend and model status")
        self.format = QComboBox(); self.format.addItems(["PNG", "JPEG", "WebP", "TIFF", "GIF", "MP4", "WebM"]); self.format.currentTextChanged.connect(self._update_format_controls)
        self.jpeg_quality = QSpinBox(); self.jpeg_quality.setRange(1,100); self.jpeg_quality.setValue(95)
        self.webp_quality = QSpinBox(); self.webp_quality.setRange(1,100); self.webp_quality.setValue(92)
        self.animation_fps = QSpinBox(); self.animation_fps.setRange(0,120); self.animation_fps.setValue(0); self.animation_fps.setSpecialValueText("Auto")
        self.animation_fps.setToolTip("0 keeps automatic timing. Set a value to force video exports to a fixed FPS.")
        self.video_bitrate = QSpinBox(); self.video_bitrate.setRange(0,50000); self.video_bitrate.setValue(0); self.video_bitrate.setSuffix(" kbps"); self.video_bitrate.setSpecialValueText("Auto")
        self.video_bitrate.setToolTip("0 lets ffmpeg choose. Higher values improve quality but increase file size.")
        self.gif_colors = QSpinBox(); self.gif_colors.setRange(2,256); self.gif_colors.setValue(256)
        self.gif_dither = QCheckBox("Dither GIF gradients")
        self.gif_dither.setChecked(True)
        self.gif_optimize = QCheckBox("Optimize GIF size")
        self.preserve_alpha = QCheckBox("Preserve transparency"); self.preserve_alpha.setChecked(True)
        self.timestamp_folder = QCheckBox("Create timestamped output folder"); self.timestamp_folder.setChecked(True)
        self.export_zip = QCheckBox("Export completed queue as ZIP")
        self.skip_larger = QCheckBox("Skip sources already at or above target size")
        self.max_workers = QSpinBox()
        self.max_workers.setRange(1, 50)
        self.max_workers.setValue(min(8, max(2, os.cpu_count() or 4)))
        self.max_workers.setSuffix(" workers")
        self.max_workers.setToolTip("Upper limit only. ImageSuite automatically lowers the active count when image size, RAM, CPU count, or animated GIFs make the requested value unsafe. Maximum: 50.")
        self.resize_advanced = QCheckBox("Show advanced output options")
        self.resize_advanced.toggled.connect(self._set_resize_advanced)

        form.addRow("Sizing", self.mode)
        form.addRow("Scale factor", self.scale)
        form.addRow("Target width", self.target_w)
        form.addRow("Target height", self.target_h)
        form.addRow("Method", self.method)
        form.addRow("Maximum parallel workers (non-AI)", self.max_workers)
        worker_hint = QLabel("Upper limit only — ImageSuite automatically lowers active workers for large images, limited RAM, CPU-heavy formats, and animated GIFs.", objectName="Muted")
        worker_hint.setWordWrap(True)
        form.addRow("", worker_hint)
        form.addRow("AI model", model_row)
        form.addRow("AI profile", self.ai_profile)
        form.addRow("Device", self.device)
        form.addRow("Precision", self.ai_precision)
        form.addRow("Tile size", self.tile)
        form.addRow("AI preview input", self.ai_preview_size)
        form.addRow("", self.ai_oom_recovery)
        form.addRow("AI status", self.ai_status)
        form.addRow("Output format", self.format)
        form.addRow("", self.resize_advanced)
        form.addRow("JPEG quality", self.jpeg_quality)
        form.addRow("WebP quality", self.webp_quality)
        form.addRow("Animation FPS", self.animation_fps)
        form.addRow("Video bitrate", self.video_bitrate)
        form.addRow("GIF colors", self.gif_colors)
        form.addRow("", self.gif_dither)
        form.addRow("", self.gif_optimize)
        form.addRow("", self.preserve_alpha)
        form.addRow("", self.timestamp_folder)
        form.addRow("", self.export_zip)
        form.addRow("", self.skip_larger)
        self.resize_form = form
        self.ai_fields = [self.model_row, self.ai_profile, self.device, self.ai_precision, self.tile, self.ai_preview_size, self.ai_oom_recovery, self.ai_status]
        self.resize_advanced_fields = [self.jpeg_quality, self.webp_quality, self.animation_fps, self.video_bitrate, self.gif_colors, self.gif_dither, self.gif_optimize, self.preserve_alpha, self.timestamp_folder, self.export_zip, self.skip_larger]
        for widget in (self.model, self.device, self.ai_precision, self.tile, self.ai_preview_size):
            if isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self._ai_control_changed)
            else:
                widget.valueChanged.connect(self._ai_control_changed)
        self.ai_oom_recovery.toggled.connect(self._ai_control_changed)
        self._set_resize_advanced(False)
        self._update_format_controls()
        self.workflow_preset.setCurrentText("Custom")
        self.workflow_preset.currentTextChanged.connect(self.apply_workflow_preset)
        return page

    def apply_workflow_preset(self, name: str) -> None:
        if name == "Custom":
            return
        if name == "Fast resize":
            self.mode.setCurrentText("Scale factor")
            self.scale.setValue(2.0)
            self.method.setCurrentText("Bicubic")
            self.format.setCurrentText("JPEG")
            self.max_workers.setValue(min(8, max(4, os.cpu_count() or 4)))
            if hasattr(self, "quality_preset"):
                self.quality_preset.setCurrentText("Custom")
                self.sharpen.setValue(0.0)
                self.denoise.setValue(0.0)
                self.contrast.setValue(1.0)
                self.brightness.setValue(1.0)
                self.saturation.setValue(1.0)
        elif name == "Best non-AI quality":
            self.mode.setCurrentText("Scale factor")
            self.scale.setValue(2.0)
            self.method.setCurrentText("Lanczos")
            self.max_workers.setValue(min(8, max(2, os.cpu_count() or 4)))
            self.quality_preset.setCurrentText("Maximum Detail") if hasattr(self, "quality_preset") else None
        elif name == "AI upscale":
            self.mode.setCurrentText("Scale factor")
            self.scale.setValue(4.0)
            self.method.setCurrentText("AI model")
            self.ai_profile.setCurrentText("Balanced (recommended)")
            self.max_workers.setValue(1)
        if hasattr(self, "status"):
            self.status.setText(f"Quick setup: {name}")

    def _set_resize_advanced(self, visible: bool) -> None:
        if not hasattr(self, "resize_form"):
            return
        ai = hasattr(self, "method") and self.method.currentText() == "AI model"
        fmt = self.format.currentText() if hasattr(self, "format") else "PNG"
        is_gif = fmt == "GIF"
        is_video = fmt in {"MP4", "WebM"}
        for field in self.resize_advanced_fields:
            show = visible
            if field in {self.animation_fps, self.video_bitrate}:
                show = visible and is_video
            elif field in {self.gif_colors, self.gif_dither, self.gif_optimize}:
                show = visible and is_gif
            self.resize_form.setRowVisible(field, show)

    def _build_finish_tab(self) -> QWidget:
        page = QWidget(); form = QFormLayout(page)
        self.quality_preset = QComboBox(); self.quality_preset.addItems(["Custom","Clean Photo","Crisp Illustration","Soft Portrait","Maximum Detail"]); self.quality_preset.currentTextChanged.connect(self.apply_quality_preset)
        self.sharpen = QDoubleSpinBox(); self.sharpen.setRange(0,3); self.sharpen.setValue(0.15); self.sharpen.setSingleStep(0.05)
        self.denoise = QDoubleSpinBox(); self.denoise.setRange(0,1); self.denoise.setSingleStep(0.1)
        self.contrast = QDoubleSpinBox(); self.contrast.setRange(0,3); self.contrast.setValue(1); self.contrast.setSingleStep(0.05)
        self.brightness = QDoubleSpinBox(); self.brightness.setRange(0,3); self.brightness.setValue(1); self.brightness.setSingleStep(0.05)
        self.saturation = QDoubleSpinBox(); self.saturation.setRange(0,3); self.saturation.setValue(1); self.saturation.setSingleStep(0.05)
        form.addRow("Preset", self.quality_preset); form.addRow("Sharpen", self.sharpen); form.addRow("Denoise", self.denoise); form.addRow("Contrast", self.contrast); form.addRow("Brightness", self.brightness); form.addRow("Saturation", self.saturation)
        return page

    def _build_text_watermark_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.watermark_preset = QComboBox()
        self.watermark_preset.setToolTip("Load a built-in style or one of your saved text watermark presets.")
        preset_save = QPushButton("Save current")
        preset_save.clicked.connect(self.save_text_watermark_preset)
        preset_delete = QPushButton("Delete")
        preset_delete.clicked.connect(self.delete_text_watermark_preset)
        preset_holder = QWidget()
        preset_row = QHBoxLayout(preset_holder)
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.addWidget(self.watermark_preset, 1)
        preset_row.addWidget(preset_save)
        preset_row.addWidget(preset_delete)
        form.addRow("Preset", preset_holder)

        self.text_enable = QCheckBox("Enable text watermark")
        self.text_value = QLineEdit("© YourWatermark")
        self.font_path = QLineEdit()
        font_browse = QPushButton("Browse")
        font_browse.clicked.connect(self.choose_font)
        font_holder = QWidget()
        font_row = QHBoxLayout(font_holder)
        font_row.setContentsMargins(0, 0, 0, 0)
        font_row.addWidget(self.font_path, 1)
        font_row.addWidget(font_browse)
        self.font_size = QSpinBox(); self.font_size.setRange(6, 500); self.font_size.setValue(48)
        self.text_rotation = QDoubleSpinBox(); self.text_rotation.setRange(-360, 360); self.text_rotation.setValue(0)
        self.font_color = QLineEdit("#FFFFFF")
        self.text_opacity = QDoubleSpinBox(); self.text_opacity.setRange(0, 1); self.text_opacity.setValue(0.65); self.text_opacity.setSingleStep(0.05)
        self.anchor = QComboBox(); self.anchor.addItems(["Top-Left", "Top-Center", "Top-Right", "Center-Left", "Center", "Center-Right", "Bottom-Left", "Bottom-Center", "Bottom-Right", "Custom"]); self.anchor.setCurrentText("Bottom-Right")
        self.text_x = QDoubleSpinBox(); self.text_x.setRange(0, 100); self.text_x.setValue(100)
        self.text_y = QDoubleSpinBox(); self.text_y.setRange(0, 100); self.text_y.setValue(100)
        self.margin_x = QSpinBox(); self.margin_x.setRange(0, 500); self.margin_x.setValue(12)
        self.margin_y = QSpinBox(); self.margin_y.setRange(0, 500); self.margin_y.setValue(12)
        self.outline = QCheckBox("Outline"); self.outline.setChecked(True)
        self.outline_color = QLineEdit("#000000")
        self.outline_width = QSpinBox(); self.outline_width.setRange(0, 20); self.outline_width.setValue(2)
        self.shadow = QCheckBox("Shadow"); self.shadow.setChecked(True)
        self.shadow_offset = QSpinBox(); self.shadow_offset.setRange(0, 30); self.shadow_offset.setValue(2)
        self.shadow_opacity = QDoubleSpinBox(); self.shadow_opacity.setRange(0, 1); self.shadow_opacity.setValue(0.6); self.shadow_opacity.setSingleStep(0.05)
        self.background = QCheckBox("Background panel")
        self.background_color = QLineEdit("#000000")
        self.background_opacity = QDoubleSpinBox(); self.background_opacity.setRange(0, 1); self.background_opacity.setValue(0.45); self.background_opacity.setSingleStep(0.05)

        form.addRow("", self.text_enable)
        form.addRow("Text", self.text_value)
        form.addRow("Font", font_holder)
        form.addRow("Font size", self.font_size)
        form.addRow("Rotation", self.text_rotation)
        form.addRow("Color", self.font_color)
        form.addRow("Opacity", self.text_opacity)
        form.addRow("Anchor", self.anchor)
        form.addRow("Custom X %", self.text_x)
        form.addRow("Custom Y %", self.text_y)
        form.addRow("Margin X", self.margin_x)
        form.addRow("Margin Y", self.margin_y)
        form.addRow("", self.outline)
        form.addRow("Outline color", self.outline_color)
        form.addRow("Outline width", self.outline_width)
        form.addRow("", self.shadow)
        form.addRow("Shadow offset", self.shadow_offset)
        form.addRow("Shadow opacity", self.shadow_opacity)
        form.addRow("", self.background)
        form.addRow("Background color", self.background_color)
        form.addRow("Background opacity", self.background_opacity)

        self._refresh_text_watermark_presets()
        self.watermark_preset.currentTextChanged.connect(self.apply_text_watermark_preset)
        for widget in self._text_watermark_widgets():
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._mark_text_watermark_custom)
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self._mark_text_watermark_custom)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._mark_text_watermark_custom)
            else:
                widget.valueChanged.connect(self._mark_text_watermark_custom)
        return page

    def _text_watermark_widgets(self) -> list[QWidget]:
        return [
            self.text_enable, self.text_value, self.font_path, self.font_size, self.text_rotation,
            self.font_color, self.text_opacity, self.anchor, self.text_x, self.text_y,
            self.margin_x, self.margin_y, self.outline, self.outline_color, self.outline_width,
            self.shadow, self.shadow_offset, self.shadow_opacity, self.background,
            self.background_color, self.background_opacity,
        ]

    def _load_custom_watermark_presets(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.watermark_presets_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(name): values for name, values in raw.items() if isinstance(name, str) and isinstance(values, dict)}

    def _save_custom_watermark_presets(self) -> None:
        atomic_write_text(
            self.watermark_presets_path,
            json.dumps(self.custom_watermark_presets, indent=2, ensure_ascii=False),
        )

    def _refresh_text_watermark_presets(self, selected: str = "Custom") -> None:
        self.watermark_preset.blockSignals(True)
        self.watermark_preset.clear()
        self.watermark_preset.addItem("Custom", "custom-state")
        for name in self.TEXT_WATERMARK_PRESETS:
            self.watermark_preset.addItem(name, "built-in")
        for name in sorted(self.custom_watermark_presets, key=str.casefold):
            self.watermark_preset.addItem(name, "user")
        index = self.watermark_preset.findText(selected)
        self.watermark_preset.setCurrentIndex(index if index >= 0 else 0)
        self.watermark_preset.blockSignals(False)

    def _current_text_watermark_preset(self) -> dict:
        return {
            "enabled": self.text_enable.isChecked(),
            "text": self.text_value.text(),
            "font_path": self.font_path.text(),
            "font_size": self.font_size.value(),
            "rotation": self.text_rotation.value(),
            "font_color": self.font_color.text(),
            "opacity": self.text_opacity.value(),
            "anchor": self.anchor.currentText(),
            "x": self.text_x.value(),
            "y": self.text_y.value(),
            "margin_x": self.margin_x.value(),
            "margin_y": self.margin_y.value(),
            "outline": self.outline.isChecked(),
            "outline_color": self.outline_color.text(),
            "outline_width": self.outline_width.value(),
            "shadow": self.shadow.isChecked(),
            "shadow_offset": self.shadow_offset.value(),
            "shadow_opacity": self.shadow_opacity.value(),
            "background": self.background.isChecked(),
            "background_color": self.background_color.text(),
            "background_opacity": self.background_opacity.value(),
        }

    def apply_text_watermark_preset(self, name: str) -> None:
        if not name or name == "Custom":
            return
        values = self.TEXT_WATERMARK_PRESETS.get(name) or self.custom_watermark_presets.get(name)
        if not values:
            return
        self._loading_watermark_preset = True
        try:
            setters = {
                "enabled": (self.text_enable.setChecked, bool),
                "text": (self.text_value.setText, str),
                "font_path": (self.font_path.setText, str),
                "font_size": (self.font_size.setValue, int),
                "rotation": (self.text_rotation.setValue, float),
                "font_color": (self.font_color.setText, str),
                "opacity": (self.text_opacity.setValue, float),
                "anchor": (self.anchor.setCurrentText, str),
                "x": (self.text_x.setValue, float),
                "y": (self.text_y.setValue, float),
                "margin_x": (self.margin_x.setValue, int),
                "margin_y": (self.margin_y.setValue, int),
                "outline": (self.outline.setChecked, bool),
                "outline_color": (self.outline_color.setText, str),
                "outline_width": (self.outline_width.setValue, int),
                "shadow": (self.shadow.setChecked, bool),
                "shadow_offset": (self.shadow_offset.setValue, int),
                "shadow_opacity": (self.shadow_opacity.setValue, float),
                "background": (self.background.setChecked, bool),
                "background_color": (self.background_color.setText, str),
                "background_opacity": (self.background_opacity.setValue, float),
            }
            for key, value in values.items():
                entry = setters.get(key)
                if entry is None:
                    continue
                setter, converter = entry
                try:
                    setter(converter(value))
                except (TypeError, ValueError, OverflowError):
                    continue
            if "enabled" not in values:
                self.text_enable.setChecked(True)
        finally:
            self._loading_watermark_preset = False
        self.status.setText(f"Text watermark preset: {name}")

    def _mark_text_watermark_custom(self, *_args) -> None:
        if self._loading_watermark_preset or not hasattr(self, "watermark_preset"):
            return
        if self.watermark_preset.currentText() != "Custom":
            self.watermark_preset.blockSignals(True)
            self.watermark_preset.setCurrentIndex(0)
            self.watermark_preset.blockSignals(False)

    def save_text_watermark_preset(self) -> None:
        name, accepted = QInputDialog.getText(self, "Save text watermark preset", "Preset name:")
        name = name.strip()
        if not accepted or not name:
            return
        reserved = {"custom", *(item.casefold() for item in self.TEXT_WATERMARK_PRESETS)}
        if name.casefold() in reserved:
            QMessageBox.information(self, "Text watermark presets", "Choose a name that is not used by a built-in preset.")
            return
        existing_name = next((item for item in self.custom_watermark_presets if item.casefold() == name.casefold()), None)
        if existing_name is not None:
            answer = QMessageBox.question(self, "Replace preset?", f'Replace the saved preset "{existing_name}"?')
            if answer != QMessageBox.Yes:
                return
            name = existing_name
        previous = self.custom_watermark_presets.get(name)
        self.custom_watermark_presets[name] = self._current_text_watermark_preset()
        try:
            self._save_custom_watermark_presets()
        except OSError as exc:
            if previous is None:
                self.custom_watermark_presets.pop(name, None)
            else:
                self.custom_watermark_presets[name] = previous
            show_operation_error(self, "Preset could not be saved", "The text watermark preset was not written.", str(exc))
            return
        self._refresh_text_watermark_presets(name)
        self.status.setText(f"Saved text watermark preset: {name}")

    def delete_text_watermark_preset(self) -> None:
        name = self.watermark_preset.currentText()
        if name not in self.custom_watermark_presets:
            QMessageBox.information(self, "Text watermark presets", "Only presets you saved can be deleted.")
            return
        if QMessageBox.question(self, "Delete preset?", f'Delete the saved preset "{name}"?') != QMessageBox.Yes:
            return
        deleted = self.custom_watermark_presets.pop(name)
        try:
            self._save_custom_watermark_presets()
        except OSError as exc:
            self.custom_watermark_presets[name] = deleted
            show_operation_error(self, "Preset could not be deleted", "The preset file could not be updated.", str(exc))
            return
        self._refresh_text_watermark_presets()
        self.status.setText(f"Deleted text watermark preset: {name}")

    def _build_image_watermark_tab(self) -> QWidget:
        page = QWidget(); form = QFormLayout(page)
        self.image_enable = QCheckBox("Enable image watermark")
        self.logo_path = QLineEdit()
        browse = QPushButton("Browse"); browse.clicked.connect(self.choose_logo)
        holder = QWidget(); row = QHBoxLayout(holder); row.setContentsMargins(0,0,0,0); row.addWidget(self.logo_path,1); row.addWidget(browse)
        self.logo_scale = QDoubleSpinBox(); self.logo_scale.setRange(0.01,1); self.logo_scale.setValue(0.12); self.logo_scale.setSingleStep(0.01)
        self.logo_opacity = QDoubleSpinBox(); self.logo_opacity.setRange(0,1); self.logo_opacity.setValue(0.5); self.logo_opacity.setSingleStep(0.05)
        self.logo_x = QDoubleSpinBox(); self.logo_x.setRange(0,100); self.logo_x.setValue(100)
        self.logo_y = QDoubleSpinBox(); self.logo_y.setRange(0,100); self.logo_y.setValue(100)
        form.addRow("", self.image_enable); form.addRow("Image", holder); form.addRow("Scale", self.logo_scale); form.addRow("Opacity", self.logo_opacity); form.addRow("X %", self.logo_x); form.addRow("Y %", self.logo_y)
        return page

    def settings(self) -> UpscaleSettings:
        model_path = self.model.currentData() or ""
        return UpscaleSettings(
            mode=self.mode.currentText(), scale_factor=self.scale.value(), target_width=self.target_w.value(), target_height=self.target_h.value(), method=self.method.currentText(), model_path=str(model_path), device=self.device.currentText(), tile_size=self.tile.value(), ai_precision=self.ai_precision.currentText(), ai_oom_recovery=self.ai_oom_recovery.isChecked(), ai_preview_max_side=self.ai_preview_size.value(), output_format=self.format.currentText(), jpeg_quality=self.jpeg_quality.value(), webp_quality=self.webp_quality.value(), animation_fps=self.animation_fps.value(), video_bitrate_kbps=self.video_bitrate.value(), gif_colors=self.gif_colors.value(), gif_dither=self.gif_dither.isChecked(), gif_optimize=self.gif_optimize.isChecked(), preserve_transparency=self.preserve_alpha.isChecked(), preserve_metadata=self.preserve_metadata, create_timestamped_folder=self.timestamp_folder.isChecked(), export_zip=self.export_zip.isChecked(), skip_if_larger=self.skip_larger.isChecked(), max_workers=self.max_workers.value(), sharpen=self.sharpen.value(), denoise=self.denoise.value(), contrast=self.contrast.value(), brightness=self.brightness.value(), saturation=self.saturation.value(), text_watermark=self.text_enable.isChecked(), watermark_text=self.text_value.text(), font_path=self.font_path.text(), font_size=self.font_size.value(), text_rotation=self.text_rotation.value(), font_color=self.font_color.text(), text_opacity=self.text_opacity.value(), text_anchor=self.anchor.currentText(), text_x_percent=self.text_x.value(), text_y_percent=self.text_y.value(), margin_x=self.margin_x.value(), margin_y=self.margin_y.value(), outline=self.outline.isChecked(), outline_color=self.outline_color.text(), outline_width=self.outline_width.value(), shadow=self.shadow.isChecked(), shadow_offset=self.shadow_offset.value(), shadow_opacity=self.shadow_opacity.value(), background=self.background.isChecked(), background_color=self.background_color.text(), background_opacity=self.background_opacity.value(), image_watermark=self.image_enable.isChecked(), image_watermark_path=self.logo_path.text(), image_scale=self.logo_scale.value(), image_opacity=self.logo_opacity.value(), image_x_percent=self.logo_x.value(), image_y_percent=self.logo_y.value(),
        )

    def _selected_model_summary(self) -> str:
        path = Path(str(self.model.currentData() or ""))
        if not path.is_file():
            return "No AI model selected"
        size_mb = path.stat().st_size / 1024**2
        tile = "Auto tile" if self.tile.value() == 0 else f"{self.tile.value()}px tile"
        return f"{path.name} · {size_mb:.1f} MB · {self.device.currentText()} · {self.ai_precision.currentText()} · {tile}"

    def _update_ai_status_text(self) -> None:
        if not hasattr(self, "ai_status"):
            return
        if self.method.currentText() != "AI model":
            self.ai_status.setText("AI controls appear when Method is set to AI model.")
            return
        self.ai_status.setText(self._selected_model_summary())

    def _ai_control_changed(self, *_args) -> None:
        if hasattr(self, "ai_profile") and self.ai_profile.currentText() != "Custom":
            self.ai_profile.blockSignals(True)
            self.ai_profile.setCurrentText("Custom")
            self.ai_profile.blockSignals(False)
        self._update_ai_status_text()

    def apply_ai_profile(self, name: str) -> None:
        if name == "Custom":
            return
        controls = (self.ai_precision, self.tile, self.ai_preview_size, self.ai_oom_recovery)
        for control in controls:
            control.blockSignals(True)
        try:
            if name == "Fast":
                self.ai_precision.setCurrentText("Auto")
                self.tile.setValue(768)
                self.ai_preview_size.setValue(512)
                self.ai_oom_recovery.setChecked(True)
            elif name == "Low memory":
                self.ai_precision.setCurrentText("Auto")
                self.tile.setValue(192)
                self.ai_preview_size.setValue(384)
                self.ai_oom_recovery.setChecked(True)
            elif name == "Maximum quality":
                self.ai_precision.setCurrentText("FP32")
                self.tile.setValue(384)
                self.ai_preview_size.setValue(768)
                self.ai_oom_recovery.setChecked(True)
            else:
                self.ai_precision.setCurrentText("Auto")
                self.tile.setValue(0)
                self.ai_preview_size.setValue(640)
                self.ai_oom_recovery.setChecked(True)
        finally:
            for control in controls:
                control.blockSignals(False)
        self._update_ai_status_text()
        if hasattr(self, "status"):
            self.status.setText(f"AI profile: {name}")

    def check_ai_setup(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            summary = ai_environment_summary(self.device.currentText())
            model = self._selected_model_summary()
            self.ai_status.setText(f"{model}\n{summary}")
            self.status.setText("AI environment checked")
        finally:
            QApplication.restoreOverrideCursor()

    def install_ai_support(self) -> None:
        launcher = self.base_dir / "ImageSuite.bat"
        if os.name != "nt" or not launcher.is_file():
            QMessageBox.information(
                self,
                "Install AI support",
                "The one-file installer is available in the source/portable package as ImageSuite.bat. "
                "Run ImageSuite.bat --install-ai from that folder.",
            )
            return
        result = QMessageBox.question(
            self,
            "Install or repair AI support",
            "ImageSuite will open one setup window using the same ImageSuite.bat launcher. "
            "Restart the app after installation finishes. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            return
        try:
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(
                ["cmd.exe", "/c", str(launcher), "--install-ai"],
                cwd=str(self.base_dir),
                creationflags=creationflags,
            )
            self.status.setText("AI installer opened — restart ImageSuite after it finishes")
        except Exception as exc:
            show_operation_error(self, "AI installer could not start", "ImageSuite.bat could not be opened.", str(exc))

    def choose_font(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Choose font", "", "Fonts (*.ttf *.otf)")
        if name:
            self.font_path.setText(name)

    def apply_quality_preset(self, name: str) -> None:
        presets = {
            "Clean Photo": (0.15, 0.0, 1.02, 1.0, 1.0),
            "Crisp Illustration": (0.45, 0.0, 1.08, 1.0, 1.06),
            "Soft Portrait": (0.05, 0.35, 0.98, 1.02, 0.96),
            "Maximum Detail": (0.75, 0.0, 1.12, 1.0, 1.03),
        }
        if name in presets:
            values = presets[name]
            for widget, value in zip((self.sharpen,self.denoise,self.contrast,self.brightness,self.saturation), values):
                widget.setValue(value)

    def refresh_models(self) -> None:
        current = self.model.currentData()
        self.model.clear()
        for path in available_models(self.models_dir):
            self.model.addItem(path.name, str(path))
        if self.model.count() == 0:
            self.model.addItem("Place .pth/.pt/.safetensors files in models", "")
        if current:
            index = self.model.findData(current)
            if index >= 0: self.model.setCurrentIndex(index)
        self._update_ai_status_text()

    def add_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Add images", "", "Images and animations (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.mp4 *.webm)")
        self._append([Path(n) for n in names])

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add image folder")
        if folder:
            self.add_paths([Path(folder)])

    def _append(self, paths: list[Path], *, select_first: bool = True) -> None:
        existing = {self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())}
        added: list[str] = []
        for path in paths:
            if path.is_file() and str(path) not in existing:
                self.files.addItem(path.name)
                item = self.files.item(self.files.count()-1); item.setData(Qt.UserRole, str(path)); item.setToolTip(str(path))
                existing.add(str(path))
                added.append(str(path))
        if added:
            self.pathsAdded.emit(added)
        if select_first and self.files.count() and self.files.currentRow() < 0:
            self.files.setCurrentRow(0)
        self._update_queue_label()
        self.status.setText(f"Queue contains {self.files.count()} image(s)")

    def clear_files(self) -> None:
        if self.files.count() > 0 and QMessageBox.question(self, "Clear queue", f"Remove all {self.files.count()} image(s) from the queue?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.files.clear()
        self._preview_pixmap = QPixmap()
        self.preview.setPixmap(QPixmap())
        self.preview.setText("Select an input image")
        self._update_queue_label()

    def choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Output folder", self.output_edit.text())
        if folder: self.output_edit.setText(folder)

    def choose_logo(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Choose watermark image", "", "Images (*.png *.webp *.jpg *.jpeg)")
        if name: self.logo_path.setText(name)

    def selected_path(self) -> Path | None:
        item = self.files.currentItem()
        return Path(item.data(Qt.UserRole)) if item else None

    def show_source_preview(self, _row: int) -> None:
        path = self.selected_path()
        if not path: return
        try:
            image = read_thumbnail(path)
            try:
                self._preview_pixmap = QPixmap.fromImage(QImage(ImageQt(image)))
            finally:
                image.close()
            self._render_preview()
        except Exception as exc:
            self._preview_pixmap = QPixmap()
            self.preview.setText(str(exc))

    def _render_preview(self) -> None:
        if not self._preview_pixmap.isNull():
            self.preview.setPixmap(
                self._preview_pixmap.scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._render_preview()

    def _paths(self) -> list[Path]:
        return self.queue_paths()

    def generate_preview(self) -> None:
        path = self.selected_path()
        if not path: return
        self._launch([path], preview=True)

    def start_queue(self) -> None:
        paths = self._paths()
        if not paths:
            QMessageBox.information(self, "Upscale", "Add one or more images first.")
            return
        output_text = self.output_edit.text().strip()
        if not output_text:
            QMessageBox.information(self, "Upscale", "Choose an output folder first.")
            return
        self._launch(paths, preview=False)

    def _launch(self, paths: list[Path], preview: bool) -> None:
        if self.thread and self.thread.isRunning(): return
        settings = self.settings()
        try:
            validate_settings(settings)
            output = Path(self.output_edit.text().strip() or self.outputs_dir)
            if not preview and settings.create_timestamped_folder:
                output = unique_destination(output, time.strftime("job_%Y%m%d_%H%M%S"))
            output.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            show_operation_error(self, "Enhance cannot start", "Check the selected settings and output folder.", str(exc))
            return
        plan = plan_worker_count(paths, settings)
        self.thread = QThread(self)
        self.worker = UpscaleWorker(paths, output, settings, preview_only=preview, plan=plan)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._progress)
        self.worker.previewReady.connect(self._preview_ready)
        self.worker.finished.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.failed.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._thread_done)
        self.thread.finished.connect(self.thread.deleteLater)
        self.start_btn.setEnabled(False); self.cancel_btn.setEnabled(True)
        self.job = self.jobs.create("Upscale preview" if preview else f"Upscale {len(paths)} image(s)", "Upscale", len(paths))
        if not preview:
            worker_note = f"{plan.effective} worker{'s' if plan.effective != 1 else ''}"
            if plan.effective < plan.requested:
                worker_note += f" (requested {plan.requested}; limited by {plan.reason})"
            self.status.setText(f"Starting {len(paths)} image(s) with {worker_note}…")
        self.thread.start()

    def _progress(self, done: int, total: int, detail: str) -> None:
        self.progress.setValue(int(done * 100 / total) if total else 0)
        self.status.setText(detail); self.statusChanged.emit(detail)
        if self.job: self.jobs.update(self.job, done, total, detail)

    def _preview_ready(self, image) -> None:
        try:
            self._preview_pixmap = QPixmap.fromImage(QImage(ImageQt(image)))
        finally:
            close = getattr(image, "close", None)
            if close:
                close()
        self._render_preview()

    def open_result_in_editor(self) -> None:
        if self.last_outputs:
            self.openInEditor.emit(str(self.last_outputs[0]))

    def reveal_result(self) -> None:
        if self.last_outputs:
            open_folder(self.last_outputs[0].parent)

    def _show_results(self, outputs: list, errors: list) -> None:
        valid_outputs = [Path(path) for path in outputs if Path(path).suffix.lower() != ".zip" and Path(path).exists()]
        self.last_output_count = len(valid_outputs)
        # The UI only opens/reveals the first result. Retaining every path forever
        # made very large completed queues unnecessarily sticky in memory.
        self.last_outputs = valid_outputs[:1]
        self.result_bar.setVisible(bool(self.last_outputs or errors))
        if self.last_outputs:
            first = self.last_outputs[0]
            try:
                preview = read_thumbnail(first)
                try:
                    self._preview_pixmap = QPixmap.fromImage(QImage(ImageQt(preview)))
                finally:
                    preview.close()
                self._render_preview()
                with Image.open(first) as raw:
                    width, height = raw.size
                summary = f"{self.last_output_count} image(s) ready · {width} × {height}\n{first}"
            except Exception:
                summary = f"{self.last_output_count} image(s) ready\n{first}"
        else:
            summary = "No output images were created"
        if errors:
            summary += f"\n{len(errors)} file(s) failed; see the warning for details"
        self.result_label.setText(summary)
        self.retry_failed_btn.setVisible(bool(self.last_failed_paths))

    def _finished(self, outputs: list, errors: list) -> None:
        cancelled = bool(self.worker and self.worker.cancelled)
        self.last_failed_paths = list(self.worker.failed_paths) if self.worker else []
        detail = "Cancelled" if cancelled else (f"Saved {len(outputs)} image(s)" if outputs else "Preview complete")
        if errors:
            detail += f"; {len(errors)} error(s)"
        self.status.setText(detail)
        self.progress.setValue(100 if not cancelled else self.progress.value())
        if self.job:
            state = "Cancelled" if cancelled else ("Completed" if not errors else "Completed with errors")
            self.jobs.finish(self.job, state, detail)
        if not cancelled:
            self._show_results(outputs, errors)
        if errors:
            QMessageBox.warning(self, "Enhance results", detail + "\n\n" + "\n".join(errors[:12]))

    def _failed(self, error: str) -> None:
        self.status.setText("Failed")
        if self.job: self.jobs.finish(self.job, "Failed", error.splitlines()[-1] if error else "Unknown error")
        self.last_failed_paths = list(self.worker.files) if self.worker else []
        self.retry_failed_btn.setVisible(bool(self.last_failed_paths))
        summary = error.strip().splitlines()[-1] if error.strip() else "The Enhance operation failed."
        show_operation_error(self, "Enhance failed", summary, error)

    def _thread_done(self) -> None:
        self.start_btn.setEnabled(True); self.cancel_btn.setEnabled(False)
        self.worker = None; self.thread = None

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.status.setText("Cancelling after the current image…")
            self.statusChanged.emit(self.status.text())

    def retry_failed(self) -> None:
        paths = [path for path in self.last_failed_paths if path.is_file()]
        if not paths:
            self.last_failed_paths = []
            self.retry_failed_btn.setVisible(False)
            return
        self._launch(paths, preview=False)

    def is_busy(self) -> bool:
        return bool(self.thread and self.thread.isRunning())

    def cancel_and_wait(self, timeout_ms: int = 7000) -> bool:
        if not self.is_busy():
            return True
        self.cancel()
        deadline = time.monotonic() + timeout_ms / 1000
        while self.thread and self.thread.isRunning() and time.monotonic() < deadline:
            QApplication.processEvents()
            self.thread.wait(50)
        return not self.is_busy()
