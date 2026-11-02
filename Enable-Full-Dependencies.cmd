@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Enable-Full-Dependencies.ps1" %*
set "FFA_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %FFA_RESULT%
