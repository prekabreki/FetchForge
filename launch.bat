@echo off
title FetchForge - YouTube Downloader and H.265 Converter
cd /d "%~dp0"

set "PATH=%~dp0fetchforge\_internal;%PATH%"

set "VENV=%~dp0.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"

rem FetchForge needs Python 3.12+ (requires-python in pyproject.toml). Check it
rem before building or reusing the venv: apps that bundle an older Python often
rem put it on the machine PATH, ahead of the user's own, and a venv built from
rem it is one pip then refuses to install into.
if exist "%VENV_PY%" (
    call :py_ok "%VENV_PY%" || goto :stale_venv
    goto :venv_ready
)

rem An explicit PYTHON is the user's choice: check it, never replace it.
rem Otherwise try `python`, then the Python launcher's newest 3.x.
if defined PYTHON (
    call :py_ok "%PYTHON%" || goto :user_python_too_old
) else (
    set "PYTHON=python"
    call :py_ok python || call :try_py_launcher || goto :no_python
)

echo Creating virtualenv in %VENV% with %PYTHON% ...
"%PYTHON%" -m venv "%VENV%"
if errorlevel 1 goto :venv_failed

:venv_ready
set "PYTHON=%VENV_PY%"

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
    if errorlevel 1 (echo Setup failed - installing FetchForge into the virtualenv did not succeed. & pause & exit /b 1)
    type nul > "%STAMP%"
)
rem The server (run_server) opens the browser itself once it's listening.
"%PYTHON%" -m fetchforge
exit /b %errorlevel%


rem ---- helpers --------------------------------------------------------------

:py_ok
rem errorlevel 0 when %1 runs and is Python 3.12 or newer.
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
exit /b %errorlevel%

:try_py_launcher
rem Resolve `py -3` to a real python.exe so the rest of the script can quote it.
set "PY_LAUNCHER_EXE="
for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY_LAUNCHER_EXE=%%i"
if not defined PY_LAUNCHER_EXE exit /b 1
call :py_ok "%PY_LAUNCHER_EXE%"
if errorlevel 1 exit /b 1
set "PYTHON=%PY_LAUNCHER_EXE%"
exit /b 0

:describe
rem Print "- <interpreter>: Python X.Y.Z", or "not found" when it does not run.
"%~1" --version >nul 2>&1
if errorlevel 1 goto :describe_missing
<nul set /p "=- %~1: "
"%~1" --version 2>&1
exit /b 0
:describe_missing
echo - %~1: not found
exit /b 0

:describe_py_launcher
py -3 --version >nul 2>&1
if errorlevel 1 goto :describe_py_launcher_missing
<nul set /p "=- py -3: "
py -3 --version 2>&1
exit /b 0
:describe_py_launcher_missing
echo - py -3: not found
exit /b 0


rem ---- failures -------------------------------------------------------------

:stale_venv
echo Setup failed - the existing virtualenv was built with a Python older than 3.12:
call :describe "%VENV_PY%"
echo Delete this folder, then run launch.bat again:
echo   %VENV%
pause
exit /b 1

:user_python_too_old
echo Setup failed - PYTHON is set, but FetchForge needs Python 3.12 or newer:
call :describe "%PYTHON%"
echo Point PYTHON at a 3.12+ python.exe, or clear it to let the launcher find one.
pause
exit /b 1

:no_python
echo Setup failed - FetchForge needs Python 3.12 or newer, and none was found. Tried:
call :describe python
call :describe_py_launcher
echo Install Python 3.12+ from python.org, or set PYTHON to a 3.12+ python.exe.
pause
exit /b 1

:venv_failed
echo Setup failed - could not create the virtualenv with:
call :describe "%PYTHON%"
pause
exit /b 1
