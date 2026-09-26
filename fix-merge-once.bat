@echo off
REM One-time repair: uploads the cleaned-up files (Ale's county support + the address fix, with the
REM leftover merge-conflict markers removed). Safe: it never uploads .env.
cd /d "%~dp0"
title Ghost Bus - upload merge fix

echo Getting the latest from GitHub...
git fetch origin || goto :fail

REM Point this folder's history at GitHub's latest commit WITHOUT touching the files on disk,
REM so the next commit is exactly "GitHub's broken version -> the repaired files here".
git reset --soft origin/main || goto :fail
git add -A

git diff --cached --name-only | findstr /x /c:".env" >nul
if not errorlevel 1 (
  echo STOP: .env was about to be uploaded. Nothing was pushed.
  git reset -q
  goto :fail
)

git commit -m "Fix leftover merge-conflict markers from county update" -m "Keeps Broward/Palm Tran/Key West support and the Miami-style address fix." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01P9VdYp31DtX6caojU1X2Fi" || goto :fail
git push origin main || goto :fail

echo.
echo Done! GitHub is repaired and Render will redeploy in about a minute.
echo Tell Ale to run "git pull" before he makes more changes.
pause
exit /b 0

:fail
echo.
echo Something went wrong above. Take a screenshot of this window and send it to Claude.
pause
exit /b 1
