from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from imagesuite import __version__
from imagesuite.diagnostics import configure_logging, install_exception_handlers
from imagesuite.main_window import MainWindow
from imagesuite.single_instance import SingleInstanceServer, send_to_existing
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

    startup_paths = [Path(arg) for arg in sys.argv[1:] if Path(arg).exists()]
    if send_to_existing(startup_paths):
        return 0

    try:
        instance_server = SingleInstanceServer(app)
    except RuntimeError:
        if send_to_existing(startup_paths, timeout_ms=1200):
            return 0
        raise
    base_dir = application_dir()
    window = MainWindow(base_dir)
    window._single_instance_server = instance_server
    instance_server.pathsReceived.connect(window.receive_external_paths)
    icon_path = base_dir / "resources" / "imagesuite.ico"
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()

    if startup_paths:
        QTimer.singleShot(0, lambda: window.route_paths(startup_paths))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
