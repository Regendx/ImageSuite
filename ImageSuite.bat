@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title ImageSuite

set "MODE=%~1"
set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PIP_LOG=%TEMP%\ImageSuite-pip-install.log"
set "AI_LOG=%TEMP%\ImageSuite-ai-install.log"
set "PAUSE_ON_EXIT=0"

call :cleanup_legacy

if /I "%MODE%"=="--debug" (
  set "PYTHONFAULTHANDLER=1"
  set "QT_LOGGING_RULES=qt.qpa.*=true"
  set "PAUSE_ON_EXIT=1"
)

if /I "%MODE%"=="--repair" (
  echo Removing the existing ImageSuite environment...
  rmdir /s /q "%VENV%" >nul 2>nul
  set "MODE="
)

if not exist "app.py" goto :missing_files
if not exist "requirements.txt" goto :missing_files
if not exist "dependency_check.py" goto :missing_files

call :ensure_environment
if errorlevel 1 exit /b 1

call :ensure_dependencies
if errorlevel 1 exit /b 1

if /I "%MODE%"=="--install-ai" goto :install_ai

echo Starting ImageSuite...
"%PY%" app.py
set "APP_EXIT=%ERRORLEVEL%"
if "%APP_EXIT%"=="0" (
  if "%PAUSE_ON_EXIT%"=="1" (
    echo.
    echo ImageSuite exited normally.
    pause
  )
  exit /b 0
)

echo.
echo ERROR: ImageSuite closed because of an application error.
echo The traceback above identifies the failing component.
pause
exit /b %APP_EXIT%

:cleanup_legacy
for %%F in (
  "run_imagesuite.bat"
  "run_imagesuite_debug.bat"
  "install_ai_support.bat"
  "build_exe.bat"
  "build_exe_ai.bat"
  "build_installer.bat"
  "build_release.bat"
  "publish_to_github.bat"
) do if exist "%%~F" del /q "%%~F" >nul 2>nul
exit /b 0

:ensure_environment
if not exist "%PY%" goto :create_environment
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 exit /b 0

echo The existing ImageSuite environment is damaged or outdated.
echo Rebuilding it...
rmdir /s /q "%VENV%" >nul 2>nul

:create_environment
call :find_python
if errorlevel 1 goto :no_python

echo Found Python:
call %SYSTEM_PY% --version

echo Creating ImageSuite environment...
call %SYSTEM_PY% -m venv "%VENV%"
if errorlevel 1 goto :env_fail
exit /b 0

:ensure_dependencies
"%PY%" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo Preparing pip...
  "%PY%" -m ensurepip --upgrade
  if errorlevel 1 goto :env_fail
)

"%PY%" dependency_check.py --quiet >nul 2>nul
if not errorlevel 1 exit /b 0

echo.
echo ImageSuite found missing or broken dependencies:
"%PY%" dependency_check.py --packages-only

echo.
echo Installing or repairing ImageSuite dependencies...
del /q "%PIP_LOG%" >nul 2>nul
"%PY%" -m pip --log "%PIP_LOG%" install --disable-pip-version-check --upgrade pip setuptools wheel
if errorlevel 1 echo WARNING: pip could not update itself. Continuing with the installed pip version.

"%PY%" -m pip --log "%PIP_LOG%" install --disable-pip-version-check --prefer-binary -r requirements.txt
if not errorlevel 1 goto :verify_dependencies

echo.
echo The first package-install attempt failed. Retrying without pip's cache...
"%PY%" -m pip --log "%PIP_LOG%" install --disable-pip-version-check --prefer-binary --no-cache-dir -r requirements.txt
if errorlevel 1 goto :dependency_fail

:verify_dependencies
echo.
echo Verifying ImageSuite...
"%PY%" dependency_check.py
if errorlevel 1 goto :dependency_import_fail
exit /b 0

:install_ai
if not exist "requirements-ai.txt" goto :missing_ai_files
title ImageSuite AI Setup
echo.
echo Installing or repairing optional AI support...
del /q "%AI_LOG%" >nul 2>nul
"%PY%" -m pip --log "%AI_LOG%" install --disable-pip-version-check --prefer-binary -r requirements-ai.txt
if not errorlevel 1 goto :verify_ai

echo.
echo The first AI install attempt failed. Retrying without pip's cache...
"%PY%" -m pip --log "%AI_LOG%" install --disable-pip-version-check --prefer-binary --no-cache-dir -r requirements-ai.txt
if errorlevel 1 goto :ai_install_fail

:verify_ai
echo.
echo Verifying AI support...
"%PY%" dependency_check.py --ai
if errorlevel 1 goto :ai_import_fail

echo.
echo AI support installed successfully.
echo Restart ImageSuite, choose an AI model, and use Check AI.
pause
exit /b 0

:find_python
set "SYSTEM_PY="
where py >nul 2>nul
if not errorlevel 1 (
  for %%V in (3.13 3.12 3.11 3.10) do (
    py -%%V -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 (
      set "SYSTEM_PY=py -%%V"
      exit /b 0
    )
  )
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "SYSTEM_PY=py -3"
    exit /b 0
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "SYSTEM_PY=python"
    exit /b 0
  )
)
where python3 >nul 2>nul
if not errorlevel 1 (
  python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "SYSTEM_PY=python3"
    exit /b 0
  )
)
exit /b 1

:missing_files
echo.
echo ERROR: The ImageSuite package is incomplete.
echo app.py, requirements.txt, and dependency_check.py must be beside ImageSuite.bat.
echo Extract the complete ZIP before running it.
pause
exit /b 1

:missing_ai_files
echo.
echo ERROR: requirements-ai.txt is missing from this ImageSuite package.
pause
exit /b 1

:no_python
echo.
echo ERROR: Python 3.10 or newer was not found.
echo Install Python with pip enabled, then run ImageSuite.bat again.
pause
exit /b 1

:env_fail
echo.
echo ERROR: ImageSuite could not create or prepare its Python environment.
echo Run ImageSuite.bat --repair, or repair your Python installation.
pause
exit /b 1

:dependency_fail
echo.
echo ERROR: pip could not install one or more ImageSuite packages.
echo Detailed pip log: %PIP_LOG%
echo.
echo Repair command:
echo ImageSuite.bat --repair
pause
exit /b 1

:dependency_import_fail
echo.
echo ERROR: Packages installed, but ImageSuite still could not import them.
echo The exact missing module or DLL is shown above.
echo Detailed pip log: %PIP_LOG%
echo Repair command: ImageSuite.bat --repair
pause
exit /b 1

:ai_install_fail
echo.
echo ERROR: Optional AI support could not be installed.
echo Review the exact pip error above.
echo Detailed AI log: %AI_LOG%
pause
exit /b 1

:ai_import_fail
echo.
echo ERROR: AI packages installed, but one or more could not be imported.
echo The exact missing module or DLL is shown above.
echo Detailed AI log: %AI_LOG%
pause
exit /b 1
