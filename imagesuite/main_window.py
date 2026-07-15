from __future__ import annotations

from pathlib import Path
import os
import sys

from PySide6.QtCore import QDateTime, QEvent, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QCursor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from imagesuite import __version__
from imagesuite.diagnostics import diagnostics_report
from imagesuite.editor.workspace import EditorWorkspace
from imagesuite.jobs import JobManager
from imagesuite.similarity.workspace import SimilarityWorkspace
from imagesuite.upscale.workspace import UpscaleWorkspace
from imagesuite.updater import RELEASES_PAGE_URL, UpdateClient, UpdateInfo, launch_installer_after_exit
from imagesuite.utils import IMAGE_FILE_FILTER, app_data_dir, expand_image_paths, open_folder


def _setting_int(settings: QSettings, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(settings.value(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class PreferencesDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("ImageSuite preferences")
        self.setMinimumWidth(470)
        root = QVBoxLayout(self)
        form = QFormLayout()

        self.startup = QComboBox()
        self.startup.addItems(["Last used", "Edit", "Enhance", "Organize"])
        self.startup.setCurrentText(str(settings.value("preferences/startup_workspace", "Last used")))
        form.addRow("Startup workspace", self.startup)

        self.history_depth = QSpinBox()
        self.history_depth.setRange(5, 100)
        self.history_depth.setValue(_setting_int(settings, "preferences/history_depth", 30, 5, 100))
        self.history_depth.setToolTip("Maximum undo entries per image")
        form.addRow("Undo entries", self.history_depth)

        self.history_memory = QSpinBox()
        self.history_memory.setRange(64, 4096)
        self.history_memory.setSuffix(" MB")
        self.history_memory.setValue(_setting_int(settings, "preferences/history_memory_mb", 512, 64, 4096))
        self.history_memory.setToolTip("Approximate memory ceiling per document history")
        form.addRow("Undo memory limit", self.history_memory)

        self.autosave = QSpinBox()
        self.autosave.setRange(10, 600)
        self.autosave.setSuffix(" seconds")
        self.autosave.setValue(_setting_int(settings, "preferences/autosave_seconds", 30, 10, 600))
        form.addRow("Recovery interval", self.autosave)

        self.preserve_metadata = QCheckBox("Preserve ICC color profile and DPI")
        self.preserve_metadata.setChecked(settings.value("preferences/preserve_metadata", True, type=bool))
        self.preserve_metadata.setToolTip("ImageSuite does not copy personal EXIF metadata such as GPS or camera details")
        form.addRow("Saving", self.preserve_metadata)

        self.restore_tabs = QCheckBox("Restore clean editor tabs from the previous session")
        self.restore_tabs.setChecked(settings.value("preferences/restore_editor_tabs", True, type=bool))
        self.restore_tabs.setToolTip("Tabs are restored one at a time after the window opens to keep startup responsive")
        form.addRow("Session", self.restore_tabs)

        self.auto_updates = QCheckBox("Check for ImageSuite updates automatically")
        self.auto_updates.setChecked(settings.value("updates/automatic", True, type=bool))
        self.auto_updates.setToolTip("Checks GitHub Releases at most once per day. Updates are downloaded only after confirmation.")
        form.addRow("Updates", self.auto_updates)

        self.prerelease_updates = QCheckBox("Include release candidates")
        self.prerelease_updates.setChecked(settings.value("updates/include_prereleases", "RC" in __version__, type=bool))
        form.addRow("Update channel", self.prerelease_updates)

        output_holder = QWidget()
        output_row = QHBoxLayout(output_holder)
        output_row.setContentsMargins(0, 0, 0, 0)
        self.output = QLineEdit(str(settings.value("preferences/default_output", "") or ""))
        browse = QPushButton("Browse")
        browse.clicked.connect(self._choose_output)
        output_row.addWidget(self.output, 1)
        output_row.addWidget(browse)
        form.addRow("Default output folder", output_holder)

        root.addLayout(form)
        note = QLabel("ImageSuite preserves color profile and DPI only. EXIF, GPS, and camera metadata are intentionally stripped.", objectName="Muted")
        note.setWordWrap(True)
        root.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._defaults)
        root.addWidget(buttons)

    def _choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Default output folder", self.output.text())
        if folder:
            self.output.setText(folder)

    def _defaults(self) -> None:
        self.startup.setCurrentText("Last used")
        self.history_depth.setValue(30)
        self.history_memory.setValue(512)
        self.autosave.setValue(30)
        self.preserve_metadata.setChecked(True)
        self.restore_tabs.setChecked(True)
        self.auto_updates.setChecked(True)
        self.prerelease_updates.setChecked("RC" in __version__)
        self.output.clear()

    def save(self) -> None:
        self.settings.setValue("preferences/startup_workspace", self.startup.currentText())
        self.settings.setValue("preferences/history_depth", self.history_depth.value())
        self.settings.setValue("preferences/history_memory_mb", self.history_memory.value())
        self.settings.setValue("preferences/autosave_seconds", self.autosave.value())
        self.settings.setValue("preferences/preserve_metadata", self.preserve_metadata.isChecked())
        self.settings.setValue("preferences/restore_editor_tabs", self.restore_tabs.isChecked())
        self.settings.setValue("updates/automatic", self.auto_updates.isChecked())
        self.settings.setValue("updates/include_prereleases", self.prerelease_updates.isChecked())
        self.settings.setValue("preferences/default_output", self.output.text().strip())


class JobsPage(QWidget):
    def __init__(self, jobs: JobManager) -> None:
        super().__init__()
        self.jobs = jobs
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 10)
        header = QHBoxLayout()
        header.addWidget(QLabel("BATCH JOBS", objectName="Brand"))
        header.addStretch(1)
        clear = QPushButton("Clear completed")
        clear.clicked.connect(self.clear_completed)
        header.addWidget(clear)
        root.addLayout(header)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Type", "Job", "Status", "Progress", "Detail", "Started", "Finished"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for column, width in enumerate((100, 260, 150, 90, 460, 150, 150)):
            self.table.setColumnWidth(column, width)
        root.addWidget(self.table, 1)
        self.empty = QLabel("Long-running upscale and similarity tasks appear here.", objectName="Muted")
        root.addWidget(self.empty)
        self.jobs.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.table.setRowCount(len(self.jobs.jobs))
        self.empty.setVisible(not self.jobs.jobs)
        for row, job in enumerate(self.jobs.jobs):
            values = [
                job.category,
                job.name,
                job.status,
                f"{job.percent}%",
                job.detail,
                job.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                job.finished_at.strftime("%Y-%m-%d %H:%M:%S") if job.finished_at else "",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))

    def clear_completed(self) -> None:
        self.jobs.jobs = [job for job in self.jobs.jobs if job.finished_at is None and job.status == "Running"]
        self.jobs.changed.emit()


class AboutPage(QWidget):
    def __init__(self, base_dir: Path) -> None:
        super().__init__()
        self.base_dir = base_dir
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.addWidget(QLabel("IMAGESUITE", objectName="Brand"))
        root.addWidget(QLabel(f"Version {__version__}", objectName="Muted"))
        description = QLabel(
            "A unified native PySide6 application combining QuickFX editing, UpMark upscaling and watermarking, and VisualDupe similarity cleanup."
        )
        description.setWordWrap(True)
        root.addWidget(description)
        root.addSpacing(14)
        for text in [
            "Editor: persistent selections, protected face circles, live previews, censor brushes, clone/heal, annotations, adjustments, effects, transforms, multiple documents, recovery and batch presets.",
            "Enhance: fast Pillow resizing, optional Spandrel/PyTorch AI models, GPU/CPU choice, tiling, finishing, watermarks, queues and result review.",
            "Organize: perceptual matching, grouped review, keep-best rules, reversible moves, safe file actions and CSV export.",
        ]:
            label = QLabel(text, objectName="Muted")
            label.setWordWrap(True)
            root.addWidget(label)
        root.addSpacing(12)
        root.addWidget(QLabel("DIAGNOSTICS", objectName="SectionTitle"))
        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.report.setMinimumHeight(190)
        root.addWidget(self.report)
        row = QHBoxLayout()
        copy = QPushButton("Copy diagnostics")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report.toPlainText()))
        logs = QPushButton("Open log folder")
        logs.clicked.connect(lambda: open_folder(app_data_dir() / "logs"))
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.refresh(include_ai=True))
        row.addWidget(copy)
        row.addWidget(logs)
        row.addWidget(refresh)
        row.addStretch(1)
        root.addLayout(row)
        self.refresh(include_ai=False)

    def refresh(self, *, include_ai: bool = True) -> None:
        self.report.setPlainText(diagnostics_report(self.base_dir, include_ai=include_ai))


class MainWindow(QMainWindow):
    RECENT_LIMIT = 10

    def __init__(self, base_dir: Path) -> None:
        super().__init__()
        self.base_dir = base_dir
        self.settings = (
            QSettings(str(base_dir / "portable.ini"), QSettings.IniFormat)
            if (base_dir / "portable.flag").exists()
            else QSettings("Regendx", "ImageSuite")
        )
        self.jobs = JobManager()
        self.update_client = UpdateClient(self)
        self._manual_update_check = False
        self._update_progress: QProgressDialog | None = None
        self.update_client.updateAvailable.connect(self._update_available)
        self.update_client.noUpdateAvailable.connect(self._no_update_available)
        self.update_client.errorOccurred.connect(self._update_error)
        self.update_client.downloadProgress.connect(self._update_download_progress)
        self.update_client.downloadReady.connect(self._update_download_ready)
        self.setWindowTitle(f"ImageSuite {__version__}")
        self.resize(1500, 900)
        self.setMinimumSize(1120, 700)
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_menu()
        self._connect_workflows()
        self._restore_settings()
        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(0, self._restore_workspace_state)
        QTimer.singleShot(2500, self._automatic_update_check)

    def _build_ui(self) -> None:
        root = QWidget(objectName="AppRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        top = QFrame(objectName="TopBar")
        top_row = QHBoxLayout(top)
        top_row.setContentsMargins(12, 7, 12, 7)
        top_row.addWidget(QLabel("IMAGESUITE", objectName="Brand"))
        top_row.addWidget(QLabel("QuickFX · UpMark · VisualDupe", objectName="SubBrand"))
        top_row.addStretch(1)
        self.top_status = QLabel("Ready", objectName="Muted")
        top_row.addWidget(self.top_status)
        layout.addWidget(top)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        sidebar = QFrame(objectName="SideBar")
        sidebar.setFixedWidth(205)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(10, 12, 10, 10)
        side.setSpacing(5)

        self.stack = QStackedWidget()
        self.editor = EditorWorkspace()
        portable_or_source = (self.base_dir / "portable.flag").exists() or not getattr(sys, "frozen", False)
        models_dir = self.base_dir / "models" if portable_or_source else app_data_dir() / "models"
        outputs_dir = self.base_dir / "outputs" if portable_or_source else Path.home() / "Pictures" / "ImageSuite"
        models_dir.mkdir(parents=True, exist_ok=True)
        outputs_dir.mkdir(parents=True, exist_ok=True)
        self.upscale = UpscaleWorkspace(self.jobs, models_dir, outputs_dir, self.base_dir)
        self.similarity = SimilarityWorkspace(self.jobs)
        self.jobs_page = JobsPage(self.jobs)
        self.about = AboutPage(self.base_dir)
        pages = [
            ("Edit", self.editor),
            ("Enhance", self.upscale),
            ("Organize", self.similarity),
            ("Batch Jobs", self.jobs_page),
            ("About", self.about),
        ]
        self.nav_buttons: list[QPushButton] = []
        for index, (name, page) in enumerate(pages):
            self.stack.addWidget(page)
            if index >= 3:
                continue
            button = QPushButton(name, objectName="NavButton")
            button.setAccessibleName(f"{name} workspace")
            button.setCheckable(True)
            button.setToolTip(f"Switch to {name} (Ctrl+{index + 1})")
            button.clicked.connect(lambda _checked=False, i=index: self.switch_page(i))
            side.addWidget(button)
            self.nav_buttons.append(button)
        side.addStretch(1)

        self.more_nav = QToolButton(objectName="NavButton")
        self.more_nav.setText("More")
        self.more_nav.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.more_nav.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(self.more_nav)
        jobs_action = more_menu.addAction("Batch Jobs")
        jobs_action.setShortcut("Ctrl+4")
        jobs_action.triggered.connect(lambda: self.switch_page(3))
        about_action = more_menu.addAction("About")
        about_action.setShortcut("Ctrl+5")
        about_action.triggered.connect(lambda: self.switch_page(4))
        self.more_nav.setMenu(more_menu)
        side.addWidget(self.more_nav)
        side.addWidget(QLabel("Drop once, then choose the task\nonly when the choice is ambiguous", objectName="SubBrand"))
        content.addWidget(sidebar)
        content.addWidget(self.stack, 1)
        layout.addLayout(content, 1)

        status = QFrame(objectName="StatusBar")
        status_row = QHBoxLayout(status)
        status_row.setContentsMargins(8, 2, 8, 2)
        self.status_text = QLabel("Ready", objectName="Status")
        status_row.addWidget(self.status_text, 1)
        self.page_label = QLabel("Editor", objectName="Status")
        status_row.addWidget(self.page_label)
        layout.addWidget(status)
        self.switch_page(0)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        self.open_action = QAction("Open images…", self)
        self.open_action.setShortcut("Ctrl+O")
        self.open_action.triggered.connect(self.open_images)
        file_menu.addAction(self.open_action)
        folder_action = QAction("Open folder…", self)
        folder_action.setShortcut("Ctrl+Alt+O")
        folder_action.triggered.connect(self.open_folder_prompt)
        file_menu.addAction(folder_action)

        self.recent_menu = file_menu.addMenu("Recent")
        self._refresh_recent_menu()
        file_menu.addSeparator()

        self.save_action = QAction("Save", self)
        self.save_action.setShortcut("Ctrl+S")
        self.save_action.triggered.connect(self.editor.save)
        file_menu.addAction(self.save_action)
        self.save_as_action = QAction("Save As…", self)
        self.save_as_action.setShortcut("Ctrl+Shift+S")
        self.save_as_action.triggered.connect(self.editor.save_as)
        file_menu.addAction(self.save_as_action)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Alt+F4")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        navigate = self.menuBar().addMenu("Navigate")
        names = ["Editor", "Enhance", "Organize", "Batch Jobs", "About"]
        for index, name in enumerate(names):
            action = QAction(name, self)
            action.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            action.triggered.connect(lambda _checked=False, i=index: self.switch_page(i))
            navigate.addAction(action)
        navigate.addSeparator()
        palette = QAction("Command palette…", self)
        palette.setShortcut("Ctrl+K")
        palette.triggered.connect(self.show_command_palette)
        navigate.addAction(palette)
        previous = QAction("Previous workspace", self)
        previous.setShortcut("Alt+Left")
        previous.triggered.connect(lambda: self.switch_page((self.stack.currentIndex() - 1) % self.stack.count()))
        navigate.addAction(previous)
        following = QAction("Next workspace", self)
        following.setShortcut("Alt+Right")
        following.triggered.connect(lambda: self.switch_page((self.stack.currentIndex() + 1) % self.stack.count()))
        navigate.addAction(following)

        help_menu = self.menuBar().addMenu("Help")
        shortcuts = QAction("Mouse & keyboard shortcuts", self)
        shortcuts.setShortcut("F1")
        shortcuts.triggered.connect(self.show_shortcuts)
        help_menu.addAction(shortcuts)
        welcome = QAction("Welcome guide", self)
        welcome.triggered.connect(self.show_welcome)
        help_menu.addAction(welcome)
        preferences = QAction("Preferences…", self)
        preferences.triggered.connect(self.show_preferences)
        help_menu.addAction(preferences)
        update_action = QAction("Check for updates…", self)
        update_action.triggered.connect(lambda: self.check_for_updates(manual=True))
        help_menu.addAction(update_action)
        help_menu.addSeparator()
        output_action = QAction("Open output folder", self)
        output_action.triggered.connect(lambda: open_folder(self.upscale.output_edit.text()))
        help_menu.addAction(output_action)
        about_action = QAction("About ImageSuite", self)
        about_action.triggered.connect(lambda: self.switch_page(4))
        help_menu.addAction(about_action)

        self.editor.documentAvailabilityChanged.connect(self._set_document_actions_enabled)
        self._set_document_actions_enabled(False)

    def _connect_workflows(self) -> None:
        self.editor.statusChanged.connect(self.set_status)
        self.upscale.statusChanged.connect(self.set_status)
        self.similarity.statusChanged.connect(self.set_status)
        self.similarity.openInEditor.connect(self.open_in_editor)
        self.upscale.openInEditor.connect(self.open_in_editor)
        self.editor.sendToEnhance.connect(self.add_to_enhance)
        self.editor.pathsOpened.connect(self._remember_images)
        self.upscale.pathsAdded.connect(self._remember_images)
        self.similarity.sourceChanged.connect(self._remember_folder)

    @staticmethod
    def _setting_list(settings: QSettings, key: str) -> list[str]:
        value = settings.value(key, [])
        if isinstance(value, str):
            return [value] if value else []
        return [str(item) for item in (value or []) if str(item)]

    def _remember(self, key: str, path: str | Path, limit: int | None = None) -> None:
        text = str(Path(path))
        values = [text] + [item for item in self._setting_list(self.settings, key) if item != text and Path(item).exists()]
        self.settings.setValue(key, values[: limit or self.RECENT_LIMIT])
        self._refresh_recent_menu()

    def _remember_images(self, paths: list) -> None:
        for path in reversed(paths):
            path = Path(path)
            self._remember("recent/images", path)
            self._remember("recent/folders", path.parent, 6)

    def _remember_folder(self, path: str) -> None:
        if path:
            self._remember("recent/folders", path, 6)

    def _refresh_recent_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.clear()
        images = [path for path in self._setting_list(self.settings, "recent/images") if Path(path).is_file()]
        folders = [path for path in self._setting_list(self.settings, "recent/folders") if Path(path).is_dir()]
        if images:
            image_menu = self.recent_menu.addMenu("Images")
            for path in images:
                action = image_menu.addAction(Path(path).name)
                action.setToolTip(path)
                action.triggered.connect(lambda _checked=False, p=path: self.open_in_editor(p))
        if folders:
            folder_menu = self.recent_menu.addMenu("Folders")
            for path in folders:
                action = folder_menu.addAction(Path(path).name or path)
                action.setToolTip(path)
                action.triggered.connect(lambda _checked=False, p=path: self.show_folder_actions(Path(p)))
        if not images and not folders:
            empty = self.recent_menu.addAction("No recent items")
            empty.setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            clear = self.recent_menu.addAction("Clear recent items")
            clear.triggered.connect(self.clear_recent)

    def clear_recent(self) -> None:
        self.settings.remove("recent/images")
        self.settings.remove("recent/folders")
        self._refresh_recent_menu()

    def open_images(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Open images", "", IMAGE_FILE_FILTER)
        if names:
            self._open_editor_paths([Path(name) for name in names])

    def open_folder_prompt(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose image folder")
        if folder:
            self.show_folder_actions(Path(folder))

    def show_folder_actions(self, folder: Path) -> None:
        if not folder.is_dir():
            return
        self._remember_folder(str(folder))
        menu = QMenu(self)
        edit = menu.addAction("Edit images from this folder")
        enhance = menu.addAction("Enhance this folder")
        organize = menu.addAction("Find similar images")
        selected = menu.exec(QCursor.pos())
        if selected is edit:
            self._open_editor_paths(expand_image_paths([folder]))
        elif selected is enhance:
            self.add_to_enhance(expand_image_paths([folder]))
        elif selected is organize:
            self.similarity.set_source_folder(folder)
            self.switch_page(2)

    def route_paths(self, raw_paths: list[str | Path]) -> None:
        paths = [Path(path) for path in raw_paths]
        folders = [path for path in paths if path.is_dir()]
        if folders:
            menu = QMenu(self)
            edit = menu.addAction("Edit images from dropped folders")
            enhance = menu.addAction("Enhance images from dropped folders")
            organize = menu.addAction("Find similar images in folder") if len(folders) == 1 and len(paths) == 1 else None
            selected = menu.exec(QCursor.pos())
            if selected is organize:
                self.similarity.set_source_folder(folders[0])
                self.switch_page(2)
                return
            if selected not in {edit, enhance}:
                return
            images = expand_image_paths(paths)
            if not images:
                self.set_status("No supported images found")
            elif selected is edit:
                self._open_editor_paths(images)
            else:
                self.add_to_enhance(images)
            return

        images = expand_image_paths(paths)
        if not images:
            self.set_status("No supported images found")
        elif len(images) == 1:
            self._open_editor_paths(images)
        else:
            menu = QMenu(self)
            edit = menu.addAction(f"Edit {len(images)} image(s)")
            enhance = menu.addAction(f"Enhance {len(images)} image(s)")
            selected = menu.exec(QCursor.pos())
            if selected is edit:
                self._open_editor_paths(images)
            elif selected is enhance:
                self.add_to_enhance(images)

    def _open_editor_paths(self, paths: list[Path]) -> None:
        self.editor.load_paths(paths)
        self.switch_page(0)

    def add_to_enhance(self, paths: list) -> None:
        self.upscale.add_paths([Path(path) for path in paths])
        self.switch_page(1)

    def show_command_palette(self) -> None:
        actions = {
            "Open image files": self.open_images,
            "Open a folder": self.open_folder_prompt,
            "Paste image into editor": self.editor.paste_image,
            "Add files to Enhance": self.upscale.add_files,
            "Add folder to Enhance": self.upscale.add_folder,
            "Find similar images": self.similarity.choose_source,
            "Open output folder": lambda: open_folder(self.upscale.output_edit.text()),
            "Preferences": self.show_preferences,
            "Check for updates": lambda: self.check_for_updates(manual=True),
            "Copy diagnostics": lambda: QApplication.clipboard().setText(diagnostics_report(self.base_dir, include_ai=True)),
            "Switch to Edit": lambda: self.switch_page(0),
            "Switch to Enhance": lambda: self.switch_page(1),
            "Switch to Organize": lambda: self.switch_page(2),
        }
        choice, accepted = QInputDialog.getItem(self, "Command palette", "Action", list(actions), 0, False)
        if accepted and choice:
            actions[choice]()

    def _automatic_update_check(self) -> None:
        if not UpdateClient.supported_installation():
            return
        if not self.settings.value("updates/automatic", True, type=bool):
            return
        previous = self.settings.value("updates/last_check")
        if previous:
            checked = QDateTime.fromString(str(previous), Qt.ISODate)
            if checked.isValid() and checked.secsTo(QDateTime.currentDateTimeUtc()) < 24 * 60 * 60:
                return
        self.check_for_updates(manual=False)

    def check_for_updates(self, *, manual: bool = True) -> None:
        self._manual_update_check = manual
        if manual:
            self.set_status("Checking for updates…")
        include_prereleases = self.settings.value("updates/include_prereleases", "RC" in __version__, type=bool)
        self.update_client.check(include_prereleases=include_prereleases)

    def _update_available(self, info: UpdateInfo) -> None:
        self.settings.setValue("updates/last_check", QDateTime.currentDateTimeUtc().toString(Qt.ISODate))
        self.set_status(f"ImageSuite {info.version} is available")
        notes = info.notes.strip()
        if len(notes) > 1800:
            notes = notes[:1800].rstrip() + "…"
        message = f"ImageSuite {info.version} is available.\n\n{notes}" if notes else f"ImageSuite {info.version} is available."
        if info.asset is None:
            result = QMessageBox.question(self, "ImageSuite update", message + "\n\nNo Windows installer is attached. Open the release page?", QMessageBox.Open | QMessageBox.Cancel)
            if result == QMessageBox.Open:
                QDesktopServices.openUrl(QUrl(info.page_url or RELEASES_PAGE_URL))
            return
        result = QMessageBox.question(self, "ImageSuite update", message + "\n\nDownload the installer now?", QMessageBox.Yes | QMessageBox.No)
        if result == QMessageBox.Yes:
            self._update_progress = QProgressDialog("Downloading ImageSuite update…", "Cancel", 0, max(0, info.asset.size), self)
            self._update_progress.setWindowTitle("ImageSuite update")
            self._update_progress.setMinimumDuration(0)
            self._update_progress.canceled.connect(self._cancel_update_download)
            self._update_progress.show()
            self.update_client.download(info)

    def _no_update_available(self) -> None:
        self.settings.setValue("updates/last_check", QDateTime.currentDateTimeUtc().toString(Qt.ISODate))
        self.set_status("ImageSuite is up to date")
        if self._manual_update_check:
            QMessageBox.information(self, "ImageSuite update", f"ImageSuite {__version__} is up to date.")

    def _update_error(self, message: str) -> None:
        if self._update_progress is not None:
            self._update_progress.close()
            self._update_progress = None
        self.set_status("Update check failed")
        if self._manual_update_check:
            QMessageBox.warning(self, "ImageSuite update", f"The update could not be completed.\n\n{message}")

    def _cancel_update_download(self) -> None:
        self.update_client.cancel()
        self._update_progress = None
        self.set_status("Update download canceled")

    def _update_download_progress(self, received: int, total: int) -> None:
        if self._update_progress is None:
            return
        if total > 0:
            self._update_progress.setMaximum(total)
        self._update_progress.setValue(max(0, received))

    def _update_download_ready(self, installer: Path, info: UpdateInfo) -> None:
        if self._update_progress is not None:
            self._update_progress.close()
            self._update_progress = None
        result = QMessageBox.question(
            self,
            "Install ImageSuite update",
            f"ImageSuite {info.version} is ready. Close ImageSuite, install it silently, and reopen?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            self.set_status(f"Update downloaded to {installer.name}")
            return
        if not launch_installer_after_exit(installer):
            QMessageBox.warning(self, "ImageSuite update", "The installer could not be scheduled. Open the downloaded installer manually.")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(installer.parent)))
            return
        QApplication.quit()

    def receive_external_paths(self, paths: list[Path]) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        if paths:
            self.route_paths(paths)

    def show_preferences(self) -> None:
        dialog = PreferencesDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            dialog.save()
            self._apply_preferences()
            self.set_status("Preferences saved")

    def _apply_preferences(self) -> None:
        depth = _setting_int(self.settings, "preferences/history_depth", 30, 5, 100)
        memory = _setting_int(self.settings, "preferences/history_memory_mb", 512, 64, 4096)
        autosave = _setting_int(self.settings, "preferences/autosave_seconds", 30, 10, 600)
        preserve = self.settings.value("preferences/preserve_metadata", True, type=bool)
        self.editor.apply_preferences(depth, memory, autosave, preserve)
        self.upscale.preserve_metadata = preserve
        default_output = str(self.settings.value("preferences/default_output", "") or "")
        if default_output and not self.upscale.output_edit.text().strip():
            self.upscale.output_edit.setText(default_output)

    def show_welcome(self) -> None:
        QMessageBox.information(
            self,
            "Welcome to ImageSuite",
            "ImageSuite has three main workflows:\n\n"
            "1. Edit — open or drop an image, choose an area, then apply an effect or annotation.\n"
            "2. Enhance — queue images, choose resize/AI settings, then review the outputs.\n"
            "3. Organize — scan a folder, review similar groups, then move or recycle duplicates.\n\n"
            "Press F1 at any time for mouse and keyboard navigation. AI support is optional.",
        )

    def _set_document_actions_enabled(self, available: bool) -> None:
        self.save_action.setEnabled(available)
        self.save_as_action.setEnabled(available)

    def show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "ImageSuite navigation",
            "Global\n"
            "• Ctrl+K: command palette\n"
            "• Ctrl+1…5: workspaces\n"
            "• Drop one image: open in Edit\n"
            "• Drop folders or multiple images: choose Edit, Enhance, or Organize\n\n"
            "Editor\n"
            "• Space + drag, middle-drag, or right-drag: pan\n"
            "• Wheel: zoom around pointer; Shift+wheel: horizontal pan\n"
            "• R/L/C/T/S/A/X/P: common tools\n"
            "• Arrows: move active item; Alt+arrows: resize\n\n"
            "Enhance\n"
            "• Delete: remove selected queue items\n"
            "• Ctrl+Enter: start queue; Ctrl+P: preview\n\n"
            "Organize\n"
            "• J/K: next/previous group; Space: toggle checked\n"
            "• Enter: open; E: open in editor; B: select duplicates",
        )

    def switch_page(self, index: int) -> None:
        index = max(0, min(self.stack.count() - 1, index))
        self.stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)
        names = ["Editor", "Enhance & Watermark", "Organize Similar Images", "Batch Jobs", "About"]
        self.page_label.setText(names[index])
        self.set_status(f"{names[index]} workspace")
        page = self.stack.currentWidget()
        if page is self.editor:
            self.editor.canvas.setFocus()
        elif page is self.upscale:
            self.upscale.files.setFocus()
        elif page is self.similarity:
            (self.similarity.group_list if self.similarity.groups else self.similarity.source).setFocus()
        else:
            page.setFocus()

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.MouseButtonPress and hasattr(watched, "window") and watched.window() is self:
            if event.button() in {Qt.BackButton, Qt.ForwardButton}:
                direction = -1 if event.button() == Qt.BackButton else 1
                if self.stack.currentWidget() is self.similarity and self.similarity.groups:
                    self.similarity.navigate_group(direction)
                else:
                    self.switch_page((self.stack.currentIndex() + direction) % self.stack.count())
                return True
        return super().eventFilter(watched, event)

    def set_status(self, text: str) -> None:
        self.status_text.setText(text)
        self.top_status.setText(text)

    def open_in_editor(self, path: str) -> None:
        self._open_editor_paths([Path(path)])

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        raw_paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        self.route_paths(raw_paths)
        event.acceptProposedAction()

    def _restore_settings(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        startup = str(self.settings.value("preferences/startup_workspace", "Last used"))
        if startup == "Last used":
            try:
                page = int(self.settings.value("page", 0))
            except (TypeError, ValueError):
                page = 0
        else:
            page = {"Edit": 0, "Enhance": 1, "Organize": 2}.get(startup, 0)
        self.switch_page(max(0, min(4, page)))
        self._apply_preferences()
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen" and not self.settings.value("welcome_shown", False, type=bool):
            self.settings.setValue("welcome_shown", True)
            QTimer.singleShot(500, self.show_welcome)

    def _restore_workspace_state(self) -> None:
        queue = [Path(path) for path in self._setting_list(self.settings, "session/upscale_queue") if Path(path).is_file()]
        if queue:
            self.upscale.add_paths(queue, select_first=False)
        output = str(self.settings.value("session/output", "") or "")
        if output:
            self.upscale.output_edit.setText(output)
        source = str(self.settings.value("session/similarity_source", "") or "")
        if source and Path(source).is_dir():
            self.similarity.set_source_folder(Path(source), emit=False)
        destination = str(self.settings.value("session/similarity_destination", "") or "")
        if destination:
            self.similarity.destination.setText(destination)
        if self.settings.value("preferences/restore_editor_tabs", True, type=bool):
            paths = [Path(path) for path in self._setting_list(self.settings, "session/editor_paths") if Path(path).is_file()][:20]
            self._editor_restore_queue = paths
            try:
                self._editor_restore_target = int(self.settings.value("session/editor_active", 0))
            except (TypeError, ValueError):
                self._editor_restore_target = 0
            if paths:
                QTimer.singleShot(0, self._restore_next_editor_path)
        tool = str(self.settings.value("session/editor_tool", "select") or "select")
        if tool in self.editor.tool_buttons:
            self.editor.choose_mode(tool)
        self.editor.advanced_toggle.setChecked(self.settings.value("session/editor_advanced", False, type=bool))
        try:
            self.editor.tool_tabs.setCurrentIndex(int(self.settings.value("session/editor_panel", 0)))
        except (TypeError, ValueError):
            self.editor.tool_tabs.setCurrentIndex(0)
        try:
            splitter_sizes = [int(value) for value in self._setting_list(self.settings, "session/editor_splitter_sizes")]
        except (TypeError, ValueError):
            splitter_sizes = []
        if len(splitter_sizes) == 2 and all(value > 0 for value in splitter_sizes):
            self.editor.editor_splitter.setSizes(splitter_sizes)

    def _restore_next_editor_path(self) -> None:
        queue = getattr(self, "_editor_restore_queue", [])
        if not queue:
            if self.editor.documents:
                target = max(0, min(len(self.editor.documents) - 1, getattr(self, "_editor_restore_target", 0)))
                self.editor.document_tabs.setCurrentIndex(target)
                self.editor._activate(target)
            return
        path = queue.pop(0)
        self.editor.load_paths([path])
        QTimer.singleShot(0, self._restore_next_editor_path)

    def _save_workspace_state(self) -> None:
        self.settings.setValue("session/upscale_queue", [str(path) for path in self.upscale.queue_paths()])
        self.settings.setValue("session/output", self.upscale.output_edit.text())
        self.settings.setValue("session/similarity_source", self.similarity.source.text())
        self.settings.setValue("session/similarity_destination", self.similarity.destination.text())
        clean_paths = [str(doc.path) for doc in self.editor.documents if doc.path and not doc.dirty and doc.path.is_file()]
        self.settings.setValue("session/editor_paths", clean_paths[:20])
        self.settings.setValue("session/editor_active", self.editor.active_index)
        self.settings.setValue("session/editor_tool", self.editor.canvas.mode)
        self.settings.setValue("session/editor_advanced", self.editor.advanced_toggle.isChecked())
        self.settings.setValue("session/editor_panel", self.editor.tool_tabs.currentIndex())
        self.settings.setValue("session/editor_splitter_sizes", self.editor.editor_splitter.sizes())

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self.update_client.cancel()
        active_jobs = [workspace for workspace in (self.upscale, self.similarity) if workspace.is_busy()]
        if active_jobs:
            result = QMessageBox.question(
                self,
                "Background work is still running",
                "Cancel the active job(s) and exit? The current image operation may need a moment to finish safely.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if result != QMessageBox.Yes:
                event.ignore()
                return
            if not all(workspace.cancel_and_wait(7000) for workspace in active_jobs):
                QMessageBox.warning(self, "Still working", "ImageSuite is still finishing the current operation. Try exiting again after it stops.")
                event.ignore()
                return
        if not self.editor.prepare_close():
            event.ignore()
            return
        if not self.editor.wait_for_recovery(7000):
            QMessageBox.warning(self, "Recovery is still active", "ImageSuite is still finishing a recovery write. Try exiting again in a moment.")
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("page", self.stack.currentIndex())
        self._save_workspace_state()
        self.editor.finalize_close()
        event.accept()
