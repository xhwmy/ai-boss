@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0一键自动投递.ps1"
if errorlevel 1 pause
