import json

import pytest


def make_job(jobs, status="done", clips=("short_01.mp4",)):
    job = {"id": "abcdef0123", "created": 1, "status": status, "detail": "", "log": [], "waveform": [],
           "url": "https://example.com/v", "meta": {"kind": "vod", "title": "t"}, "mode": "best",
           "source": {"id": "s-1", "name": "Streamer"}, "privacy": "public",
           "clips": [{"file": f, "start": 0.0, "end": 10.0, "hook": "h", "title": "T", "description": "D",
                      "youtube_id": None} for f in clips]}
    d = jobs.JOBS / job["id"]
    d.mkdir()
    (d / "job.json").write_text(json.dumps(job))
    for f in clips:
        (d / f).write_bytes(b"\0")
    return job


@pytest.mark.parametrize("bad", ["..", ".", "abc", "../jobs", "ABCDEF0123", "abcdef0123/..", ""])
def test_job_ids_cannot_leave_the_jobs_folder(cliphound, bad):
    _, jobs = cliphound
    with pytest.raises(LookupError):
        jobs.job_dir(bad)


@pytest.mark.parametrize("bad", ["job.json", "../x.mp4", "short_1.mp4", "short_01.mp4/..", "error.txt"])
def test_clip_names_are_checked(cliphound, bad):
    _, jobs = cliphound
    with pytest.raises(LookupError):
        jobs.clip_path("abcdef0123", bad)


def test_traversal_over_http_touches_nothing(client):
    c, jobs = client
    for path in ("/api/jobs/%2e%2e/cancel", "/api/jobs/..%2F/cancel"):
        r = c.post(path)
        assert r.status_code == 404
    assert not (jobs.JOBS.parent / "cancel").exists()
    assert c.delete("/api/jobs/%2e%2e").status_code == 404
    assert jobs.JOBS.exists()


def test_media_only_serves_shorts(client):
    c, jobs = client
    make_job(jobs)
    assert c.get("/media/abcdef0123/short_01.mp4").status_code == 200
    assert c.get("/media/abcdef0123/job.json").status_code == 404


def test_other_hosts_are_refused(client):
    c, _ = client
    assert c.get("/api/settings", headers={"Host": "evil.example"}).status_code == 403
    assert c.get("/api/settings", headers={"Host": "localhost:8765"}).status_code == 200


def test_cross_site_writes_are_refused(client):
    c, jobs = client
    make_job(jobs)
    r = c.delete("/api/jobs/abcdef0123", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.delete("/api/jobs/abcdef0123", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert (jobs.JOBS / "abcdef0123").exists()
    r = c.delete("/api/jobs/abcdef0123", headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


def test_double_post_starts_one_upload(cliphound, monkeypatch):
    import threading
    import cliphound.upload as upload
    _, jobs = cliphound
    make_job(jobs)
    gate, calls = threading.Event(), []
    monkeypatch.setattr(upload, "status", lambda fresh=False: {"connected": True})

    def fake_upload(*a, **k):
        calls.append(a)
        gate.wait(5)
        return "vid123", "private"
    monkeypatch.setattr(upload, "upload", fake_upload)
    jobs.post_clip("abcdef0123", "short_01.mp4")
    with pytest.raises(ValueError):
        jobs.post_clip("abcdef0123", "short_01.mp4")
    gate.set()
    for _ in range(100):
        if not jobs._posting:
            break
        threading.Event().wait(0.02)
    clip = jobs.get("abcdef0123")["clips"][0]
    assert len(calls) == 1 and clip["youtube_id"] == "vid123" and clip["privacy"] == "private"
    assert calls[0][4] == "public"


def test_retry_remembers_what_was_posted(cliphound):
    _, jobs = cliphound
    job = make_job(jobs, status="failed", clips=("short_01.mp4", "short_02.mp4"))
    job["clips"][0]["youtube_id"], job["clips"][0]["start"] = "abc", 61.04
    assert jobs._posted_from(job) == {"61.0": {"youtube_id": "abc", "privacy": None}}


def test_delete_stops_a_running_job(cliphound):
    _, jobs = cliphound
    make_job(jobs, status="cutting")
    jobs.delete("abcdef0123")
    assert "abcdef0123" in jobs._cancelled
    assert not (jobs.JOBS / "abcdef0123").exists()


def test_edit_rules(cliphound):
    _, jobs = cliphound
    make_job(jobs)
    assert jobs.edit_clip("abcdef0123", "short_01.mp4", title="  New   title ")["title"] == "New title"
    for bad in ("", "x" * 101, "a <b>"):
        with pytest.raises(ValueError):
            jobs.edit_clip("abcdef0123", "short_01.mp4", title=bad)
