@echo off
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"
"%PS%" -ExecutionPolicy Bypass -NoProfile -File "%~dp0run_frontend.ps1"
pause
