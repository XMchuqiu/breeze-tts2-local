@echo off
rem Breeze Voice Studio launcher build entry.
rem Keep this file ASCII-only: cmd.exe mis-parses UTF-8 batch files with
rem long Chinese lines. All logic and Chinese output live in launcher\build.py
setlocal
cd /d "%~dp0"

set "PY=%~dp0vendor\index-tts\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] python.exe not found.
    echo Expected: vendor\index-tts\.venv\Scripts\python.exe
    echo Install project dependencies first, then run this script again.
    pause
    exit /b 1
)

"%PY%" "%~dp0launcher\build.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo Build failed. Common causes: no network, disk full,
    echo read-only folder, or antivirus blocking PyInstaller.
)
pause
exit /b %CODE%
