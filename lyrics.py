"""Lyrics for karaoke display.

Order of preference:
  1. Subtitles uploaded to YouTube by the channel (manual captions)
  2. Whisper (faster-whisper, batched) transcription of the separated *vocals* track, word timestamps,
     Thai word segmentation with PyThaiNLP, lines cut on breath pauses
  3. YouTube auto-generated captions (only if Whisper is unavailable)

Result format (lyrics.json):
  {"source": "youtube"|"whisper"|"auto"|"none", "language": "th", "lines": [
      {"s": 12.3, "e": 15.8, "text": "...", "words": [{"s": 12.3, "e": 12.9, "w": "..."}, ...]}, ...]}
"""
from __future__ import annotations

import json
import re
import threading
import time

import numpy as np
import torch
import torchaudio
import yt_dlp

THAI_RE = re.compile(r"[฀-๿]")


# ---------------------------------------------------------------- YouTube captions
def _parse_json3(raw: bytes) -> list:
    data = json.loads(raw.decode("utf-8"))
    lines = []
    for ev in data.get("events", []):
        segs = ev.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text or "tStartMs" not in ev:
            continue
        s = ev["tStartMs"] / 1000.0
        e = s + (ev.get("dDurationMs") or 3000) / 1000.0
        words = []
        for sg in segs:
            w = sg.get("utf8", "").strip()
            if not w:
                continue
            ws = s + (sg.get("tOffsetMs") or 0) / 1000.0
            words.append({"s": ws, "e": e, "w": w})
        for i in range(len(words) - 1):
            words[i]["e"] = words[i + 1]["s"]
        lines.append({"s": s, "e": e, "text": text, "words": words})
    return [ln for ln in lines if ln["e"] > ln["s"]]


def fetch_youtube_captions(vid: str, ydl_opts: dict, prefer=("th", "en"), allow_auto=False):
    """Returns (lines, lang, source) or None."""
    url = "https://www.youtube.com/watch?v=" + vid
    opts = dict(ydl_opts)
    opts.update({"skip_download": True, "writesubtitles": True, "writeautomaticsub": allow_auto,
                 "subtitleslangs": ["all"]})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        pools = [("youtube", info.get("subtitles") or {})]
        if allow_auto:
            pools.append(("auto", info.get("automatic_captions") or {}))
        for source, subs in pools:
            langs = [l for l in prefer if l in subs] + sorted(k for k in subs if k not in prefer and not k.startswith("live_chat"))
            for lang in langs:
                fmt = next((f for f in subs[lang] if f.get("ext") == "json3"), None)
                if not fmt or not fmt.get("url"):
                    continue
                try:
                    lines = _parse_json3(ydl.urlopen(fmt["url"]).read())
                    if len(lines) >= 3:
                        return lines, lang, source
                except Exception as e:  # noqa
                    print(f"[lyrics] caption fetch failed {lang}: {e}")
    return None


# ---------------------------------------------------------------- Whisper
def _to16k(vocals: torch.Tensor, sr: int) -> np.ndarray:
    return torchaudio.functional.resample(vocals.mean(0), sr, 16000).numpy().astype(np.float32)


class Transcriber:
    def __init__(self, model_name: str = "large-v3", compute_type: str = "float16", device: str = "cuda"):
        from faster_whisper import WhisperModel, BatchedInferencePipeline
        t0 = time.time()
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self.batched = BatchedInferencePipeline(self.model)
        print(f"[lyrics] whisper {model_name} ({compute_type}) loaded in {time.time()-t0:.1f}s")

    def detect_language(self, vocals: torch.Tensor, sr: int, gpu_lock=None, may_run=None):
        audio16 = _to16k(vocals, sr)[: 16000 * 30]
        while may_run is not None and not may_run():
            time.sleep(0.2)
        with (gpu_lock or threading.Lock()):
            lang, prob, _ = self.model.detect_language(audio16)
        return lang, float(prob)

    def transcribe(self, vocals: torch.Tensor, sr: int, language: str | None = None,
                   gpu_lock=None, may_run=None, batch_size: int = 8, beam_size: int = 5, on_progress=None):
        """Whole-track batched transcription. Returns (raw_lines, language): raw lines carry Whisper token
        'words' (for Thai these are sub-word pieces; see postprocess)."""
        audio16 = _to16k(vocals, sr)
        total = len(audio16) / 16000
        lock = gpu_lock or threading.Lock()

        def wait_turn():
            while may_run is not None and not may_run():
                time.sleep(0.2)

        wait_turn()
        with lock:
            segments, info = self.batched.transcribe(
                audio16, language=language or None, batch_size=batch_size, beam_size=beam_size,
                word_timestamps=True, vad_filter=True, condition_on_previous_text=False,
                vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
            )
        lines, it = [], iter(segments)
        while True:
            wait_turn()
            with lock:  # each next() decodes one batch; release between batches so separation is not starved
                try:
                    seg = next(it)
                except StopIteration:
                    break
            text = seg.text.strip()
            if not text:
                continue
            words = [{"s": float(w.start), "e": float(w.end), "w": w.word} for w in (seg.words or []) if w.word.strip()]
            lines.append({"s": float(seg.start), "e": float(seg.end), "text": text, "words": words})
            if on_progress:
                on_progress(min(1.0, float(seg.end) / max(total, 1)))
        return lines, info.language


# ---------------------------------------------------------------- helpers
def voiced_fraction(vocals: torch.Tensor, sr: int, frame_ms: int = 50) -> float:
    mono = vocals.mean(0).numpy()
    n = int(sr * frame_ms / 1000)
    nf = len(mono) // n
    if nf == 0:
        return 0.0
    rms = np.sqrt((mono[: nf * n].reshape(nf, n) ** 2).mean(1))
    peak = np.percentile(rms, 95)
    return 0.0 if peak < 1e-3 else float((rms > 0.15 * peak).mean())


def loudest_window(vocals: torch.Tensor, sr: int, win_sec: float) -> torch.Tensor:
    T = vocals.shape[1]
    w = int(win_sec * sr)
    if T <= w:
        return vocals
    mono = vocals.mean(0).numpy()
    hop = w // 4
    best, best_e = 0, -1.0
    for s in range(0, T - w, hop):
        e = float((mono[s:s + w] ** 2).mean())
        if e > best_e:
            best, best_e = s, e
    return vocals[:, best:best + w]


# ---------------------------------------------------------------- post-processing
def energy_filter(lines: list, vocals: torch.Tensor, sr: int, frame_ms: int = 50) -> list:
    """Drop tokens/lines that fall on near-silent parts of the vocals track (Whisper hallucinations)."""
    mono = vocals.mean(0).numpy()
    n = int(sr * frame_ms / 1000)
    nf = len(mono) // n
    if nf == 0:
        return lines
    rms = np.sqrt((mono[: nf * n].reshape(nf, n) ** 2).mean(1))
    voiced = rms[rms > 1e-4]
    if len(voiced) == 0:
        return []
    thr = 0.06 * np.percentile(voiced, 90)

    def mean_rms(s, e):
        a = int(s * 1000 / frame_ms)
        b = max(int(e * 1000 / frame_ms), a + 1)
        a, b = max(0, min(a, nf - 1)), max(1, min(b, nf))
        return float(rms[a:b].mean()) if b > a else 0.0

    out, prev, repeat = [], None, 0
    for ln in lines:
        if ln["words"]:
            kept = [w for w in ln["words"] if mean_rms(w["s"], w["e"]) >= thr]
            if not kept:
                continue
            ln = dict(ln, words=kept, s=kept[0]["s"], e=kept[-1]["e"], text="".join(w["w"] for w in kept).strip())
        elif mean_rms(ln["s"], ln["e"]) < thr:
            continue
        norm = re.sub(r"\s+", "", ln["text"]).lower()
        repeat = repeat + 1 if norm == prev else 0
        prev = norm
        if repeat >= 3:
            continue
        out.append(ln)
    return out


def _thai_tokenizer():
    try:
        from pythainlp.tokenize import word_tokenize
        return lambda t: word_tokenize(t, engine="newmm", keep_whitespace=False)
    except Exception:
        return None


def tokens_to_words(lines: list) -> list:
    """Turn Whisper token pieces into real words with timings.
    Thai text: re-segment with PyThaiNLP and map timings through character offsets.
    Other text: Whisper's pieces are already words (they start with a space)."""
    tok = _thai_tokenizer()
    words = []
    for ln in lines:
        pieces = ln.get("words") or []
        if not pieces:
            words.append({"s": ln["s"], "e": ln["e"], "w": ln["text"].strip(), "brk": True})
            continue
        text = "".join(p["w"] for p in pieces)
        if tok and THAI_RE.search(text):
            # character -> time map (linear inside each piece)
            ctimes, csrc = [], []
            for p in pieces:
                n = max(len(p["w"]), 1)
                for i, ch in enumerate(p["w"]):
                    ctimes.append((p["s"] + (p["e"] - p["s"]) * i / n, p["s"] + (p["e"] - p["s"]) * (i + 1) / n))
                    csrc.append(ch)
            pos, first = 0, True
            for w in tok(text):
                idx = text.find(w, pos)
                if idx < 0:
                    continue
                brk = first or (idx > 0 and text[idx - 1].isspace())
                a, b = idx, idx + len(w) - 1
                words.append({"s": ctimes[a][0], "e": ctimes[b][1], "w": w.strip(), "brk": brk})
                pos = idx + len(w)
                first = False
        else:
            for i, p in enumerate(pieces):
                w = p["w"].strip()
                if w:
                    words.append({"s": p["s"], "e": p["e"], "w": w, "brk": i == 0})
    words = [w for w in words if w["w"]]
    for i in range(len(words) - 1):  # keep timings monotonic
        if words[i + 1]["s"] < words[i]["s"]:
            words[i + 1]["s"] = words[i]["s"]
        if words[i]["e"] > words[i + 1]["s"]:
            words[i]["e"] = words[i + 1]["s"]
    return words


def _join(ws: list) -> str:
    out = ""
    for w in ws:
        if out and (re.search(r"[A-Za-z0-9,.!?')]$", out) or re.match(r"[A-Za-z0-9(]", w["w"])) and not THAI_RE.search(out[-1] + w["w"][0]):
            out += " "
        elif out and re.search(r"[A-Za-z0-9]$", out) and THAI_RE.search(w["w"][0]):
            out += " "
        elif out and THAI_RE.search(out[-1]) and re.match(r"[A-Za-z0-9]", w["w"]):
            out += " "
        out += w["w"]
    return out.strip()


def build_lines(words: list, max_chars: int = 34, max_dur: float = 5.5, pause: float = 0.45) -> list:
    """Cut a word stream into karaoke lines: on breath pauses, phrase boundaries (Whisper spaces),
    or when a line gets too long / too slow."""
    lines, cur = [], []

    def flush():
        if cur:
            lines.append({"s": cur[0]["s"], "e": cur[-1]["e"], "text": _join(cur),
                          "words": [{"s": w["s"], "e": w["e"], "w": w["w"]} for w in cur]})

    for w in words:
        if cur:
            gap = w["s"] - cur[-1]["e"]
            ln_len = len(_join(cur + [w]))
            dur = w["e"] - cur[0]["s"]
            if gap > pause or ln_len > max_chars or dur > max_dur or (w.get("brk") and len(_join(cur)) >= max_chars * 0.55):
                flush()
                cur = []
        cur.append(w)
    flush()
    # merge tiny fragments (1-3 chars) into the neighbour they are closest to
    merged = []
    for ln in lines:
        if merged and len(ln["text"]) < 4 and ln["s"] - merged[-1]["e"] <= pause and len(merged[-1]["text"]) + len(ln["text"]) <= max_chars:
            p = merged.pop()
            ws = p["words"] + ln["words"]
            ln = {"s": p["s"], "e": ln["e"], "text": _join(ws), "words": ws}
        merged.append(ln)
    return [ln for ln in merged if ln["text"]]


def postprocess(raw_lines: list, vocals: torch.Tensor, sr: int) -> list:
    return build_lines(tokens_to_words(energy_filter(raw_lines, vocals, sr)))


# ---------------------------------------------------------------- captions -> karaoke lines
def _tokenize_text(text: str) -> list:
    """Split caption text into display words (PyThaiNLP for Thai, whitespace otherwise), keeping char offsets."""
    tok = _thai_tokenizer()
    out, pos = [], 0
    if tok and THAI_RE.search(text):
        for w in tok(text):
            idx = text.find(w, pos)
            if idx < 0:
                continue
            if w.strip():
                out.append((w.strip(), idx, idx + len(w.strip())))
            pos = idx + len(w)
    else:
        for m in re.finditer(r"\S+", text):
            out.append((m.group(), m.start(), m.end()))
    return out


def split_caption_lines(cap_lines: list, max_chars: int = 34) -> list:
    """Captions without word timing: cut long cues into short lines, timing spread by character count."""
    out = []
    for ln in cap_lines:
        text = ln["text"].strip()
        if len(text) <= max_chars:
            out.append({"s": ln["s"], "e": ln["e"], "text": text, "words": []})
            continue
        toks = _tokenize_text(text)
        groups, cur = [], []
        for w, a, b in toks:
            cand = cur + [(w, a, b)]
            if cur and (b - cur[0][1] > max_chars or (text[a - 1:a].isspace() and b - cur[0][1] > max_chars * 0.55)):
                groups.append(cur); cur = [(w, a, b)]
            else:
                cur = cand
        if cur:
            groups.append(cur)
        total = max(sum(g[-1][2] - g[0][1] for g in groups), 1)
        t = ln["s"]
        for g in groups:
            span = (g[-1][2] - g[0][1]) / total * (ln["e"] - ln["s"])
            out.append({"s": t, "e": t + span, "text": text[g[0][1]:g[-1][2]].strip(), "words": []})
            t += span
    return out


def align_captions(cap_lines: list, raw_whisper: list, vocals: torch.Tensor, sr: int) -> list:
    """Keep the uploader's caption text (accurate words) but take word timing from Whisper:
    character-level alignment of the two transcripts (spaces ignored), unmatched characters
    interpolated, then cut into karaoke lines on pauses. Falls back to proportional timing per cue
    when the alignment is poor."""
    import difflib
    # 1) Whisper character timeline
    wch, wt = [], []
    for ln in energy_filter(raw_whisper, vocals, sr):
        for p in ln.get("words") or []:
            piece = p["w"]
            n = max(len(piece), 1)
            for i, ch in enumerate(piece):
                if ch.isspace():
                    continue
                wch.append(ch.lower())
                wt.append((p["s"] + (p["e"] - p["s"]) * i / n, p["s"] + (p["e"] - p["s"]) * (i + 1) / n))
    # 2) caption character sequence with back-references
    cch, cref = [], []  # cref: (line_idx, char_idx)
    for li, ln in enumerate(cap_lines):
        for ci, ch in enumerate(ln["text"]):
            if not ch.isspace():
                cch.append(ch.lower()); cref.append((li, ci))
    if not wch or not cch:
        return split_caption_lines(cap_lines)
    sm = difflib.SequenceMatcher(None, wch, cch, autojunk=False)
    ctime = [None] * len(cch)
    matched = 0
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            ctime[b + k] = wt[a + k]; matched += 1
    ratio = matched / len(cch)
    if ratio < 0.35:
        print(f"[lyrics] caption alignment weak ({ratio:.0%}); using proportional timing")
        return split_caption_lines(cap_lines)
    # 3) interpolate unmatched characters between neighbours (bounded by the cue's own window)
    last_t, next_idx = None, 0
    for i in range(len(cch)):
        if ctime[i] is None:
            j = i + 1
            while j < len(cch) and ctime[j] is None:
                j += 1
            lo = last_t if last_t is not None else cap_lines[cref[i][0]]["s"]
            hi = ctime[j][0] if j < len(cch) else cap_lines[cref[i][0]]["e"]
            span = max(j - i, 1)
            for k in range(i, j):
                t0 = lo + (hi - lo) * (k - i) / span
                t1 = lo + (hi - lo) * (k - i + 1) / span
                ctime[k] = (t0, t1)
        last_t = ctime[i][1]
    # clamp into the cue window (+/- 1.5 s) so a misalignment cannot throw a line far away
    for i, (li, _) in enumerate(cref):
        lo, hi = cap_lines[li]["s"] - 1.5, cap_lines[li]["e"] + 1.5
        ctime[i] = (min(max(ctime[i][0], lo), hi), min(max(ctime[i][1], lo), hi))
    # 4) words per caption line with timing from their characters
    per_line = {}
    for i, (li, ci) in enumerate(cref):
        per_line.setdefault(li, {})[ci] = ctime[i]
    words = []
    for li, ln in enumerate(cap_lines):
        cmap = per_line.get(li, {})
        for wi, (w, a, b) in enumerate(_tokenize_text(ln["text"])):
            ts = [cmap[c] for c in range(a, b) if c in cmap]
            if not ts:
                continue
            words.append({"s": min(t[0] for t in ts), "e": max(t[1] for t in ts), "w": w,
                          "brk": wi == 0 or ln["text"][a - 1:a].isspace()})
    words.sort(key=lambda w: w["s"])
    for i in range(len(words) - 1):
        if words[i]["e"] > words[i + 1]["s"]:
            words[i]["e"] = words[i + 1]["s"]
    print(f"[lyrics] caption alignment {ratio:.0%} matched, {len(words)} words")
    return build_lines(words)
