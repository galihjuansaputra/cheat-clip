@echo off
title CHEAT CLIP - Quick Setup
echo ========================================================
echo    CHEAT CLIP - Automatic Installer (Windows)
echo ========================================================
echo.

:: 1. Check Node.js
where node >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Node.js is not installed!
    echo Please download and install Node.js (LTS version) from:
    echo   https://nodejs.org/
    echo.
    echo Press any key to exit...
    pause >nul
    exit /b 1
)
echo [OK] Node.js is installed.

:: 2. Check Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please download and install Python from:
    echo   https://www.python.org/downloads/
    echo IMPORTANT: Make sure to check "Add Python to PATH" during installation!
    echo.
    echo Press any key to exit...
    pause >nul
    exit /b 1
)
echo [OK] Python is installed.
echo.

:: 3. Install frontend dependencies
echo [1/3] Installing frontend dependencies (npm install)...
call npm install
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install npm dependencies.
    pause
    exit /b 1
)
echo.

:: 4. Install backend dependencies
echo [2/3] Installing Python dependencies (pip install)...
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Python dependencies.
    pause
    exit /b 1
)
echo.

:: 5. Copy .env if not exists
echo [3/3] Checking environment configuration...
if not exist "backend\.env" (
    if exist "backend\.env.template" (
        copy "backend\.env.template" "backend\.env" >nul
        echo [OK] Created backend\.env from template.
    )
)

echo.
echo ========================================================
echo    Setup Complete! You are ready to go!
echo ========================================================
echo.
echo To start Cheat Clip:
echo   1. Double-click "start.bat"
echo      OR
echo   2. Run "npm run dev" in this folder
echo.
pause
