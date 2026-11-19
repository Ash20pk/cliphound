"""Upload to YouTube with the Data API v3.

Needs an OAuth client (Desktop app) from a Google Cloud project with the YouTube Data API
enabled, saved as data/client_secret.json. On first connect Google asks which channel to
use: pick the channel the Shorts should go to.

Note: until the Cloud project passes YouTube's API audit, every video it uploads is locked
to private by YouTube regardless of the privacy setting here.
"""
import json
import os
import threading
import time
from pathlib import Path

import requests

from .config import CLIENT_SECRET, TOKEN_FILE


class QuotaExceeded(RuntimeError):
    pass

# Google only issues tokens over https, except to a loopback redirect like ours.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
CALLBACK_PATH = "/auth/google/callback"
_pending: dict = {}      # state -> (flow, created)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return creds if creds.valid else None


_status = {"at": 0.0, "value": None}
_status_lock = threading.Lock()
STATUS_TTL = 300


def forget_status():
    with _status_lock:
        _status["at"], _status["value"] = 0.0, None


def status(fresh: bool = False) -> dict:
    """Which channel we post to. Cached for 5 minutes; a network blip keeps the last good answer."""
    with _status_lock:
        if not fresh and _status["value"] and time.time() - _status["at"] < STATUS_TTL:
            return _status["value"]
        last = _status["value"]
    try:
        value = _status_now()
    except (requests.RequestException, OSError, TimeoutError) as e:
        if last and last.get("connected"):
            return last
        return {"connected": False, "reason": f"offline: {e}"}
    except Exception as e:
        if last and last.get("connected") and "HttpError" not in type(e).__name__:
            return last
        value = {"connected": False, "reason": f"token_error: {e}"}
    with _status_lock:
        _status["at"], _status["value"] = time.time(), value
    return value


def _status_now() -> dict:
    if not CLIENT_SECRET.exists():
        return {"connected": False, "reason": "no_client_secret"}
    try:
        creds = _creds()
    except (requests.RequestException, OSError):
        raise
    except Exception as e:  # revoked / corrupt token
        if "TransportError" in type(e).__name__:
            raise OSError(str(e))
        return {"connected": False, "reason": f"token_error: {e}"}
    if not creds:
        return {"connected": False, "reason": "not_connected"}
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        return {"connected": False, "reason": "no_channel"}
    ch = items[0]["snippet"]
    thumb = (ch.get("thumbnails", {}).get("default") or {}).get("url")
    return {"connected": True, "channel": ch.get("title"), "handle": ch.get("customUrl"),
            "channel_id": items[0]["id"], "thumbnail": thumb}


def start(redirect_uri: str) -> str:
    """Authorization URL for Sign in with Google. Google's account picker also asks which
    channel (personal or brand account) to use."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES, redirect_uri=redirect_uri,
                                         autogenerate_code_verifier=True)
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account",
                                        include_granted_scopes="true")
    for k in [k for k, v in _pending.items() if time.time() - v[1] > 900]:
        _pending.pop(k, None)
    _pending[state] = (flow, time.time())
    return url


def finish(full_url: str, params: dict):
    if params.get("error"):
        raise RuntimeError("Google sign-in was cancelled" if params["error"] == "access_denied"
                           else f"Google sign-in failed: {params['error']}")
    pend = _pending.pop(params.get("state", ""), None)
    if not pend:
        raise RuntimeError("This sign-in link expired or was already used. Start again from Cliphound")
    flow = pend[0]
    flow.fetch_token(authorization_response=full_url)
    creds = flow.credentials
    if not set(SCOPES) <= set(creds.scopes or SCOPES):
        raise RuntimeError("YouTube upload permission wasn't granted. Sign in again and allow it")
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    forget_status()


def sign_out():
    if TOKEN_FILE.exists():
        try:
            tok = json.loads(TOKEN_FILE.read_text())
            requests.post("https://oauth2.googleapis.com/revoke", timeout=15,
                          data={"token": tok.get("refresh_token") or tok.get("token")})
        except (requests.RequestException, ValueError):
            pass
        TOKEN_FILE.unlink(missing_ok=True)
    forget_status()


def upload(path: Path, title: str, description: str, tags: list[str], privacy: str, log,
           category_id: str = "20") -> tuple[str, str]:
    """Returns (video id, the privacy YouTube actually applied). An unaudited Cloud project
    asks for public and gets private, so callers show what YouTube did, not what was asked."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    creds = _creds()
    if not creds:
        raise RuntimeError("YouTube is not connected")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {"title": title[:100], "description": description[:5000], "tags": tags,
                    "categoryId": category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    try:
        while resp is None:
            st, resp = req.next_chunk()
            if st:
                log(f"Uploading {path.name} {int(st.progress() * 100)}%")
    except HttpError as e:
        reason = ""
        try:
            reason = json.loads(e.content)["error"]["errors"][0]["reason"]
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        if reason in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded"):
            raise QuotaExceeded("YouTube's daily upload limit for this Google project is used up. "
                                "Post the rest tomorrow") from None
        if e.resp.status in (401, 403):
            forget_status()
        raise RuntimeError(f"YouTube refused the upload: {reason or e.resp.status}") from None
    applied = (resp.get("status") or {}).get("privacyStatus") or privacy
    return resp["id"], applied
