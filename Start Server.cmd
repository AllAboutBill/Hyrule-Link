@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" ( echo Please run Install.cmd first. & pause & exit /b )
echo Starting HyruleLink server...  (close this window to stop it)
".venv\Scripts\python.exe" run_server.py --open
pause
