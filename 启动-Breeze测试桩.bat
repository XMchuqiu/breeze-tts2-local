@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Breeze TTS 2 test stub (protocol check only, NOT real speech)
echo Breeze TTS 2 protocol test stub - outputs test tones, not real speech.
echo For real speech use 启动-Breeze本地服务.bat instead.
echo Keep this window open while using the Breeze TTS 2 model.
start "" /min "%~dp0vendor\index-tts\.venv\Scripts\python.exe" "%~dp0tools\breeze_stub.py" --port 7860 --log tmp\breeze_stub.jsonl
