"""Sign in with ChatGPT (open-source / local app flow, plan usage).

Follows https://developers.openai.com/siwc/token-sharing-open-source/sign-in:
first sign-in registers this app with client_id=dynamic_agent_client and gets a real
client_id back on the callback; later sign-ins and refreshes reuse that issued id.
No client secret is involved. Requests then go to the normal Responses API with the
user's access token, and count against their ChatGPT plan.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import uuid

import jwt
import requests

from .config import DATA

ISSUER = "https://auth.openai.com"
AUTHORIZE = "https://auth.openai.com/api/accounts/authorize"
TOKEN = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY = "https://auth.openai.com/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
APP_NAME = "Cliphound"
CALLBACK_PATH = "/auth/callback"   # the docs fix host and path; only the port may vary

CREDS = DATA / "chatgpt_auth.json"
HOST_ID = DATA / "chatgpt_host_id"

_pending: dict[str, dict] = {}       # state -> {verifier, nonce, redirect_uri, client_id, at}
_refresh_lock = threading.Lock()


class SignInError(Exception):
    pass


class NeedsSignIn(Exception):
    """Tokens are gone for good (revoked / refresh rejected): the user must sign in again."""


def _write_private(path, data: dict):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _creds() -> dict | None:
    return json.loads(CREDS.read_text()) if CREDS.exists() else None


def _host_id() -> str:
    # Stable and opaque per install, as the docs require for ext_agent_host_id.
    if not HOST_ID.exists():
        HOST_ID.write_text(f"urn:uuid:{uuid.uuid4()}")
    return HOST_ID.read_text().strip()


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def status() -> dict:
    c = _creds()
    if not c or not c.get("refresh_token"):
        return {"signed_in": False}
    return {"signed_in": True, "email": c.get("email"), "name": c.get("name"),
            "first_use_seen": c.get("first_use_seen", False)}


def mark_first_use_seen():
    c = _creds()
    if c:
        c["first_use_seen"] = True
        _write_private(CREDS, c)


def start(redirect_uri: str) -> str:
    """Build the authorize URL. The callback lands on this app's own server."""
    c = _creds() or {}
    verifier = _b64url(secrets.token_bytes(48))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    issued = c.get("client_id")
    params = {
        "client_id": issued or "dynamic_agent_client",
        "ext_agent_host_id": _host_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    if issued:
        if c.get("email"):
            params["login_hint"] = c["email"]
    else:
        params["agent_name_hint"] = APP_NAME     # only on first registration
    for k in [k for k, v in _pending.items() if time.time() - v["at"] > 900]:
        _pending.pop(k, None)
    _pending[state] = {"verifier": verifier, "nonce": nonce, "redirect_uri": redirect_uri,
                       "client_id": issued, "at": time.time()}
    return AUTHORIZE + "?" + urllib.parse.urlencode(params)


def _validate_id_token(id_token: str, client_id: str, nonce: str | None) -> dict:
    jwks_uri = requests.get(DISCOVERY, timeout=20).json()["jwks_uri"]
    key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(id_token, key, algorithms=["RS256", "ES256"], audience=client_id, issuer=ISSUER)
    if nonce is not None and claims.get("nonce") != nonce:
        raise SignInError("The sign-in response didn't match this request. Try again")
    return claims


def finish(params: dict) -> dict:
    """Handle the callback query string: check state, exchange the code, save the tokens."""
    state = params.get("state")
    pend = _pending.pop(state, None) if state else None
    if not pend:
        raise SignInError("This sign-in link expired or was already used. Start again from the app")
    if params.get("error"):
        if params["error"] == "access_denied":
            raise SignInError("Sign-in was cancelled")
        raise SignInError(f"ChatGPT sign-in failed: {params.get('error_description') or params['error']}")
    client_id = params.get("client_id") or pend["client_id"]
    if not client_id or client_id == "dynamic_agent_client":
        raise SignInError("OpenAI didn't return a client id for this app")
    r = requests.post(TOKEN, timeout=30, data={
        "grant_type": "authorization_code", "client_id": client_id, "code": params.get("code", ""),
        "code_verifier": pend["verifier"], "redirect_uri": pend["redirect_uri"], "resource": RESOURCE})
    if r.status_code != 200:
        raise SignInError(f"ChatGPT sign-in failed while exchanging the code ({r.status_code}): {r.text[:200]}")
    tok = r.json()
    granted = (tok.get("scope") or params.get("scope") or "").split()
    if PLAN_SCOPE not in granted:
        raise SignInError("ChatGPT plan usage wasn't granted, so AI mode can't use your plan. "
                          "It needs ChatGPT Plus or Pro")
    claims = _validate_id_token(tok["id_token"], client_id, pend["nonce"])
    old = _creds() or {}
    creds = {
        "client_id": client_id, "ext_agent_host_id": _host_id(), "issuer": ISSUER,
        "subject": claims.get("sub"), "email": claims.get("email"), "name": claims.get("name"),
        "id_token": tok["id_token"], "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60, "scopes": granted,
        "saved_at": time.time(), "first_use_seen": old.get("first_use_seen", False),
    }
    _write_private(CREDS, creds)
    return status()


_TERMINAL = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
             "refresh_token_invalidated", "refresh_token_reused"}


def access_token() -> str:
    """A valid access token, refreshing (one at a time) when it's about to expire."""
    with _refresh_lock:
        c = _creds()
        if not c or not c.get("refresh_token"):
            raise NeedsSignIn("Sign in with ChatGPT first")
        if time.time() < c.get("expires_at", 0):
            return c["access_token"]
        r = requests.post(TOKEN, timeout=30, data={
            "grant_type": "refresh_token", "client_id": c["client_id"],
            "refresh_token": c["refresh_token"], "resource": RESOURCE})
        if r.status_code != 200:
            code = ""
            try:
                code = r.json().get("error", "")
            except ValueError:
                pass
            if code in _TERMINAL:
                c.update(access_token=None, refresh_token=None)
                _write_private(CREDS, c)
                raise NeedsSignIn("Your ChatGPT sign-in expired or was revoked. Sign in again")
            raise RuntimeError(f"Couldn't refresh the ChatGPT sign-in ({r.status_code})")
        tok = r.json()
        c.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token") or c["refresh_token"],
                 expires_at=time.time() + int(tok.get("expires_in", 3600)) - 60)
        if tok.get("id_token"):
            c["id_token"] = tok["id_token"]
        _write_private(CREDS, c)
        return c["access_token"]


def sign_out():
    """Revoke the refresh token at OpenAI, then forget the tokens (the issued client id is kept)."""
    c = _creds()
    if not c:
        return
    if c.get("refresh_token"):
        try:
            endpoint = requests.get(DISCOVERY, timeout=20).json()["revocation_endpoint"]
            for _ in range(3):
                r = requests.post(endpoint, timeout=20, data={
                    "token": c["refresh_token"], "token_type_hint": "refresh_token", "client_id": c["client_id"]})
                if r.status_code < 500:
                    break
        except requests.RequestException:
            pass
    keep = {k: c[k] for k in ("client_id", "ext_agent_host_id", "email") if k in c}
    _write_private(CREDS, keep)
