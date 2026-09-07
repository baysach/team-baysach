@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo The local Python environment is missing.
  echo Follow the Install section in README.md, then run this file again.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
