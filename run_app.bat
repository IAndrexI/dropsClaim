@echo off
rem ===========================================================
rem Auto Loot Claimer - Windows Desktop Application Launcher
rem Opens the dashboard as a standalone windowed desktop application
rem ===========================================================

setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    python scripts\desktop_app.py %*
    goto :done
)

where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    py scripts\desktop_app.py %*
    goto :done
)

rem Fallback: If Python is not on PATH, attempt direct Chromium/Edge app launch
echo [NOTICE] Python not found on PATH. Attempting direct browser app launch...
start "" msedge --app=http://localhost:8080 --window-size=1280,840

:done
endlocal
