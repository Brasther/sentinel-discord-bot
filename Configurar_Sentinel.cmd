@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
set "PYTHONUTF8=1"
py -3.12 -u "%~dp0configurar_sentinel.py"
set "sentinel_result=%errorlevel%"
if not "%sentinel_result%"=="0" (
    echo.
    echo Si falta Python 3.12: instala Python y ejecuta: py install 3.12
    pause
)
exit /b %sentinel_result%
