"""Fetch a VOD (and its chat replay when the platform has one) with yt-dlp."""
import re
from pathlib import Path

import yt_dlp


def _progress(log, label):
    last = {"pct": -10}

    def hook(d):
        if d.get("status") == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if not total and d.get("fragment_count"):
                done, total = d.get("fragment_index") or 0, d["fragment_count"]
            if total:
                pct = int(done * 100 / total)
                if pct >= last["pct"] + 10:
                    last["pct"] = pct
                    log(f"{label} {pct}%")
    return hook


def _cache(url: str) -> Path:
    import hashlib
    from .config import DATA
    d = DATA / "cache" / hashlib.sha1(url.encode()).hexdigest()[:16]
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_audio(url: str, workdir: Path, log) -> Path:
    """The smallest audio stream of the whole VOD, for the loudness scan. Loudness doesn't need
    quality, so a 9 h YouTube stream is ~200 MB and takes well under a minute.

    Cached per URL under data/cache so re-clipping a VOD with other settings skips the download.
    """
    cache = _cache(url)
    done = cache / "complete"
    if done.exists():
        log("Using the audio downloaded earlier")
        return next(p for p in cache.glob("audio.*") if not re.search(r"(rechat|live_chat|part)", p.name))
    opts = {
        "format": "Audio_Only/wa/ba/worst",
        "outtmpl": str(cache / "audio.%(ext)s"),
        "concurrent_fragment_downloads": 8,
        "progress_hooks": [_progress(log, "Downloading audio")],
        "quiet": True, "noprogress": True, "noplaylist": True,
    }
    log("Downloading the stream's audio")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    audio = next(p for p in cache.glob("audio.*") if not re.search(r"(rechat|live_chat|part)", p.name))
    done.write_text(url)
    return audio


def fetch_chat(url: str, log) -> Path | None:
    """The chat replay, if the platform keeps one. Slow on long YouTube streams (YouTube hands it
    out a few seconds at a time), so it's opt-in."""
    cache = _cache(url)
    found = next((p for p in cache.glob("*.json") if re.search(r"(rechat|live_chat)", p.name)), None)
    if found or (cache / "chat.none").exists():
        return found
    log("Downloading the chat replay (this can take a few minutes on long streams)")
    opts = {"skip_download": True, "writesubtitles": True, "subtitleslangs": ["rechat", "live_chat"],
            "outtmpl": str(cache / "chat.%(ext)s"), "quiet": True, "no_warnings": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    found = next((p for p in cache.glob("*.json") if re.search(r"(rechat|live_chat)", p.name)), None)
    if not found:
        (cache / "chat.none").touch()
    log("Chat replay found" if found else "No chat replay for this stream")
    return found


def _caption_track(info: dict) -> tuple[str, bool] | None:
    """(language, automatic?) of the best transcript: real subtitles first, then YouTube's
    automatic captions in the stream's own language (the '-orig' track, not a translation)."""
    subs = {k: v for k, v in (info.get("subtitles") or {}).items() if k not in ("live_chat", "rechat")}
    for k in sorted(subs, key=lambda k: (not k.startswith("en"), k)):
        return k, False
    auto = info.get("automatic_captions") or {}
    orig = [k for k in auto if k.endswith("-orig")]
    if orig:
        return orig[0], True
    return None


def parse_json3(raw: dict) -> list[dict]:
    """YouTube json3 captions -> words [{w, s, e}] in seconds. Automatic captions time every
    word; plain subtitles only time each line, so their words are spread across the line."""
    events = [e for e in raw.get("events", []) if e.get("segs")]
    words = []
    for k, ev in enumerate(events):
        t0 = ev.get("tStartMs", 0) / 1000
        dur = ev.get("dDurationMs", 0) / 1000
        nxt = events[k + 1].get("tStartMs", 0) / 1000 if k + 1 < len(events) else t0 + dur
        end_ev = min(t0 + dur, nxt) if dur else nxt
        segs = [s for s in ev["segs"] if (s.get("utf8") or "").strip()]
        for j, seg in enumerate(segs):
            s0 = t0 + seg.get("tOffsetMs", 0) / 1000
            s1 = t0 + segs[j + 1].get("tOffsetMs", 0) / 1000 if j + 1 < len(segs) else max(end_ev, s0 + 0.3)
            parts = [w for w in seg["utf8"].split() if w != ">>"]   # >> marks a speaker change
            if not parts:
                continue
            step = max(0.05, (s1 - s0) / len(parts))
            for i, w in enumerate(parts):
                words.append({"w": w, "s": round(s0 + i * step, 3), "e": round(min(s1, s0 + (i + 1) * step), 3)})
    words.sort(key=lambda w: w["s"])
    return words


def fetch_transcript(url: str, log) -> list[dict] | None:
    """The platform's own transcript with word times, or None if it has none (Twitch, Kick and
    live YouTube streams). A whole 9 h stream comes down in a few seconds."""
    import json
    cache = _cache(url)
    saved = cache / "transcript.json"
    if saved.exists():
        words = json.loads(saved.read_text())
        return words or None
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as y:
        info = y.extract_info(url, download=False)
    track = _caption_track(info)
    if not track:
        saved.write_text("[]")
        return None
    lang, auto = track
    opts = {"skip_download": True, "writesubtitles": not auto, "writeautomaticsub": auto,
            "subtitleslangs": [lang], "subtitlesformat": "json3", "outtmpl": str(cache / "captions.%(ext)s"),
            "quiet": True, "no_warnings": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([url])
    f = next(cache.glob("captions*.json3"), None)
    words = parse_json3(json.loads(f.read_text())) if f else []
    saved.write_text(json.dumps(words))
    if words:
        log(f"Got the stream's transcript from YouTube ({len(words):,} words, {'automatic' if auto else 'subtitles'}, {lang})")
    return words or None


def fetch_section(url: str, start: float, end: float, out: Path, log) -> Path:
    """Download just [start, end] of the VOD at up to 1080p."""
    from yt_dlp.utils import download_range_func
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out.with_suffix(".%(ext)s")),
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "concurrent_fragment_downloads": 8,
        "quiet": True, "noprogress": True, "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    got = next(out.parent.glob(out.stem + ".mp4"), None) or next(out.parent.glob(out.stem + ".*"))
    return got


_OFFSET_PATTERNS = [
    (re.compile(r'"content_offset_seconds"\s*:\s*([\d.]+)'), 1.0),     # Twitch rechat (v5 style)
    (re.compile(r'"contentOffsetSeconds"\s*:\s*([\d.]+)'), 1.0),       # Twitch rechat (gql style)
    (re.compile(r'"videoOffsetTimeMsec"\s*:\s*"?(\d+)'), 0.001),       # YouTube live_chat
]


def chat_times(chat_path: Path | None) -> list[float]:
    """Message timestamps in seconds from the start of the VOD, whatever the chat format."""
    if not chat_path or not chat_path.exists():
        return []
    raw = chat_path.read_text(errors="ignore")
    for pat, scale in _OFFSET_PATTERNS:
        found = pat.findall(raw)
        if found:
            return sorted(float(x) * scale for x in found)
    return []
