import html
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, chatgpt_auth, jobs, sources, upload
from .config import CATEGORIES, CLIENT_SECRET, PRIVACY, SettingsError, load_settings, save_settings

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app):
    jobs.start()
    yield


app = FastAPI(title="Cliphound", lifespan=lifespan)

# Cliphound only talks to the browser on this computer. Checking Host stops DNS-rebinding
# pages from reaching it under another name; checking Origin stops any other website open
# in the same browser from sending it requests (posting clips, deleting jobs, signing out).
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _host_ok(host: str | None) -> bool:
    if not host:
        return False
    name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    return name.lower() in LOCAL_HOSTS


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host")
    if not _host_ok(host):
        return JSONResponse({"detail": "Cliphound only answers on 127.0.0.1"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site")
        if origin is not None and origin != f"http://{host}":
            return JSONResponse({"detail": "Requests from other websites are blocked"}, status_code=403)
        if site is not None and site not in ("same-origin", "none"):
            return JSONResponse({"detail": "Requests from other websites are blocked"}, status_code=403)
    return await call_next(request)


def _not_found(e: Exception):
    raise HTTPException(404, str(e) or "Not found")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


# Settings & streamers ---------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.put("/api/settings")
def put_settings(patch: dict):
    try:
        return save_settings(patch)
    except SettingsError as e:
        raise HTTPException(400, str(e))


@app.get("/api/meta")
def meta():
    return {"categories": CATEGORIES, "privacy": PRIVACY}


class SourcePatch(BaseModel):
    permission: bool | None = None
    notes: str | None = None


@app.patch("/api/sources/{sid}")
def patch_source(sid: str, p: SourcePatch):
    s = load_settings()
    if not any(src["id"] == sid for src in s["sources"]):
        raise HTTPException(404, "No such streamer")
    for src in s["sources"]:
        if src["id"] == sid:
            src.update({k: v for k, v in p.model_dump().items() if v is not None})
    return save_settings({"sources": s["sources"]})


@app.delete("/api/sources/{sid}")
def del_source(sid: str):
    s = load_settings()
    return save_settings({"sources": [x for x in s["sources"] if x["id"] != sid]})


# Links & jobs -----------------------------------------------------------------------

@app.get("/api/resolve")
def resolve(url: str):
    try:
        meta = sources.resolve(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    src = next((x for x in load_settings()["sources"]
                if meta.get("channel") and x["name"].lower() == meta["channel"].lower()), None)
    return meta | {"permission": bool(src and src.get("permission")), "source_id": src and src["id"]}


class JobIn(BaseModel):
    url: str
    clips: int | None = None
    length: int | None = None
    at: str | None = None
    upload: bool | None = None
    privacy: str | None = None
    ai: bool | None = None


@app.post("/api/jobs")
def new_job(j: JobIn):
    if j.privacy and j.privacy not in PRIVACY:
        raise HTTPException(400, "Privacy must be private, unlisted or public")
    if j.clips is not None and j.clips < 1:
        raise HTTPException(400, "Ask for at least one Short")
    try:
        return jobs.create(j.url, j.clips, j.length, j.at, j.upload, j.privacy, j.ai)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/jobs")
def list_jobs():
    return jobs.all_jobs()


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    try:
        jobs.cancel(jid)
    except LookupError as e:
        _not_found(e)
    return {"ok": True}


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: str):
    try:
        return jobs.retry(jid)
    except LookupError as e:
        _not_found(e)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    try:
        jobs.delete(jid)
    except LookupError as e:
        _not_found(e)
    return {"ok": True}


class ClipPatch(BaseModel):
    title: str | None = None
    description: str | None = None


@app.patch("/api/jobs/{jid}/clips/{name}")
def edit_clip(jid: str, name: str, p: ClipPatch):
    try:
        return jobs.edit_clip(jid, name, p.title, p.description)
    except LookupError as e:
        _not_found(e)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.delete("/api/jobs/{jid}/clips/{name}")
def remove_clip(jid: str, name: str):
    try:
        jobs.remove_clip(jid, name)
    except LookupError as e:
        _not_found(e)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/jobs/{jid}/post/{name}")
def post_clip(jid: str, name: str):
    try:
        return jobs.post_clip(jid, name, privacy="public")
    except LookupError as e:
        _not_found(e)
    except ValueError as e:
        raise HTTPException(409, str(e))


def _clip_file(jid: str, name: str) -> Path:
    try:
        path = jobs.clip_path(jid, name)
    except LookupError as e:
        _not_found(e)
    if not path.exists():
        raise HTTPException(404, "No such Short")
    return path


@app.get("/media/{jid}/{name}")
def media(jid: str, name: str):
    return FileResponse(_clip_file(jid, name), media_type="video/mp4")


@app.get("/api/jobs/{jid}/download/{name}")
def download_clip(jid: str, name: str):
    path = _clip_file(jid, name)
    job = jobs.get(jid)
    clip = next((c for c in job["clips"] if c["file"] == name), {})
    stem = "".join(ch for ch in f"{job['source']['name']} {clip.get('hook', name)}" if ch.isalnum() or ch in " -_").strip()
    return FileResponse(path, media_type="video/mp4", filename=f"{stem[:80] or name}.mp4")


# Disk -------------------------------------------------------------------------------

@app.get("/api/storage")
def storage():
    return jobs.storage()


@app.delete("/api/storage/cache")
def clear_cache():
    return {"freed": jobs.clear_cache()} | jobs.storage()


# YouTube --------------------------------------------------------------------------

@app.get("/api/youtube")
def yt_status(fresh: bool = False):
    return upload.status(fresh=fresh)


@app.post("/api/youtube/client-secret")
async def yt_secret(file: UploadFile):
    data = await file.read(200_000)
    try:
        parsed = json.loads(data)
        inner = parsed.get("installed") or parsed.get("web") or {}
        ok = bool(inner.get("client_id") and inner.get("client_secret"))
    except (ValueError, AttributeError):
        ok = False
    if not ok:
        raise HTTPException(400, "That file isn't an OAuth client. Download the Desktop app client JSON")
    fd = os.open(CLIENT_SECRET, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    upload.forget_status()
    return {"ok": True}


@app.get("/api/youtube/start")
def yt_start(request: Request):
    if not CLIENT_SECRET.exists():
        raise HTTPException(400, "Add the OAuth client file first")
    port = request.url.port or 8765
    return RedirectResponse(upload.start(f"http://127.0.0.1:{port}{upload.CALLBACK_PATH}"))


@app.get(upload.CALLBACK_PATH)
def yt_callback(request: Request):
    try:
        upload.finish(str(request.url).replace("localhost", "127.0.0.1"), dict(request.query_params))
        return RedirectResponse("/?youtube=connected")
    except Exception as e:
        return _sign_in_failed("Google", e)


@app.post("/api/youtube/signout")
def yt_signout():
    upload.sign_out()
    return upload.status(fresh=True)


# AI mode ----------------------------------------------------------------------------

@app.get("/api/ai")
def ai_status():
    s = load_settings()
    st = ai.status(s)
    st["models"], st["models_error"] = [], None
    if st["method"]:
        try:
            st["models"] = ai.models(s)
            st["default_model"] = ai.default_model(st["models"])
        except Exception as e:
            st["models_error"] = str(e)
    return st


class KeyIn(BaseModel):
    key: str


@app.post("/api/ai/key")
def ai_key(k: KeyIn):
    try:
        ai.save_key(k.key)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    save_settings({"ai_auth": "key", "ai_model": None})
    return ai_status()


@app.delete("/api/ai/key")
def ai_key_remove():
    ai.remove_key()
    return ai_status()


@app.get("/api/ai/chatgpt/start")
def chatgpt_start(request: Request):
    port = request.url.port or 8765
    return RedirectResponse(chatgpt_auth.start(f"http://127.0.0.1:{port}{chatgpt_auth.CALLBACK_PATH}"))


@app.get(chatgpt_auth.CALLBACK_PATH)
def chatgpt_callback(request: Request):
    try:
        chatgpt_auth.finish(dict(request.query_params))
        save_settings({"ai_auth": "chatgpt", "ai_model": None})
        return RedirectResponse("/?ai=signed-in")
    except Exception as e:
        return _sign_in_failed("ChatGPT", e)


def _sign_in_failed(who: str, e: Exception):
    msg = html.escape(str(e))
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8><title>{who} sign-in didn't finish</title>
<body style="font:16px system-ui;background:#1B1530;color:#F6E7CF;padding:48px;max-width:640px">
<h1 style="font-size:24px">Sign-in didn't finish</h1><p>{msg}</p><p><a style="color:#7FE3D0" href="/">Back to Cliphound</a></p>""",
                        status_code=400)


@app.post("/api/ai/chatgpt/signout")
def chatgpt_signout():
    chatgpt_auth.sign_out()
    return ai_status()


@app.post("/api/ai/chatgpt/seen")
def chatgpt_seen():
    chatgpt_auth.mark_first_use_seen()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
