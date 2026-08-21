@echo off
setlocal EnableExtensions
title Z-SPAN CLI boot demo
cd /d "%~dp0"

rem  run-zspan.bat
rem  Runs the real boot ceremony from zspan_cli/boot.py, no install, no
rem  side effects. Python's zipimport treats the wheel as a package when
rem  we put it on PYTHONPATH. The wheel is 100%% pure-Python and stdlib
rem  only on the boot path — verified against boot.py's imports.
rem
rem  Requires: Python 3.11+ on PATH. Wheel next to this .bat.
rem
rem  The wheel is a RELEASE ASSET, not a repo file — it is deliberately
rem  never committed (a built distribution is not source, and a stale one
rem  can carry files the current packaging rules would exclude). Download
rem  it from the Releases page and drop it beside this .bat.

set "WHEEL=%~dp0zspan_cli-0.1.0-py3-none-any.whl"

if not exist "%WHEEL%" (
    echo.
    echo   Cannot find zspan_cli-0.1.0-py3-none-any.whl next to this .bat.
    echo.
    echo   The wheel ships as a release asset, not in the repository.
    echo   Download it from the project's Releases page, put it in this
    echo   same folder, and run this file again.
    echo.
    pause
    exit /b 1
)

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
    goto :run
)
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY=python"
    goto :run
)

echo.
echo   Python 3.11+ is not on PATH.
echo   Install from https://www.python.org/downloads/
echo   ^(check "Add python.exe to PATH" during install^) and try again.
echo.
pause
exit /b 2

:run
chcp 65001 >nul 2>nul
mode con cols=120 lines=40 >nul 2>nul
cls

set "PYTHONPATH=%WHEEL%"
%PY% -m zspan_cli.boot
set "BOOT_EXIT=%ERRORLEVEL%"

echo.
if not "%BOOT_EXIT%"=="0" (
    echo   zspan_cli.boot exited with code %BOOT_EXIT%
    echo.
)
pause
exit /b %BOOT_EXIT%
