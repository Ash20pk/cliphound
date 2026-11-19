"""Turn whatever link was pasted into something the pipeline can read.

Every provider answers the same two questions:
  audio()                -> one audio file covering the whole stream, t=0 at the stream start
  section(start, end)    -> (video file, where that file starts in stream time)
so the rest of the pipeline doesn't care whether it's a local file, a VOD or a live stream.
"""
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

import yt_dlp

from . import download

_cache: dict[str, tuple[float, dict]] = {}


def parse_time(s) -> float | None:
    """'1:02:03', '1h2m3s', '3723', '62m' -> seconds."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().lower()
    if re.fullmatch(r"[\d.]+", s):
        return float(s)
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        secs = 0.0
        for p in parts:
            secs = secs * 60 + p
        return secs
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", s)
    if m and any(m.groups()):
        h, mi, se = (float(x) if x else 0 for x in m.groups())
        return h * 3600 + mi * 60 + se
    raise ValueError(f"Couldn't read the time '{s}'. Use 1:02:03 or 1h2m3s")


def _time_in_url(url: str) -> float | None:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("t", "start", "time_continue"):
        if key in q:
            try:
                return parse_time(q[key][0])
            except ValueError:
                return None
    return None


def _platform(key: str) -> str:
    k = key.lower()
    for name in ("YouTube", "Twitch", "Kick"):
        if k.startswith(name.lower()):
            return name
    return re.sub(r"(Vod|Tab|:.*)$", "", key) or "Web"


def _probe_seconds(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0


def resolve(url: str) -> dict:
    """Metadata for the preview card and the job. Cached for 10 minutes."""
    url = url.strip()
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    local = Path(url).expanduser()
    if local.exists():
        meta = {"kind": "local", "url": str(local), "title": local.stem, "channel": None, "channel_url": "",
                "duration": _probe_seconds(local), "thumbnail": None, "platform": "File", "at": None}
    else:
        if not re.match(r"https?://", url):
            raise ValueError("Paste a Twitch, YouTube or Kick link, or the path to a video on this Mac")
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=False, process=False)
        except yt_dlp.utils.DownloadError as e:
            msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?", "", str(e))
            raise ValueError(f"That link didn't open: {msg}")
        if info.get("_type") == "playlist":
            raise ValueError("That's a channel or playlist link. Paste the link to one stream or video")
        live = info.get("live_status") == "is_live" or info.get("is_live")
        if info.get("live_status") == "is_upcoming":
            raise ValueError("That stream hasn't started yet")
        meta = {
            "kind": "live" if live else "vod",
            "url": url,
            "title": info.get("title") or "Untitled stream",
            "channel": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
            "channel_url": info.get("uploader_url") or info.get("channel_url") or "",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url"),
            "platform": _platform(info.get("extractor_key") or ""),
            "at": info.get("start_time") or _time_in_url(url),
            "viewers": info.get("concurrent_view_count"),
            "views": info.get("view_count"),
        }
        if live and meta["platform"].lower() != "youtube":
            raise ValueError("Live streams can only be clipped from YouTube for now. "
                             "For Twitch, wait for the stream to end and paste the VOD link")
    _cache[url] = (time.time(), meta)
    return meta


class Local:
    def __init__(self, meta, workdir, log, check):
        self.path = Path(meta["url"])
        self.duration = meta["duration"]

    def audio(self) -> Path:
        return self.path

    def section(self, start, end, out) -> tuple[Path, float]:
        return self.path, 0.0

    def owns_sections(self):
        return False


class Vod:
    def __init__(self, meta, workdir, log, check):
        self.url, self.workdir, self.log, self.check = meta["url"], workdir, log, check
        self.duration = meta.get("duration")

    def audio(self) -> Path:
        path, self.chat = download.fetch_audio(self.url, self.workdir, self.log)
        return path

    def section(self, start, end, out) -> tuple[Path, float]:
        return download.fetch_section(self.url, start, end, out, self.log), start

    def owns_sections(self):
        return True


class Live:
    def __init__(self, meta, workdir, log, check):
        from .live import LiveStream
        self.stream = LiveStream(meta["url"], log, check)
        self.duration = self.stream.duration
        self.workdir = workdir
        self.chat = None

    def audio(self) -> Path:
        return self.stream.fetch_audio(self.workdir / "audio_live.mp4")

    def section(self, start, end, out) -> tuple[Path, float]:
        return self.stream.fetch_section(start, end, out)

    def owns_sections(self):
        return True


def provider(meta: dict, workdir: Path, log, check):
    return {"local": Local, "vod": Vod, "live": Live}[meta["kind"]](meta, workdir, log, check)
