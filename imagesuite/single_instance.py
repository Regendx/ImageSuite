from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "Regendx.ImageSuite"


def encode_paths(paths: list[Path]) -> bytes:
    return json.dumps([str(path) for path in paths]).encode("utf-8") + b"\n"


def decode_paths(data: bytes) -> list[Path]:
    try:
        values = json.loads(data.strip().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    return [Path(value) for value in values if isinstance(value, str)] if isinstance(values, list) else []


def send_to_existing(paths: list[Path], timeout_ms: int = 500) -> bool:
    """Forward command-line paths to an existing ImageSuite process."""
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if not socket.waitForConnected(timeout_ms):
        return False
    payload = encode_paths(paths)
    socket.write(payload)
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    return True


class SingleInstanceServer(QObject):
    pathsReceived = Signal(list)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept_connections)
        if not self.server.listen(SERVER_NAME):
            raise RuntimeError(self.server.errorString())

    def _accept_connections(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.readyRead.connect(lambda current=socket: self._read_socket(current))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._read_socket(socket)

    def _read_socket(self, socket: QLocalSocket) -> None:
        buffered = bytes(socket.property("imagesuite_buffer") or b"") + bytes(socket.readAll())
        if b"\n" not in buffered:
            socket.setProperty("imagesuite_buffer", buffered)
            return
        payload, _separator, remainder = buffered.partition(b"\n")
        socket.setProperty("imagesuite_buffer", remainder)
        self.pathsReceived.emit(decode_paths(payload))
