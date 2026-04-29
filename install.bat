@echo off
:: install.bat — Judgeman installer for Windows 11
:: Run from an Administrator terminal or double-click

setlocal EnableDelayedExpansion
set "JUDGEMAN_SRC=%~dp0"
set "JUDGEMAN_SRC=%JUDGEMAN_SRC:~0,-1%"
set "INSTALL_DIR=%LOCALAPPDATA%\Programs\Judgeman"
set "SCRIPTS_DIR=%LOCALAPPDATA%\Programs\Judgeman\scripts"
set "START_MENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Judgeman"

echo.
echo   ====================================================
echo     JUDGEMAN — OSINT Analytical Reasoning Engine
echo   ====================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python not found.
    echo   Install Python 3.11+ from https://python.org
    echo   Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo   Python found: %PYVER%

:: Install Python dependencies
echo.
echo   Installing dependencies...
python -m pip install click colorama flask --quiet
if errorlevel 1 (
    echo   [WARN] pip install had issues. Trying with --user flag...
    python -m pip install click colorama flask --quiet --user
)
echo   Dependencies installed.

:: Create install directory
echo.
echo   Creating install directory: %INSTALL_DIR%
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
if not exist "%SCRIPTS_DIR%" mkdir "%SCRIPTS_DIR%"

:: Write jm.bat (CLI launcher)
echo   Writing CLI launcher (jm.bat)...
(
echo @echo off
echo setlocal
echo set "JUDGEMAN_HOME=%%USERPROFILE%%\.judgeman"
echo set "PYTHONPATH=%JUDGEMAN_SRC%\judgeman"
echo python "%JUDGEMAN_SRC%\judgeman\cli.py" %%*
) > "%SCRIPTS_DIR%\jm.bat"

:: Write jm-gui.bat (GUI launcher)
echo   Writing GUI launcher (jm-gui.bat)...
(
echo @echo off
echo setlocal
echo set "JUDGEMAN_HOME=%%USERPROFILE%%\.judgeman"
echo set "PYTHONPATH=%JUDGEMAN_SRC%\judgeman"
echo echo.
echo echo   Starting Judgeman GUI...
echo echo   Opening http://127.0.0.1:7432
echo echo   Press Ctrl+C to stop
echo echo.
echo python "%JUDGEMAN_SRC%\jm-gui.py" %%*
) > "%SCRIPTS_DIR%\jm-gui.bat"

:: Write jm-gui.py (cross-platform launcher)
echo   Writing GUI Python launcher...
(
echo import sys, os, subprocess, webbrowser, time, threading, argparse
echo.
echo SCRIPT_DIR = r'%JUDGEMAN_SRC%'
echo GUI_APP    = os.path.join^(SCRIPT_DIR, 'gui', 'app.py'^)
echo.
echo sys.path.insert^(0, os.path.join^(SCRIPT_DIR, 'judgeman'^)^)
echo.
echo def main^(^):
echo     parser = argparse.ArgumentParser^(^)
echo     parser.add_argument^('--port', type=int, default=7432^)
echo     parser.add_argument^('--no-browser', action='store_true'^)
echo     args = parser.parse_args^(^)
echo     port = args.port
echo     url  = f'http://127.0.0.1:{port}'
echo     if not args.no_browser:
echo         def open_b^(^):
echo             time.sleep^(1.4^)
echo             webbrowser.open^(url^)
echo         threading.Thread^(target=open_b, daemon=True^).start^(^)
echo     env = os.environ.copy^(^)
echo     env['JUDGEMAN_PORT'] = str^(port^)
echo     env['JUDGEMAN_HOME'] = os.path.join^(os.path.expanduser^('~'^), '.judgeman'^)
echo     print^(f'\n  Judgeman running at {url}\n  Press Ctrl+C to quit\n'^)
echo     import subprocess
echo     subprocess.run^([sys.executable, GUI_APP], env=env^)
echo.
echo if __name__ == '__main__':
echo     main^(^)
) > "%JUDGEMAN_SRC%\jm-gui.py"

:: Add scripts dir to user PATH (non-admin)
echo.
echo   Adding to PATH...
set "CURRENT_PATH="
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v PATH 2^>nul') do set "CURRENT_PATH=%%b"
echo %CURRENT_PATH% | findstr /i "%SCRIPTS_DIR%" >nul
if errorlevel 1 (
    if defined CURRENT_PATH (
        setx PATH "%CURRENT_PATH%;%SCRIPTS_DIR%" >nul
    ) else (
        setx PATH "%SCRIPTS_DIR%" >nul
    )
    echo   Added to PATH: %SCRIPTS_DIR%
) else (
    echo   Already in PATH.
)

:: Start Menu shortcut via PowerShell
echo.
echo   Creating Start Menu shortcut...
if not exist "%START_MENU%" mkdir "%START_MENU%"
powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $s = $ws.CreateShortcut('%START_MENU%\Judgeman.lnk'); ^
   $s.TargetPath = '%SCRIPTS_DIR%\jm-gui.bat'; ^
   $s.WorkingDirectory = '%JUDGEMAN_SRC%'; ^
   $s.Description = 'Judgeman OSINT Reasoning Engine'; ^
   $s.Save()" 2>nul
if errorlevel 0 echo   Start Menu shortcut created.

:: Desktop shortcut
powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $s = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Judgeman.lnk'); ^
   $s.TargetPath = '%SCRIPTS_DIR%\jm-gui.bat'; ^
   $s.WorkingDirectory = '%JUDGEMAN_SRC%'; ^
   $s.Description = 'Judgeman OSINT Reasoning Engine'; ^
   $s.Save()" 2>nul
if errorlevel 0 echo   Desktop shortcut created.

echo.
echo   ====================================================
echo     Installation complete.
echo.
echo     To launch the GUI:
echo       jm-gui                     (after restarting terminal)
echo       OR double-click Judgeman on your Desktop
echo.
echo     To use the CLI:
echo       jm init "My Investigation"
echo       jm status
echo.
echo     NOTE: Restart your terminal for PATH changes to take effect.
echo   ====================================================
echo.
pause
