@echo off
cd /d "%~dp0"
if not exist "..\venv\Scripts\python.exe" (
  echo Creating venv...
  python -m venv ..\venv
  ..\venv\Scripts\pip.exe install -r requirements.txt
)
echo Starting BACKEND on http://127.0.0.1:5050
..\venv\Scripts\python.exe app.py
pause
