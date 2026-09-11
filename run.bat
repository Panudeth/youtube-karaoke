@echo off
setlocal
cd /d %~dp0
title YouTube Karaoke
set PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%

REM ============ settings (edit here) ============
REM model: htdemucs (fast, default) | htdemucs_ft (a bit cleaner, ~4x slower)
set KARAOKE_MODEL=htdemucs
REM shifts: 0 (fastest) | 1 | 2 (slightly cleaner, each step adds a full pass)
set KARAOKE_SHIFTS=0
REM accomp: mix_minus_vocals (full instrument detail) | sum_stems (cleaner vocal removal, duller music)
set KARAOKE_ACCOMP=mix_minus_vocals
REM lyrics: 1 = on, 0 = off. Whisper model: large-v3 (best) | medium | small. Language: blank = auto, or th
set KARAOKE_LYRICS=1
set KARAOKE_WHISPER_MODEL=large-v3
set KARAOKE_LANG=
REM Thai fine-tuned Whisper (auto-used for Thai songs). Set empty to disable and save ~1.6GB VRAM
set KARAOKE_WHISPER_TH_MODEL=Vinxscribe/biodatlab-whisper-th-large-v3-faster
REM ==============================================

echo.
echo  ===== YouTube Karaoke =====
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [setup] creating Python environment...
    py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv 2>nul || py -3.10 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo  Python was not found. Install Python 3.10-3.12 from python.org
        echo  ^(tick "Add python.exe to PATH"^), then run this file again.
        echo.
        start https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" install.py
if errorlevel 1 (
    echo.
    echo  Setup did not finish. See the messages above.
    pause
    exit /b 1
)

REM open the browser once the server reports it is ready (model loaded)
start "" /b ".venv\Scripts\python.exe" wait_open.py

echo.
echo  Starting server... leave this window open while you use YouTube.
echo  Extension: chrome://extensions -^> Developer mode -^> Load unpacked -^> the "extension" folder
echo.
".venv\Scripts\python.exe" server.py
echo.
echo  Server stopped.
pause
