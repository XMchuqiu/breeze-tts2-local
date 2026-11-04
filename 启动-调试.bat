@echo off
chcp 65001 >nul
cd /d "%~dp0"
set HF_ENDPOINT=https://hf-mirror.com
set TOKENIZERS_PARALLELISM=false
set BREEZE_DEBUG=1
"%~dp0vendor\index-tts\.venv\Scripts\python.exe" "%~dp0app\desktop.py"
pause
