@echo off
REM Everything here is also available from the tray icon; this is for when you
REM want it on the command line.
REM
REM   Debrief.cmd status     what it has seen and uploaded
REM   Debrief.cmd doctor     why isn't it uploading?
REM   Debrief.cmd review     resolve anything it wasn't sure about
REM   Debrief.cmd login --email
setlocal
set "VENV=%LOCALAPPDATA%\GamingDiver\DebriefUploader\venv"
cd /d "%~dp0"
if not exist "%VENV%\Scripts\python.exe" (
  echo Not set up yet - double-click Start-DebriefUploader.cmd first.
  pause
  exit /b 1
)
"%VENV%\Scripts\python.exe" -m debrief_uploader %*
if "%~1"=="" pause
