"""Find the moments worth clipping.

1. Loudness: RMS per 0.5 s window, compared with the stream's own rolling baseline, so a
   loud streamer isn't "always exciting" and a quiet one still gets peaks.
2. Chat: messages per window, also against a rolling baseline (Twitch/YouTube replays).
3. Pick the top candidates, transcribe only those windows with Whisper, boost the ones
   with hype phrases, and snap each cut to sentence boundaries.
"""
import subprocess
import tempfile
from pathlib import Path

import numpy as np

SR = 16000
WIN = 0.5  # seconds per scoring window


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def loudness_curve(path: Path) -> np.ndarray:
    """dBFS per WIN-second window, streamed so multi-hour VODs don't load into memory."""
    hop = int(SR * WIN)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-af", "aresample=async=1:first_pts=0",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    vals, buf = [], b""
    while True:
        chunk = proc.stdout.read(hop * 2 * 512)
        if not chunk:
            break
        buf += chunk
        n = len(buf) // (hop * 2)
        if n:
            a = np.frombuffer(buf[: n * hop * 2], dtype=np.int16).astype(np.float32) / 32768
            rms = np.sqrt((a.reshape(n, hop) ** 2).mean(axis=1) + 1e-10)
            vals.append(20 * np.log10(rms))
            buf = buf[n * hop * 2:]
    proc.wait()
    return np.concatenate(vals) if vals else np.zeros(0)


def _rolling(x: np.ndarray, size: int, fn) -> np.ndarray:
    size = max(1, min(size, len(x)))
    pad = np.pad(x, (size // 2, size - 1 - size // 2), mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(pad, size)
    return fn(win, axis=1)


def _smooth(x: np.ndarray, seconds: float) -> np.ndarray:
    k = max(1, int(seconds / WIN))
    return np.convolve(x, np.ones(k) / k, mode="same")


SILENCE_DB = -70  # digital silence: Twitch's copyright-muted VOD sections sit at -100 dB


def _baseline(x: np.ndarray, baseline_s: float = 120, ignore: np.ndarray | None = None) -> np.ndarray:
    """Rolling median, subsampled for speed on long VODs, then stretched back.

    Windows in `ignore` (muted stretches) are left out, otherwise the audio right after a
    mute looks ~70 dB louder than its surroundings and every unmute becomes a "highlight".
    """
    step = 8
    y = x.astype(float).copy()
    if ignore is not None:
        y[ignore] = np.nan
    fallback = np.nanmedian(y) if np.isfinite(y).any() else 0.0
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            base = _rolling(y[::step], int(baseline_s / WIN / step), np.nanmedian)
    base = np.where(np.isfinite(base), base, fallback)
    return np.repeat(base, step)[: len(x)]


def _zscore_vs_baseline(x: np.ndarray, baseline_s: float = 120) -> np.ndarray:
    base = _baseline(x, baseline_s)
    step = 8
    spread = np.repeat(_rolling(np.abs(x - base)[::step], int(baseline_s / WIN / step), np.median),
                       step)[: len(x)]
    return (x - base) / np.maximum(spread, 1.0)


def score_curve(db: np.ndarray, chat: list[float], log) -> np.ndarray:
    # Loudness in 3 dB steps above the stream's own rolling level. Dividing by the spread
    # instead saturates whenever the baseline is quiet, so talking ties with screaming.
    muted = db < SILENCE_DB
    if muted.mean() > 0.01:
        log(f"{muted.mean() * 100:.0f}% of the audio is silent (muted VOD sections); ignoring it")
    raw = np.clip((db - _baseline(db, ignore=muted)) / 3.0, -3, 15)
    raw[muted] = -3
    score = _smooth(raw, 3)
    if chat:
        log(f"Scoring {len(chat)} chat messages")
        counts = np.bincount((np.array(chat) / WIN).astype(int), minlength=len(db))[: len(db)]
        # Chat reacts a few seconds after the moment, so pull it back ~4 s.
        shift = int(4 / WIN)
        counts = np.concatenate([counts[shift:], np.zeros(shift)])
        chat_z = _smooth(np.clip(_zscore_vs_baseline(_smooth(counts.astype(float), 5)), -3, 8), 3)
        score = score + 1.5 * chat_z
    return score


def candidates(score: np.ndarray, n: int, clip_s: float, total_s: float) -> list[dict]:
    """Greedy non-overlapping peaks. Window leans before the peak: the reaction follows the event."""
    s = score.copy()
    out = []
    before, after = clip_s * 0.65, clip_s * 0.35
    # Peaks closer than one clip length would produce overlapping clips; farther is fine.
    guard = int((clip_s + 5) / WIN)
    while len(out) < n:
        i = int(np.argmax(s))
        if not np.isfinite(s[i]) or s[i] <= 0.5:
            break
        t = i * WIN
        start, end = max(0.0, t - before), min(total_s, t + after)
        out.append({"peak": t, "start": start, "end": end, "base_score": float(s[i])})
        s[max(0, i - guard): i + guard] = -np.inf
    return out


_whisper_cache = {}


def _whisper(wav: str, model: str) -> dict:
    """Whisper on Apple Silicon (mlx-whisper), or faster-whisper on everything else.
    Both return {"segments": [{"text", "start", "end", "words": [{"word", "start", "end"}]}]}."""
    try:
        import mlx_whisper
    except ImportError:
        mlx_whisper = None
    if mlx_whisper:
        return mlx_whisper.transcribe(wav, path_or_hf_repo=model, word_timestamps=True,
                                      condition_on_previous_text=False, verbose=None)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("No speech-to-text engine is installed. On Apple Silicon run `uv sync`; "
                           "elsewhere install faster-whisper") from None
    name = "large-v3-turbo" if "large-v3-turbo" in model else model.rsplit("/", 1)[-1].replace("whisper-", "")
    if name not in _whisper_cache:
        _whisper_cache[name] = WhisperModel(name, device="auto", compute_type="auto")
    segments, _ = _whisper_cache[name].transcribe(wav, word_timestamps=True, condition_on_previous_text=False)
    return {"segments": [{"text": s.text, "start": s.start, "end": s.end,
                          "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (s.words or [])]}
                         for s in segments]}


def transcribe(video: Path, start: float, end: float, model: str) -> list[dict]:
    """Word-level transcript for [start, end], times relative to the VOD."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
                        "-i", str(video), "-vn", "-ac", "1", "-ar", str(SR), tmp.name], check=True)
        res = _whisper(tmp.name, model)
    segs = []
    for seg in res.get("segments", []):
        words = [{"w": w["word"].strip(), "s": w["start"] + start, "e": w["end"] + start}
                 for w in seg.get("words", []) if w["word"].strip()]
        if words:
            segs.append({"text": seg["text"].strip(), "s": seg["start"] + start, "e": seg["end"] + start,
                         "words": words})
    return segs


def refine(video: Path, cands: list[dict], settings: dict, total_s: float, log, check=lambda: None) -> list[dict]:
    hype = [h.lower() for h in settings["hype_words"]]
    max_len = 59.0
    for k, c in enumerate(cands, 1):
        check()
        if len(cands) > 1:
            log(f"Listening to moment {k} of {len(cands)} at {fmt(c['peak'])}")
        lo, hi = max(0.0, c["start"] - 6), min(total_s, c["end"] + 6)
        segs = transcribe(video, lo, hi, settings["whisper_model"])
        c["segs"], c["win_start"], c["win_end"] = segs, lo, hi   # kept for AI mode
        text = " ".join(s["text"] for s in segs if s["e"] > c["start"] and s["s"] < c["end"]).lower()
        hits = sum(text.count(h) for h in hype)
        c["score"] = c["base_score"] + 0.6 * min(hits, 6)
        # Snap: start at the beginning of the sentence in progress, end when the sentence finishes.
        starts = [s["s"] for s in segs if c["start"] - 5 <= s["s"] <= c["start"] + 1]
        ends = [s["e"] for s in segs if c["end"] - 1 <= s["e"] <= c["end"] + 5]
        if starts:
            c["start"] = max(0.0, min(starts) - 0.3)
        if ends:
            c["end"] = max(ends) + 0.4
        c["end"] = min(c["end"], c["start"] + max_len, total_s)
        c["words"] = [w for s in segs for w in s["words"] if c["start"] <= w["s"] < c["end"]]
        c["hook"] = make_hook(segs, c, hype)
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands


def make_hook(segs: list[dict], c: dict, hype: list[str]) -> str:
    """Pick the punchiest line in the clip as the title hook."""
    inside = [s["text"] for s in segs if s["e"] > c["start"] and s["s"] < c["end"] and len(s["text"]) > 3]
    if not inside:
        return "Watch this moment"
    best = max(inside, key=lambda t: (sum(t.lower().count(h) for h in hype), "!" in t or "?" in t, -abs(len(t) - 40)))
    best = best.strip(" .,")
    if len(best) > 70:
        best = best[:67].rsplit(" ", 1)[0] + "..."
    return f"“{best}”"


def fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def waveform(db: np.ndarray, bins: int = 720) -> list[float]:
    """Stream overview for the UI: per bin a 0..1 loudness, or -1 where the audio is muted."""
    if not len(db):
        return []
    edges = np.linspace(0, len(db), min(bins, len(db)) + 1).astype(int)
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = db[a:max(b, a + 1)]
        if (seg < SILENCE_DB).mean() > 0.8:
            out.append(-1)
        else:
            lvl = np.percentile(seg[seg >= SILENCE_DB], 90)
            out.append(round(float(np.clip((lvl + 60) / 50, 0.02, 1)), 3))
    return out


def find_clips(audio: Path, chat: list[float], settings: dict, n: int, clip_s: float, log,
               check=lambda: None, chooser=None) -> tuple[list[dict], list[float], float]:
    """Best n moments in the whole stream. Returns (clips, waveform, stream length).

    `chooser`, when given (AI mode), sees a wider set of transcribed candidates and picks the
    n best itself. If it fails for any reason the loudness + speech ranking is used instead.
    """
    log("Measuring loudness across the stream")
    db = loudness_curve(audio)
    if not len(db):
        raise RuntimeError("The stream has no audio track")
    total = len(db) * WIN
    log(f"Stream length {fmt(total)}")
    check()
    score = score_curve(db, chat, log)
    cands = candidates(score, min(n * 6, max(36, n * 2)) if chooser else n * 3, clip_s, total)
    if not cands:
        raise RuntimeError("Nothing in the stream stood out from its usual level, so there's nothing to clip")
    log(f"Found {len(cands)} loud moments; listening to each to pick the best {n}")
    ranked = refine(audio, cands, settings, total, log, check)
    picked = None
    if chooser:
        check()
        try:
            picked = chooser(ranked)
        except Exception as e:
            log(f"AI picking didn't work: {e}. Using loudness and speech instead")
    picked = picked or ranked[:n]
    picked.sort(key=lambda c: c["start"])
    return picked, waveform(db), total


def moment(section: Path, at_in_section: float, settings: dict, clip_s: float, log) -> dict:
    """One clip around a chosen timestamp, snapped to speech. Times are within `section`."""
    total = duration(section)
    c = {"peak": at_in_section, "start": max(0.0, at_in_section - clip_s * 0.5),
         "end": min(total, at_in_section + clip_s * 0.5), "base_score": 0.0}
    log("Transcribing the moment")
    return refine(section, [c], settings, total, log)[0]


def slim(clips: list[dict]) -> list[dict]:
    """Drop the bulky transcript windows before saving clips to disk."""
    return [{k: v for k, v in c.items() if k != "segs"} for c in clips]

