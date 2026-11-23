@echo off
setlocal
cd /d "%~dp0"
if exist runtime\api-packages\fastapi goto portable
if not exist .venv\Scripts\python.exe (
  python -m venv .venv || exit /b 1
)
.venv\Scripts\python.exe -c "import fastapi,uvicorn" 2>nul || .venv\Scripts\python.exe -m pip install -r requirements-api.txt || exit /b 1
.venv\Scripts\python.exe -B launch_ui.py
exit /b %errorlevel%
:portable
runtime\python.exe -B launch_ui.py
