@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set REPORT_DATE=%%i
) else (
    set REPORT_DATE=%~1
)

python run_automated_reports.py --date %REPORT_DATE% --left-turn --group TSM

if errorlevel 1 (
    echo.
    echo ОШИБКА. Код завершения: %errorlevel%
    pause
    exit /b %errorlevel%
)

echo.
echo Готово. Дата отчёта: %REPORT_DATE%
pause
