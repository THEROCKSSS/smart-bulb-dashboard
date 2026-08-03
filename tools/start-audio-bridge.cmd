@echo off
REM Start the Smart Bulb Dashboard audio bridge.
REM
REM Double-click this, or run it from a terminal. It stays open and shows a
REM live level meter so you can see audio actually moving -- close the window
REM or press Ctrl-C to stop.
REM
REM   start-audio-bridge.cmd              stream the default device
REM   start-audio-bridge.cmd --probe      find which device has sound on it
REM   start-audio-bridge.cmd --list       list devices (duplicates collapsed)
REM   start-audio-bridge.cmd --device 85  stream a specific device
REM
REM Anything you pass is handed straight to sbd-audio-bridge.py.

setlocal
cd /d "%~dp0.."

set PY=backend\venv\Scripts\python.exe
if not exist "%PY%" (
  echo Could not find %PY%
  echo Run this from a checkout with the backend venv created.
  pause
  exit /b 1
)

REM Default to the device that was verified working on this machine. Override
REM by passing --device yourself; --probe tells you which one to use.
set ARGS=%*
if "%ARGS%"=="" set ARGS=--device 85

echo Smart Bulb Dashboard - audio bridge
echo   dashboard: http://127.0.0.1:8502/#/audio
echo   bridge port: 8503
echo.
echo Press Ctrl-C to stop.
echo.

"%PY%" tools\sbd-audio-bridge.py %ARGS%

echo.
echo Bridge stopped.
pause
