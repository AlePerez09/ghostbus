@echo off
REM Uploads this project to https://github.com/AlePerez09/ghostbus  (safe to run every time you want to push)
cd /d "%~dp0"
title Ghost Bus - push to GitHub

REM Git needs a name/email for the commit history (shown publicly on GitHub).
git config user.name >nul || call :askname
git config user.email >nul || call :askemail

if not exist ".git" git init -b main
git remote get-url origin >nul 2>&1
if errorlevel 1 (
  git remote add origin https://github.com/AlePerez09/ghostbus.git
) else (
  git remote set-url origin https://github.com/AlePerez09/ghostbus.git
)

git add .

REM Safety check: never upload the database password.
git diff --cached --name-only | findstr /x /c:".env" >nul
if not errorlevel 1 (
  echo STOP: .env was about to be uploaded. Nothing was pushed.
  git reset -q
  goto :fail
)

set "MSG=Update Ghost Bus"
set /p MSG=Describe what changed (or just press Enter): 
git commit -m "%MSG%" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01P9VdYp31DtX6caojU1X2Fi"

echo.
echo Pushing... if a GitHub sign-in window opens, sign in with YOUR GitHub account.
git push -u origin main
if not errorlevel 1 goto :done

echo.
echo GitHub has changes you don't have yet - pulling them in first...
git pull --rebase origin main
if errorlevel 1 goto :conflict
git push -u origin main
if errorlevel 1 goto :pushfail

:done
echo.
echo Done! Open https://github.com/AlePerez09/ghostbus to see it.
pause
exit /b 0

:askname
set /p GITNAME=Your name for GitHub commits: 
git config --global user.name "%GITNAME%"
exit /b 0

:askemail
set /p GITEMAIL=The email on your GitHub account: 
git config --global user.email "%GITEMAIL%"
exit /b 0

:conflict
echo.
echo Your changes and GitHub's changes touch the same lines (a merge conflict).
echo Nothing is lost. Take a screenshot of this window and send it to Claude.
goto :fail

:pushfail
echo.
echo Push failed. Most likely you are not a collaborator on AlePerez09/ghostbus yet:
echo   Ale opens the repo - Settings - Collaborators - Add people - adds IShar2005,
echo   you accept the invite (check your email), then run this file again.

:fail
pause
exit /b 1
