@echo off
REM Double-click this to start the uploader. It sets itself up the first time,
REM then puts an icon in your system tray and this window closes -- everything
REM after that happens from the tray icon.
setlocal
set "HOME_DIR=%LOCALAPPDATA%\GamingDiver\DebriefUploader"
set "VENV=%HOME_DIR%\venv"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "PY=%VENV%\Scripts\python.exe"
cd /d "%~dp0"

REM --- find Python -----------------------------------------------------------
set "BOOTPY="
where py >nul 2>&1 && set "BOOTPY=py -3"
if not defined BOOTPY (where python >nul 2>&1 && set "BOOTPY=python")
if not defined BOOTPY (
  echo.
  echo Python is not installed.
  echo.
  echo Get it from https://www.python.org/downloads/  --  during setup, TICK
  echo "Add python.exe to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

REM --- one-time setup --------------------------------------------------------
if not exist "%PY%" (
  echo Setting up for the first time. This takes a minute...
  %BOOTPY% -m venv "%VENV%" || goto :fail
  "%PY%" -m pip install --quiet --upgrade pip
  "%PY%" -m pip install --quiet -r requirements.txt || goto :fail
  "%PY%" -m debrief_uploader setup
  echo.
  echo Ready. Look for the diver icon in your system tray -- click it and
  echo choose "Sign in..." to finish.
  echo.
)

REM --- the tray needs pystray; without it we would vanish silently ------------
"%PY%" -c "import pystray" >nul 2>&1
if errorlevel 1 (
  echo Installing the system tray component...
  "%PY%" -m pip install --quiet pystray || goto :fail
)

REM --- launch detached and get out of the way --------------------------------
REM pythonw.exe has no console, and `start` hands it off so this window can
REM close immediately rather than sitting there for the rest of the session.
if not exist "%PYW%" (
  echo Could not find pythonw.exe -- starting with a console window instead.
  start "" /D "%~dp0" "%PY%" -m debrief_uploader run --tray
  exit /b 0
)

start "" /D "%~dp0" "%PYW%" -m debrief_uploader run --tray --quiet
exit /b 0

:fail
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
