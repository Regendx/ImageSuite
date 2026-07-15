from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from imagesuite import single_instance
from imagesuite.updater import (
    select_update,
    updater_script,
    verify_asset_digest,
    version_key,
)


def test_version_comparison_orders_release_candidates_and_stable_versions() -> None:
    assert version_key("v0.9.0-RC33") > version_key("0.9.0 RC32")
    assert version_key("0.9.0") > version_key("0.9.0 RC999")
    assert version_key("1.0.0 RC1") > version_key("0.9.9")


def test_select_update_prefers_newest_eligible_installer() -> None:
    releases = [
        {
            "tag_name": "v0.9.0-RC33",
            "name": "RC33",
            "prerelease": True,
            "assets": [{"name": "ImageSuite-Setup-v0.9.0-RC33.exe", "browser_download_url": "https://example/rc33.exe", "size": 123}],
        },
        {
            "tag_name": "v0.9.0-RC34",
            "name": "RC34",
            "prerelease": True,
            "assets": [{"name": "source.zip", "browser_download_url": "https://example/source.zip"}],
        },
    ]
    update = select_update(releases, current_version="0.9.0 RC32", include_prereleases=True)
    assert update is not None
    assert update.version == "0.9.0-RC34"
    assert update.asset is None
    assert select_update(releases, current_version="0.9.0 RC32", include_prereleases=False) is None


def test_update_digest_verification(tmp_path: Path) -> None:
    payload = b"ImageSuite update"
    target = tmp_path / "setup.exe"
    target.write_bytes(payload)
    digest = "sha256:" + sha256(payload).hexdigest()
    assert verify_asset_digest(target, digest)
    assert not verify_asset_digest(target, "sha256:" + "0" * 64)


def test_update_script_waits_runs_silent_installer_and_reopens(tmp_path: Path) -> None:
    script = updater_script(tmp_path / "setup.exe", tmp_path / "ImageSuite.exe", 321)
    assert "PID eq 321" in script
    assert "/VERYSILENT" in script
    assert "/CLOSEAPPLICATIONS" in script
    assert "ImageSuite.exe" in script


def test_single_instance_path_payload_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "sample image.png"
    payload = single_instance.encode_paths([source])
    assert single_instance.decode_paths(payload) == [source]
    assert single_instance.decode_paths(b"not-json") == []


def test_installer_defines_dedicated_explorer_verbs() -> None:
    installer = (Path(__file__).parents[1] / "installer.iss").read_text(encoding="utf-8")
    assert "Open in ImageSuite" in installer
    assert "SystemFileAssociations\\image\\shell\\ImageSuite" in installer
    assert "SystemFileAssociations\\video\\shell\\ImageSuite" in installer
    assert "Directory\\shell\\ImageSuite" in installer
    assert "OpenWith" not in installer
    assert "ImageSuite-Setup-v0.9.0-RC33" in installer


def test_release_workflow_builds_and_publishes_installer() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "PyInstaller" in workflow
    assert "ISCC.exe" in workflow
    assert "softprops/action-gh-release" in workflow
    assert "release/*.exe" in workflow
