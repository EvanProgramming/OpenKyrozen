@echo off
REM ============================================================
REM  OpenKyrozen — Git Push (Windows)
REM ============================================================
git push origin main
if errorlevel 1 (
    echo [ERROR] Push failed. Check your network and remote configuration.
    pause
)
