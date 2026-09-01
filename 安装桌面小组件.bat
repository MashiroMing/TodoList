@echo off
rem ============================================================
rem  TODO Widget - One-click installer
rem  1. Install Python dependencies (pywebview / pystray / pillow)
rem  2. Run serve.py --install to create desktop shortcut + startup
rem ============================================================
setlocal

set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "SCRIPT=%~dp0serve.py"

if not exist "%SCRIPT%" (
    echo [ERROR] serve.py not found next to this script.
    pause
    exit /b 1
)

echo ============================================
echo   TODO Widget Desktop Installer
echo ============================================
echo.

echo [1/2] Installing dependencies (pywebview pystray pillow)...
"%PY%" -m pip install pywebview pystray pillow
if errorlevel 1 (
    echo [ERROR] Dependency install failed. Check your network and retry.
    pause
    exit /b 1
)
echo.

echo [2/2] Creating desktop shortcut and startup entry...
"%PY%" "%SCRIPT%" --install

echo.
echo Done. A desktop shortcut was created and
echo the widget will auto-start on login.
pause
