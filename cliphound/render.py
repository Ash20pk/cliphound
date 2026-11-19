"""Render a clip as a 1080x1920 Short with word-by-word captions.

Homebrew's ffmpeg ships without libass/drawtext, so captions are drawn with Pillow into
transparent PNGs and laid over the video through the concat demuxer + overlay filter.
"""
import functools
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
# Heavy caption fonts, first one found wins: macOS, Windows, then common Linux packages.
FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _font(size: int):
    for f in FONTS:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


@functools.cache
def _encoders() -> str:
    try:
        return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except OSError:
        return ""


def _video_codec() -> list[str]:
    """The Mac's hardware encoder when there is one, otherwise x264 (everywhere else)."""
    if "h264_videotoolbox" in _encoders():
        return ["-c:v", "h264_videotoolbox", "-b:v", "12M", "-profile:v", "high"]
    return _X264


_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-profile:v", "high"]
CAPTION_Y = 1340          # caption block centre (below the 16:9 frame in "fit" layout)
CREDIT_Y = 300
YELLOW = (255, 214, 0, 255)
WHITE = (255, 255, 255, 255)


def _chunks(words: list[dict], max_words=3, gap=0.6) -> list[list[dict]]:
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > gap):
            out.append(cur)
            cur = []
        cur.append(w)
        if w["w"].endswith((".", "!", "?", ",")):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _frame(credit: str, chunk: list[dict] | None, active: int, font, small) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if credit:
        tw = d.textlength(credit, font=small)
        d.rounded_rectangle([(W - tw) / 2 - 28, CREDIT_Y - 44, (W + tw) / 2 + 28, CREDIT_Y + 44],
                            radius=44, fill=(0, 0, 0, 150))
        d.text((W / 2, CREDIT_Y), credit, font=small, fill=WHITE, anchor="mm")
    if chunk:
        words = [w["w"].upper().strip(",.") for w in chunk]
        space = d.textlength(" ", font=font)
        widths = [d.textlength(t, font=font) for t in words]
        # wrap to two lines if needed
        lines, line, lw = [], [], 0
        for i, wd in enumerate(widths):
            if line and lw + space + wd > W - 120:
                lines.append(line)
                line, lw = [], 0
            lw += (space if line else 0) + wd
            line.append(i)
        lines.append(line)
        lh = font.size * 1.15
        y = CAPTION_Y - lh * (len(lines) - 1) / 2
        for ln in lines:
            total = sum(widths[i] for i in ln) + space * (len(ln) - 1)
            x = (W - total) / 2
            for i in ln:
                d.text((x, y), words[i], font=font, fill=YELLOW if i == active else WHITE,
                       anchor="lm", stroke_width=9, stroke_fill=(0, 0, 0, 255))
                x += widths[i] + space
            y += lh
    return img


def build_overlay(clip: dict, credit: str, tmp: Path) -> Path:
    """Write PNG frames + a concat list timed to the words; returns the list path."""
    font = _font(92)
    small = _font(44)
    t0, dur = clip["start"], clip["end"] - clip["start"]
    words = [dict(w, s=w["s"] - t0, e=w["e"] - t0) for w in clip.get("words", [])]
    events = []  # (start, image)
    blank = _frame(credit, None, -1, font, small)
    for chunk in _chunks(words):
        for i, w in enumerate(chunk):
            nxt = chunk[i + 1]["s"] if i + 1 < len(chunk) else chunk[-1]["e"] + 0.25
            events.append((max(0.0, w["s"]), min(dur, nxt), _frame(credit, chunk, i, font, small)))
    lines, cursor, n = [], 0.0, 0

    def put(img, d):
        nonlocal n
        if d <= 0.01:
            return
        p = tmp / f"f{n:04d}.png"
        img.save(p)
        lines.append(f"file '{p.name}'\nduration {d:.3f}")
        n += 1

    for s, e, img in events:
        s = max(s, cursor)
        if e <= s:
            continue
        put(blank, s - cursor)
        put(img, e - s)
        cursor = e
    put(blank, max(0.05, dur - cursor))
    # concat demuxer ignores the last duration unless the file is repeated
    lines.append(lines[-1].split("\n")[0])
    lst = tmp / "overlay.txt"
    lst.write_text("\n".join(lines) + "\n")
    return lst


def render(video: Path, clip: dict, out: Path, credit: str, layout: str, log) -> Path:
    tmp = out.parent / (out.stem + "_frames")
    tmp.mkdir(exist_ok=True)
    lst = build_overlay(clip, credit, tmp)
    dur = clip["end"] - clip["start"]
    if layout == "crop":
        base = f"[0:v]scale=-2:{H},crop={W}:{H},setsar=1[base]"
    else:
        base = (f"[0:v]split[a][b];"
                f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"gblur=sigma=40,eq=brightness=-0.12[bg];"
                f"[b]scale={W}:-2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2-80,setsar=1[base]")
    graph = f"{base};[base][1:v]overlay=0:0:format=auto,format=yuv420p[v];[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]"
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-ss", f"{clip['start']:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
           "-f", "concat", "-safe", "0", "-i", str(lst),
           "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
           *_video_codec(),
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", "-t", f"{dur:.3f}", str(out)]
    log(f"Rendering {out.name} ({dur:.0f}s)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and "h264_videotoolbox" in cmd:
        # The hardware encoder can be busy or missing on some Macs; x264 always works.
        i = cmd.index("-c:v")
        cmd[i:i + 6] = _X264
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-800:]}")
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()
    return out
