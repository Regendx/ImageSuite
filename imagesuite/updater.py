from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable

from PySide6.QtCore import QFile, QObject, QSaveFile, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from imagesuite import __version__
from imagesuite.utils import app_data_dir

GITHUB_REPOSITORY = "Regendx/ImageSuite"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases?per_page=20"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"
_VERSION_RE = re.compile(r"(?i)^v?(\d+)\.(\d+)\.(\d+)(?:[\s._-]*rc[\s._-]*(\d+))?$")


@dataclass(frozen=True)
class UpdateAsset:
    name: str
    url: str
    size: int = 0
    digest: str = ""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    title: str
    notes: str
    page_url: str
    prerelease: bool
    asset: UpdateAsset | None


def version_key(value: str) -> tuple[int, int, int, int, int]:
    """Return a key where a stable release beats an RC of the same base."""
    match = _VERSION_RE.match(str(value).strip())
    if not match:
        raise ValueError(f"Unsupported version string: {value}")
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    rc_text = match.group(4)
    return major, minor, patch, 1 if rc_text is None else 0, int(rc_text or 0)


def is_newer_version(candidate: str, current: str = __version__) -> bool:
    try:
        return version_key(candidate) > version_key(current)
    except ValueError:
        return False


def _asset_from_release(release: dict[str, Any]) -> UpdateAsset | None:
    candidates: list[UpdateAsset] = []
    for raw in release.get("assets") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        lowered = name.lower()
        if not lowered.endswith(".exe") or "imagesuite" not in lowered or "setup" not in lowered:
            continue
        url = str(raw.get("browser_download_url") or "")
        if not url:
            continue
        candidates.append(UpdateAsset(name, url, max(0, int(raw.get("size") or 0)), str(raw.get("digest") or "")))
    if not candidates:
        return None
    candidates.sort(key=lambda asset: ("x64" not in asset.name.lower(), "arm64" in asset.name.lower(), asset.name.lower()))
    return candidates[0]


def select_update(
    releases: Iterable[dict[str, Any]],
    *,
    current_version: str = __version__,
    include_prereleases: bool = True,
) -> UpdateInfo | None:
    best: tuple[tuple[int, int, int, int, int], UpdateInfo] | None = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        prerelease = bool(release.get("prerelease"))
        if prerelease and not include_prereleases:
            continue
        tag = str(release.get("tag_name") or release.get("name") or "").strip()
        if not is_newer_version(tag, current_version):
            continue
        info = UpdateInfo(
            version=tag.removeprefix("v"),
            title=str(release.get("name") or tag),
            notes=str(release.get("body") or ""),
            page_url=str(release.get("html_url") or RELEASES_PAGE_URL),
            prerelease=prerelease,
            asset=_asset_from_release(release),
        )
        key = version_key(tag)
        if best is None or key > best[0]:
            best = key, info
    return best[1] if best else None


def verify_asset_digest(path: Path, digest: str) -> bool:
    if not digest:
        return True
    algorithm, separator, expected = digest.partition(":")
    if separator != ":" or algorithm.lower() != "sha256" or not expected:
        return False
    hasher = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest().lower() == expected.lower()


def updater_script(installer: Path, executable: Path, pid: int) -> str:
    installer_text = str(installer).replace('"', '""')
    executable_text = str(executable).replace('"', '""')
    return (
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        ":wait_for_imagesuite\r\n"
        f'tasklist /FI "PID eq {int(pid)}" 2>NUL | findstr /R /C:"[ ]{int(pid)}[ ]" >NUL\r\n'
        "if not errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >NUL\r\n"
        "  goto wait_for_imagesuite\r\n"
        ")\r\n"
        f'start "" /wait "{installer_text}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS\r\n'
        f'if exist "{executable_text}" start "" "{executable_text}"\r\n'
        f'del /q "{installer_text}" >NUL 2>NUL\r\n'
        'del /q "%~f0" >NUL 2>NUL\r\n'
    )


def launch_installer_after_exit(installer: Path, executable: Path | None = None) -> bool:
    if os.name != "nt" or not installer.is_file():
        return False
    executable = Path(executable or sys.executable).resolve()
    script_path = Path(tempfile.gettempdir()) / f"ImageSuite-update-{os.getpid()}.cmd"
    script_path.write_text(updater_script(installer.resolve(), executable, os.getpid()), encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(["cmd.exe", "/d", "/c", "start", "", "/min", str(script_path)], close_fds=True, creationflags=creationflags)
    return True


class UpdateClient(QObject):
    updateAvailable = Signal(object)
    noUpdateAvailable = Signal()
    errorOccurred = Signal(str)
    downloadProgress = Signal(int, int)
    downloadReady = Signal(object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._save_file: QSaveFile | None = None
        self._download_info: UpdateInfo | None = None
        self._download_path: Path | None = None

    @staticmethod
    def supported_installation() -> bool:
        return os.name == "nt" and bool(getattr(sys, "frozen", False))

    @staticmethod
    def _request(url: str) -> QNetworkRequest:
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setRawHeader(b"X-GitHub-Api-Version", b"2022-11-28")
        request.setRawHeader(b"User-Agent", f"ImageSuite/{__version__.replace(' ', '-')}".encode("ascii", "ignore"))
        return request

    def check(self, *, include_prereleases: bool = True) -> None:
        self.cancel()
        reply = self.manager.get(self._request(RELEASES_API_URL))
        self._reply = reply
        reply.finished.connect(lambda: self._check_finished(reply, include_prereleases))

    def _check_finished(self, reply: QNetworkReply, include_prereleases: bool) -> None:
        if reply is not self._reply:
            reply.deleteLater()
            return
        self._reply = None
        try:
            if reply.error() != QNetworkReply.NoError:
                raise RuntimeError(reply.errorString())
            payload = json.loads(bytes(reply.readAll()).decode("utf-8"))
            if not isinstance(payload, list):
                raise RuntimeError("GitHub returned an unexpected update response.")
            info = select_update(payload, include_prereleases=include_prereleases)
            self.updateAvailable.emit(info) if info else self.noUpdateAvailable.emit()
        except Exception as exc:
            self.errorOccurred.emit(str(exc))
        finally:
            reply.deleteLater()

    def download(self, info: UpdateInfo) -> None:
        if info.asset is None:
            self.errorOccurred.emit("This release does not include a Windows installer asset.")
            return
        self.cancel()
        updates_dir = app_data_dir() / "updates"
        updates_dir.mkdir(parents=True, exist_ok=True)
        destination = updates_dir / info.asset.name
        save_file = QSaveFile(str(destination))
        if not save_file.open(QFile.WriteOnly):
            self.errorOccurred.emit(f"Could not create {destination.name}.")
            return
        self._save_file, self._download_info, self._download_path = save_file, info, destination
        reply = self.manager.get(self._request(info.asset.url))
        self._reply = reply
        reply.readyRead.connect(lambda: self._download_chunk(reply))
        reply.downloadProgress.connect(self.downloadProgress)
        reply.finished.connect(lambda: self._download_finished(reply))

    def _download_chunk(self, reply: QNetworkReply) -> None:
        if reply is self._reply and self._save_file is not None:
            self._save_file.write(reply.readAll())

    def _download_finished(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            reply.deleteLater()
            return
        self._download_chunk(reply)
        self._reply = None
        save_file, info, destination = self._save_file, self._download_info, self._download_path
        self._save_file = self._download_info = self._download_path = None
        try:
            if save_file is None or info is None or destination is None:
                raise RuntimeError("The update download was not initialized correctly.")
            if reply.error() != QNetworkReply.NoError:
                save_file.cancelWriting()
                raise RuntimeError(reply.errorString())
            if not save_file.commit():
                raise RuntimeError("The downloaded installer could not be finalized.")
            if not verify_asset_digest(destination, info.asset.digest if info.asset else ""):
                destination.unlink(missing_ok=True)
                raise RuntimeError("The downloaded update failed its SHA-256 integrity check.")
            self.downloadReady.emit(destination, info)
        except Exception as exc:
            if save_file is not None:
                save_file.cancelWriting()
            self.errorOccurred.emit(str(exc))
        finally:
            reply.deleteLater()

    def cancel(self) -> None:
        if self._reply is not None:
            self._reply.abort()
            self._reply.deleteLater()
            self._reply = None
        if self._save_file is not None:
            self._save_file.cancelWriting()
            self._save_file = None
        self._download_info = self._download_path = None
