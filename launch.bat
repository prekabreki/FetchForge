@echo off
title FetchForge - YouTube Downloader and H.265 Converter
cd /d "%~dp0"

set "PATH=%~dp0fetchforge\_internal;%PATH%"

if not defined PYTHON set "PYTHON=python"

set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\python.exe" (
    echo Creating virtualenv in %VENV% ...
    "%PYTHON%" -m venv "%VENV%" || (echo Setup failed - ensure Python 3.12+ is installed and on PATH. & pause & exit /b 1)
)
set "PYTHON=%VENV%\Scripts\python.exe"

rem Install deps on first run (or after a venv wipe), or when pyproject.toml is
rem newer than the stamp left by the last successful install, so a dependency
rem added to pyproject.toml reaches an existing venv.
set "STAMP=%VENV%\.fetchforge-installed"
set "NEED_INSTALL="
"%PYTHON%" -c "import importlib.metadata as m, os, sys; m.version('fetchforge'); s=sys.argv[1]; sys.exit(0 if os.path.exists(s) and os.path.getmtime('pyproject.toml') <= os.path.getmtime(s) else 1)" "%STAMP%" >nul 2>&1 || set "NEED_INSTALL=1"
if defined NEED_INSTALL (
    "%PYTHON%" -m pip --version >nul 2>&1
    if errorlevel 1 (
        uv pip install --python "%PYTHON%" -e .
    ) else (
        "%PYTHON%" -m pip install -e .
    )
    if errorlevel 1 (echo Setup failed - ensure Python 3.12+ is installed and on PATH. & pause & exit /b 1)
    type nul > "%STAMP%"
)
rem The server (run_server) opens the browser itself once it's listening.
"%PYTHON%" -m fetchforge
