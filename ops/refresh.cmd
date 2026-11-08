@echo off
REM Ежедневное обновление фактов портфеля. Запускается планировщиком Windows.
REM Обновляет только собираемые данные: токены, репозитории, оценки, дашборд, снимок.
REM Ручные данные (цели, готовность, бэклог, оценка) не трогает — их ведёт Клод.
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
if not exist "ops\logs" mkdir "ops\logs"
py -3 tools\pm.py refresh >> "ops\logs\refresh.log" 2>&1
echo [%date% %time%] exit=%errorlevel% >> "ops\logs\refresh.log"
endlocal
