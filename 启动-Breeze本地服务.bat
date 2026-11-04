@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Breeze TTS 2 local server (port 7860)

set "BIN=vendor\breeze-cpp\breeze-tts-2-v0.1.6-windows-x64-vulkan\breeze-server.exe"
set "MODEL=vendor\breeze-cpp\models\breeze-tts-2-q8_0.gguf"

if not exist "%BIN%" (
    echo [ERROR] breeze-server.exe not found: %BIN%
    echo Please download Breeze-TTS-2.cpp first.
    pause
    exit /b 1
)
if not exist "%MODEL%" (
    echo [ERROR] model not found: %MODEL%
    echo Please download breeze-tts-2-q8_0.gguf first.
    pause
    exit /b 1
)

echo Starting Breeze TTS 2 (Q8_0, GPU Vulkan) on http://127.0.0.1:7860 ...
echo Keep this window open while using the Breeze TTS 2 model.
echo To use Q4_K instead, edit MODEL in this script.
echo.

"%BIN%" "%MODEL%" --host 127.0.0.1 --port 7860

echo.
echo Server exited. Press any key to close.
pause >nul
