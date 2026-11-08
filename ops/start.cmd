@echo off
rem Запуск TaskTreker одним ярлыком: локальная модель (если установлена) и доска с автозапуском ролей.
rem Ярлык на рабочем столе указывает сюда. Повторный запуск второй сервер не поднимает — только открывает доску.
rem START_MIMO=0 — не поднимать локальную модель (освобождает видеопамять).
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
set START_MIMO=1
set PORT=8787
cd /d "%~dp0.."
title TaskTreker

rem 1. Локальная модель MiMo: есть рантайм и веса, порт 8089 свободен — запустить в свёрнутом окне.
if "%START_MIMO%"=="1" if exist "models\runtime\llama-b12199\llama-server.exe" (
  call :listening 8089
  if errorlevel 1 (
    echo Запускаю локальную модель MiMo...
    start "TaskTreker — MiMo" /min cmd /c ops\mimo-server.cmd
  ) else (
    echo Локальная модель уже работает.
  )
)

rem 2. Доска уже работает — открыть её и выйти: второй сервер дал бы второй автозапуск сессий.
call :board
if not errorlevel 1 (
  echo Доска уже запущена: открываю http://127.0.0.1:%PORT%/
  start "" "http://127.0.0.1:%PORT%/"
  timeout /t 3 >nul
  exit /b 0
)

rem 3. Запустить доску. Браузер откроется сам со ссылкой владельца; это окно — сервер, не закрывать.
echo Запускаю доску TaskTreker. Это окно — сервер: закроете его — доска остановится.
py -3 tools\pm.py serve --port %PORT%
echo.
echo Сервер доски остановлен.
pause
exit /b 0

:listening
powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', %1); exit 0 } catch { exit 1 } finally { $c.Close() }" >nul 2>&1
exit /b %errorlevel%

:board
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://127.0.0.1:%PORT%/api/ping'; if ($r.Content -match '\"served\"') { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
exit /b %errorlevel%
