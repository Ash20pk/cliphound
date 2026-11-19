"""Read a YouTube live stream from its rewind buffer.

yt-dlp can only record a live stream to its end, which never comes for a 20-hour stream,
and YouTube keeps no archive of streams over 12 hours. The live DASH stream is a numbered
sequence of ~2 s segments (`&sq=N`) reachable from the start of the broadcast, so we fetch
the audio segments for scoring and only the video segments each clip needs.
"""
import math
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}


class LiveStream:
    def __init__(self, url: str, log, check=lambda: None):
        self.url, self.log, self.check = url, log, check
        self.refresh()

    def refresh(self):
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "live_from_start": True,
                "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(self.url, download=False)
        fmts = [f for f in info["formats"] if f.get("protocol", "").startswith("http_dash_segments")
                or "dash" in (f.get("protocol") or "") or f.get("format_id", "").isdigit()]
        by_id = {f["format_id"]: f for f in fmts}
        self.audio_url = (by_id.get("139") or by_id.get("140") or
                          next(f for f in fmts if f.get("vcodec") == "none"))["url"]
        videos = [f for f in fmts if f.get("vcodec", "none") != "none" and (f.get("height") or 0) <= 1080
                  and (f.get("vcodec") or "").startswith("avc1")]
        best = max(videos, key=lambda f: ((f.get("height") or 0), (f.get("fps") or 0)))
        self.video_url = best["url"]
        self.video_label = f"{best.get('height')}p{best.get('fps') or ''}"
        _, h = self._get(self.audio_url, 0, headers_only=True)
        self.head_seq = int(h["X-Head-Seqnum"])
        self.seg = float(h["X-Head-Time-Sec"]) / max(1, self.head_seq)
        self.duration = self.head_seq * self.seg

    def _get(self, base: str, sq: int, headers_only=False):
        last = None
        for attempt in range(5):
            try:
                r = urllib.request.urlopen(urllib.request.Request(f"{base}&sq={sq}", headers=UA), timeout=30)
                return (None if headers_only else r.read()), r.headers
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 404:          # segment not available (yet / any more)
                    return None, {}
                time.sleep(1.5 * (attempt + 1))
            except Exception as e:  # timeouts, resets
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"segment {sq} failed: {last}")

    def _fetch_range(self, base: str, sq0: int, sq1: int, out: Path, label: str, workers=16):
        total, done_n, missing = sq1 - sq0, 0, 0
        next_pct = 10
        with open(out, "wb") as f, ThreadPoolExecutor(workers) as ex:
            for chunk in range(sq0, sq1, 400):
                self.check()
                seqs = range(chunk, min(chunk + 400, sq1))
                for data, _ in ex.map(lambda s: self._get(base, s), seqs):
                    if data:
                        f.write(data)
                    else:
                        missing += 1
                done_n += len(seqs)
                pct = int(done_n * 100 / max(1, total))
                if total > 400 and pct >= next_pct:
                    self.log(f"{label} {pct}%")
                    next_pct = pct + 10
        if missing:
            self.log(f"{missing} of {total} segments were unavailable; gaps are treated as silence")
        return out

    def fetch_audio(self, out: Path) -> Path:
        hours = self.duration / 3600
        self.log(f"Live stream, {hours:.1f} h so far. Fetching audio from the start of the broadcast")
        return self._fetch_range(self.audio_url, 0, self.head_seq, out, "Fetching audio")

    def fetch_section(self, start: float, end: float, out: Path) -> tuple[Path, float]:
        sq0, sq1 = max(0, math.floor(start / self.seg)), min(self.head_seq, math.ceil(end / self.seg))
        out = out.with_suffix(".mp4")   # ffmpeg picks the container from the extension
        v, a = out.with_suffix(".v.mp4"), out.with_suffix(".a.mp4")
        try:
            self._fetch_range(self.video_url, sq0, sq1, v, "video", workers=8)
            self._fetch_range(self.audio_url, sq0, sq1, a, "audio", workers=8)
        except RuntimeError:
            self.refresh()            # signed URLs expire after a few hours
            self._fetch_range(self.video_url, sq0, sq1, v, "video", workers=8)
            self._fetch_range(self.audio_url, sq0, sq1, a, "audio", workers=8)
        # Segments keep their broadcast timestamps. That's fine: ffmpeg's input -ss is relative
        # to the file's own start, so the section behaves as if it began at 0.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(v), "-i", str(a), "-map", "0:v", "-map", "1:a",
                        "-c", "copy", str(out)], check=True)
        v.unlink(missing_ok=True)
        a.unlink(missing_ok=True)
        return out, sq0 * self.seg
