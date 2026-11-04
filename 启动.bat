@echo off
chcp 65001 >nul
cd /d "%~dp0"
set HF_ENDPOINT=https://hf-mirror.com
set TOKENIZERS_PARALLELISM=false
start "" "%~dp0vendor\index-tts\.venv\Scripts\pythonw.exe" "%~dp0app\desktop.py"
