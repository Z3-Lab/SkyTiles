@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please install build dependencies first. See README.md.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" scripts\package_windows.py
pause
