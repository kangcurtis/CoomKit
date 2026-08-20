@echo off
rem CoomKit launcher for Windows - stdlib Python only, nothing to install.
rem Double-click this file, then open http://127.0.0.1:3939 in your browser.
setlocal
rem pushd, not cd /d: cmd cannot make a UNC path the current directory, and a
rem failed cd does NOT stop a batch file - running from a network share would
rem sail on and try to start server.py out of C:\Windows. pushd maps a drive
rem letter for UNC paths, and the existence check catches the other silent
rem wrong-directory case: double-clicking run.bat inside an unextracted ZIP,
rem where Explorer unpacks only the .bat into a temp folder.
pushd "%~dp0" >nul 2>nul
if not exist server.py (
  echo This window is not running from the CoomKit folder - server.py is not
  echo next to run.bat. If you launched this from inside a ZIP, extract the
  echo whole folder first and run it from there.
  echo.
  pause
  exit /b 1
)

rem Prefer the py launcher (ships with python.org installers); fall back to
rem python on PATH. On a bare Windows, "python" is a Microsoft Store alias
rem that prints an ad and exits nonzero - the version check below catches it
rem rather than trusting the name.
set "PY=py -3"
%PY% -c "import sys" >nul 2>nul || set "PY=python"

rem 3.10 is a hard floor: the annotations use PEP 604 unions, evaluated at
rem import time, so 3.9 dies with a TypeError that reads like a CoomKit bug.
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo CoomKit needs Python 3.10 or newer, and no working one was found.
  echo Install it from https://www.python.org/downloads/ and run this again.
  echo Nothing else to install - CoomKit itself has no dependencies.
  echo.
  pause
  exit /b 1
)

echo CoomKit starting on http://127.0.0.1:3939 - keep this window open.
%PY% server.py %*

rem Keep the window up when the server exits, so a crash is readable instead
rem of a window that flashes and vanishes.
echo.
echo CoomKit stopped.
pause
