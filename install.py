"""One-shot setup, run by run.bat before the server starts. Idempotent: everything that is already
installed / downloaded is skipped in a second or two.

  1. Python version check
  2. PyTorch with CUDA
  3. Python packages from requirements.txt
  4. ffmpeg (winget) and a JavaScript runtime for yt-dlp (deno, winget)
  5. AI models: Demucs, Whisper, Thai Whisper (downloaded with progress bars)
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
VENV_PY = sys.executable
LINKS = Path(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
os.environ["PATH"] = str(LINKS) + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

MODEL = os.environ.get("KARAOKE_MODEL", "htdemucs")
WHISPER_MODEL = os.environ.get("KARAOKE_WHISPER_MODEL", "large-v3")
WHISPER_TH_MODEL = os.environ.get("KARAOKE_WHISPER_TH_MODEL", "Vinxscribe/biodatlab-whisper-th-large-v3-faster")
LYRICS = os.environ.get("KARAOKE_LYRICS", "1") != "0"


def step(msg):
    print(f"\n[setup] {msg}", flush=True)


def pip(*args):
    subprocess.run([VENV_PY, "-m", "pip", *args], check=True)


def have(module):
    try:
        __import__(module)
        return True
    except Exception:
        return False


def winget_install(pkg_id, name):
    if not shutil.which("winget"):
        print(f"  !! winget not found. Please install {name} manually.")
        return False
    print(f"  installing {name} with winget...")
    r = subprocess.run(["winget", "install", "--id", pkg_id, "-e", "--accept-source-agreements", "--accept-package-agreements", "--silent"])
    return r.returncode == 0


def main():
    # 1) python
    v = sys.version_info
    step(f"Python {v.major}.{v.minor}.{v.micro}")
    if not (3, 10) <= (v.major, v.minor) <= (3, 13):
        print("  !! Python 3.10-3.13 is required.")
        sys.exit(1)

    # 2) torch with CUDA
    step("PyTorch")
    ok = False
    if have("torch"):
        import torch
        ok = torch.version.cuda is not None
        print(f"  torch {torch.__version__} (cuda build: {ok})")
    if not ok:
        print("  installing PyTorch with CUDA 12.4 (about 2.5 GB, a few minutes)...")
        pip("install", "--upgrade", "pip", "-q")
        pip("install", "torch", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cu124")

    # 3) packages
    step("Python packages")
    req = HERE / "requirements.txt"
    stamp = HERE / ".venv" / "requirements.sha1"
    digest = hashlib.sha1(req.read_bytes()).hexdigest()
    need = not all(have(m) for m in ["demucs", "faster_whisper", "yt_dlp", "soundfile", "fastapi", "uvicorn", "pythainlp"])
    if need or not stamp.exists() or stamp.read_text() != digest:
        print("  installing / updating packages...")
        pip("install", "-r", str(req), "-q")
        stamp.write_text(digest)
    else:
        print("  ok")

    # 4) ffmpeg + JS runtime
    step("ffmpeg")
    if shutil.which("ffmpeg") or list(Path(os.environ.get("LOCALAPPDATA", ""), "Microsoft/WinGet/Packages").glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe")):
        print("  ok")
    else:
        winget_install("Gyan.FFmpeg", "ffmpeg")
    step("JavaScript runtime for yt-dlp (deno or node)")
    if shutil.which("deno") or shutil.which("node"):
        print("  ok")
    else:
        winget_install("DenoLand.Deno", "Deno")

    # 5) GPU
    step("GPU")
    import torch
    if torch.cuda.is_available():
        print(f"  {torch.cuda.get_device_name(0)}, {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
    else:
        print("  !! No CUDA GPU detected. The server will run on CPU and be far too slow for live use.")

    # 6) models
    step(f"Demucs model '{MODEL}' (downloads on first run, ~80-330 MB)")
    from demucs.pretrained import get_model
    get_model(MODEL)
    print("  ok")
    if LYRICS:
        from faster_whisper.utils import download_model
        for name in [WHISPER_MODEL, WHISPER_TH_MODEL]:
            if not name:
                continue
            step(f"Whisper model '{name}' (~1.5-3 GB on first run)")
            download_model(name)
            print("  ok")

    print("\n[setup] everything is ready.\n", flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"\n[setup] a step failed (exit {e.returncode}). Fix the error above and run again.")
        sys.exit(1)
