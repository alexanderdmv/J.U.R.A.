@echo off
rem J.U.R.A. - install on a clean Windows PC. Double-click or: install.cmd [flags]
rem Runs scripts\bootstrap.ps1 (finds or installs Python, then runs scripts\install.py).
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1" %*
set CODE=%ERRORLEVEL%
echo.
pause
exit /b %CODE%
