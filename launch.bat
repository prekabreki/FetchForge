@echo off
title FetchForge — YouTube Downloader and H.265 Converter
cd /d "%~dp0"

set "PATH=%~dp0_internal;%PATH%"

if not defined PYTHON set "PYTHON=python"
%PYTHON% -m pip show fetchforge >nul 2>&1 || %PYTHON% -m pip install -e . || (echo Setup failed - ensure Python 3.12+ is installed and on PATH. & pause & exit /b 1)
rem The server (run_server) opens the browser itself once it's listening.
%PYTHON% -m fetchforge
