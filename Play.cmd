@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" ( echo Please run Install.cmd first. & pause & exit /b )
start "" ".venv\Scripts\pythonw.exe" agent_gui.py
