from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from imagesuite import __version__
from imagesuite.diagnostics import configure_logging, install_exception_handlers
from imagesuite.main_window import MainWindow
from imagesuite.theme import APP_QSS


def application_dir() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def main() -> int:
    configure_logging()
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    install_exception_handlers()
    app.setApplicationName("ImageSuite")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Regendx")
    app.setStyle("Fusion")
    app.setStyleSheet(APP_QSS)

    base_dir = application_dir()
    window = MainWindow(base_dir)
    icon_path = base_dir / "resources" / "imagesuite.ico"
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()

    startup_paths = [Path(arg) for arg in sys.argv[1:] if Path(arg).exists()]
    if startup_paths:
        QTimer.singleShot(0, lambda: window.route_paths(startup_paths))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
