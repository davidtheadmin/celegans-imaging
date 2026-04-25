@echo off
setlocal enabledelayedexpansion
:: Resolve repo root regardless of where this bat is run from.
:: setup.bat lives in <repo_root>\launcher\, so parent dir = repo root.
cd /d "%~dp0.."
set "REPO_ROOT=%CD%"
echo.
echo ============================================================
echo  WormScan setup
echo ============================================================
echo.
:: ------------------------------------------------------------------
:: 1. Python check
:: ------------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo.
    echo Install Python 3.11 or newer from https://www.python.org/downloads/
    echo On the first installer screen, tick "Add Python to PATH" before
    echo clicking Install Now.
    echo.
    pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.11 or newer is required.
    for /f "tokens=*" %%V in ('python --version 2^>^&1') do echo Found: %%V
    echo.
    echo Install a newer version from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" on the first installer screen.
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%V in ('python --version 2^>^&1') do echo Using %%V
:: ------------------------------------------------------------------
:: 2. Create virtual environment (idempotent)
:: ------------------------------------------------------------------
if exist "%REPO_ROOT%\launcher\.venv\Scripts\pythonw.exe" (
    echo Virtual environment already exists, skipping creation.
) else (
    echo Creating virtual environment...
    python -m venv "%REPO_ROOT%\launcher\.venv"
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        echo.
        pause
        exit /b 1
    )
)
:: ------------------------------------------------------------------
:: 3. Install dependencies
:: ------------------------------------------------------------------
echo Installing / updating dependencies...
"%REPO_ROOT%\launcher\.venv\Scripts\pip.exe" install --quiet -r "%REPO_ROOT%\launcher\requirements.txt"
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. See the output above.
    echo.
    echo Screenshot or copy everything above this line and send it to David.
    echo.
    pause
    exit /b 1
)
echo Dependencies OK.
:: ------------------------------------------------------------------
:: 4. Create desktop shortcut via temp PowerShell script
:: ------------------------------------------------------------------
set "PS1_TEMP=%TEMP%\wormscan_shortcut_%RANDOM%.ps1"
> "%PS1_TEMP%" echo $s = (New-Object -COM WScript.Shell).CreateShortcut("$env:USERPROFILE\Desktop\WormScan.lnk")
>>"%PS1_TEMP%" echo $s.TargetPath = '%REPO_ROOT%\launcher\.venv\Scripts\pythonw.exe'
>>"%PS1_TEMP%" echo $s.Arguments = '"%REPO_ROOT%\launcher\main.py"'
>>"%PS1_TEMP%" echo $s.WorkingDirectory = '%REPO_ROOT%'
>>"%PS1_TEMP%" echo $s.IconLocation = '%REPO_ROOT%\launcher\assets\wormscan.ico'
>>"%PS1_TEMP%" echo $s.Save()
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_TEMP%"
set PS1_EXIT=%ERRORLEVEL%
del "%PS1_TEMP%" 2>nul
if %PS1_EXIT% neq 0 (
    echo.
    echo ERROR: Could not create the desktop shortcut.
    echo Please send David a screenshot of this window.
    echo.
    pause
    exit /b 1
)
:: ------------------------------------------------------------------
:: Done
:: ------------------------------------------------------------------
echo.
echo ============================================================
echo  Setup complete. Look for the WormScan icon on your desktop.
echo ============================================================
echo.
pause
endlocal
