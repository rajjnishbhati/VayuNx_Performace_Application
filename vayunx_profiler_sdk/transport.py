"""Minimal JSON-over-HTTP transport using only the standard library (no requests/httpx dependency)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class ProfilerHTTPError(RuntimeError):
    def __init__(self, status: int, detail: Any, method: str, url: str):
        super().__init__(f"{method} {url} -> HTTP {status}: {detail}")
        self.status, self.detail = status, detail


class ProfilerConnectionError(RuntimeError):
    pass


class HttpTransport:
    def __init__(self, base_url: str, timeout: float = 10.0, user_agent: str = "vayunx-profiler-sdk-python",
                 api_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        # needed only when the Service requires sign-in (VAYUNX_AUTH=oidc); never logged
        self.api_token = api_token or os.environ.get("VAYUNX_API_TOKEN") or None

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        url = self.base_url + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": self.user_agent}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw).get("detail", raw.decode("utf-8", "replace"))
            except ValueError:
                detail = raw.decode("utf-8", "replace")
            raise ProfilerHTTPError(exc.code, detail, method, url) from None
        except urllib.error.URLError as exc:
            raise ProfilerConnectionError(f"cannot reach Profiler Service at {self.base_url}: {exc.reason}") from None
        return json.loads(body) if body else None

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, payload)

    def get(self, path: str) -> Any:
        return self.request("GET", path)
