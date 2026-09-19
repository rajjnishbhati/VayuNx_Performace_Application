"""The FastAPI example app works in both variants, without the profiler running."""

import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

APP = Path(__file__).resolve().parent.parent / "examples" / "fastapi-login" / "app.py"


def serve(monkeypatch, variant):
    monkeypatch.setenv("VAYUNX_VARIANT", variant)
    spec = importlib.util.spec_from_file_location(f"example_app_{variant}", APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return srv, thread, f"http://127.0.0.1:{port}"


def status(url, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


@pytest.mark.parametrize("variant", ["md5", "argon2id"])
def test_register_and_login(monkeypatch, variant):
    srv, thread, url = serve(monkeypatch, variant)
    try:
        assert status(url, "/health")[1]["variant"] == variant
        assert status(url, "/register", {"username": "ana", "password": "s3cret"})[0] == 201
        assert status(url, "/register", {"username": "ana", "password": "other"})[0] == 409
        assert status(url, "/login", {"username": "ana", "password": "s3cret"})[0] == 200
        assert status(url, "/login", {"username": "ana", "password": "wrong"})[0] == 401
        assert status(url, "/login", {"username": "nobody", "password": "s3cret"})[0] == 401
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


def test_unknown_variant_is_refused():
    import os
    r = subprocess.run([sys.executable, str(APP)], env={**os.environ, "VAYUNX_VARIANT": "sha1"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "must be 'md5' or 'argon2id'" in r.stderr
