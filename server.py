"""Karaoke live server: resolve YouTube URLs, download audio, separate vocals in chunks, transcribe lyrics, serve both."""
import json
import math
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import soundfile as sf
import torch
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent

# ---- settings (override with environment variables, see run.bat) ----
MODEL = os.environ.get("KARAOKE_MODEL", "htdemucs")                 # htdemucs (fast) | htdemucs_ft (cleaner, 4x slower)
SHIFTS = int(os.environ.get("KARAOKE_SHIFTS", "0"))
ACCOMP_MODE = os.environ.get("KARAOKE_ACCOMP", "mix_minus_vocals")   # mix_minus_vocals | sum_stems
CHUNK_SEC = float(os.environ.get("KARAOKE_CHUNK_SEC", "10"))
MARGIN_SEC = float(os.environ.get("KARAOKE_MARGIN_SEC", "3"))
OPUS_KBPS = os.environ.get("KARAOKE_OPUS_KBPS", "160")                # chunk encoding bitrate (small files for the extension)
LYRICS_ENABLED = os.environ.get("KARAOKE_LYRICS", "1") != "0"
WHISPER_MODEL = os.environ.get("KARAOKE_WHISPER_MODEL", "large-v3")
WHISPER_COMPUTE = os.environ.get("KARAOKE_WHISPER_COMPUTE", "int8_float16")
WHISPER_TH_MODEL = os.environ.get("KARAOKE_WHISPER_TH_MODEL", "Vinxscribe/biodatlab-whisper-th-large-v3-faster")
WHISPER_BATCH = int(os.environ.get("KARAOKE_WHISPER_BATCH", "16"))   # batched decoding: ~7x faster than sequential
WHISPER_BEAM = int(os.environ.get("KARAOKE_WHISPER_BEAM", "5"))
WHISPER_MIN_AHEAD_SEC = float(os.environ.get("KARAOKE_WHISPER_MIN_AHEAD", "45"))
LYRICS_LANG = os.environ.get("KARAOKE_LANG", "") or None
LRCLIB_ENABLED = os.environ.get("KARAOKE_LRCLIB", "1") != "0"   # look lyrics text up on lrclib.net
QUALITY_TAG = f"{MODEL}-{ACCOMP_MODE}" + (f"-s{SHIFTS}" if SHIFTS else "")
CACHE = BASE / "cache" / QUALITY_TAG
CACHE.mkdir(parents=True, exist_ok=True)
print(f"quality: model={MODEL} shifts={SHIFTS} accomp={ACCOMP_MODE} chunk={CHUNK_SEC}s margin={MARGIN_SEC}s lyrics={LYRICS_ENABLED}")


def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    for p in Path(local, "Microsoft/WinGet/Packages").glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
        return str(p)
    raise RuntimeError("ffmpeg not found")


FFMPEG = find_ffmpeg()
print("ffmpeg:", FFMPEG)

app = FastAPI(title="Karaoke Live")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------- state ----------------
lock = threading.Lock()          # protects tracks/pending/lyrics_pending (never call set_status while holding it)
tracks: dict = {}
pending: list = []
wake = threading.Event()
separator = None
lyrics_pending: list = []
lyrics_force: dict = {}          # vid -> "whisper" | "auto" (ignore the uploader's subtitles once)
lyrics_wake = threading.Event()
transcriber = None
transcriber_th = None


class PriorityGPU:
    """One GPU job at a time. Separation (high) never waits behind Whisper (low): low-priority
    acquirers back off while a high-priority job is waiting."""
    def __init__(self):
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._high_waiting = 0

    class _Ctx:
        def __init__(self, parent, high): self.p, self.high = parent, high
        def __enter__(self): self.p.acquire(self.high)
        def __exit__(self, *a): self.p.release()

    def acquire(self, high: bool):
        if high:
            with self._cv:
                self._high_waiting += 1
            self._lock.acquire()
            with self._cv:
                self._high_waiting -= 1
        else:
            while True:
                with self._cv:
                    if self._high_waiting == 0 and self._lock.acquire(blocking=False):
                        return
                time.sleep(0.005)

    def release(self):
        self._lock.release()

    @property
    def high(self): return PriorityGPU._Ctx(self, True)

    @property
    def low(self): return PriorityGPU._Ctx(self, False)


gpu = PriorityGPU()


def set_status(vid: str, **kw):
    with lock:
        tracks.setdefault(vid, {"id": vid}).update(kw)


def get_status(vid: str) -> dict:
    with lock:
        return dict(tracks.get(vid) or {"id": vid, "state": "idle"})


def load_cached():
    for d in CACHE.iterdir():
        meta = d / "meta.json"
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                m["state"] = "done"
                m["lyrics"] = "done" if (d / "lyrics.json").exists() else "pending"
                tracks[d.name] = m
            except Exception:
                pass


load_cached()

# ---------------- yt-dlp ----------------
COOKIES_BROWSER = os.environ.get("KARAOKE_COOKIES_BROWSER")


def ydl_opts(extra=None):
    o = {"quiet": True, "no_warnings": True, "ffmpeg_location": FFMPEG}
    if COOKIES_BROWSER:
        o["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    if extra:
        o.update(extra)
    return o


def resolve(url: str) -> list:
    with yt_dlp.YoutubeDL(ydl_opts({"extract_flat": "in_playlist", "skip_download": True})) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") if info.get("_type") == "playlist" else [info]
    out = []
    for e in entries or []:
        if not e or not e.get("id"):
            continue
        vid = e["id"]
        out.append({"id": vid, "title": e.get("title") or vid, "duration": e.get("duration") or 0,
                    "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                    "uploader": e.get("uploader") or e.get("channel") or ""})
    return out


def download_audio(vid: str, dest: Path) -> Path:
    src = dest / "src.wav"
    if src.exists():
        return src
    tmpl = str(dest / "raw.%(ext)s")
    url = "https://www.youtube.com/watch?v=" + vid
    with yt_dlp.YoutubeDL(ydl_opts({"format": "bestaudio/best", "outtmpl": tmpl, "overwrites": True})) as ydl:
        info = ydl.extract_info(url, download=True)
        set_status(vid, title=info.get("title") or vid, track=info.get("track"), artist=info.get("artist"),
                   uploader=info.get("uploader") or info.get("channel"))
    raw = next(dest.glob("raw.*"))
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(raw),
                    "-ac", "2", "-ar", "44100", "-f", "wav", str(src)], check=True)
    raw.unlink(missing_ok=True)
    return src


# ---------------- separation worker ----------------
def write_chunk(path: Path, x: torch.Tensor, sr: int):
    """Encode a [2, T] float tensor to Ogg/Opus (about 10x smaller than WAV; the extension moves chunks
    through a service worker, so size matters)."""
    tmp = path.with_suffix(".tmp.ogg")
    pcm = x.T.contiguous().numpy().astype("float32").tobytes()
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", "pipe:0",
                    "-c:a", "libopus", "-b:a", f"{OPUS_KBPS}k", "-vbr", "on", "-application", "audio", "-f", "ogg", str(tmp)],
                   input=pcm, check=True)
    os.replace(tmp, path)


def process(vid: str):
    dest = CACHE / vid
    dest.mkdir(exist_ok=True)
    try:
        set_status(vid, state="downloading", chunks_ready=0, total_chunks=0, error=None)
        src = download_audio(vid, dest)
        data, sr = sf.read(str(src), dtype="float32", always_2d=True)
        wav = torch.from_numpy(data.T.copy())
        if sr != separator.sr:
            import torchaudio
            wav = torchaudio.functional.resample(wav, sr, separator.sr)
        duration = wav.shape[1] / separator.sr
        total = math.ceil(duration / CHUNK_SEC)
        set_status(vid, state="processing", total_chunks=total, duration=duration,
                   chunk_sec=CHUNK_SEC, chunks_ready=0, sep_started=time.time())
        t0 = time.time()
        vocals, futures = [], []
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:   # encode on CPU while the GPU separates the next chunk
            def mark_ready():
                n = 0
                for fa, fv in futures:
                    if fa.done() and fv.done():
                        fa.result(); fv.result(); n += 1
                    else:
                        break
                set_status(vid, chunks_ready=n)
            for i, acc, voc in separator.iter_chunks(wav, CHUNK_SEC, MARGIN_SEC, gpu_lock=gpu.high):
                futures.append((pool.submit(write_chunk, dest / f"c{i:04d}_acc.ogg", acc, separator.sr),
                                pool.submit(write_chunk, dest / f"c{i:04d}_voc.ogg", voc, separator.sr)))
                vocals.append(voc)
                mark_ready()
            for fa, fv in futures:
                fa.result(); fv.result()
            set_status(vid, chunks_ready=len(futures))
        sf.write(str(dest / "vocals.wav"), torch.cat(vocals, dim=1).T.numpy(), separator.sr, subtype="PCM_16")
        print(f"[{vid}] separated {duration:.0f}s audio in {time.time()-t0:.1f}s")
        meta = {k: v for k, v in get_status(vid).items() if k not in ("state", "error", "lyrics", "playhead")}
        meta.update(chunks_ready=total, total_chunks=total)
        (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        src.unlink(missing_ok=True)
        set_status(vid, state="done")
        if LYRICS_ENABLED and LRCLIB_ENABLED:
            lookup_lrclib(vid)
        enqueue_lyrics(vid, front=True)
    except Exception as e:  # noqa
        import traceback
        traceback.print_exc()
        set_status(vid, state="error", error=str(e))


def worker():
    global separator
    from separator import Separator
    t0 = time.time()
    separator = Separator(MODEL, shifts=SHIFTS, accomp_mode=ACCOMP_MODE)
    print(f"model loaded on {separator.device} in {time.time()-t0:.1f}s")
    while True:
        vid = None
        with lock:
            if pending:
                vid = pending.pop(0)
        if vid is None:
            wake.wait(timeout=1.0)
            lyrics_wake.set()
            wake.clear()
            continue
        if get_status(vid).get("state") in ("done", "processing", "downloading"):
            continue
        process(vid)


threading.Thread(target=worker, daemon=True).start()


# ---------------- lyrics ----------------
def pick_transcriber(lang):
    return transcriber_th if (lang == "th" and transcriber_th is not None) else transcriber


def whisper_may_run() -> bool:
    """Whisper gets the GPU only while every running separation is comfortably ahead of playback."""
    now = time.time()
    with lock:
        for t in tracks.values():
            st = t.get("state")
            if st == "downloading":
                return False
            if st == "processing":
                ph = t.get("playhead")  # (position, playing, reported_at) from the extension
                if ph and now - ph[2] < 10:
                    if not ph[1]:
                        continue  # video paused: no real-time pressure
                    pos = ph[0] + (now - ph[2])
                else:
                    pos = now - (t.get("sep_started") or now)
                if (t.get("chunks_ready") or 0) * CHUNK_SEC - pos < WHISPER_MIN_AHEAD_SEC:
                    return False
    return True


def enqueue_lyrics(vid: str, front: bool = False):
    if not LYRICS_ENABLED:
        return
    with lock:
        t = tracks.get(vid) or {}
        if t.get("state") != "done" or t.get("lyrics") in ("done", "working"):
            return
        t["lyrics"] = "pending"
        if vid in lyrics_pending:
            lyrics_pending.remove(vid)
        lyrics_pending.insert(0, vid) if front else lyrics_pending.append(vid)
    lyrics_wake.set()


def prefetch_captions(vid: str):
    """Fetch the uploader's subtitles right away (no GPU) so lyrics can show before separation finishes."""
    import lyrics as L
    dest = CACHE / vid
    dest.mkdir(parents=True, exist_ok=True)
    try:
        got = L.fetch_youtube_captions(vid, ydl_opts(), allow_auto=False)
        if got:
            (dest / "captions.json").write_text(json.dumps({"source": got[2], "language": got[1], "lines": got[0]}, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa
        print(f"[lyrics] caption prefetch failed {vid}: {e}")
    set_status(vid, captions_checked=True)


def lookup_lrclib(vid: str):
    """Lyrics database lookup (text only, no GPU). Cached in lrclib.json; {} means not found."""
    import lyrics as L
    dest = CACHE / vid
    p = dest / "lrclib.json"
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        return d or None
    t = get_status(vid)
    title = t.get("track") or t.get("title") or ""
    artist = t.get("artist") or (t.get("uploader") or "").replace("Official", "").replace("official", "").strip()
    got = None
    try:
        got = L.fetch_lrclib(title, artist, float(t.get("duration") or 0))
        if not got and t.get("track") and t.get("title"):
            got = L.fetch_lrclib(t["title"], None, float(t.get("duration") or 0))
    except Exception as e:  # noqa
        print(f"[lyrics] lrclib lookup failed {vid}: {e}")
    dest.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(got or {}, ensure_ascii=False), encoding="utf-8")
    return got


def make_lyrics(vid: str):
    import lyrics as L
    dest = CACHE / vid
    out = {"source": "none", "language": None, "lines": []}
    try:
        set_status(vid, lyrics="working", lyrics_progress=0.0)
        force = lyrics_force.pop(vid, None)
        cap = dest / "captions.json"
        got = None
        if force is None:
            if cap.exists():
                c = json.loads(cap.read_text(encoding="utf-8"))
                got = (c["lines"], c["language"], c["source"])
            else:
                try:
                    got = L.fetch_youtube_captions(vid, ydl_opts(), allow_auto=False)
                except Exception as e:  # noqa
                    print(f"[lyrics] youtube captions failed: {e}")
        cands = []   # (name, lines, language): trusted texts to be timed with Whisper
        if got:
            cands.append((got[2], got[0], got[1]))
        if force is None and LRCLIB_ENABLED:
            lr = lookup_lrclib(vid)
            if lr:
                cands.append(("lrclib", lr["lines"], None))
        have_vocals = transcriber is not None and force != "auto" and (dest / "vocals.wav").exists()
        if cands and not have_vocals:
            name, lines_c, language = cands[0]
            timed = [ln for ln in lines_c if ln.get("s") is not None]
            out = {"source": name, "language": language, "lines": L.split_caption_lines(timed) if timed else []}
        elif have_vocals:
            data, sr = sf.read(str(dest / "vocals.wav"), dtype="float32", always_2d=True)
            voc = torch.from_numpy(data.T.copy())
            t0 = time.time()
            lang = get_status(vid).get("lang") or LYRICS_LANG or (got[1] if got and len(got[1]) == 2 else None)
            if not lang:
                det, prob = transcriber.detect_language(L.loudest_window(voc, sr, 30.0), sr, gpu_lock=gpu.low, may_run=whisper_may_run)
                if prob >= 0.5:
                    lang = det
                print(f"[lyrics] {vid}: language {det} (p={prob:.2f})")
            model = pick_transcriber(lang)
            raw, lang_out = model.transcribe(voc, sr, language=lang, gpu_lock=gpu.low, may_run=whisper_may_run,
                                             batch_size=WHISPER_BATCH, beam_size=WHISPER_BEAM,
                                             on_progress=lambda p: set_status(vid, lyrics_progress=p))
            best = None   # keep the trusted text that matches the singing best
            for name, lines_c, language in cands:
                aligned, ratio = L.align_text(lines_c, raw, voc, sr, min_ratio=0.45)
                print(f"[lyrics] {vid}: candidate {name} alignment {ratio:.0%}")
                if ratio >= 0.45 and (best is None or ratio > best[1] + 0.05):
                    best = (name, ratio, aligned, language)
            if best:
                out = {"source": best[0] + "+whisper", "language": best[3] or lang_out, "lines": best[2], "match": round(best[1], 2)}
            else:
                out = {"source": "whisper", "language": lang_out, "lines": L.postprocess(raw, voc, sr)}
            lines = out["lines"]
            print(f"[lyrics] {vid}: {out['source']} {len(lines)} lines in {time.time()-t0:.1f}s")
        if not out["lines"]:
            try:
                got = L.fetch_youtube_captions(vid, ydl_opts(), allow_auto=True)
                if got:
                    out = {"source": got[2], "language": got[1], "lines": got[0]}
            except Exception as e:  # noqa
                print(f"[lyrics] auto captions failed: {e}")
        (dest / "lyrics.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        set_status(vid, lyrics="done", lyrics_source=out["source"], lyrics_progress=1.0)
    except Exception as e:  # noqa
        import traceback
        traceback.print_exc()
        set_status(vid, lyrics="error", lyrics_error=str(e))


def lyrics_worker():
    global transcriber, transcriber_th
    if not LYRICS_ENABLED:
        return
    while separator is None:
        time.sleep(0.5)
    try:
        from lyrics import Transcriber
        transcriber = Transcriber(WHISPER_MODEL, WHISPER_COMPUTE)
    except Exception as e:  # noqa
        print(f"[lyrics] whisper unavailable ({e}); will use YouTube captions only")
    if transcriber is not None and WHISPER_TH_MODEL:
        try:
            transcriber_th = Transcriber(WHISPER_TH_MODEL, WHISPER_COMPUTE)
        except Exception as e:  # noqa
            print(f"[lyrics] Thai whisper model unavailable ({e}); using {WHISPER_MODEL} for Thai too")
    print("\n  READY  ->  web player: http://127.0.0.1:8765   |   extension: open youtube.com\n", flush=True)
    while True:
        vid = None
        with lock:
            if lyrics_pending:
                vid = lyrics_pending.pop(0)
        if vid is None:
            lyrics_wake.wait(timeout=1.0)
            lyrics_wake.clear()
            continue
        make_lyrics(vid)


threading.Thread(target=lyrics_worker, daemon=True).start()


# ---------------- API ----------------
class UrlIn(BaseModel):
    url: str


class PrepareIn(BaseModel):
    ids: list
    lang: str | None = None


class PlayheadIn(BaseModel):
    id: str
    t: float
    playing: bool = True


class RedoIn(BaseModel):
    source: str = "whisper"
    lang: str | None = None


@app.post("/api/resolve")
def api_resolve(body: UrlIn):
    try:
        items = resolve(body.url.strip())
    except Exception as e:
        raise HTTPException(400, f"ดึงข้อมูลไม่ได้: {e}")
    with lock:
        for it in items:
            tracks.setdefault(it["id"], {"id": it["id"], "state": "idle"}).setdefault("title", it["title"])
    return {"items": items}


@app.post("/api/prepare")
def api_prepare(body: PrepareIn):
    """Set processing priority (first id = playing now). Also queues lyrics and caption prefetch."""
    prefetch = []
    with lock:
        pending.clear()
        for vid in body.ids:
            t = tracks.setdefault(vid, {"id": vid})
            t["lang"] = body.lang or None
            st = t.get("state")
            if st not in ("done", "processing", "downloading"):
                t["state"] = "queued"
                pending.append(vid)
            if LYRICS_ENABLED and st != "done" and not t.get("captions_checked"):
                t["captions_checked"] = "working"
                prefetch.append(vid)
    wake.set()
    for vid in prefetch:
        threading.Thread(target=prefetch_captions, args=(vid,), daemon=True).start()
    for i, vid in enumerate(body.ids):
        enqueue_lyrics(vid, front=(i == 0))
    return {"ok": True, "pending": list(pending)}


@app.post("/api/playhead")
def api_playhead(body: PlayheadIn):
    set_status(body.id, playhead=(body.t, body.playing, time.time()))
    return {"ok": True}


@app.get("/api/status/{vid}")
def api_status(vid: str):
    t = get_status(vid)
    t.pop("playhead", None)
    t["model_ready"] = separator is not None
    return t


@app.get("/api/status")
def api_status_all():
    with lock:
        return {"model_ready": separator is not None, "pending": list(pending), "quality": QUALITY_TAG,
                "tracks": {k: {"state": v.get("state"), "chunks_ready": v.get("chunks_ready", 0),
                               "total_chunks": v.get("total_chunks", 0), "lyrics": v.get("lyrics")} for k, v in tracks.items()}}


@app.get("/api/chunk/{vid}/{n}/{stem}")
def api_chunk(vid: str, n: int, stem: str):
    if stem not in ("acc", "voc"):
        raise HTTPException(400, "bad stem")
    p = CACHE / vid / f"c{n:04d}_{stem}.ogg"
    if p.exists():
        return FileResponse(str(p), media_type="audio/ogg", headers={"Cache-Control": "max-age=86400"})
    p = CACHE / vid / f"c{n:04d}_{stem}.wav"
    if p.exists():
        return FileResponse(str(p), media_type="audio/wav", headers={"Cache-Control": "max-age=86400"})
    raise HTTPException(404, "chunk not ready")


@app.post("/api/lyrics/{vid}/redo")
def api_lyrics_redo(vid: str, body: RedoIn):
    """Re-transcribe with Whisper (or YouTube auto captions), ignoring the uploader's subtitles."""
    (CACHE / vid / "lyrics.json").unlink(missing_ok=True)
    (CACHE / vid / "captions.json").unlink(missing_ok=True)
    with lock:
        t = tracks.get(vid)
        if not t or t.get("state") != "done":
            raise HTTPException(400, "track not separated yet")
        t["lyrics"] = "pending"
        t["lang"] = body.lang or None
        lyrics_force[vid] = body.source
    enqueue_lyrics(vid, front=True)
    return {"ok": True}


@app.get("/api/lyrics/{vid}")
def api_lyrics(vid: str):
    p = CACHE / vid / "lyrics.json"
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        d["state"] = "done"; d["final"] = True
        return d
    t = get_status(vid)
    cap = CACHE / vid / "captions.json"
    lr = CACHE / vid / "lrclib.json"
    early = None
    if cap.exists() and vid not in lyrics_force:
        early = json.loads(cap.read_text(encoding="utf-8"))
    elif lr.exists() and vid not in lyrics_force:
        d = json.loads(lr.read_text(encoding="utf-8"))
        if d and d.get("synced"):
            early = {"source": "lrclib", "language": None, "lines": d["lines"]}
    if early:
        import lyrics as L
        early["lines"] = L.split_caption_lines([ln for ln in early.get("lines") or [] if ln.get("s") is not None])
        early["state"] = "done"; early["final"] = transcriber is None   # refined (Whisper-timed) version follows
        early["progress"] = t.get("lyrics_progress", 0.0)
        return early
    return {"state": t.get("lyrics") or ("pending" if t.get("state") == "done" else "waiting"),
            "progress": t.get("lyrics_progress", 0.0), "error": t.get("lyrics_error"),
            "separation": t.get("state"), "sep_progress": (t.get("chunks_ready") or 0) / (t.get("total_chunks") or 1)}


app.mount("/", StaticFiles(directory=str(BASE / "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
