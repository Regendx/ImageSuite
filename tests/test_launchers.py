from pathlib import Path

from imagesuite import __version__


ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "developer_tools"


def test_runtime_package_has_one_obvious_root_launcher() -> None:
    assert [path.name for path in ROOT.glob("*.bat")] == ["ImageSuite.bat"]
    assert not (ROOT / "run_imagesuite.bat").exists()
    assert not (ROOT / "run_imagesuite_debug.bat").exists()
    assert not (ROOT / "install_ai_support.bat").exists()


def test_single_launcher_creates_environment_after_python_discovery() -> None:
    script = (ROOT / "ImageSuite.bat").read_text(encoding="utf-8")
    assert "call :ensure_environment" in script
    assert ":create_environment" in script
    assert "call %SYSTEM_PY% -m venv" in script
    assert 'if not exist "%PY%" (\n  call :find_python' not in script.replace("\r\n", "\n")


def test_single_launcher_supports_ai_debug_repair_and_legacy_cleanup() -> None:
    script = (ROOT / "ImageSuite.bat").read_text(encoding="utf-8")
    assert '"--install-ai"' in script
    assert '"--debug"' in script
    assert '"--repair"' in script
    assert ":install_ai" in script
    assert "PYTHONFAULTHANDLER" in script
    for legacy in ("run_imagesuite.bat", "run_imagesuite_debug.bat", "install_ai_support.bat", "build_exe.bat"):
        assert legacy in script


def test_runtime_launcher_checks_complete_extraction() -> None:
    script = (ROOT / "ImageSuite.bat").read_text(encoding="utf-8")
    assert 'if not exist "app.py" goto :missing_files' in script
    assert 'if not exist "dependency_check.py" goto :missing_files' in script


def test_developer_launchers_are_grouped_outside_runtime_root() -> None:
    expected = {"build_exe.bat", "build_exe_ai.bat", "build_installer.bat", "build_release.bat", "publish_to_github.bat"}
    assert expected <= {path.name for path in DEV.glob("*.bat")}
    for path in DEV.glob("*.bat"):
        assert 'cd /d "%~dp0\\.."' in path.read_text(encoding="utf-8")


def test_release_build_installs_test_requirements_and_runs_self_check() -> None:
    script = (DEV / "build_exe.bat").read_text(encoding="utf-8")
    assert "-r requirements.txt -r requirements-test.txt" in script
    assert '"%PY%" release_check.py' in script
    assert (ROOT / "ImageSuite.spec").is_file()
    assert (ROOT / "version_info.txt").is_file()


def test_release_scripts_use_the_same_candidate_version() -> None:
    expected = __version__.replace(" ", "-")
    assert expected in (DEV / "build_release.bat").read_text(encoding="utf-8")
    assert expected in (ROOT / "installer.iss").read_text(encoding="utf-8")


def test_release_metadata_matches_runtime_version() -> None:
    base, candidate = __version__.split(" RC", 1)
    pep440 = f"{base}rc{candidate}"
    assert f'# ImageSuite {__version__}' in (ROOT / "README.md").read_text(encoding="utf-8")
    assert f'version = "{pep440}"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert __version__ in (ROOT / "version_info.txt").read_text(encoding="utf-8")
    assert __version__ in (ROOT / "installer.iss").read_text(encoding="utf-8")


def test_core_requirements_include_animation_runtime_dependencies() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "imageio>=" in requirements
    assert "imageio-ffmpeg>=" in requirements


def test_launcher_uses_explicit_dependency_diagnostics() -> None:
    script = (ROOT / "ImageSuite.bat").read_text(encoding="utf-8")
    assert 'dependency_check.py --packages-only' in script
    assert 'dependency_check.py' in script
    assert '--prefer-binary' in script
    assert '--no-cache-dir' in script
    assert (ROOT / "dependency_check.py").is_file()


def test_dependency_check_covers_every_core_import() -> None:
    import dependency_check

    modules = {module for _display, module in dependency_check.CORE_DEPENDENCIES}
    assert {"PySide6", "PIL", "numpy", "send2trash", "imageio", "imageio_ffmpeg"} <= modules


def test_dependency_check_reports_exact_missing_module(monkeypatch) -> None:
    import dependency_check

    real_import = dependency_check.importlib.import_module

    def fake_import(name: str):
        if name == "imageio":
            raise ModuleNotFoundError("No module named 'imageio'")
        return real_import(name)

    monkeypatch.setattr(dependency_check.importlib, "import_module", fake_import)
    errors = dependency_check.check_modules((("ImageIO", "imageio"),))
    assert errors == ["ImageIO (imageio): ModuleNotFoundError: No module named 'imageio'"]
