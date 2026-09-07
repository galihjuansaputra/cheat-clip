@echo off
title CHEAT CLIP - Quick Updater
echo ========================================================
echo    CHEAT CLIP - Quick Update (Windows)
echo ========================================================
echo.

where git >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Git is not installed or not in PATH!
    echo Please download and install Git from:
    echo   https://git-scm.com/downloads
    echo.
    pause
    exit /b 1
)

echo [1/3] Pulling latest updates from GitHub...
git pull
if %errorlevel% neq 0 (
    echo [WARNING] Git pull encountered an issue. If you have local changes, stash them or resolve conflicts.
)
echo.

echo [2/3] Checking and updating frontend packages...
call npm install
echo.

echo [3/3] Checking and updating Python backend packages...
python -m pip install -r backend\requirements.txt
echo.

echo ========================================================
echo    Cheat Clip is now up to date!
echo ========================================================
echo You can now double-click "start.bat" to launch the app.
echo.
pause
