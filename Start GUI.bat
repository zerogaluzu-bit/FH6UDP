@echo off
cd /d "%~dp0"
python udp_listener_gui.py
if errorlevel 1 pause
