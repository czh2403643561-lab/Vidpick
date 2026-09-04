@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON_CMD="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD goto :no_python

set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo.
    echo [Vidpick] First-time setup: creating Python environment...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :venv_failed
)

if not exist ".venv\.vidpick_ready" (
    echo.
    echo [Vidpick] First-time setup: installing required packages...
    set "ALL_PROXY="
    set "HTTP_PROXY="
    set "HTTPS_PROXY="
    "%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :packages_failed
    echo [Vidpick] First-time setup: installing Chromium. This may take a moment...
    "%VENV_PY%" -m playwright install chromium
    if errorlevel 1 goto :browser_failed
    >".venv\.vidpick_ready" echo ready
)

echo [Vidpick] Starting...
"%VENV_PY%" main.py
if errorlevel 1 goto :app_failed
goto :end

:no_python
echo.
echo [Vidpick] Python 3.10 or newer was not found. Please install Python, then run this file again.
goto :failed
:venv_failed
echo.
echo [Vidpick] Could not create the local Python environment.
goto :failed
:packages_failed
echo.
echo [Vidpick] Could not install required packages. Please check your network connection and try again.
goto :failed
:browser_failed
echo.
echo [Vidpick] Could not install Chromium. Please check your network connection and try again.
goto :failed
:app_failed
echo.
echo [Vidpick] Vidpick stopped unexpectedly. Please copy the error above if you need help.
goto :failed
:failed
echo.
pause
:end
endlocal
