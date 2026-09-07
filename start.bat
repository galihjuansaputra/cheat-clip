@echo off
title CHEAT CLIP - Launcher
echo ========================================================
echo    Starting CHEAT CLIP...
echo ========================================================
echo.
echo Starting Frontend (http://localhost:5173) and Backend (http://localhost:8000)...
echo Press Ctrl+C in this window anytime to stop.
echo.

:: Launch default web browser after 3 seconds
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:5173"

:: Start both frontend and backend concurrently
call npm run dev

pause
