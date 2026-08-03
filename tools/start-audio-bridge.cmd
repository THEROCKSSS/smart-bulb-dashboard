@echo off
REM Start the Smart Bulb Dashboard audio bridge.
REM
REM Double-click this, or run it from a terminal. It stays open and shows a
REM live level meter so you can see audio actually moving -- close the window
REM or press Ctrl-C to stop.
REM
REM   start-audio-bridge.cmd               auto-pick whatever has sound on it
REM   start-audio-bridge.cmd --probe       full per-device signal breakdown
REM   start-audio-bridge.cmd --list        list devices (duplicates collapsed)
REM   start-audio-bridge.cmd --device 124  stream a specific device
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

REM Default to auto-detect rather than a fixed index. This used to hardcode
REM "--device 85", which is silent on this machine whenever desktop audio is
REM routed through Voicemeeter instead of VB-Cable -- so the bridge connected,
REM streamed flawlessly, and delivered nothing but zeros. Everything looked
REM healthy and the lights did nothing.
REM
REM --auto measures every input and picks the one that actually has sound on
REM it right now. Override by passing --device yourself; --probe prints the
REM full per-device breakdown.
set ARGS=%*
if "%ARGS%"=="" set ARGS=--auto

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
