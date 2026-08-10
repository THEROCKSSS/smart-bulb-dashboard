@echo off
REM Start a BRIDGE audio-reactive session (the step the bridge alone does NOT do).
REM
REM Run start-audio-bridge.cmd FIRST and leave it open -- it streams Windows
REM audio to the backend. This script starts the session that consumes that
REM stream and actually drives the bulb. A bridge with no session is silent
REM lights and no error message anywhere, which is why these are two scripts
REM and not one.
REM
REM You will be prompted for the dashboard PIN. It is not echoed and is not
REM passed on the command line, so it stays out of shell history.
REM
REM   start-audio-session.cmd                 bulb-1, band_fixed, 6 bands
REM   start-audio-session.cmd --mode vu_meter
REM   start-audio-session.cmd --stop          stop the session
REM
REM Anything you pass is handed straight to start-audio-session.py.

setlocal
cd /d "%~dp0.."

REM Prefer the backend venv, but fall back to whatever `python` is on PATH:
REM this script is stdlib-only on purpose, so it does not need the venv the
REM way the bridge does (sounddevice/numpy).
set PY=backend\venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" tools\start-audio-session.py %*

echo.
pause
