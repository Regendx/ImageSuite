@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."
set "VERSION=0.9.0-RC33"
call developer_tools\build_exe.bat --no-pause
if errorlevel 1 exit /b 1

if exist "release\ImageSuite-Portable-v%VERSION%" rmdir /s /q "release\ImageSuite-Portable-v%VERSION%"
mkdir "release\ImageSuite-Portable-v%VERSION%"
xcopy "dist\ImageSuite\*" "release\ImageSuite-Portable-v%VERSION%\" /E /I /Y >nul
type nul > "release\ImageSuite-Portable-v%VERSION%\portable.flag"
copy /Y README.md "release\ImageSuite-Portable-v%VERSION%\README.md" >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'release\ImageSuite-Portable-v%VERSION%\*' -DestinationPath 'release\ImageSuite-Portable-v%VERSION%.zip' -Force"
if errorlevel 1 exit /b 1

echo.
echo Portable build created: release\ImageSuite-Portable-v%VERSION%.zip
echo Run developer_tools\build_installer.bat after installing Inno Setup 6 to create the installer.
pause
