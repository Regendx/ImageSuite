from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from pathlib import Path
import os
import shutil
import time
import traceback

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QMouseEvent, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMenu, QMessageBox, QProgressBar, QPushButton, QSlider,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from imagesuite.jobs import JobManager, JobRecord
from imagesuite.diagnostics import show_operation_error
from imagesuite.similarity.engine import (
    SimilarityGroup, choose_best, copy_files, export_csv, format_bytes, move_files,
    open_file, recycle_files, reveal_file, scan_folder,
)
from imagesuite.utils import read_thumbnail, unique_destination


class ScanWorker(QObject):
    progress = Signal(str, int, int)
    finished = Signal(list, list)
    failed = Signal(str)

    def __init__(self, root: Path, recursive: bool, threshold: float, workers: int) -> None:
        super().__init__(); self.root = root; self.recursive = recursive; self.threshold = threshold; self.workers = workers; self.cancelled = False
    def cancel(self) -> None: self.cancelled = True
    def run(self) -> None:
        try:
            groups, errors = scan_folder(self.root, self.recursive, self.threshold, self.workers, lambda s,d,t: self.progress.emit(s,d,t), lambda: self.cancelled)
            self.finished.emit(groups, errors)
        except Exception: self.failed.emit(traceback.format_exc())


class SimilarityWorkspace(QWidget):
    statusChanged = Signal(str)
    openInEditor = Signal(str)
    sourceChanged = Signal(str)

    def __init__(self, jobs: JobManager) -> None:
        super().__init__(); self.jobs = jobs; self.groups: list[SimilarityGroup] = []; self.thread: QThread | None = None; self.worker: ScanWorker | None = None; self.job: JobRecord | None = None; self._building = False
        self._preview_pixmap = QPixmap()
        self._thumbnail_cache: OrderedDict[str, tuple[QPixmap, int]] = OrderedDict()
        self._thumbnail_cache_bytes = 0
        self._thumbnail_cache_limit = 32 * 1024 * 1024
        self._thumbnail_load_timer = QTimer(self)
        self._thumbnail_load_timer.setSingleShot(True)
        self._thumbnail_load_timer.setInterval(20)
        self._thumbnail_load_timer.timeout.connect(self._load_visible_thumbnails)
        self.last_move: list[tuple[str, str]] = []
        self._shortcuts: list[tuple[QShortcut, str]] = []
        self._build_ui()
        self._bind_shortcuts()
        QApplication.instance().focusChanged.connect(self._update_shortcut_states)
        self._update_shortcut_states(None, QApplication.focusWidget())

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 8)
        title = QHBoxLayout()
        title.addWidget(QLabel("ORGANIZE SIMILAR IMAGES", objectName="Brand"))
        title.addStretch(1)
        root.addLayout(title)

        source_row = QHBoxLayout()
        self.source = QLineEdit()
        self.source.setPlaceholderText("Choose a folder containing images…")
        self.source.returnPressed.connect(self.start_scan)
        browse = QPushButton("Choose folder")
        browse.clicked.connect(self.choose_source)
        self.scan_btn = QPushButton("Find similar images", objectName="Accent")
        self.scan_btn.clicked.connect(self.start_scan)
        self.cancel_btn = QPushButton("Cancel", objectName="Danger")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setVisible(False)
        source_row.addWidget(self.source, 1)
        source_row.addWidget(browse)
        source_row.addWidget(self.scan_btn)
        source_row.addWidget(self.cancel_btn)
        root.addLayout(source_row)

        options_toggle = QToolButton()
        options_toggle.setText("Scan options")
        options_toggle.setCheckable(True)
        options_toggle.setArrowType(Qt.RightArrow)
        root.addWidget(options_toggle, 0, Qt.AlignLeft)
        self.scan_options = QWidget()
        grid = QGridLayout(self.scan_options)
        grid.setContentsMargins(0, 2, 0, 4)
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(True)
        self.threshold = QSlider(Qt.Horizontal)
        self.threshold.setRange(60, 100)
        self.threshold.setValue(88)
        self.threshold_label = QLabel("88%", objectName="Muted")
        self.threshold.valueChanged.connect(lambda v: self.threshold_label.setText(f"{v}%"))
        self.workers = QSpinBox()
        self.workers.setRange(1, 32)
        self.workers.setValue(max(2, min(8, os.cpu_count() or 4)))
        grid.addWidget(self.recursive, 0, 0, 1, 2)
        grid.addWidget(QLabel("Similarity threshold"), 1, 0)
        grid.addWidget(self.threshold, 1, 1)
        grid.addWidget(self.threshold_label, 1, 2)
        grid.addWidget(QLabel("Workers"), 2, 0)
        grid.addWidget(self.workers, 2, 1)
        self.scan_options.setVisible(False)
        options_toggle.toggled.connect(lambda shown: (self.scan_options.setVisible(shown), options_toggle.setArrowType(Qt.DownArrow if shown else Qt.RightArrow)))
        root.addWidget(self.scan_options)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 6, 0)
        ll.addWidget(QLabel("Groups", objectName="SectionTitle"))
        self.group_list = QListWidget()
        self.group_list.setAccessibleName("Similar image groups")
        self.group_list.currentRowChanged.connect(self.load_group)
        ll.addWidget(self.group_list)
        group_nav = QHBoxLayout()
        previous_group = QPushButton("◀")
        previous_group.setToolTip("Previous group (K)")
        previous_group.clicked.connect(lambda: self.navigate_group(-1))
        next_group = QPushButton("▶")
        next_group.setToolTip("Next group (J)")
        next_group.clicked.connect(lambda: self.navigate_group(1))
        group_nav.addWidget(previous_group)
        group_nav.addWidget(next_group)
        ll.addLayout(group_nav)
        group_hint = QLabel("J/K changes group · ↑/↓ changes image", objectName="Muted")
        group_hint.setWordWrap(True)
        ll.addWidget(group_hint)
        splitter.addWidget(left)

        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(6, 0, 6, 0)
        self.table = QTableWidget(0, 8)
        self.table.setAccessibleName("Images in selected similarity group")
        self.table.setHorizontalHeaderLabels(["Remove", "Preview", "Score", "Dimensions", "Size", "Modified", "Sharpness", "Path"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.itemChanged.connect(self.item_changed)
        self.table.itemSelectionChanged.connect(self.update_preview)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_menu)
        self.table.itemDoubleClicked.connect(lambda _item: self.open_editor())
        self.table.verticalScrollBar().valueChanged.connect(lambda _value: self._thumbnail_load_timer.start())
        self.table.setColumnWidth(0, 62)
        self.table.setColumnWidth(1, 96)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 105)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 145)
        self.table.setColumnWidth(6, 90)
        self.table.setColumnWidth(7, 500)
        cl.addWidget(self.table, 1)

        review_row = QHBoxLayout()
        self.rule = QComboBox()
        self.rule.addItems(["Keep highest resolution", "Keep sharpest", "Keep newest", "Keep oldest", "Keep largest file", "Keep smallest file", "Keep shortest path"])
        except_best = QPushButton("Select duplicates", objectName="Accent")
        except_best.setToolTip("Check every image except the best match according to the selected rule (B)")
        except_best.clicked.connect(self.check_except_best)
        selection_more = QToolButton()
        selection_more.setText("More")
        selection_more.setPopupMode(QToolButton.InstantPopup)
        selection_menu = QMenu(selection_more)
        selection_menu.addAction("Check whole group", lambda: self.set_checks(True))
        selection_menu.addAction("Uncheck whole group", lambda: self.set_checks(False))
        selection_more.setMenu(selection_menu)
        review_row.addWidget(self.rule, 1)
        review_row.addWidget(except_best)
        review_row.addWidget(selection_more)
        cl.addLayout(review_row)

        destination_row = QHBoxLayout()
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("Destination; defaults to _similarity_filtered_removed")
        dest_btn = QPushButton("Choose")
        dest_btn.clicked.connect(self.choose_destination)
        move = QPushButton("Move selected", objectName="Accent")
        move.clicked.connect(self.move_checked)
        file_more = QToolButton()
        file_more.setText("More")
        file_more.setPopupMode(QToolButton.InstantPopup)
        file_menu = QMenu(file_more)
        self.undo_move_action = file_menu.addAction("Undo last move", self.undo_last_move)
        self.undo_move_action.setEnabled(False)
        file_menu.addSeparator()
        file_menu.addAction("Copy selected…", self.copy_checked)
        file_menu.addAction("Recycle selected…", self.recycle_checked)
        file_menu.addSeparator()
        file_menu.addAction("Open selected externally", self.open_selected)
        file_menu.addAction("Reveal selected in Explorer", self.reveal_selected)
        file_menu.addAction("Export groups to CSV…", self.export_groups)
        file_more.setMenu(file_menu)
        destination_row.addWidget(self.destination, 1)
        destination_row.addWidget(dest_btn)
        destination_row.addWidget(move)
        destination_row.addWidget(file_more)
        cl.addLayout(destination_row)

        open_editor = QPushButton("Open selected in editor")
        open_editor.clicked.connect(self.open_editor)
        cl.addWidget(open_editor)
        splitter.addWidget(center)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)
        rl.addWidget(QLabel("Preview", objectName="SectionTitle"))
        self.preview = QLabel("Select an image")
        self.preview.setAccessibleName("Selected similar image preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(330, 330)
        self.preview.setStyleSheet("background:#13151a; border:1px solid #303540;")
        rl.addWidget(self.preview, 1)
        self.info = QLabel("", objectName="Muted")
        self.info.setWordWrap(True)
        rl.addWidget(self.info)
        splitter.addWidget(right)
        splitter.setSizes([235, 800, 370])
        root.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.status = QLabel("Ready", objectName="Muted")
        self.checked_label = QLabel("0 selected", objectName="Muted")
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Details")
        self.details_toggle.setCheckable(True)
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.checked_label)
        bottom.addWidget(self.status)
        bottom.addWidget(self.details_toggle)
        root.addLayout(bottom)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(90)
        self.log.setPlaceholderText("Skipped files and action logs appear here.")
        self.log.setVisible(False)
        self.details_toggle.toggled.connect(self.log.setVisible)
        root.addWidget(self.log)


    def _show_table_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        if row >= 0:
            self.table.selectRow(row)
        menu = QMenu(self)
        toggle = menu.addAction("Toggle selected")
        edit = menu.addAction("Open in editor")
        external = menu.addAction("Open externally")
        reveal = menu.addAction("Reveal in Explorer")
        copy_path = menu.addAction("Copy path")
        menu.addSeparator()
        select_duplicates = menu.addAction("Select duplicates in this group")
        selected = menu.exec(self.table.viewport().mapToGlobal(position))
        if selected is toggle:
            self.toggle_selected_check()
        elif selected is edit:
            self.open_editor()
        elif selected is external:
            self.open_selected()
        elif selected is reveal:
            self.reveal_selected()
        elif selected is copy_path:
            member = self.selected_member()
            if member:
                QApplication.clipboard().setText(member.fp.path)
        elif selected is select_duplicates:
            self.check_except_best()

    def _bind_shortcuts(self) -> None:
        bindings = [
            ("Space", self.toggle_selected_check, "results"), ("X", self.toggle_selected_check, "results"),
            ("Ctrl+A", lambda: self.set_checks(True), "results"), ("Ctrl+Shift+A", lambda: self.set_checks(False), "results"),
            ("Return", self.open_selected, "results"), ("E", self.open_editor, "results"), ("O", self.open_selected, "results"),
            ("R", self.reveal_selected, "results"), ("B", self.check_except_best, "results"),
            ("J", lambda: self.navigate_group(1), "navigation"), ("K", lambda: self.navigate_group(-1), "navigation"),
            ("Ctrl+Delete", self.recycle_checked, "results"), ("Ctrl+Return", self.start_scan, "always"),
            ("Escape", self.cancel, "always"),
        ]
        for sequence, callback, policy in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append((shortcut, policy))

    def _update_shortcut_states(self, _old, new) -> None:
        editing = isinstance(new, (QLineEdit, QSpinBox, QSlider, QComboBox, QTextEdit))
        results_focus = (
            new is self.table or new is self.group_list
            or (new is not None and (self.table.isAncestorOf(new) or self.group_list.isAncestorOf(new)))
        )
        for shortcut, policy in self._shortcuts:
            if policy == "results":
                shortcut.setEnabled(results_focus)
            elif policy == "navigation":
                shortcut.setEnabled(not editing)
            else:
                shortcut.setEnabled(True)

    def navigate_group(self, direction: int) -> None:
        if not self.groups:
            return
        current = self.group_list.currentRow()
        self.group_list.setCurrentRow((current + direction) % len(self.groups))
        self.group_list.setFocus()

    def toggle_selected_check(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item:
            item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
            self.table.selectRow(row)
            self._update_checked_label()

    def _update_checked_label(self) -> None:
        count = sum(member.checked for group in self.groups for member in group.members)
        self.checked_label.setText(f"{count} selected")

    def recycle_selected_row(self) -> None:
        group = self.current_group()
        row = self.table.currentRow()
        if not group or not (0 <= row < len(group.members)):
            return
        for other in self.groups:
            for member in other.members:
                member.checked = False
        self._building = True
        for table_row in range(self.table.rowCount()):
            self.table.item(table_row, 0).setCheckState(Qt.Checked if table_row == row else Qt.Unchecked)
        self._building = False
        group.members[row].checked = True
        self.recycle_checked()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.BackButton:
            self.navigate_group(-1)
            event.accept()
            return
        if event.button() == Qt.ForwardButton:
            self.navigate_group(1)
            event.accept()
            return
        super().mousePressEvent(event)

    def set_source_folder(self, folder: str | Path, *, emit: bool = True) -> None:
        path = Path(folder)
        if not path.is_dir():
            return
        self.source.setText(str(path))
        if not self.destination.text():
            self.destination.setText(str(path / "_similarity_filtered_removed"))
        if emit:
            self.sourceChanged.emit(str(path))

    def choose_source(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose image folder", self.source.text())
        if folder:
            self.set_source_folder(folder)

    def choose_destination(self) -> None:
        folder = QFileDialog.getExistingDirectory(self,"Choose destination",self.destination.text() or self.source.text())
        if folder: self.destination.setText(folder)

    def start_scan(self) -> None:
        root = Path(self.source.text())
        if not root.is_dir():
            QMessageBox.information(self, "Scan", "Choose a valid image folder first.")
            return
        if self.thread and self.thread.isRunning():
            return
        self.sourceChanged.emit(str(root))

        self.groups.clear()
        self.group_list.clear()
        self._thumbnail_load_timer.stop()
        self._thumbnail_cache.clear()
        self._thumbnail_cache_bytes = 0
        self.table.setRowCount(0)
        self.preview.setPixmap(QPixmap())
        self.preview.setText("Scanning…")

        self.thread = QThread(self)
        self.worker = ScanWorker(
            root,
            self.recursive.isChecked(),
            float(self.threshold.value()),
            self.workers.value(),
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.scan_progress)
        self.worker.finished.connect(self.scan_finished)
        self.worker.failed.connect(self.scan_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.failed.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread_done)
        self.thread.finished.connect(self.thread.deleteLater)

        self.scan_btn.setEnabled(False)
        self.scan_btn.setVisible(False)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)
        self.job = self.jobs.create(f"Scan {root.name}", "Similarity")
        self.thread.start()

    def scan_progress(self, stage: str, done: int, total: int) -> None:
        self.progress.setValue(int(done*100/total) if total else 0); detail = f"{stage}: {done}/{total}"; self.status.setText(detail); self.statusChanged.emit(detail)
        if self.job: self.jobs.update(self.job,done,total,detail)

    def scan_finished(self, groups: list, errors: list) -> None:
        if self.worker and self.worker.cancelled:
            self.status.setText("Scan cancelled")
            self.statusChanged.emit("Scan cancelled")
            if self.job:
                self.jobs.finish(self.job, "Cancelled", "Scan cancelled")
            return
        self.groups = groups
        self._rebuild_group_list()
        if groups:
            self.group_list.setCurrentRow(0)
            self.table.setFocus()
        else:
            self._preview_pixmap = QPixmap()
            self.preview.setPixmap(QPixmap())
            self.preview.setText("No similar groups found")
        detail = f"Found {len(groups)} group(s)" + (f"; skipped {len(errors)} file(s)" if errors else ""); self.status.setText(detail); self.progress.setValue(100)
        if errors: self.log.setPlainText("\n".join(errors[:100]))
        if self.job: self.jobs.finish(self.job,"Completed" if not errors else "Completed with errors",detail)

    def scan_failed(self, error: str) -> None:
        self.log.setPlainText(error); self.status.setText("Scan failed")
        if self.job: self.jobs.finish(self.job,"Failed",error.splitlines()[-1] if error else "Unknown error")
        summary = error.strip().splitlines()[-1] if error.strip() else "The similarity scan failed."
        show_operation_error(self, "Scan failed", summary, error)

    def thread_done(self) -> None:
        self.scan_btn.setEnabled(True); self.scan_btn.setVisible(True); self.cancel_btn.setEnabled(False); self.cancel_btn.setVisible(False); self.worker = None; self.thread = None

    def cancel(self) -> None:
        if self.worker: self.worker.cancel(); self.status.setText("Cancelling…")

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

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._thumbnail_load_timer.stop()
        self._thumbnail_cache.clear()
        self._thumbnail_cache_bytes = 0
        self._preview_pixmap = QPixmap()
        super().closeEvent(event)

    def current_group(self) -> SimilarityGroup | None:
        row = self.group_list.currentRow(); return self.groups[row] if 0 <= row < len(self.groups) else None

    def _rebuild_group_list(self) -> None:
        self.group_list.clear()
        for group in self.groups:
            self.group_list.addItem(
                f"Group {group.id} · {group.count} images · min {group.min_score:.1f}% · {format_bytes(group.total_bytes)}"
            )

    def _thumbnail(self, path: str, max_side: int = 160) -> QPixmap:
        key = f"{path}:{max_side}"
        cached = self._thumbnail_cache.pop(key, None)
        if cached is not None:
            self._thumbnail_cache[key] = cached
            return cached[0]
        image = read_thumbnail(path, max_side)
        try:
            pixmap = QPixmap.fromImage(QImage(ImageQt(image)))
        finally:
            image.close()
        byte_size = max(1, pixmap.width() * pixmap.height() * 4)
        self._thumbnail_cache[key] = (pixmap, byte_size)
        self._thumbnail_cache_bytes += byte_size
        while self._thumbnail_cache and self._thumbnail_cache_bytes > self._thumbnail_cache_limit:
            _, (_removed, removed_bytes) = self._thumbnail_cache.popitem(last=False)
            self._thumbnail_cache_bytes -= removed_bytes
        return pixmap

    def load_group(self, _row: int) -> None:
        group = self.current_group(); self._building = True; self.table.setRowCount(0)
        if group:
            self.table.setRowCount(len(group.members))
            for row, member in enumerate(group.members):
                check = QTableWidgetItem(); check.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsUserCheckable); check.setCheckState(Qt.Checked if member.checked else Qt.Unchecked); self.table.setItem(row,0,check)
                thumb = QTableWidgetItem("…")
                thumb.setData(Qt.UserRole, member.fp.path)
                self.table.setItem(row, 1, thumb)
                fp = member.fp; values = [f"{member.score_to_anchor:.1f}%",f"{fp.width}×{fp.height}",format_bytes(fp.size_bytes),datetime.fromtimestamp(fp.mtime).strftime("%Y-%m-%d %H:%M"),f"{fp.sharpness:.1f}",fp.path]
                for col,value in enumerate(values,2): self.table.setItem(row,col,QTableWidgetItem(value))
                self.table.setRowHeight(row,88)
            if group.members:
                self.table.selectRow(0)
        self._building = False
        self._thumbnail_load_timer.start()
        self._update_checked_label()

    def item_changed(self, item: QTableWidgetItem) -> None:
        if self._building or item.column()!=0: return
        group = self.current_group()
        if group and 0 <= item.row() < len(group.members):
            group.members[item.row()].checked = item.checkState() == Qt.Checked
            self._update_checked_label()

    def _load_visible_thumbnails(self) -> None:
        group = self.current_group()
        if not group or self.table.rowCount() == 0:
            return
        viewport = self.table.viewport()
        first = self.table.rowAt(0)
        last = self.table.rowAt(max(0, viewport.height() - 1))
        if first < 0:
            first = 0
        if last < 0:
            last = min(self.table.rowCount() - 1, first + 12)
        first = max(0, first - 3)
        last = min(self.table.rowCount() - 1, last + 3)
        for row in range(first, last + 1):
            item = self.table.item(row, 1)
            if item is None or item.data(Qt.DecorationRole) is not None:
                continue
            path = item.data(Qt.UserRole)
            if not path:
                continue
            try:
                pix = self._thumbnail(str(path), 160).scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                item.setText("")
                item.setData(Qt.DecorationRole, pix)
            except Exception:
                item.setText("Unavailable")

    def selected_member(self):
        group = self.current_group(); row = self.table.currentRow(); return group.members[row] if group and 0 <= row < len(group.members) else None

    def update_preview(self) -> None:
        member = self.selected_member()
        if not member: return
        try:
            image = read_thumbnail(member.fp.path)
            try:
                self._preview_pixmap = QPixmap.fromImage(QImage(ImageQt(image)))
            finally:
                image.close()
            self._render_preview()
            fp = member.fp
            self.info.setText(f"{Path(fp.path).name}\n{fp.width} × {fp.height} · {format_bytes(fp.size_bytes)}\nSimilarity: {member.score_to_anchor:.2f}% · Sharpness: {fp.sharpness:.2f}\n{fp.path}")
        except Exception as exc:
            self._preview_pixmap = QPixmap()
            self.preview.setText(str(exc))

    def _render_preview(self) -> None:
        if not self._preview_pixmap.isNull():
            self.preview.setPixmap(
                self._preview_pixmap.scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )

    def resizeEvent(self,event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._render_preview()
        if hasattr(self, "_thumbnail_load_timer"):
            self._thumbnail_load_timer.start()

    def set_checks(self, checked: bool) -> None:
        group = self.current_group()
        if not group: return
        self._building=True
        for row,member in enumerate(group.members): member.checked=checked; self.table.item(row,0).setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._building=False
        self._update_checked_label()

    def check_except_best(self) -> None:
        group=self.current_group()
        if not group:return
        best=choose_best(group,self.rule.currentText()); self._building=True
        for row,member in enumerate(group.members): member.checked=row!=best; self.table.item(row,0).setCheckState(Qt.Checked if row!=best else Qt.Unchecked)
        self._building=False; self.table.selectRow(best); self._update_checked_label()

    def checked_paths(self) -> list[str]:
        return [m.fp.path for g in self.groups for m in g.members if m.checked and Path(m.fp.path).exists()]

    def _remove_paths_from_groups(self, paths: set[str]) -> None:
        new=[]
        for group in self.groups:
            group.members=[m for m in group.members if m.fp.path not in paths]
            if len(group.members)>=2:
                group.anchor=max(group.members,key=lambda m:(m.fp.width*m.fp.height,m.fp.size_bytes,m.fp.sharpness)).fp
                new.append(group)
        self.groups=new
        for i,g in enumerate(self.groups,1):g.id=i
        self._rebuild_group_list()
        if self.groups:
            self.group_list.setCurrentRow(0)
        else:
            self.table.setRowCount(0)
            self._preview_pixmap = QPixmap()
            self.preview.setPixmap(QPixmap())
            self.preview.setText("No similar groups found")
        self._update_checked_label()

    def move_checked(self) -> None:
        paths=self.checked_paths()
        if not paths:return
        dest=self.destination.text() or str(Path(self.source.text())/"_similarity_filtered_removed")
        if QMessageBox.question(self,"Move checked",f"Move {len(paths)} file(s) to:\n{dest}?",QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        try:
            moved = move_files(paths, dest)
            self.last_move = moved
            self.undo_move_action.setEnabled(bool(moved))
            self.log.append(f"Moved {len(moved)} files to {dest}")
            self._remove_paths_from_groups(set(paths))
            self.status.setText(f"Moved {len(moved)} file(s) · Undo is available under More")
        except Exception as exc:
            show_operation_error(self, "Move failed", "No files were left partially moved.", str(exc))

    def undo_last_move(self) -> None:
        if not self.last_move:
            return
        restored: list[tuple[str, str]] = []
        remaining: list[tuple[str, str]] = []
        errors: list[str] = []
        for original, moved in reversed(self.last_move):
            source = Path(moved)
            if not source.exists():
                errors.append(f"Missing: {source}")
                remaining.append((original, moved))
                continue
            target = Path(original)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target = unique_destination(target.parent, target.name)
            try:
                shutil.move(source, target)
                restored.append((str(source), str(target)))
            except Exception as exc:
                errors.append(f"{source}: {exc}")
                remaining.append((original, moved))
        self.last_move = list(reversed(remaining))
        self.undo_move_action.setEnabled(bool(self.last_move))
        if restored:
            self.log.append(f"Restored {len(restored)} moved file(s); rescan to review them again")
            self.status.setText(f"Restored {len(restored)} file(s) · Rescan to refresh groups")
        if errors:
            QMessageBox.warning(self, "Undo move", "Some files could not be restored:\n\n" + "\n".join(errors[:12]))

    def copy_checked(self) -> None:
        paths=self.checked_paths()
        if not paths:return
        dest=self.destination.text() or str(Path(self.source.text())/"_similarity_filtered_removed")
        try: copied=copy_files(paths,dest); self.log.append(f"Copied {len(copied)} files to {dest}")
        except Exception as exc: show_operation_error(self, "Copy failed", "Incomplete copies were removed.", str(exc))

    def recycle_checked(self) -> None:
        paths=self.checked_paths()
        if not paths:return
        if QMessageBox.question(self,"Recycle checked",f"Send {len(paths)} checked file(s) to the Recycle Bin?",QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        try:
            recycled, errors = recycle_files(paths)
            if recycled:
                self.log.append(f"Recycled {len(recycled)} files")
                self._remove_paths_from_groups(set(recycled))
            if errors:
                QMessageBox.warning(self, "Some files were not recycled", "\n".join(errors[:12]))
        except Exception as exc:
            show_operation_error(self, "Recycle failed", "The selected files could not be sent to the Recycle Bin.", str(exc))

    def open_selected(self) -> None:
        member=self.selected_member()
        if member:
            try: open_file(member.fp.path)
            except Exception as exc: show_operation_error(self, "Open failed", "The selected file could not be opened.", str(exc))

    def open_editor(self) -> None:
        member=self.selected_member()
        if member: self.openInEditor.emit(member.fp.path)

    def reveal_selected(self) -> None:
        member=self.selected_member()
        if member:
            try: reveal_file(member.fp.path)
            except Exception as exc: show_operation_error(self, "Reveal failed", "The selected file could not be revealed.", str(exc))

    def export_groups(self) -> None:
        if not self.groups:return
        path,_=QFileDialog.getSaveFileName(self,"Export groups",str(Path(self.source.text())/"visual_similarity_groups.csv"),"CSV (*.csv)")
        if path:
            try: export_csv(self.groups,path); self.log.append(f"Exported CSV: {path}")
            except Exception as exc: show_operation_error(self, "Export failed", "The CSV was not replaced.", str(exc))
