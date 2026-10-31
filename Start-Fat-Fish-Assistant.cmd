@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0scripts\Desktop.ps1" -Action Start %*
set "FFA_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %FFA_RESULT%
