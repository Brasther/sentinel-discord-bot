@echo off
setlocal
cd /d "%~dp0"
set "SENTINEL_PORT=5001"
set "SENTINEL_HOST=127.0.0.1"
echo Panel de demostracion: http://localhost:5001
call Iniciar_Sentinel.cmd --preview
