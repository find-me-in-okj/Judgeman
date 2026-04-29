# install.ps1 — Judgeman installer for Windows 11 (PowerShell alternative)
# Run: Right-click → Run with PowerShell
# Or: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$JudgemanSrc = $PSScriptRoot
$InstallDir  = "$env:LOCALAPPDATA\Programs\Judgeman\scripts"
$StartMenu   = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Judgeman"

Write-Host ""
Write-Host "  JUDGEMAN — OSINT Analytical Reasoning Engine" -ForegroundColor Red
Write-Host "  Installing..." -ForegroundColor Gray
Write-Host ""

# Python check
try {
    $pyver = (python --version 2>&1).ToString()
    Write-Host "  Python: $pyver" -ForegroundColor Green
} catch {
    Write-Host "  [ERROR] Python not found. Install from https://python.org" -ForegroundColor Red
    Write-Host "  Ensure 'Add Python to PATH' is checked during installation." -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

# Pip install
Write-Host "  Installing Python dependencies..." -ForegroundColor Gray
python -m pip install click colorama flask --quiet
Write-Host "  Dependencies ready." -ForegroundColor Green

# Create directories
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $StartMenu  | Out-Null

# Write jm.bat
$jmBat = @"
@echo off
setlocal
set JUDGEMAN_HOME=%USERPROFILE%\.judgeman
set PYTHONPATH=$JudgemanSrc\judgeman
python "$JudgemanSrc\judgeman\cli.py" %*
"@
$jmBat | Out-File -FilePath "$InstallDir\jm.bat" -Encoding ASCII
Write-Host "  CLI launcher written: $InstallDir\jm.bat" -ForegroundColor Green

# Write jm-gui.bat
$guiBat = @"
@echo off
setlocal
set JUDGEMAN_HOME=%USERPROFILE%\.judgeman
set PYTHONPATH=$JudgemanSrc\judgeman
python "$JudgemanSrc\jm-gui.py" %*
"@
$guiBat | Out-File -FilePath "$InstallDir\jm-gui.bat" -Encoding ASCII
Write-Host "  GUI launcher written: $InstallDir\jm-gui.bat" -ForegroundColor Green

# Write cross-platform jm-gui.py
$guiPy = @"
import sys, os, webbrowser, time, threading, argparse, subprocess

SCRIPT_DIR = r'$JudgemanSrc'
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'judgeman'))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=7432)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    port = args.port
    url  = f'http://127.0.0.1:{port}'
    if not args.no_browser:
        def open_b():
            time.sleep(1.4)
            webbrowser.open(url)
        threading.Thread(target=open_b, daemon=True).start()
    env = os.environ.copy()
    env['JUDGEMAN_PORT'] = str(port)
    env['JUDGEMAN_HOME'] = os.path.join(os.path.expanduser('~'), '.judgeman')
    gui_app = os.path.join(SCRIPT_DIR, 'gui', 'app.py')
    print(f'\n  Judgeman running at {url}\n  Press Ctrl+C to quit\n')
    subprocess.run([sys.executable, gui_app], env=env)

if __name__ == '__main__':
    main()
"@
$guiPy | Out-File -FilePath "$JudgemanSrc\jm-gui.py" -Encoding UTF8
Write-Host "  GUI Python launcher written: $JudgemanSrc\jm-gui.py" -ForegroundColor Green

# Add to user PATH
$currentPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($currentPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable("PATH", "$currentPath;$InstallDir", "User")
    Write-Host "  Added to PATH: $InstallDir" -ForegroundColor Green
} else {
    Write-Host "  Already in PATH." -ForegroundColor Gray
}

# Start Menu shortcut
$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut("$StartMenu\Judgeman.lnk")
$shortcut.TargetPath    = "$InstallDir\jm-gui.bat"
$shortcut.WorkingDirectory = $JudgemanSrc
$shortcut.Description   = "Judgeman OSINT Reasoning Engine"
$shortcut.Save()
Write-Host "  Start Menu shortcut created." -ForegroundColor Green

# Desktop shortcut
$desktop = [Environment]::GetFolderPath("Desktop")
$ds = $ws.CreateShortcut("$desktop\Judgeman.lnk")
$ds.TargetPath     = "$InstallDir\jm-gui.bat"
$ds.WorkingDirectory = $JudgemanSrc
$ds.Description    = "Judgeman OSINT Reasoning Engine"
$ds.Save()
Write-Host "  Desktop shortcut created." -ForegroundColor Green

Write-Host ""
Write-Host "  Installation complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Launch GUI:  jm-gui  (restart terminal first)" -ForegroundColor White
Write-Host "  Use CLI:     jm init `"My Investigation`"" -ForegroundColor White
Write-Host ""
Write-Host "  NOTE: Open a new terminal window for PATH to take effect." -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to exit"
