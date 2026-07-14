from __future__ import annotations

from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
import logging
import platform
import sys
import threading
import time
import traceback

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
from PIL import __version__ as pillow_version

from imagesuite import __version__
from imagesuite.utils import app_data_dir

_LOGGER = logging.getLogger("imagesuite")
_RECENT_ERRORS: deque[tuple[str, float]] = deque(maxlen=20)
_EXCEPTION_BRIDGE: "_ExceptionBridge | None" = None


class _ExceptionBridge(QObject):
    reportReady = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.reportReady.connect(_show_exception_dialog, Qt.QueuedConnection)


def configure_logging() -> Path:
    log_dir = app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "imagesuite.log"
    if not _LOGGER.handlers:
        _LOGGER.setLevel(logging.INFO)
        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _LOGGER.addHandler(handler)
        _LOGGER.propagate = False
    _LOGGER.info("ImageSuite %s started", __version__)
    return log_path


def diagnostics_report(base_dir: Path | None = None, *, include_ai: bool = False) -> str:
    lines = [
        f"ImageSuite: {__version__}",
        f"Python: {platform.python_version()} ({sys.executable})",
        f"PySide6: {pyside_version}",
        f"Pillow: {pillow_version}",
        f"OS: {platform.platform()}",
        f"Architecture: {platform.machine()}",
        f"Portable mode: {'yes' if base_dir and (base_dir / 'portable.flag').exists() else 'no'}",
        f"Data folder: {app_data_dir()}",
    ]
    try:
        import numpy
        lines.append(f"NumPy: {numpy.__version__}")
    except Exception:
        lines.append("NumPy: unavailable")
    torch = sys.modules.get("torch")
    if include_ai and torch is None:
        try:
            import torch as imported_torch
            torch = imported_torch
        except Exception:
            lines.append("PyTorch/AI: not installed")
    if torch is not None:
        try:
            cuda_available = bool(torch.cuda.is_available())
            lines.append(f"PyTorch: {torch.__version__}")
            lines.append(f"CUDA available: {cuda_available}")
            if cuda_available:
                lines.append(f"CUDA device: {torch.cuda.get_device_name(0)}")
        except Exception:
            lines.append("PyTorch/AI: installed, diagnostics unavailable")
    elif not include_ai:
        lines.append("PyTorch/AI: not checked (use Refresh to check)")
    return "\n".join(lines)


def _show_exception_dialog(report: str) -> None:
    app = QApplication.instance()
    if app is None:
        return
    box = QMessageBox()
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle("ImageSuite encountered an error")
    box.setText("That action could not be completed. Your open image has not been discarded.")
    box.setInformativeText("A diagnostic log was written. You can copy the report below when reporting the bug.")
    box.setDetailedText(report)
    copy_button = QPushButton("Copy error report")
    box.addButton(copy_button, QMessageBox.ActionRole)
    box.addButton(QMessageBox.Ok)
    box.exec()
    if box.clickedButton() is copy_button:
        QApplication.clipboard().setText(report)


def handle_exception(exc_type, exc_value, exc_tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    signature = f"{exc_type.__name__}:{exc_value}:{traceback.extract_tb(exc_tb)[-1] if exc_tb else ''}"
    now = time.monotonic()
    duplicate = any(sig == signature and now - when < 5 for sig, when in _RECENT_ERRORS)
    _RECENT_ERRORS.append((signature, now))
    _LOGGER.error("Unhandled exception\n%s", text)
    if not duplicate:
        report = f"{diagnostics_report(include_ai=False)}\n\nTime: {datetime.now().isoformat(timespec='seconds')}\n\n{text}"
        if _EXCEPTION_BRIDGE is not None:
            _EXCEPTION_BRIDGE.reportReady.emit(report)
        else:
            _show_exception_dialog(report)


def install_exception_handlers() -> None:
    global _EXCEPTION_BRIDGE
    sys.excepthook = handle_exception
    app = QApplication.instance()
    if app is not None and _EXCEPTION_BRIDGE is None:
        _EXCEPTION_BRIDGE = _ExceptionBridge(app)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        handle_exception(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = thread_hook


def log_warning(message: str, *args) -> None:
    _LOGGER.warning(message, *args)


def log_exception(message: str) -> None:
    _LOGGER.exception(message)


def show_operation_error(parent, title: str, summary: str, details: str = "") -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle(title)
    box.setText(summary)
    if details and details.strip() != summary.strip():
        box.setDetailedText(details)
    box.setStandardButtons(QMessageBox.Ok)
    box.exec()
