"""Login for access from outside the home network.

Caddy decides who is "home": requests whose real client IP (resolved through
the trusted reverse proxy's X-Real-IP) is in UEYE_HOME_NETS skip auth entirely.
Everyone else goes through Caddy's forward_auth -> /api/auth/check, which wants
a signed session cookie set by /api/auth/login.

Password: `auth.password` in config.yaml (or env UEYE_PASSWORD). With no
password configured, outside access is refused outright (fail closed).

Session cookie = "<expiry>.<hmac>" signed with a random secret kept in
/config/auth_secret. Deleting that file logs every device out.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

COOKIE = "ueye_session"
SESSION_DAYS = 180
MAX_FAILS = 5            # per client IP ...
FAIL_WINDOW = 15 * 60    # ... per 15 minutes

router = APIRouter()
_fails: dict[str, list[float]] = {}
_lock = threading.Lock()


def _config_dir() -> Path:
    return Path(os.environ.get("NVR_CONFIG", "/config/config.yaml")).parent


def _password() -> str:
    env = os.environ.get("UEYE_PASSWORD")
    if env:
        return env
    try:
        raw = yaml.safe_load(Path(os.environ.get("NVR_CONFIG", "/config/config.yaml")).read_text()) or {}
    except OSError:
        return ""
    return str((raw.get("auth") or {}).get("password") or "")


_secret_cache: bytes | None = None


def _secret() -> bytes:
    global _secret_cache
    if _secret_cache is None:
        p = _config_dir() / "auth_secret"
        try:
            s = p.read_bytes().strip()
        except OSError:
            s = b""
        if len(s) < 32:
            s = secrets.token_hex(32).encode()
            try:
                p.write_bytes(s)
                p.chmod(0o600)
            except OSError:
                pass  # read-only config: sessions just won't survive a restart
        _secret_cache = s
    return _secret_cache


def _sign(exp: int) -> str:
    mac = hmac.new(_secret(), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, _, _ = token.partition(".")
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(token, _sign(exp))


def _client_ip(req: Request) -> str:
    # Set by Caddy from {client_ip}; overrides anything the client sent.
    return req.headers.get("x-client-ip") or (req.client.host if req.client else "?")


def _too_many(ip: str) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = recent
        return len(recent) >= MAX_FAILS


def _record_fail(ip: str) -> None:
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


@router.get("/api/auth/check")
def check(request: Request):
    """forward_auth target. 2xx = let the request through."""
    if _password() and valid_session(request.cookies.get(COOKIE)):
        return Response(status_code=204)
    uri = request.headers.get("x-forwarded-uri", "/")
    if uri.startswith(("/api/", "/go2rtc/")):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return RedirectResponse(f"/login.html?next={quote(uri, safe='')}", status_code=302)


@router.get("/api/auth/status")
def status(request: Request):
    return {
        "home": request.headers.get("x-ueye-home") == "1",
        "configured": bool(_password()),
        "logged_in": valid_session(request.cookies.get(COOKIE)),
    }


class LoginBody(BaseModel):
    password: str


@router.post("/api/auth/login")
def login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    if _too_many(ip):
        return JSONResponse({"detail": "Too many attempts, try again in 15 minutes"}, status_code=429)
    pw = _password()
    if not pw:
        return JSONResponse({"detail": "No password set in config.yaml (auth.password), outside access is off"},
                            status_code=503)
    if not hmac.compare_digest(body.password.encode(), pw.encode()):
        _record_fail(ip)
        time.sleep(0.5)
        return JSONResponse({"detail": "Wrong password"}, status_code=401)
    exp = int(time.time()) + SESSION_DAYS * 86400
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, _sign(exp), max_age=SESSION_DAYS * 86400, httponly=True,
                    secure=request.headers.get("x-forwarded-proto") == "https",
                    samesite="lax", path="/")
    return resp


@router.post("/api/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp
