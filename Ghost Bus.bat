@echo off
REM Ghost Bus - double-click to run everything on this computer.
REM First run: installs what it needs and asks for your Tiger Cloud URL. After that it just starts.
cd /d "%~dp0"
title Ghost Bus (keep this window open)

if not exist ".venv\Scripts\python.exe" (
  echo Installing Ghost Bus - first run only, about a minute...
  python -m venv .venv || goto :fail
  call ".venv\Scripts\activate.bat"
  python -m pip install --upgrade pip --quiet
  pip install -r requirements.txt --quiet || goto :fail
) else (
  call ".venv\Scripts\activate.bat"
)

if not exist ".env" copy ".env.example" ".env" >nul
findstr /c:"YOUR_PASSWORD" ".env" >nul
if not errorlevel 1 (
  echo.
  echo  Paste your Tiger Cloud service URL after DATABASE_URL= in Notepad, save, close Notepad,
  echo  then double-click "Ghost Bus.bat" again.
  start "" notepad ".env"
  pause
  exit /b 0
)

REM Open the app in the browser a few seconds after the server starts.
start "" /min cmd /c "timeout /t 6 /nobreak >nul & start http://localhost:8000"
echo Ghost Bus is starting at http://localhost:8000  (first run sets up the database - give it a minute)
echo Close this window to stop Ghost Bus.
python -m ghostbus
goto :eof

:fail
echo.
echo Something went wrong above. Take a screenshot of this window and send it to Claude.
pause
