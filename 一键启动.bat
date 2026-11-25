@echo off
setlocal
cd /d "%~dp0"
if exist "release\SkyTiles.exe" (
    start "" "release\SkyTiles.exe"
    exit /b
)
if not exist ".venv\Scripts\python.exe" (
    echo Please install dependencies first. See README.md.
    pause
    exit /b 1
)
if not exist "dist\index.html" (
    call npm run build
    if errorlevel 1 (pause & exit /b 1)
)
".venv\Scripts\python.exe" launcher.py
if errorlevel 1 pause
