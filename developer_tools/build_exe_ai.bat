@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."
title Build ImageSuite with AI Support
set "IMAGESUITE_BUILD_AI=1"
call developer_tools\build_exe.bat
exit /b %errorlevel%
