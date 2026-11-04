@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0scripts\desktop\Desktop.ps1" -Action Migrate %*
set "FFA_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %FFA_RESULT%
