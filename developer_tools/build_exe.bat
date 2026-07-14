@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."
title Build ImageSuite

set "VENV=%CD%\.build_venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "app.py" goto :missing_files
if not exist "ImageSuite.spec" goto :missing_files

call :ensure_environment
if errorlevel 1 exit /b 1

"%PY%" -m pip install --upgrade pip pyinstaller
if errorlevel 1 goto :fail
"%PY%" -m pip install -r requirements.txt -r requirements-test.txt
if errorlevel 1 goto :fail
if defined IMAGESUITE_BUILD_AI (
  "%PY%" -m pip install -r requirements-ai.txt
  if errorlevel 1 goto :fail
  "%PY%" -c "import torch, spandrel, safetensors"
  if errorlevel 1 goto :fail
)
"%PY%" release_check.py
if errorlevel 1 goto :fail
"%PY%" -m pytest -q
if errorlevel 1 goto :fail
"%PY%" -m PyInstaller --noconfirm --clean ImageSuite.spec
if errorlevel 1 goto :fail

if not exist "dist\ImageSuite\ImageSuite.exe" goto :fail
copy /Y README.md "dist\ImageSuite\README.md" >nul
if exist CHANGELOG.md copy /Y CHANGELOG.md "dist\ImageSuite\CHANGELOG.md" >nul
if exist RELEASE_CHECKLIST.md copy /Y RELEASE_CHECKLIST.md "dist\ImageSuite\RELEASE_CHECKLIST.md" >nul

echo.
echo Build complete: dist\ImageSuite\ImageSuite.exe
if /I not "%~1"=="--no-pause" pause
exit /b 0

:ensure_environment
if not exist "%PY%" goto :create_environment
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 exit /b 0
rmdir /s /q "%VENV%" >nul 2>nul

:create_environment
call :find_python
if errorlevel 1 goto :no_python
call %SYSTEM_PY% -m venv "%VENV%"
if errorlevel 1 goto :fail
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
echo ERROR: The complete ImageSuite source was not found. Extract the ZIP first.
if /I not "%~1"=="--no-pause" pause
exit /b 1

:no_python
echo ERROR: Python 3.10 or newer was not found.
if /I not "%~1"=="--no-pause" pause
exit /b 1

:fail
echo.
echo Build failed. Review the error above.
if /I not "%~1"=="--no-pause" pause
exit /b 1
