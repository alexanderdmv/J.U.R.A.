@echo off
rem Run J.U.R.A. from the project environment: jura.cmd shell | jura.cmd ask "..." | jura.cmd --help
chcp 65001 >nul
"%~dp0.venv\Scripts\python.exe" -m jura %*
