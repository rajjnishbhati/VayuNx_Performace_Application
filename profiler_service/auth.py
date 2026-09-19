"""Sign-in (Phase 4): OpenID Connect for people, API tokens for SDKs and CI. Off unless VAYUNX_AUTH=oidc.

People: authorization-code flow with PKCE and a nonce. The id_token is checked against the provider's JWKS
(signature, issuer, audience, expiry, nonce). A server-side session follows, in an HttpOnly cookie; only its
SHA-256 is stored, so sessions can be revoked and a database leak is not a session leak.

Programs: `Authorization: Bearer vx_...` API tokens, created by a signed-in person (shown once, stored as a
SHA-256). The tokens are 32 random bytes, so a fast hash is the right tool here - unlike passwords, they
cannot be guessed, which is why no slow password hash is needed.

Cookie-authenticated writes must come from our own origin (Origin or Referer header), which, together with
SameSite=Lax, stops cross-site request forgery. Bearer requests are not cookie-based, so they are exempt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape

import jwt
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from profiler_service.models import ApiToken, AuthSession, User

SESSION_COOKIE = "vayunx_session"
LOGIN_COOKIE = "vayunx_login"
TOKEN_PREFIX = "vx_"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {"/auth/login", "/auth/callback", "/auth/logout", "/auth/me"}
PUBLIC_PREFIXES = ("/v2/shared/",)  # share links: the token in the path is the credential (see shares.py)
ASYMMETRIC_ALGS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "off"  # "off" | "oidc"
    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = None  # optional: public clients use PKCE alone
    public_url: str = "http://127.0.0.1:8010"  # where browsers reach the Service (e.g. http://host:3000/api via the UI)
    admin_emails: frozenset = field(default_factory=frozenset)
    session_secret: str | None = None  # signs the short-lived sign-in cookie; random per process if unset
    session_hours: float = 12.0
    scopes: str = "openid email profile"

    @classmethod
    def from_env(cls, env=None) -> "AuthConfig":
        env = os.environ if env is None else env
        mode = env.get("VAYUNX_AUTH", "off").strip().lower()
        if mode not in ("off", "oidc"):
            raise ValueError("VAYUNX_AUTH must be 'off' or 'oidc'")
        cfg = cls(mode=mode, issuer=(env.get("VAYUNX_OIDC_ISSUER") or "").rstrip("/") or None,
                  client_id=env.get("VAYUNX_OIDC_CLIENT_ID"), client_secret=env.get("VAYUNX_OIDC_CLIENT_SECRET") or None,
                  public_url=(env.get("VAYUNX_PUBLIC_URL") or "http://127.0.0.1:8010").rstrip("/"),
                  admin_emails=frozenset(e.strip().lower() for e in env.get("VAYUNX_ADMIN_EMAILS", "").split(",") if e.strip()),
                  session_secret=env.get("VAYUNX_SESSION_SECRET") or None,
                  session_hours=float(env.get("VAYUNX_SESSION_HOURS", "12")))
        if mode == "oidc":
            missing = [n for n, v in (("VAYUNX_OIDC_ISSUER", cfg.issuer), ("VAYUNX_OIDC_CLIENT_ID", cfg.client_id)) if not v]
            if missing:
                raise ValueError(f"VAYUNX_AUTH=oidc needs {' and '.join(missing)}")
        return cfg

    @property
    def enabled(self) -> bool:
        return self.mode == "oidc"

    @property
    def public_origin(self) -> str:
        u = urllib.parse.urlsplit(self.public_url)
        return f"{u.scheme}://{u.netloc}"

    @property
    def callback_url(self) -> str:
        return f"{self.public_url}/auth/callback"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")


@dataclass
class Identity:
    user_id: str
    email: str | None
    name: str | None
    is_admin: bool
    via: str  # "session" | "token"
    token_id: str | None = None


# ----------------------------------------------------------------------------- OIDC client


class OidcClient:
    def __init__(self, cfg: AuthConfig):
        self.cfg = cfg
        self._discovery: dict | None = None
        self._fetched_at = 0.0
        self._jwks: jwt.PyJWKClient | None = None
        self._secret = (cfg.session_secret or secrets.token_hex(32)).encode()

    def discovery(self) -> dict:
        if self._discovery is None or time.monotonic() - self._fetched_at > 3600:
            with urllib.request.urlopen(f"{self.cfg.issuer}/.well-known/openid-configuration", timeout=10) as r:
                d = json.loads(r.read())
            if d.get("issuer", "").rstrip("/") != self.cfg.issuer:
                raise ValueError("the provider's discovery document names a different issuer")
            self._discovery, self._fetched_at = d, time.monotonic()
            self._jwks = jwt.PyJWKClient(d["jwks_uri"], cache_keys=True, timeout=10)
        return self._discovery

    # -- the short-lived, signed cookie that carries state / nonce / PKCE verifier across the redirect
    def seal(self, data: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
        mac = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{mac}"

    def unseal(self, value: str | None) -> dict | None:
        if not value or "." not in value:
            return None
        body, mac = value.rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data if data.get("exp", 0) > time.time() else None

    def authorize_url(self, state: str, nonce: str, verifier: str) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"response_type": "code", "client_id": self.cfg.client_id, "redirect_uri": self.cfg.callback_url,
             "scope": self.cfg.scopes, "state": state, "nonce": nonce, "code_challenge": challenge,
             "code_challenge_method": "S256"}
        return f"{self.discovery()['authorization_endpoint']}?{urllib.parse.urlencode(q)}"

    def exchange(self, code: str, verifier: str) -> str:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.cfg.callback_url,
                "client_id": self.cfg.client_id, "code_verifier": verifier}
        if self.cfg.client_secret:
            form["client_secret"] = self.cfg.client_secret
        req = urllib.request.Request(self.discovery()["token_endpoint"], data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())["id_token"]

    def verify(self, id_token: str, nonce: str) -> dict:
        d = self.discovery()
        algs = [a for a in d.get("id_token_signing_alg_values_supported", ["RS256"]) if a in ASYMMETRIC_ALGS] or ["RS256"]
        key = self._jwks.get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, key, algorithms=algs, audience=self.cfg.client_id, issuer=self.cfg.issuer,
                            leeway=60, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise jwt.InvalidTokenError("nonce mismatch")
        return claims


# ----------------------------------------------------------------------------- identities


def identify(sessionmaker, cookie: str | None, bearer: str | None) -> Identity | None:
    now = _now()
    with sessionmaker() as s:
        if bearer:
            tok = s.scalar(select(ApiToken).where(ApiToken.token_hash == sha256(bearer), ApiToken.revoked_at.is_(None)))
            if tok is None:
                return None
            user = s.get(User, tok.user_id)
            if tok.last_used_at is None or now - tok.last_used_at > timedelta(seconds=60):
                tok.last_used_at = now
                s.commit()
            return Identity(user.user_id, user.email, user.name, user.is_admin, "token", tok.token_id)
        if cookie:
            sess = s.get(AuthSession, sha256(cookie))
            if sess is None or sess.revoked_at is not None or sess.expires_at <= now:
                return None
            user = s.get(User, sess.user_id)
            return Identity(user.user_id, user.email, user.name, user.is_admin, "session")
    return None


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else None


def _same_origin(request: Request, cfg: AuthConfig) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    u = urllib.parse.urlsplit(source)
    return f"{u.scheme}://{u.netloc}" == cfg.public_origin


def _problem(status: int, error: str, fix: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": {"error": error, "fix": fix, **extra}})


def install(app, cfg: AuthConfig, html_routes: set[str]) -> None:
    """Add the sign-in routes and the middleware that enforces them (a no-op when auth is off)."""
    app.state.auth = cfg
    app.state.oidc = OidcClient(cfg) if cfg.enabled else None
    app.include_router(router)
    if not cfg.enabled:
        return

    @app.middleware("http")
    async def require_identity(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        who = await run_in_threadpool(identify, request.app.state.sessionmaker, request.cookies.get(SESSION_COOKIE),
                                      _bearer(request))
        if who is None:
            if path in html_routes and request.method == "GET":
                nxt = urllib.parse.quote(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
                return RedirectResponse(f"/auth/login?next={nxt}", status_code=302)
            return _problem(401, "Sign in required.", "Sign in in the browser, or send an API token: "
                            "Authorization: Bearer vx_... (create one under Settings).", login_url="/auth/login")
        if who.via == "session" and request.method not in SAFE_METHODS and not _same_origin(request, cfg):
            return _problem(403, "This change must come from the VAYUNX app itself.",
                            "Use the app's own pages, or an API token for scripts.")
        request.state.identity = who
        return await call_next(request)


# ----------------------------------------------------------------------------- routes

router = APIRouter()


def _page(status: int, title: str, message: str) -> HTMLResponse:
    return HTMLResponse(status_code=status, content=f"""<!doctype html><meta charset="utf-8"><title>{escape(title)}</title>
<body style="font-family:system-ui,sans-serif;margin:40px;max-width:640px"><h1>{escape(title)}</h1>
<p>{escape(message)}</p><p><a href="/">Back to VAYUNX</a> · <a href="/auth/login">Try signing in again</a></p></body>""")


def _safe_next(value: str | None) -> str:
    """Only paths on this site: never another host (open-redirect guard)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


@router.get("/auth/login", include_in_schema=False)
def login(request: Request, next: str | None = None):
    cfg: AuthConfig = request.app.state.auth
    if not cfg.enabled:
        return RedirectResponse(_safe_next(next), status_code=302)
    oidc: OidcClient = request.app.state.oidc
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    try:
        url = oidc.authorize_url(state, nonce, verifier)
    except (OSError, ValueError, KeyError) as exc:
        return _page(503, "Sign-in is unavailable", f"The identity provider could not be reached ({type(exc).__name__}).")
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(LOGIN_COOKIE, oidc.seal({"state": state, "nonce": nonce, "verifier": verifier,
                                             "next": _safe_next(next), "exp": time.time() + 600}),
                    max_age=600, httponly=True, samesite="lax", secure=cfg.secure_cookies, path="/")
    return resp


@router.get("/auth/callback", include_in_schema=False)
def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    cfg: AuthConfig = request.app.state.auth
    if not cfg.enabled:
        return RedirectResponse("/", status_code=302)
    oidc: OidcClient = request.app.state.oidc
    pending = oidc.unseal(request.cookies.get(LOGIN_COOKIE))
    if error:
        return _page(401, "Sign-in was not completed", f"The identity provider answered: {error[:100]}.")
    if pending is None or not state or not hmac.compare_digest(pending["state"], state) or not code:
        return _page(400, "Sign-in expired or was tampered with", "Start again from the sign-in link.")
    try:
        claims = oidc.verify(oidc.exchange(code, pending["verifier"]), pending["nonce"])
    except (jwt.PyJWTError, urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        return _page(401, "Sign-in failed", f"The identity provider's answer could not be verified ({type(exc).__name__}).")

    email = (claims.get("email") or "").strip().lower() or None
    with request.app.state.sessionmaker() as s:
        user = s.scalar(select(User).where(User.issuer == claims["iss"], User.subject == str(claims["sub"])))
        if user is None:
            user = User(user_id=uuid.uuid4().hex, issuer=claims["iss"], subject=str(claims["sub"]), created_at=_now())
            s.add(user)
        user.email, user.name = email, (claims.get("name") or None)
        user.is_admin = email is not None and email in cfg.admin_emails  # the configured list is authoritative
        user.last_login_at = _now()
        s.flush()  # the user row first: no ORM relationship tells SQLAlchemy the insert order
        raw = secrets.token_urlsafe(32)
        s.add(AuthSession(token_hash=sha256(raw), user_id=user.user_id, created_at=_now(),
                          expires_at=_now() + timedelta(hours=cfg.session_hours)))
        s.commit()
    resp = RedirectResponse(pending["next"], status_code=302)
    resp.set_cookie(SESSION_COOKIE, raw, max_age=int(cfg.session_hours * 3600), httponly=True, samesite="lax",
                    secure=cfg.secure_cookies, path="/")
    resp.delete_cookie(LOGIN_COOKIE, path="/")
    return resp


@router.post("/auth/logout", include_in_schema=False)
def logout(request: Request):
    cfg: AuthConfig = request.app.state.auth
    raw = request.cookies.get(SESSION_COOKIE)
    if cfg.enabled and raw:
        if not _same_origin(request, cfg):
            return _problem(403, "Sign-out must come from the VAYUNX app itself.", "Use the Sign out button.")
        with request.app.state.sessionmaker() as s:
            sess = s.get(AuthSession, sha256(raw))
            if sess is not None and sess.revoked_at is None:
                sess.revoked_at = _now()
                s.commit()
    resp = JSONResponse({"signed_out": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/auth/me")
def me(request: Request) -> dict:
    cfg: AuthConfig = request.app.state.auth
    if not cfg.enabled:
        return {"auth": "off", "signed_in": False}
    who = identify(request.app.state.sessionmaker, request.cookies.get(SESSION_COOKIE), _bearer(request))
    if who is None:
        return {"auth": "oidc", "signed_in": False, "login_url": "/auth/login"}
    return {"auth": "oidc", "signed_in": True, "user_id": who.user_id, "email": who.email, "name": who.name,
            "is_admin": who.is_admin, "via": who.via}


# -- API tokens (people create them; tokens cannot create tokens)


def _person(request: Request) -> Identity | JSONResponse:
    cfg: AuthConfig = request.app.state.auth
    if not cfg.enabled:
        return _problem(409, "Sign-in is off, so API tokens are not needed.",
                        "Set VAYUNX_AUTH=oidc (see the README) to require sign-in and tokens.")
    who: Identity = request.state.identity
    if who.via != "session":
        return _problem(403, "API tokens can only be managed by a signed-in person.", "Use the Settings page.")
    return who


def _token_out(t: ApiToken) -> dict:
    iso = lambda d: None if d is None else d.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: E731
    return {"token_id": t.token_id, "name": t.name, "prefix": t.prefix, "created_at": iso(t.created_at),
            "last_used_at": iso(t.last_used_at), "revoked": t.revoked_at is not None, "user_id": t.user_id,
            "project_id": t.project_id}


@router.get("/v2/tokens")
def list_tokens(request: Request):
    who = _person(request)
    if isinstance(who, JSONResponse):
        return who
    with request.app.state.sessionmaker() as s:
        q = select(ApiToken).order_by(ApiToken.created_at.desc())
        if not who.is_admin:
            q = q.where(ApiToken.user_id == who.user_id)
        return [_token_out(t) for t in s.scalars(q)]


@router.post("/v2/tokens", status_code=201)
async def create_token(request: Request):
    who = _person(request)
    if isinstance(who, JSONResponse):
        return who
    try:
        body = await request.json()
        name = str(body.get("name", "")).strip()[:128]
        project = body.get("project") or None
    except (ValueError, AttributeError):
        name, project = "", None
    if not name:
        return _problem(422, "A token needs a name.", 'Send {"name": "ci"} - the name says what the token is for.')
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)

    def save():
        from profiler_service.access import access_for
        from profiler_service.models import Project
        with request.app.state.sessionmaker() as s:
            if project is not None and (s.get(Project, str(project)) is None or not access_for(request, s).can(str(project))):
                return _problem(404, f"No such project: {project!r}.", "Pick a project from GET /v2/projects.")
            t = ApiToken(token_id=uuid.uuid4().hex, token_hash=sha256(raw), prefix=raw[:8], name=name,
                         user_id=who.user_id, created_at=_now(), project_id=project)
            s.add(t)
            s.commit()
            return {**_token_out(t), "token": raw, "note": "Copy it now: it is not shown again."}

    return await run_in_threadpool(save)


@router.delete("/v2/tokens/{token_id}", status_code=204)
def revoke_token(request: Request, token_id: str):
    who = _person(request)
    if isinstance(who, JSONResponse):
        return who
    with request.app.state.sessionmaker() as s:
        t = s.get(ApiToken, token_id)
        if t is None or (t.user_id != who.user_id and not who.is_admin):
            return _problem(404, "No such token.", "Pick one from the token list.")
        if t.revoked_at is None:
            t.revoked_at = _now()
            s.commit()
    return Response(status_code=204)
