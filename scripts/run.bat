@echo off
REM Windows wrapper for scripts\run.ps1. Double-click it or run it from cmd.
REM Usage: scripts\run.bat [--skip-wind]   or   scripts\run.bat -Test
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
