@echo off
cd /d %~dp0
set PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%

REM ---- quality settings (edit here) ----
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

start "" /b cmd /c "timeout /t 4 >nul && start http://127.0.0.1:8765"
.venv\Scripts\python.exe server.py
