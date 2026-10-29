@echo off
setlocal
title Sentinel V26
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "PYTHONUTF8=1"
py -3.12 -u "%~dp0iniciar_sentinel.py" %*
set "sentinel_result=%errorlevel%"
if "%~1"=="--check" exit /b %sentinel_result%
if not "%sentinel_result%"=="0" (
    echo.
    echo Revisa el error de arriba. Si falta Python 3.12: py install 3.12
    pause
)
exit /b %sentinel_result%
