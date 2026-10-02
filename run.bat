@echo off
rem Starts the Nexus Mods Auto Downloader. The first run creates a private Python environment.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up for the first time ^(this takes a minute^)...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ and run this again.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Installing the requirements failed.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m nexus_dl
if errorlevel 1 pause
