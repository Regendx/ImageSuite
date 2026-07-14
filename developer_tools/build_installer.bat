@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."
if not exist "dist\ImageSuite\ImageSuite.exe" call developer_tools\build_exe.bat --no-pause
if not exist "dist\ImageSuite\ImageSuite.exe" exit /b 1
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 was not found. Install it, then run this script again.
  pause
  exit /b 1
)
"%ISCC%" installer.iss
if errorlevel 1 exit /b 1
echo Installer created in release\
pause
