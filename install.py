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


def nvidia_smi():
    """(gpu name, driver version) from nvidia-smi, or (None, None)."""
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        out = subprocess.run([exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=15).stdout.strip().splitlines()
        name, drv = [x.strip() for x in out[0].split(",")[:2]]
        return name, drv
    except Exception:
        return None, None


def driver_major(drv):
    try:
        return int(str(drv).split(".")[0])
    except Exception:
        return 0


def torch_supports_gpu():
    """True when the installed PyTorch has compiled kernels for the GPU in this machine (sm_xx match)
    and a tiny CUDA op actually runs."""
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        major, minor = torch.cuda.get_device_capability(0)
        archs = torch.cuda.get_arch_list()
        if archs and not any(a in (f"sm_{major}{minor}", f"compute_{major}{minor}") for a in archs) and not any(a.startswith(f"sm_{major}") for a in archs):
            return False
        return bool((torch.ones(2, device="cuda") * 2).sum().item() == 4)
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

    # 2) torch with CUDA. RTX 50-series (Blackwell) needs a CUDA 12.8 build; older GPUs work with either.
    step("PyTorch")
    gpu_name, driver = nvidia_smi()
    if gpu_name:
        print(f"  GPU: {gpu_name} (driver {driver or '?'})")
    else:
        print("  !! nvidia-smi not found: no NVIDIA driver installed? Install the latest GeForce driver first.")
    want_cu128 = (driver_major(driver) >= 570) or ("RTX 50" in (gpu_name or "").upper())
    cuda_tag = "cu128" if want_cu128 else "cu124"
    ok = False
    if have("torch"):
        import torch
        print(f"  torch {torch.__version__} (cuda build: {torch.version.cuda})")
        ok = torch.version.cuda is not None and torch_supports_gpu()
        if not ok and torch.version.cuda is not None:
            print("  !! this PyTorch build has no kernels for your GPU; reinstalling the right build")
    if not ok:
        print(f"  installing PyTorch with CUDA ({cuda_tag}, about 2.5-3 GB, a few minutes)...")
        pip("install", "--upgrade", "pip", "-q")
        pip("install", "--upgrade", "--force-reinstall", "--no-deps", "torch", "torchaudio", "--index-url", f"https://download.pytorch.org/whl/{cuda_tag}")
        pip("install", "torch", "torchaudio", "--index-url", f"https://download.pytorch.org/whl/{cuda_tag}")  # pull their dependencies
        for m in list(sys.modules):
            if m == "torch" or m.startswith("torch."):
                del sys.modules[m]
        if not torch_supports_gpu():
            print("  !! PyTorch still cannot use this GPU. Update the NVIDIA driver (GeForce Experience / nvidia.com) and run again.")
            sys.exit(1)

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
        print(f"  {torch.cuda.get_device_name(0)}, {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB, "
              f"compute {'.'.join(map(str, torch.cuda.get_device_capability(0)))}, torch cuda {torch.version.cuda}")
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
