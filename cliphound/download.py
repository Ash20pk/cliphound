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


def fetch_audio(url: str, workdir: Path, log) -> tuple[Path, Path | None]:
    """Audio only, for scoring the whole VOD cheaply (a 12 h Twitch VOD is ~1 GB vs ~34 GB of video).

    Cached per URL under data/cache so re-clipping a VOD with other settings skips the download.
    """
    import hashlib
    from .config import DATA
    cache = DATA / "cache" / hashlib.sha1(url.encode()).hexdigest()[:16]
    done = cache / "complete"
    if done.exists():
        log("Using cached audio for this VOD")
        audio = next(p for p in cache.glob("audio.*") if not re.search(r"(rechat|live_chat|part)", p.name))
        chat = next((p for p in cache.glob("audio.*") if re.search(r"(rechat|live_chat)", p.name)), None)
        return audio, chat
    cache.mkdir(parents=True, exist_ok=True)
    workdir = cache
    opts = {
        "format": "ba/Audio_Only/worst",
        "outtmpl": str(workdir / "audio.%(ext)s"),
        "writesubtitles": True,
        "subtitleslangs": ["rechat", "live_chat"],
        "concurrent_fragment_downloads": 8,
        "progress_hooks": [_progress(log, "Downloading audio")],
        "quiet": True, "noprogress": True, "noplaylist": True,
    }
    log(f"Downloading audio of {url}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    (workdir / "title.txt").write_text(info.get("title") or "")
    audio = next(p for p in workdir.glob("audio.*") if not re.search(r"(rechat|live_chat|part)", p.name))
    chat = next((p for p in workdir.glob("audio.*") if re.search(r"(rechat|live_chat)", p.name)), None)
    log("Chat replay found" if chat else "No chat replay, scoring on audio + speech only")
    done.write_text(url)
    return audio, chat


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
