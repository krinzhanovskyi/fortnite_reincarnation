@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.ps1 first: powershell -ExecutionPolicy Bypass -File setup.ps1
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m reincarnation %*
if errorlevel 1 pause
