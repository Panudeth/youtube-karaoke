# Start Karaoke Live server, then open browser
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PATH = "$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:PATH"

# ---- quality settings (edit here) ----
$env:KARAOKE_MODEL  = "htdemucs"           # htdemucs (fast, default) | htdemucs_ft (a bit cleaner, ~4x slower)
$env:KARAOKE_SHIFTS = "0"                  # 0 fastest | 1 | 2 slightly cleaner
$env:KARAOKE_ACCOMP = "mix_minus_vocals"   # mix_minus_vocals (full instrument detail) | sum_stems
$env:KARAOKE_LYRICS = "1"                  # 1 on | 0 off
$env:KARAOKE_WHISPER_MODEL = "large-v3"    # large-v3 (best) | medium | small
$env:KARAOKE_LANG = ""                     # blank = auto-detect, or "th"
$env:KARAOKE_WHISPER_TH_MODEL = "Vinxscribe/biodatlab-whisper-th-large-v3-faster"  # Thai fine-tuned model, "" to disable

Start-Job -ScriptBlock { Start-Sleep 3; Start-Process "http://127.0.0.1:8765" } | Out-Null
& ".\.venv\Scripts\python.exe" server.py
