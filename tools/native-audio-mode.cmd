@echo off
REM Switch the Smart Bulb Dashboard into NATIVE AUDIO MODE.
REM
REM Audio-reactive lighting cannot work while the backend runs in the Docker
REM container -- the container has no host audio devices. This stops the
REM container and runs the backend directly on Windows instead, with real
REM WASAPI capture and zero added latency. It is the path for tuning presets
REM by ear.
REM
REM Double-click this, or run it from a terminal. It stays open and streams
REM the backend's log -- press Ctrl-C to stop, and the container is brought
REM back automatically.
REM
REM   native-audio-mode.cmd            switch to native mode (blocks until Ctrl-C)
REM   native-audio-mode.cmd status     show which mode is serving right now
REM   native-audio-mode.cmd off        restore the container after a hard kill
REM
REM Everyday use wants the bridge instead -- see start-audio-bridge.cmd, which
REM keeps the dashboard serving from the container the whole time.

setlocal
cd /d "%~dp0.."

set ACTION=%1
if "%ACTION%"=="" set ACTION=on

echo Smart Bulb Dashboard - native audio mode (%ACTION%)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "tools\sbd-native-audio.ps1" -Action %ACTION%

echo.
pause
