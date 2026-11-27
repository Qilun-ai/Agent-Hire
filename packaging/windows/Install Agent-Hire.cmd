@echo off
rem Installs Agent-Hire, or updates the install it finds. Double-click to run.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
