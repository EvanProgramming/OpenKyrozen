@echo off
REM ============================================================
REM  OpenKyrozen — Windows Setup
REM  Creates a Python virtual environment and installs dependencies.
REM ============================================================

setlocal enabledelayedexpansion

echo ============================================================
echo  OpenKyrozen Windows Setup
echo ============================================================
echo.

REM --- Find a working Python 3.12 (preferred) or 3.13 ---
set PYTHON=
for %%p in (py -3.12 python3.12 python3 python) do (
    call :try_python %%p
    if !PYTHON! neq "" goto :found
)

echo [ERROR] No working Python found. Install Python 3.12 or 3.13 from https://python.org
echo         Make sure "Add Python to PATH" is checked during install.
pause
exit /b 1

:found
echo [INFO] Using Python: !PYTHON!
echo.

REM --- Create virtual environment ---
echo [INFO] Creating virtual environment...
rmdir /s /q venv 2>nul
!PYTHON! -m venv venv
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

REM --- Activate and install ---
call venv\Scripts\activate.bat
echo [INFO] Upgrading pip...
python -m pip install --upgrade pip -q
echo [INFO] Installing requirements...
pip install -r requirements.txt -q
echo.
echo ============================================================
echo  Setup complete. Run 'run.bat' to start OpenKyrozen.
echo ============================================================
pause
exit /b 0

REM --- Subroutine: test if a Python command works ---
:try_python
set CMD=%*
for /f "tokens=*" %%v in ('%CMD% --version 2^>nul') do set VER=%%v
if "%VER%"=="" exit /b
echo %VER% | findstr /c:"3.12" >nul && set PYTHON=%CMD% && exit /b
echo %VER% | findstr /c:"3.13" >nul && set PYTHON=%CMD% && exit /b
exit /b
