@echo off
REM Starts the bus data feed and the dashboard in two windows, then opens the browser.
cd /d "%~dp0"

findstr /r /c:"^GTFS_RT_API_KEY=." ".env" >nul
if not errorlevel 1 (
  set "FEED=python -m ghostbus.poll_realtime"
) else if exist ".backfilled" (
  set "FEED=python -m ghostbus.simulate --live"
) else (
  set "FEED=python -m ghostbus.simulate --backfill 4 --live"
  type nul > ".backfilled"
)

start "Ghost Bus - data feed (keep open)" cmd /k "call .venv\Scripts\activate.bat && %FEED%"
start "Ghost Bus - dashboard (keep open)" cmd /k "call .venv\Scripts\activate.bat && python -m uvicorn ghostbus.api:app --port 8000"

echo Waiting for the dashboard to start...
timeout /t 8 /nobreak >nul
start "" http://localhost:8000
