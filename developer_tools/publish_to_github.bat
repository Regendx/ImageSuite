@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."
where git >nul 2>nul || goto :nogit
if not exist .git (
  git init
  git branch -M main
)
git add -A
git commit -m "Rebuild ImageSuite in PySide6"
git remote get-url origin >nul 2>nul
if errorlevel 1 git remote add origin https://github.com/Regendx/ImageSuite.git
git push -u origin main
if errorlevel 1 goto :failed
echo.
echo ImageSuite was published to GitHub.
pause
exit /b 0
:nogit
echo Git is not installed or is not available in PATH.
echo Install Git for Windows, reopen this folder, and run this file again.
pause
exit /b 1
:failed
echo.
echo Push failed. Sign in through Git Credential Manager or GitHub Desktop, then retry.
pause
exit /b 1
