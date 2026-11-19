"""AI mode: a language model reads the transcripts of the loudest moments and picks the
best ones for Shorts, trims them, and writes English titles.

Works with either an OpenAI API key or Sign in with ChatGPT (the user's plan). Requests use
the Responses API with store=false and stream=true, which plan usage requires and an API
key is fine with, so both paths share one code path.
"""
import json
import os
import re

import requests

from . import chatgpt_auth
from .config import DATA

API = "https://api.openai.com/v1"
KEY_FILE = DATA / "openai_key.json"
# Preferred models, best first; the first one the account can see is the default.
PREFERRED = [r"^gpt-6(\.\d+)?-sol$", r"^gpt-6(\.\d+)?$", r"^gpt-5\.\d+$", r"^gpt-6(\.\d+)?-luna$",
             r"^gpt-5(\.\d+)?-mini$", r"^gpt-5$"]


class AIError(Exception):
    pass


class LimitReached(AIError):
    pass


# Credentials ------------------------------------------------------------------------

def save_key(key: str):
    key = key.strip()
    if not re.match(r"^sk-[A-Za-z0-9_\-]{20,}$", key):
        raise AIError("That doesn't look like an OpenAI API key. Keys start with sk-")
    r = requests.get(f"{API}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    if r.status_code == 401:
        raise AIError("OpenAI rejected that key. Check it at platform.openai.com/api-keys")
    if r.status_code != 200:
        raise AIError(f"Couldn't check the key with OpenAI ({r.status_code})")
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"key": key}, f)


def remove_key():
    KEY_FILE.unlink(missing_ok=True)


def _key() -> str | None:
    return json.loads(KEY_FILE.read_text())["key"] if KEY_FILE.exists() else None


def method(settings: dict) -> str | None:
    """Which credential AI mode will use: the one chosen in settings if it's set up, else whichever is."""
    have = {"key": bool(_key()), "chatgpt": chatgpt_auth.status()["signed_in"]}
    pref = settings.get("ai_auth")
    if pref in have and have[pref]:
        return pref
    return "chatgpt" if have["chatgpt"] else "key" if have["key"] else None


def _auth_header(m: str) -> dict:
    if m == "key":
        return {"Authorization": f"Bearer {_key()}"}
    return {"Authorization": f"Bearer {chatgpt_auth.access_token()}"}


def status(settings: dict) -> dict:
    k = _key()
    return {"method": method(settings), "key_set": bool(k), "key_hint": f"sk-...{k[-4:]}" if k else None,
            "chatgpt": chatgpt_auth.status(), "model": settings.get("ai_model"),
            "enabled": bool(settings.get("ai_mode")) and method(settings) is not None}


def models(settings: dict) -> list[str]:
    m = method(settings)
    if not m:
        return []
    r = requests.get(f"{API}/models", headers=_auth_header(m), timeout=20)
    if r.status_code != 200:
        raise AIError(f"Couldn't list models ({r.status_code})")
    ids = [x["id"] for x in r.json().get("data", []) if x.get("visibility", "list") == "list"]
    # Text models that can do this job; drop audio, image, embedding, moderation and dated snapshots.
    ids = [i for i in ids if i.startswith(("gpt-", "o")) and not re.search(
        r"(audio|realtime|image|tts|transcribe|search|embedding|moderation|instruct|\d{4}-\d{2}-\d{2})", i)]
    return sorted(set(ids))


def default_model(available: list[str]) -> str | None:
    for pat in PREFERRED:
        hits = sorted([m for m in available if re.match(pat, m)], reverse=True)
        if hits:
            return hits[0]
    return available[0] if available else None


# Calling the model -------------------------------------------------------------------

_ERRORS = {
    "subscription_sharing_usage_limit_exceeded":
        "Your ChatGPT plan's limit for this app is used up. It resets later, or raise it at chatgpt.com/settings/usage",
    "subscription_sharing_user_not_eligible": "AI mode with ChatGPT needs a Plus or Pro plan",
    "subscription_sharing_invalid_user": "Your ChatGPT sign-in is no longer valid. Sign in again",
    "insufficient_quota": "Your OpenAI API account is out of credit",
}


def _raise_for(status_code: int, body: dict | str):
    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code") or err.get("type") or ""
    msg = _ERRORS.get(code) or err.get("message") or str(body)[:200]
    if code in ("subscription_sharing_usage_limit_exceeded", "insufficient_quota") or status_code == 429:
        raise LimitReached(msg)
    if code == "subscription_sharing_invalid_user":
        raise chatgpt_auth.NeedsSignIn(msg)
    raise AIError(f"{msg} ({status_code})")


def respond(settings: dict, instructions: str, prompt: str, schema: dict | None) -> str:
    """One Responses API call, streamed. Returns the output text."""
    m = method(settings)
    if not m:
        raise AIError("AI mode isn't connected")
    model = settings.get("ai_model") or default_model(models(settings))
    body = {"model": model, "instructions": instructions,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            "store": False, "stream": True}
    if schema:
        body["text"] = {"format": {"type": "json_schema", "name": "picks", "schema": schema, "strict": True}}
    r = requests.post(f"{API}/responses", headers=_auth_header(m) | {"Content-Type": "application/json"},
                      json=body, stream=True, timeout=(20, 600))
    if r.status_code != 200:
        try:
            payload = r.json()
        except ValueError:
            payload = r.text
        # Structured output not available on this route: ask for JSON in the prompt instead.
        code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
        if schema and code == "subscription_sharing_unsupported_capability":
            return respond(settings, instructions + "\nReply with only the JSON object, no prose.", prompt, None)
        _raise_for(r.status_code, payload)
    text, done = [], False
    for raw in r.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            break
        ev = json.loads(data)
        t = ev.get("type")
        if t == "response.output_text.delta":
            text.append(ev.get("delta", ""))
        elif t == "response.completed":
            done = True
        elif t in ("response.failed", "error"):
            err = (ev.get("response") or {}).get("error") or ev.get("error") or {}
            _raise_for(429 if "limit" in str(err.get("code", "")) else 500, {"error": err})
    if not done:
        raise AIError("The AI response was cut off before it finished")
    return "".join(text)


# Picking moments ---------------------------------------------------------------------

INSTRUCTIONS = """You edit YouTube Shorts for this channel: {brief}
You get the transcripts of candidate moments from one stream. Pick the moments that make the best standalone Shorts:
a clear setup and payoff, a genuine reaction (shock, laughter, rage, triumph), a funny line, or a wild gameplay event the streamer is reacting to.
Skip reading chat, donations and sponsor talk, menus and loading, long quiet stretches, and anything that needs context from earlier in the stream.
For each pick, trim it so it opens on the setup (no dead air) and ends right after the payoff. Keep it between 15 and 58 seconds.
Write the title in {language} even if the stream isn't: under 60 characters, describe what happens or quote the best line, no all caps, no emoji, nothing the clip doesn't show.
Do not invent events that aren't supported by the transcript."""

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["picks"],
    "properties": {"picks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "start", "end", "title", "why", "score"],
        "properties": {
            "id": {"type": "integer", "description": "candidate id"},
            "start": {"type": "number", "description": "clip start, seconds from the start of the candidate window"},
            "end": {"type": "number", "description": "clip end, seconds from the start of the candidate window"},
            "title": {"type": "string"},
            "why": {"type": "string", "description": "one short sentence"},
            "score": {"type": "number", "description": "0-10, how strong a Short this is"},
        }}}},
}


def _window_text(c: dict) -> str:
    lines = []
    for s in c["segs"]:
        lines.append(f"[{s['s'] - c['win_start']:6.1f}-{s['e'] - c['win_start']:6.1f}] {s['text']}")
    return "\n".join(lines) or "(no speech)"


def pick(settings: dict, stream_title: str, streamer: str, cands: list[dict], n: int, log) -> list[dict]:
    """cands: dicts with win_start, win_end, peak, base_score, segs. Returns n chosen clips."""
    blocks = []
    for i, c in enumerate(cands):
        blocks.append(f"### Candidate {i} (at {c['peak'] / 3600:.0f}h{c['peak'] % 3600 / 60:02.0f}m into the stream, "
                      f"window {c['win_end'] - c['win_start']:.0f}s long, loudness score {c['base_score']:.1f})\n"
                      f"{_window_text(c)}")
    prompt = (f"Stream: {stream_title}\nStreamer: {streamer}\nPick the best {n} of these {len(cands)} candidates.\n\n"
              + "\n\n".join(blocks))
    log(f"Asking {settings.get('ai_model') or 'the AI'} to pick the best {n} of {len(cands)} moments")
    instructions = INSTRUCTIONS.format(brief=settings.get("channel_brief") or "Shorts from livestreams.",
                                       language=settings.get("title_language") or "English")
    raw = respond(settings, instructions, prompt, SCHEMA)
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except ValueError:
        raise AIError("The AI's answer wasn't readable")
    out, used = [], set()
    for p in sorted(data.get("picks", []), key=lambda p: -p.get("score", 0)):
        i = p.get("id")
        if not isinstance(i, int) or not 0 <= i < len(cands) or i in used:
            continue
        c = cands[i]
        span = c["win_end"] - c["win_start"]
        s = max(0.0, min(float(p["start"]), span - 10))
        e = max(s + 10, min(float(p["end"]), span, s + 58))
        used.add(i)
        start, end = c["win_start"] + s, c["win_start"] + e
        out.append({"peak": c["peak"], "start": start, "end": end, "base_score": c["base_score"],
                    "score": float(p.get("score", 0)), "hook": p["title"].strip().strip('"'),
                    "why": p.get("why", ""),
                    "words": [w for seg in c["segs"] for w in seg["words"] if start <= w["s"] < end]})
        if len(out) >= n:
            break
    if not out:
        raise AIError("The AI didn't pick any usable moments")
    return out
