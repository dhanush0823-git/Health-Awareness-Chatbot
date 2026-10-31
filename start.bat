@echo off
cd /d "%~dp0"
if not exist .venv py -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Make sure Ollama is running and qwen2.5:1.5b is installed.
ollama list
echo.
python -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
