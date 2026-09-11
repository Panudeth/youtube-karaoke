"""Chunked vocal separation using Demucs on GPU.

Quality knobs (see run.bat / README):
  model_name : "htdemucs" (fast) or "htdemucs_ft" (fine-tuned, ~4x slower, noticeably cleaner)
  shifts     : 0 = single pass; 1-2 = average several time-shifted passes (better, linearly slower)
  accomp_mode: "mix_minus_vocals" = original mix minus estimated vocals (keeps full instrument detail,
               may leak a little vocal); "sum_stems" = drums+bass+other rebuilt by the model (cleaner
               vocal removal, but instruments can sound duller)
"""
import math
import torch
from demucs.pretrained import get_model
from demucs.apply import apply_model


class Separator:
    def __init__(self, model_name: str = "htdemucs_ft", device: str | None = None,
                 shifts: int = 0, accomp_mode: str = "mix_minus_vocals", overlap: float = 0.25):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = get_model(model_name)
        self.model.to(self.device).eval()
        self.sr: int = self.model.samplerate
        self.sources: list[str] = list(self.model.sources)
        self.vocals_idx = self.sources.index("vocals")
        self.shifts = shifts
        self.accomp_mode = accomp_mode
        self.overlap = overlap

    @torch.no_grad()
    def separate(self, wav: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """wav: float tensor [2, T] at self.sr. Returns (accompaniment, vocals), both [2, T] on CPU."""
        ref = wav.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        x = ((wav - mean) / std)[None].to(self.device)
        out = apply_model(
            self.model, x, device=self.device,
            shifts=self.shifts, split=True, overlap=self.overlap, progress=False,
        )[0]
        out = (out * std + mean).cpu()
        vocals = out[self.vocals_idx]
        if self.accomp_mode == "sum_stems":
            accomp = out.sum(0) - vocals
        else:
            accomp = wav - vocals
        return accomp.clamp(-1, 1), vocals.clamp(-1, 1)

    def iter_chunks(self, wav: torch.Tensor, chunk_sec: float, margin_sec: float, gpu_lock=None):
        """Yield (index, accomp_chunk, vocals_chunk) for consecutive chunks of chunk_sec,
        separating with margin_sec of context on each side to avoid boundary artifacts.
        gpu_lock (optional) is held only while the GPU is busy, so other GPU work can interleave."""
        T = wav.shape[1]
        chunk = int(chunk_sec * self.sr)
        margin = int(margin_sec * self.sr)
        n = math.ceil(T / chunk)
        for i in range(n):
            s, e = i * chunk, min((i + 1) * chunk, T)
            cs, ce = max(0, s - margin), min(T, e + margin)
            if gpu_lock:
                with gpu_lock:
                    acc, voc = self.separate(wav[:, cs:ce])
            else:
                acc, voc = self.separate(wav[:, cs:ce])
            a, b = s - cs, s - cs + (e - s)
            yield i, acc[:, a:b], voc[:, a:b]
