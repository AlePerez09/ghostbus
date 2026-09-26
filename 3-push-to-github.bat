@echo off
REM Uploads this project to https://github.com/AlePerez09/ghostbus
cd /d "%~dp0"
title Ghost Bus - push to GitHub

REM Git needs a name/email for the commit history (shown publicly on GitHub).
for /f "delims=" %%i in ('git config user.name') do set GITNAME=%%i
if not defined GITNAME (
  set /p GITNAME=Your name for GitHub commits:
  git config --global user.name "%GITNAME%"
)
for /f "delims=" %%i in ('git config user.email') do set GITEMAIL=%%i
if not defined GITEMAIL (
  set /p GITEMAIL=The email on your GitHub account:
  git config --global user.email "%GITEMAIL%"
)

if not exist ".git" git init -b main
git add .

REM Safety check: never upload the database password.
git diff --cached --name-only | findstr /x /c:".env" >nul
if not errorlevel 1 (
  echo STOP: .env was about to be uploaded. Nothing was pushed.
  git reset -q
  goto :fail
)

git commit -m "Ghost Bus: Tiger Data pipeline, simulator, API and dashboard" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01P9VdYp31DtX6caojU1X2Fi"

git remote get-url origin >nul 2>&1
if errorlevel 1 (
  git remote add origin https://github.com/AlePerez09/ghostbus.git
) else (
  git remote set-url origin https://github.com/AlePerez09/ghostbus.git
)

echo.
echo Pushing... if a GitHub sign-in window opens, sign in with YOUR GitHub account.
git push -u origin main || goto :pushfail

echo.
echo Done! Open https://github.com/AlePerez09/ghostbus to see it.
pause
exit /b 0

:pushfail
echo.
echo Push failed. Most likely you are not a collaborator on AlePerez09/ghostbus yet:
echo   Ale opens the repo - Settings - Collaborators - Add people - adds IShar2005,
echo   you accept the email invite, then run this file again.
:fail
pause
exit /b 1
