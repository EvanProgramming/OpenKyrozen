@echo off
REM OpenKyrozen development setup: Python backend plus optional Bubble Tea UI.

setlocal enabledelayedexpansion

echo OPENKYROZEN
echo OpenKyrozen computer-native development setup
echo.

REM --- Find a working Python 3.12 (preferred) or 3.13 ---
set PYTHON=
for /f "tokens=*" %%v in ('py -3.12 --version 2^>nul') do set "PYTHON=py -3.12"
if "!PYTHON!"=="" (
    for %%p in (python3.12 python3 python) do (
        call :try_python %%p
        if "!PYTHON!" neq "" goto :found
    )
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
echo [INFO] Installing the Python backend and web dependencies...
pip install -e ".[web]" -q
if errorlevel 1 (
    echo [ERROR] Python dependencies could not be installed.
    pause
    exit /b 1
)
if exist "%ProgramFiles%\Go\bin\go.exe" (
    set "GO=%ProgramFiles%\Go\bin\go.exe"
) else (
    where go >nul 2>nul && set "GO=go"
)
if defined GO (
    echo [INFO] Building the Bubble Tea terminal UI...
    if not exist "%USERPROFILE%\.kyrozen\bin" mkdir "%USERPROFILE%\.kyrozen\bin"
    set "TUI_OUTPUT=%TEMP%\openkyrozen-tui-%RANDOM%.exe"
    pushd tui
    "%GO%" build -trimpath -o "!TUI_OUTPUT!" .
    if errorlevel 1 (
        echo [WARN] TUI build failed; kyrozen will use the Rich fallback.
    ) else (
        move /y "!TUI_OUTPUT!" "%USERPROFILE%\.kyrozen\bin\openkyrozen-tui.exe" >nul
    )
    popd
    if exist "!TUI_OUTPUT!" del /q "!TUI_OUTPUT!"
) else (
    echo [INFO] Go was not found; the installed launcher will use the Rich fallback.
    echo        The one-line installer can provision Go automatically.
)
echo.
echo Setup complete. Run run.bat to start OpenKyrozen.
pause
exit /b 0

REM --- Subroutine: test if a Python command works ---
:try_python
set CMD=%*
set VER=
for /f "tokens=*" %%v in ('%CMD% --version 2^>nul') do set VER=%%v
if "%VER%"=="" exit /b
echo %VER% | findstr /c:"3.12" >nul && set PYTHON=%CMD% && exit /b
echo %VER% | findstr /c:"3.13" >nul && set PYTHON=%CMD% && exit /b
exit /b
