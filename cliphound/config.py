"""Paths and persisted settings."""
import json
import os
import string
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS = DATA / "jobs"
SETTINGS_FILE = DATA / "settings.json"
CLIENT_SECRET = DATA / "client_secret.json"
TOKEN_FILE = DATA / "youtube_token.json"

for p in (DATA, JOBS):
    p.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    # Streamers we clip. Each: id, name, url, credit_url, permission (bool), notes
    "sources": [],
    "clips_per_vod": 5,
    "clip_seconds": 30,          # target length; final clip is snapped to speech, max 59s
    "layout": "fit",             # "fit" = full frame over blurred fill, "crop" = centre crop to 9:16
    "auto_upload": False,        # only ever for streamers with permission recorded
    "privacy": "public",         # private | unlisted | public
    "category_id": "20",         # YouTube category: 20 Gaming, 24 Entertainment, 23 Comedy, 22 People & Blogs, 17 Sports
    "cache_days": 7,             # downloaded stream audio is kept this long for re-clipping
    "whisper_model": "mlx-community/whisper-large-v3-turbo",
    # AI mode: a language model picks and titles the moments. ai_auth: "key" | "chatgpt" | None (whichever is set up)
    "ai_mode": True,
    "ai_auth": None,
    "ai_model": None,
    "title_template": "{hook} | {streamer} #Shorts",
    "description_template": (
        "{hook}\n\n"
        "Clipped from {streamer}'s stream: {credit_url}\n"
        "All credit to {streamer}, go follow them.\n\n"
        "#Shorts"
    ),
    "tags": ["Shorts"],
    # What the channel is, so AI picking knows what makes a good Short for it.
    "channel_brief": "A YouTube Shorts channel of the best moments from livestreams.",
    "title_language": "English",
    "hype_words": ["no way", "oh my god", "what the", "clip that", "clip it", "let's go",
                   "lets go", "insane", "bro", "dude", "holy", "run", "go go", "yes",
                   "no no", "help", "wait", "how", "crazy", "dead", "wasted"],
}

_lock = threading.Lock()


TEMPLATE_FIELDS = {"hook", "streamer", "credit_url"}
PRIVACY = ("private", "unlisted", "public")
CATEGORIES = {"20": "Gaming", "24": "Entertainment", "23": "Comedy", "22": "People & Blogs", "17": "Sports"}


class SettingsError(ValueError):
    pass


def check_template(t: str, label: str):
    """Reject a template that would crash at upload time: bad braces or unknown {names}."""
    try:
        fields = [f for _, f, spec, conv in string.Formatter().parse(t) if f is not None]
    except ValueError:
        raise SettingsError(f"{label} has an unmatched {{ or }}. Use {{{{ and }}}} for literal braces")
    for f in fields:
        if f not in TEMPLATE_FIELDS:
            raise SettingsError(f"{label} uses {{{f}}}. Only {{hook}}, {{streamer}} and {{credit_url}} work")


def fill(t: str, fields: dict) -> str:
    """Fill a template; anything unexpected is left as typed instead of failing the job."""
    try:
        check_template(t, "Template")
        return t.format(**fields)
    except SettingsError:
        out = t
        for k, v in fields.items():
            out = out.replace("{" + k + "}", str(v))
        return out


def _validate(patch: dict):
    for k, label in (("title_template", "The title"), ("description_template", "The description")):
        if k in patch:
            if not isinstance(patch[k], str):
                raise SettingsError(f"{label} must be text")
            check_template(patch[k], label)
    if "privacy" in patch and patch["privacy"] not in PRIVACY:
        raise SettingsError("Upload as must be private, unlisted or public")
    if "category_id" in patch and patch["category_id"] not in CATEGORIES:
        raise SettingsError("Pick a YouTube category from the list")
    if "clips_per_vod" in patch and not (isinstance(patch["clips_per_vod"], int) and patch["clips_per_vod"] >= 1):
        raise SettingsError("Shorts per link must be 1 or more")
    if "clip_seconds" in patch and not (isinstance(patch["clip_seconds"], int) and 10 <= patch["clip_seconds"] <= 58):
        raise SettingsError("Target length must be between 10 and 58 seconds")
    if "tags" in patch and not (isinstance(patch["tags"], list) and all(isinstance(t, str) for t in patch["tags"])):
        raise SettingsError("Tags must be a list of words")


def write_json(path: Path, data, mode: int | None = None):
    """Write via a temp file and rename, so a crash never leaves half a file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode if mode is not None else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _load_unlocked() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text()))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Keep the broken file for the user and start from the defaults.
            SETTINGS_FILE.replace(SETTINGS_FILE.with_name(f"settings.broken-{int(time.time())}.json"))
    return s


def load_settings() -> dict:
    with _lock:
        return _load_unlocked()


def save_settings(patch: dict) -> dict:
    _validate(patch)
    with _lock:
        s = _load_unlocked()
        s.update({k: v for k, v in patch.items() if k in DEFAULTS})
        write_json(SETTINGS_FILE, s)
    return s
