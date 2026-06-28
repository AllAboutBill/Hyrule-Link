@echo off
cd /d "%~dp0"
echo Setting up HyruleLink (one time)...
if exist ".venv\Scripts\python.exe" goto deps
py -m venv .venv 2>nul || python -m venv .venv
:deps
echo Installing dependencies (this can take a minute)...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
echo.
echo Setup complete!  Host: run "Start Server.cmd".   Player: run "Play.cmd".
pause
