"""Background worker: link in, Shorts out. Jobs persist under data/jobs/<id>/job.json."""
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import ai, analyze, download, render, sources, upload
from .config import DATA, JOBS, fill, load_settings, save_settings, write_json

_q: "queue.Queue[str]" = queue.Queue()
_lock = threading.Lock()          # job.json writes
_cancelled: set[str] = set()      # survives the job folder being deleted under a running job

ID_RE = re.compile(r"^[0-9a-f]{10}$")
NAME_RE = re.compile(r"^short_\d{2,}\.mp4$")
RUNNING = ("queued", "fetching", "finding", "cutting", "uploading")


class Cancelled(Exception):
    pass


def job_dir(job_id: str) -> Path:
    """The folder for a job id from a URL. Anything that isn't a real id is refused, so a
    crafted id like '..' can never point outside data/jobs."""
    if not isinstance(job_id, str) or not ID_RE.match(job_id):
        raise LookupError("No such job")
    return JOBS / job_id


def clip_path(job_id: str, name: str) -> Path:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise LookupError("No such Short")
    return job_dir(job_id) / name


def _path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def get(job_id: str) -> dict:
    p = _path(job_id)
    if not p.exists():
        raise LookupError("No such job")
    return json.loads(p.read_text())


def _save(job: dict):
    with _lock:
        p = _path(job["id"])
        if not p.parent.exists():   # deleted while running
            return
        write_json(p, job)


def all_jobs() -> list[dict]:
    out = []
    for p in JOBS.glob("*/job.json"):
        try:
            out.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return sorted(out, key=lambda j: j.get("created", 0), reverse=True)


def _source_for(meta: dict) -> dict:
    """Find or create the streamer this link belongs to, so nobody has to add them by hand."""
    s = load_settings()
    name = (meta.get("channel") or "").strip().lstrip("@")
    if not name:
        name = "Unknown streamer" if meta["kind"] != "local" else "Local file"
    for src in s["sources"]:
        if src["name"].lower() == name.lower() or (meta.get("channel_url") and src.get("url") == meta["channel_url"]):
            return src
    src = {"id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + "-" + uuid.uuid4().hex[:4],
           "name": name, "url": meta.get("channel_url") or "", "credit_url": meta.get("channel_url") or "",
           "permission": meta["kind"] == "local", "notes": "Added automatically from a link"}
    save_settings({"sources": s["sources"] + [src]})
    return src


def _has_permission(job: dict) -> bool:
    if job["meta"]["kind"] == "local":
        return True
    sid = job["source"]["id"]
    return any(s["id"] == sid and s.get("permission") for s in load_settings()["sources"])


def _new(fields: dict) -> dict:
    job = {"id": uuid.uuid4().hex[:10], "created": time.time(), "status": "queued",
           "detail": "Waiting for the job ahead to finish", "log": [], "clips": [],
           "waveform": [], **fields}
    job_dir(job["id"]).mkdir()
    _save(job)
    return job


def create(url: str, clips: int | None = None, length: int | None = None, at=None,
           upload_on: bool | None = None, privacy: str | None = None, use_ai: bool | None = None,
           posted: dict | None = None) -> dict:
    s = load_settings()
    meta = sources.resolve(url)
    at_s = sources.parse_time(at) if at not in (None, "") else meta.get("at")
    job = _new({
        "url": meta["url"], "meta": meta, "source": _source_for(meta),
        "mode": "moment" if at_s is not None else "best",
        "at": at_s,
        "clips_wanted": 1 if at_s is not None else max(1, int(clips or s["clips_per_vod"])),
        "length": max(10, min(int(length or s["clip_seconds"]), 58)),
        "upload": bool(s["auto_upload"] if upload_on is None else upload_on),
        "privacy": privacy or s["privacy"],
        "use_ai": bool(s["ai_mode"] if use_ai is None else use_ai),
        "stream_length": meta.get("duration"),
        "posted": posted or {},
    })
    _q.put(job["id"])
    return job


def cancel(job_id: str):
    d = job_dir(job_id)
    _cancelled.add(job_id)
    if d.exists():
        (d / "cancel").touch()


def _posted_from(old: dict) -> dict:
    """Shorts of an earlier run that already reached YouTube, keyed by their start time, so a
    retry links them up instead of uploading them a second time."""
    posted = dict(old.get("posted") or {})
    for c in old.get("clips", []):
        if c.get("youtube_id"):
            posted[f"{c['start']:.1f}"] = {"youtube_id": c["youtube_id"], "privacy": c.get("privacy")}
    return posted


def retry(job_id: str) -> dict:
    """Run a job again. If it got as far as picking moments, reuse that work instead of redoing it."""
    old = get(job_id)
    if old["status"] in RUNNING:
        raise ValueError("This job is still running")
    picks = job_dir(job_id) / "picks.json"
    posted = _posted_from(old)
    if picks.exists() and old.get("mode") == "best":
        job = _new({**{k: old.get(k) for k in ("url", "meta", "source", "mode", "at", "clips_wanted", "length",
                                                "upload", "privacy", "use_ai", "ai")},
                    "detail": "Reusing the moments already found",
                    "waveform": old.get("waveform", []), "stream_length": old.get("stream_length"),
                    "posted": posted})
        shutil.copy2(picks, job_dir(job["id"]) / "picks.json")
        _q.put(job["id"])
        return job
    return create(old["url"], old["clips_wanted"], old["length"], old["at"] if old["mode"] == "moment" else None,
                  old["upload"], old["privacy"], old.get("use_ai"), posted)


def delete(job_id: str):
    cancel(job_id)
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def _fields(job, clip) -> dict:
    src = job["source"]
    return {"hook": clip["hook"], "streamer": src["name"],
            "credit_url": src.get("credit_url") or src.get("url") or job["url"]}


def _upload_all(job: dict, s: dict, work: Path, log, check, stage):
    """Post every Short not already on YouTube. One failure doesn't stop the rest or fail the
    job; it's shown on that Short with a Post button to try again."""
    if not _has_permission(job):
        log(f"Not posting automatically: no permission recorded for {job['source']['name']}. "
            "Turn it on in Settings > Streamers, or use Post on each Short")
        return
    if not upload.status().get("connected"):
        log("YouTube isn't connected, so the Shorts are saved here only")
        return
    todo = [c for c in job["clips"] if not c.get("youtube_id")]
    if not todo:
        return
    stage("uploading")
    for k, c in enumerate(todo):
        check()
        try:
            c["youtube_id"], c["privacy"] = upload.upload(
                work / c["file"], c["title"], c["description"], s["tags"], job["privacy"], log,
                s.get("category_id", "20"))
            c["post_error"] = None
        except upload.QuotaExceeded as e:
            for rest in todo[k:]:
                rest["post_error"] = str(e)
            log(str(e))
            _save(job)
            break
        except Exception as e:
            c["post_error"] = _friendly(e, str(e).strip() or e.__class__.__name__)
            log(f"Posting {c['file']} failed: {e}")
        _save(job)


def _run(job_id: str):
    job = get(job_id)
    work = job_dir(job_id)
    s = load_settings()

    def log(msg):
        job["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")
        job["detail"] = msg
        _save(job)

    def stage(name, detail=None):
        job["status"] = name
        if detail:
            job["detail"] = detail
        _save(job)

    def check():
        if job_id in _cancelled or (work / "cancel").exists() or not work.exists():
            raise Cancelled()

    try:
        check()
        stage("fetching", "Opening the stream")
        prov = sources.provider(job["meta"], work, log, check)
        use_ai = job.get("use_ai") and ai.method(s) is not None
        if job.get("use_ai") and not use_ai and job["mode"] == "best" and not (work / "picks.json").exists():
            log("AI picking is on but not connected (Settings), so picking by loudness and speech")
        if job["meta"]["kind"] == "live":
            job["stream_length"] = prov.duration
        L = job["length"]

        if job["mode"] == "moment":
            at = job["at"]
            stage("fetching", f"Fetching the moment at {analyze.fmt(at)}")
            s0 = max(0.0, at - L - 6)
            section, sec_start = prov.section(s0, at + L + 6, work / "section_01")
            stage("finding")
            c = analyze.moment(section, at - sec_start, s, L, log)
            if use_ai:
                try:   # let the AI trim it and write the title
                    c = ai.pick(s, job["meta"].get("title", ""), job["source"]["name"], [c], 1, log)[0]
                except Exception as e:
                    log(f"AI titling didn't work: {e}. Using the transcript instead")
            picks = [(c, section, sec_start)]
        elif (work / "picks.json").exists():
            clips = json.loads((work / "picks.json").read_text())
            log(f"Reusing {len(clips)} moments found earlier")
            picks = [(c, None, 0.0) for c in clips]
        else:
            audio = prov.audio()
            check()
            chat = download.chat_times(getattr(prov, "chat", None))
            stage("finding", "Measuring loudness across the stream")
            chooser = None
            if use_ai:
                job["ai"] = True
                chooser = lambda cands: ai.pick(s, job["meta"].get("title", ""), job["source"]["name"], cands,
                                                job["clips_wanted"], log)
            clips, wave, total = analyze.find_clips(audio, chat, s, job["clips_wanted"], L, log, check, chooser)
            clips = analyze.slim(clips)
            job["waveform"], job["stream_length"] = wave, total
            (work / "picks.json").write_text(json.dumps(clips))
            if job["meta"]["kind"] == "live":
                Path(audio).unlink(missing_ok=True)   # 500 MB+; the picks are what a retry needs
            _save(job)
            picks = [(c, None, 0.0) for c in clips]

        stage("cutting")
        credit = "" if job["meta"]["kind"] == "local" else f"@{job['source']['name']}"
        posted = job.get("posted") or {}
        for i, (c, section, sec_start) in enumerate(picks, 1):
            check()
            out = work / f"short_{i:02d}.mp4"
            if section is None and prov.owns_sections():
                pad = 2.0
                log(f"Fetching video for Short {i} ({analyze.fmt(c['start'])})")
                section, sec_start = prov.section(max(0.0, c["start"] - pad), c["end"] + pad, work / f"section_{i:02d}")
                local = dict(c, start=c["start"] - sec_start, end=c["end"] - sec_start,
                             words=[dict(w, s=w["s"] - sec_start, e=w["e"] - sec_start) for w in c.get("words", [])])
            elif section is None:
                section, local = prov.section(0, 0, None)[0], c
            else:
                local = c          # moment mode: already in section time
            log(f"Cutting Short {i} of {len(picks)}")
            render.render(section, local, out, credit, s["layout"], log)
            if prov.owns_sections():
                section.unlink(missing_ok=True)
            start_abs = local["start"] + sec_start
            f = _fields(job, c)
            prev = posted.get(f"{start_abs:.1f}") or {}
            job["clips"].append({
                "file": out.name, "start": start_abs, "end": local["end"] + sec_start,
                "score": round(c.get("score", 0), 2), "hook": c["hook"], "why": c.get("why", ""),
                "title": fill(s["title_template"], f)[:100], "description": fill(s["description_template"], f),
                "youtube_id": prev.get("youtube_id"), "privacy": prev.get("privacy"),
            })
            if prev:
                log(f"Short {i} is already on YouTube from the earlier run, so it won't be posted again")
            _save(job)

        if job["upload"]:
            _upload_all(job, s, work, log, check, stage)
        n = len(job["clips"])
        short = f"{n} Short{'s' if n != 1 else ''} ready"
        if job["mode"] != "moment" and n < job["clips_wanted"]:
            short += f", only {n} of the {job['clips_wanted']} asked for stood out"
        failed = sum(1 for c in job["clips"] if c.get("post_error"))
        if failed:
            short += f". {failed} didn't post, use Post to try again"
        stage("done", short)
    except Cancelled:
        stage("cancelled", "Stopped")
    except Exception as e:
        msg = str(e).strip() or e.__class__.__name__
        log(f"Stopped: {msg}")
        msg = _friendly(e, msg)
        if work.exists():
            (work / "error.txt").write_text(traceback.format_exc())
        stage("failed", msg)


def _friendly(e: Exception, msg: str) -> str:
    """A one-line reason for the journal; the full error stays in the log."""
    if isinstance(e, subprocess.CalledProcessError):
        tool = Path(str(e.cmd[0] if isinstance(e.cmd, (list, tuple)) else e.cmd).split()[0]).name
        return f"{tool} failed while working on the video. Run it again, or open the log for details"
    first = (msg.splitlines() or [""])[0]
    return first if len(first) <= 220 else first[:200].rstrip() + "… (full message in the log)"


def _worker():
    while True:
        job_id = _q.get()
        try:
            if _path(job_id).exists() and job_id not in _cancelled:
                _run(job_id)
        except Exception:
            traceback.print_exc()
        finally:
            _cancelled.discard(job_id)
            _q.task_done()


# One Short at a time ---------------------------------------------------------------

_posting: set[tuple[str, str]] = set()
_post_lock = threading.Lock()


def _update_clip(job_id: str, name: str, line: str | None = None, **fields) -> dict | None:
    with _post_lock:
        try:
            job = get(job_id)
        except LookupError:
            return None   # deleted meanwhile
        clip = None
        for c in job["clips"]:
            if c["file"] == name:
                c.update(fields)
                clip = c
        if line:
            job["log"].append(f"{time.strftime('%H:%M:%S')}  {line}")
        _save(job)
        return clip


def _finished_clip(job_id: str, name: str) -> tuple[dict, dict]:
    path = clip_path(job_id, name)
    job = get(job_id)
    if job["status"] in RUNNING:
        raise ValueError("Wait for this job to finish first")
    clip = next((c for c in job["clips"] if c["file"] == name), None)
    if not clip or not path.exists():
        raise LookupError("No such Short")
    return job, clip


def edit_clip(job_id: str, name: str, title: str | None = None, description: str | None = None) -> dict:
    _, clip = _finished_clip(job_id, name)
    fields = {}
    if title is not None:
        title = " ".join(title.split())
        if not title:
            raise ValueError("The title can't be empty")
        if len(title) > 100:
            raise ValueError("YouTube titles can be 100 characters at most")
        if "<" in title or ">" in title:
            raise ValueError("YouTube doesn't allow < or > in titles")
        fields["title"] = title
    if description is not None:
        if len(description.encode()) > 5000:
            raise ValueError("YouTube descriptions can be 5,000 bytes at most")
        if "<" in description or ">" in description:
            raise ValueError("YouTube doesn't allow < or > in descriptions")
        fields["description"] = description
    if clip.get("youtube_id") and fields:
        raise ValueError("This Short is already on YouTube. Edit it in YouTube Studio")
    if (job_id, name) in _posting:
        raise ValueError("This Short is being posted right now")
    return _update_clip(job_id, name, **fields) or clip


def remove_clip(job_id: str, name: str):
    _finished_clip(job_id, name)
    if (job_id, name) in _posting:
        raise ValueError("This Short is being posted right now")
    with _post_lock:
        job = get(job_id)
        job["clips"] = [c for c in job["clips"] if c["file"] != name]
        _save(job)
    clip_path(job_id, name).unlink(missing_ok=True)


def post_clip(job_id: str, name: str, privacy: str = "public") -> dict:
    """Upload one finished Short to the connected channel, in the background."""
    job, clip = _finished_clip(job_id, name)
    if clip.get("youtube_id"):
        raise ValueError("This Short is already on YouTube")
    if not upload.status().get("connected"):
        raise ValueError("Sign in to YouTube in Settings first")
    with _post_lock:   # two quick clicks must not start two uploads
        if (job_id, name) in _posting:
            raise ValueError("Already posting this Short")
        _posting.add((job_id, name))
    s = load_settings()
    _update_clip(job_id, name, posting=True, post_error=None)

    def run():
        try:
            fresh = next((c for c in get(job_id)["clips"] if c["file"] == name), clip)
            vid, applied = upload.upload(clip_path(job_id, name), fresh["title"], fresh.get("description", ""),
                                         s["tags"], privacy, lambda m: None, s.get("category_id", "20"))
            note = "" if applied == privacy else f" (YouTube applied {applied})"
            _update_clip(job_id, name, f"Posted {name} as {privacy}{note}: https://youtu.be/{vid}",
                         youtube_id=vid, posting=False, privacy=applied)
        except Exception as e:
            msg = str(e).strip() or e.__class__.__name__
            _update_clip(job_id, name, f"Posting {name} failed: {msg}", posting=False,
                         post_error=_friendly(e, msg))
        finally:
            _posting.discard((job_id, name))

    threading.Thread(target=run, daemon=True).start()
    return {"posting": True}


# Disk ------------------------------------------------------------------------------

CACHE = DATA / "cache"


def _size(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def storage() -> dict:
    return {"cache_bytes": _size(CACHE), "jobs_bytes": _size(JOBS)}


def clear_cache(older_than_days: float | None = None) -> int:
    """Delete downloaded stream audio. Returns bytes freed. Never touches the Shorts."""
    if not CACHE.exists():
        return 0
    freed, cutoff = 0, None if older_than_days is None else time.time() - older_than_days * 86400
    for d in CACHE.iterdir():
        if not d.is_dir():
            continue
        stamp = d / "complete"
        when = (stamp if stamp.exists() else d).stat().st_mtime
        if cutoff is None or when < cutoff:
            freed += _size(d)
            shutil.rmtree(d, ignore_errors=True)
    return freed


def _tidy_job_scratch(job_id: str):
    """Leftovers of an interrupted run: per-clip downloads, live audio and caption frames."""
    d = JOBS / job_id
    for p in list(d.glob("section_*")) + list(d.glob("audio_live.*")) + list(d.glob("*.tmp")):
        p.unlink(missing_ok=True)
    for p in d.glob("*_frames"):
        shutil.rmtree(p, ignore_errors=True)


def start():
    for j in all_jobs():
        if not ID_RE.match(str(j.get("id", ""))):
            continue
        changed = False
        for c in j.get("clips", []):
            if c.get("posting"):
                c["posting"], c["post_error"] = False, "Interrupted when the app restarted. Post again"
                changed = True
        if j["status"] in RUNNING:
            j["status"], j["detail"] = "failed", "Interrupted when the app restarted. Run it again"
            changed = True
        _tidy_job_scratch(j["id"])
        if changed:
            _save(j)
    clear_cache(older_than_days=load_settings().get("cache_days", 7))
    threading.Thread(target=_worker, daemon=True).start()
