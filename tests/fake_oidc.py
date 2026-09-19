"""A minimal OpenID Connect provider for tests: discovery, authorize (auto-approves), token (with PKCE), JWKS.

It signs real RS256 id_tokens, and can be told to misbehave (`idp.mode = "bad_aud"` etc.) so tests can prove the
Service rejects each kind of bad token.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from jwt.algorithms import RSAAlgorithm


class FakeIdP:
    def __init__(self, client_id: str = "vayunx-profiler", client_secret: str | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # for bad signatures
        self.codes: dict[str, dict] = {}
        self.mode: str | None = None  # None | bad_aud | bad_iss | expired | bad_nonce | bad_sig
        self.token_requests = 0
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.issuer = f"http://127.0.0.1:{self.port}"
        self.app = self._build()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def _id_token(self, entry: dict) -> str:
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": entry["sub"], "aud": self.client_id, "iat": now, "exp": now + 300,
                  "nonce": entry["nonce"], "email": entry["email"], "name": entry["name"]}
        if self.mode == "bad_aud":
            claims["aud"] = "someone-else"
        if self.mode == "bad_iss":
            claims["iss"] = "https://evil.example"
        if self.mode == "expired":
            claims["iat"], claims["exp"] = now - 7200, now - 3600
        if self.mode == "bad_nonce":
            claims["nonce"] = "not-the-nonce"
        key = self.other_key if self.mode == "bad_sig" else self.key
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/.well-known/openid-configuration")
        def discovery():
            return {"issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize",
                    "token_endpoint": f"{self.issuer}/token", "jwks_uri": f"{self.issuer}/jwks",
                    "response_types_supported": ["code"], "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"], "code_challenge_methods_supported": ["S256"]}

        @app.get("/jwks")
        def jwks():
            jwk = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            return {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}

        @app.get("/authorize")
        def authorize(client_id: str, redirect_uri: str, state: str, nonce: str, code_challenge: str,
                      code_challenge_method: str, response_type: str, scope: str, login_hint: str = "alice@example.com"):
            if client_id != self.client_id or response_type != "code" or "openid" not in scope.split():
                raise HTTPException(400, "bad authorize request")
            if code_challenge_method != "S256":
                raise HTTPException(400, "PKCE S256 required")
            code = secrets.token_urlsafe(16)
            self.codes[code] = {"redirect_uri": redirect_uri, "nonce": nonce, "challenge": code_challenge,
                                "email": login_hint, "sub": "sub-" + hashlib.sha256(login_hint.encode()).hexdigest()[:12],
                                "name": login_hint.split("@")[0].title()}
            return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)

        @app.post("/token")
        async def token(request: Request):
            form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
            grant_type, code, redirect_uri = form.get("grant_type"), form.get("code"), form.get("redirect_uri")
            client_id, code_verifier, client_secret = form.get("client_id"), form.get("code_verifier", ""), form.get("client_secret")
            self.token_requests += 1
            entry = self.codes.pop(code, None)  # single use
            if grant_type != "authorization_code" or entry is None or entry["redirect_uri"] != redirect_uri \
                    or client_id != self.client_id:
                raise HTTPException(400, "invalid_grant")
            if self.client_secret is not None and client_secret != self.client_secret:
                raise HTTPException(401, "invalid_client")
            digest = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
            if digest != entry["challenge"]:
                raise HTTPException(400, "invalid_grant (PKCE)")
            return {"access_token": secrets.token_urlsafe(16), "token_type": "Bearer", "expires_in": 300,
                    "id_token": self._id_token(entry)}

        return app
