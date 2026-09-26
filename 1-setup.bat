@echo off
REM Ghost Bus one-time setup. Double-click this file.
cd /d "%~dp0"
title Ghost Bus setup

echo [1/5] Creating Python environment...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
call ".venv\Scripts\activate.bat" || goto :fail

echo [2/5] Installing packages (takes a minute)...
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt || goto :fail

echo [3/5] Checking database settings...
if not exist ".env" copy ".env.example" ".env" >nul
findstr /c:"YOUR_PASSWORD" ".env" >nul
if not errorlevel 1 (
  echo.
  echo  ============================================================
  echo   Paste your Tiger Cloud connection string after DATABASE_URL=
  echo   in the Notepad window that just opened, then SAVE and close it.
  echo  ============================================================
  notepad ".env"
  findstr /c:"YOUR_PASSWORD" ".env" >nul
  if not errorlevel 1 (
    echo DATABASE_URL still has the placeholder. Run this file again after you paste it.
    goto :fail
  )
)

echo [4/5] Creating tables, compression and continuous aggregates...
python -m ghostbus.setup_db || goto :fail

echo [5/5] Loading the Miami-Dade bus schedule...
python -m ghostbus.load_static
if errorlevel 1 (
  echo County download failed - using the fake Miami schedule instead.
  python tools\fake_gtfs.py fake_gtfs.zip || goto :fail
  python -m ghostbus.load_static fake_gtfs.zip || goto :fail
)

echo.
echo  Setup finished! Now double-click 2-start.bat
pause
exit /b 0

:fail
echo.
echo  Something went wrong above. Take a screenshot of this window and send it to Claude.
pause
exit /b 1
