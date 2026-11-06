@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0..\..\market_probe\vendor;%PYTHONPATH%"
py server.py
pause
