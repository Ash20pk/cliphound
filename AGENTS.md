# Cliphound: guide for coding agents

Cliphound turns long Twitch, YouTube and Kick streams into vertical Shorts with burned-in captions, and can post them to YouTube. It's a local FastAPI app with a single-page UI. Everything runs on the user's machine. Humans should read `README.md`; this file is the fast path for an agent setting it up or changing it.

## Set it up

Run these in order and stop at the first failure.

1. **Check the two system tools.**
   ```sh
   uv --version
   ffmpeg -version | head -1
   ```
   If one is missing, install it for the OS, then open a new shell so it's on `PATH`:
   | OS | uv | ffmpeg |
   |---|---|---|
   | macOS | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `brew install ffmpeg` (needs Homebrew) |
   | Windows | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` | `winget install --id Gyan.FFmpeg -e` |
   | Debian/Ubuntu | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `sudo apt install ffmpeg` |

   Ask the user before installing system packages.

2. **Install and test.** uv installs the right Python (3.12+) by itself.
   ```sh
   uv sync
   uv run pytest -q
   ```
   All tests should pass. They use a temporary data folder and never touch `data/`.

3. **Start the server.** It blocks, so run it in the background.
   ```sh
   sh run.sh            # macOS / Linux
   run.bat              # Windows
   ```
   It's ready when the log says `Application startup complete`. Then check:
   ```sh
   curl -s http://127.0.0.1:8765/api/settings | head -c 200
   ```
   Tell the user to open http://127.0.0.1:8765.

4. **Optional: smoke test with a real link.** This downloads video and, on the first run, the speech-to-text model (about 1.6 GB), so ask first.
   ```sh
   curl -s -X POST http://127.0.0.1:8765/api/jobs -H 'Content-Type: application/json' \
     -d '{"url":"<stream or VOD link>","clips":2,"length":30,"upload":false,"ai":false}'
   curl -s http://127.0.0.1:8765/api/jobs     # poll until status is done or failed
   ```

### Needs the user

Don't try to do these yourself. Point the user at the matching README section.

- **YouTube posting** needs the user's own Google Cloud project, a Desktop OAuth client JSON and a browser sign-in. See "Optional: post to YouTube" in the README. Posting is optional: every Short has a Download button.
- **AI picking** needs the user's OpenAI API key (Settings > AI picking) or their ChatGPT sign-in. Never ask the user to paste a key into chat or a file. They enter it in the app.

## How it works

```
link -> sources.resolve()            metadata, live or finished, platform
     -> provider (sources.py)        Local | Vod (yt-dlp) | Live (live.py, YouTube rewind buffer)
     -> find moments (analyze.py)
          finished YouTube: transcript mode, YouTube's captions, no audio download
          everything else:  audio mode, loudness vs a rolling baseline (+ chat if enabled)
          optional AI:      ai.pick() chooses, trims and titles from the transcripts
     -> per clip: fetch just that section's video (3 at a time), Whisper the clip for captions
     -> render.py                    1080x1920, Pillow caption PNGs overlaid with ffmpeg
     -> upload.py                    YouTube Data API, only with permission recorded or a manual Post
```

| File | What's in it |
|---|---|
| `cliphound/app.py` | Routes, plus the local-only middleware (Host and Origin checks) |
| `cliphound/jobs.py` | Job queue and worker, `_run` pipeline, per-clip post/edit/remove, cache cleanup |
| `cliphound/analyze.py` | Loudness and transcript scoring, candidates, `refine` (snap to sentences), Whisper wrapper (mlx-whisper on Apple Silicon, faster-whisper elsewhere) |
| `cliphound/download.py` | yt-dlp: smallest audio, sections, chat replay, YouTube transcript (json3) |
| `cliphound/live.py` | Live YouTube streams read segment by segment (`&sq=N`) |
| `cliphound/sources.py` | Link resolving and the three providers |
| `cliphound/render.py` | Captions and the ffmpeg render; VideoToolbox on Mac, x264 elsewhere |
| `cliphound/ai.py`, `chatgpt_auth.py` | OpenAI Responses API calls, key storage, ChatGPT sign-in |
| `cliphound/upload.py` | Google OAuth, cached channel status, `videos.insert` |
| `cliphound/config.py` | Paths, `DEFAULTS`, settings validation, atomic JSON writes |
| `cliphound/static/index.html` | The whole UI: one file, plain JS, no build step |
| `tests/` | pytest; `conftest.py` points every path at a temp folder |

## API quick reference

All on `http://127.0.0.1:8765`.

| Method and path | Purpose |
|---|---|
| `GET /api/resolve?url=` | Preview a link |
| `POST /api/jobs` | Start a job: `{url, clips, length, at, upload, privacy, ai}` (all but `url` optional) |
| `GET /api/jobs` | All jobs, newest first, with status, log and clips |
| `POST /api/jobs/{id}/cancel`, `/retry`; `DELETE /api/jobs/{id}` | Manage a job |
| `PATCH /api/jobs/{id}/clips/{file}` | Edit `{title, description}` |
| `POST /api/jobs/{id}/post/{file}` | Upload one Short publicly |
| `DELETE /api/jobs/{id}/clips/{file}` | Remove one Short |
| `GET/PUT /api/settings` | Read or change settings (`PUT` takes a partial object, validated) |
| `GET /api/youtube`, `GET /api/ai`, `GET /api/storage` | Connection and disk status |

Job ids are 10 lowercase hex characters and clip files are `short_NN.mp4`. Anything else is a 404 by design.

## Rules when changing code

- **Never commit `data/`.** It holds settings, OAuth tokens, API keys, downloaded audio and the user's Shorts. It's gitignored, so keep it that way. Credential files are written with mode `0600`.
- **Keep it local-only.** The middleware in `app.py` refuses other Host headers and cross-site writes. Don't loosen it or bind to `0.0.0.0`. When calling the API with curl, either send no `Origin` header or `Origin: http://127.0.0.1:8765`.
- **Validate ids from URLs** with `jobs.job_dir()` and `jobs.clip_path()`. Never build paths from raw input.
- **Write JSON atomically** with `config.write_json`.
- **Templates** only allow `{hook}`, `{streamer}` and `{credit_url}`. Use `config.fill()`, never `str.format` on user text.
- **Respect streamer permission.** Automatic posting requires permission recorded for that streamer; manual Post asks the user to confirm in the UI. Keep both.
- **The UI stays one file** with no build tooling and no new dependencies. The polling list updates each Short card on its own, so playing videos aren't reloaded. Keep that.
- **Copy in the UI and docs:** plain, conversational, sentence case, no em dashes.
- **Tests:** add or update tests in `tests/` for behaviour changes and run `uv run pytest -q` before finishing. Use the `cliphound` and `client` fixtures so tests never touch real data.
- After changing Python, restart the server (`run.sh` doesn't auto-reload). The UI file is served with `no-cache`, so a browser refresh is enough for HTML changes.

## Gotchas

- Transcript mode only applies to **finished** YouTube streams. Live streams, Twitch and Kick use audio mode.
- The chat replay is off by default (`read_chat`). On long YouTube streams it takes minutes to download.
- Twitch live streams can't be clipped; use the VOD once the stream ends.
- YouTube keeps API uploads **private** until the user's Cloud project passes Google's audit. That's expected, not a bug. Each clip stores the privacy YouTube actually applied.
- Test-mode Google sign-ins expire after 7 days; the fix is signing in again.
- The first transcription downloads the Whisper model. Off Apple Silicon it runs on the CPU and is slower.
