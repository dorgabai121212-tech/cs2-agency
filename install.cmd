@echo off
rem ===========================================================
rem  Imagination Agency - double-click this file to install or update.
rem  It only runs install.ps1 from this folder (see that file for details).
rem ===========================================================
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 pause
