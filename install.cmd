@echo off
rem ────────────────────────────────────────────────────────────────────────────
rem  LeafHub — Windows cmd.exe bootstrap
rem
rem  Usage (from the project root):
rem    install.cmd
rem    install.cmd /Reinstall
rem    install.cmd /Uninstall
rem
rem  Delegates to install.ps1 in the same directory.
rem ────────────────────────────────────────────────────────────────────────────
setlocal

set "PS_SCRIPT=%~dp0install.ps1"

if not exist "%PS_SCRIPT%" (
    echo Error: install.ps1 not found at %PS_SCRIPT%
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "EXITCODE=%ERRORLEVEL%"

exit /b %EXITCODE%
