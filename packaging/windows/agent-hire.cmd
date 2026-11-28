@echo off
rem Agent-Hire launcher: agent-hire start ^| stop ^| restart ^| status ^| open ^| logs ^| version ^| update ^| uninstall
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0agent-hire.ps1" %*
