@echo off
REM ============================================================
REM  OpenKyrozen — Windows Run
REM  Activates the virtual environment and starts the agent.
REM ============================================================

setlocal

REM --- Check that venv exists ---
if not exist venv\Scripts\activate.bat (
    echo [ERROR] Virtual environment not found. Run 'setup.bat' first.
    pause
    exit /b 1
)

REM --- Activate and run ---
call venv\Scripts\activate.bat
python main.py %*
pause
