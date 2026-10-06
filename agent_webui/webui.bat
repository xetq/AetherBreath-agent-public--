@echo off
REM NOTE: keep this file ASCII-only. cmd.exe parses batch files in the OEM
REM codepage (GBK on zh-CN Windows), so non-ASCII bytes get torn apart mid-line.
setlocal EnableExtensions
REM ============================================================
REM  AetherBreath WebUI launcher (one-stop)
REM    webui.bat           start gateway, wait until healthy, open browser
REM    webui.bat --no-open start gateway only (no browser)
REM    webui.bat --ab      also send the "power on AB" request (spends LLM quota)
REM    webui.bat --setup   create venv-gateway and install backend requirements
REM  Port: override with AETHER_WEBUI_PORT (default 8900).
REM  No hardcoded paths: everything is relative to this script's folder.
REM  AB is NOT started by default on purpose - use --ab or click the button.
REM ============================================================
set "WEBUI=%~dp0"
set "MAIN=%WEBUI%backend\main.py"
set "REQ=%WEBUI%backend\requirements.txt"
set "PORT=%AETHER_WEBUI_PORT%"
if "%PORT%"=="" set "PORT=8900"
set "URL=http://127.0.0.1:%PORT%"

if /i "%~1"=="--setup" goto setup
if not exist "%MAIN%" (
  echo [X] gateway entry not found: "%MAIN%"
  exit /b 2
)
REM ---- 选解释器：网关独立 venv -^> 项目根 venv -^> PATH 上的 python ----
set "GW=%WEBUI%venv-gateway\Scripts\python.exe"
if not exist "%GW%" set "GW=%WEBUI%..\venv\Scripts\python.exe"
if not exist "%GW%" set "GW=python"

REM ---- 依赖自检：解释器里有没有 fastapi / uvicorn ----
"%GW%" -c "import fastapi, uvicorn" >nul 2>&1
if not "%ERRORLEVEL%"=="0" (
  echo [X] fastapi / uvicorn not found via: %GW%
  echo     Install the backend dependencies first ^(run in the project root^):
  echo         pip install -r requirements.txt
  echo     Or create a dedicated gateway venv:  webui.bat --setup
  exit /b 2
)

REM ---- already running? then just open the page (a 2nd gateway would abort on port preflight) ----
curl -s -o nul --max-time 2 "%URL%/api/health"
if "%ERRORLEVEL%"=="0" (
  echo [i] gateway already up at %URL%
  goto open
)

echo [i] starting gateway at %URL% ...
start "AetherBreath-WebUI" /min "%GW%" "%MAIN%"

set /a TRIES=0
:wait
set /a TRIES+=1
curl -s -o nul --max-time 2 "%URL%/api/health"
if "%ERRORLEVEL%"=="0" goto ready
if %TRIES% GEQ 20 (
  echo [X] gateway not ready in 20s. Check the minimized "AetherBreath-WebUI" window,
  echo     or run in a terminal:  "%GW%" "%MAIN%"
  exit /b 3
)
timeout /t 1 /nobreak >nul
goto wait

:ready
echo [OK] gateway is up.
if /i "%~1"=="--ab" (
  curl -s -o nul --max-time 30 -X POST -H "Content-Type: application/json" -d "{}" "%URL%/api/agent/start"
  echo [OK] power-on sent; AB needs ~15-25s to boot.
)

:open
if /i "%~1"=="--no-open" (
  echo [i] skipped opening a browser. UI: %URL%
  exit /b 0
)
echo [i] opening %URL%  - click the green power button in the UI to start AB.
start "" "%URL%"
exit /b 0

:setup
echo [i] creating venv-gateway and installing backend requirements ...
python -m venv "%WEBUI%venv-gateway"
if not "%ERRORLEVEL%"=="0" (
  echo [X] venv creation failed - is python on PATH?
  exit /b 4
)
"%WEBUI%venv-gateway\Scripts\python.exe" -m pip install -r "%REQ%"
if not "%ERRORLEVEL%"=="0" (
  echo [X] pip install failed
  exit /b 5
)
echo [OK] dependencies ready. Now run:  webui.bat
exit /b 0
