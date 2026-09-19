# Example apps: MD5 → Argon2id in a real app

Two small login services whose password hashing is picked by `VAYUNX_VARIANT`:

| Variant | Hashing | Status |
|---|---|---|
| `md5` | unsalted MD5 | **insecure - demo only, never store passwords like this** |
| `argon2id` | Argon2id, OWASP minimum (m = 19456 KiB, t = 2, p = 1) | recommended |

Both expose `POST /register`, `POST /login` (`{username, password}`) and `GET /health`, keep users in
memory, and label their hashing with `span("register")` / `span("login")` so the compare view matches
exactly that work across variants (MD5 re-hashes at login, Argon2id verifies).

## Run it

Service on :8010 and UI on :3000 running (see the top-level README), then:

```powershell
.\.venv\Scripts\python.exe examples\run_demo.py fastapi           # ~1 min: both variants, 20 s of logins each
# Next.js (build once):
cd examples\next-login; npm install; npm run build; cd ..\..      # needs sdk-node built first (cd sdk-node; npm install; npm run build)
.\.venv\Scripts\python.exe examples\run_demo.py next
```

Each prints the client's view (logins/s, latency) and a link that opens the comparison under **My app**.

By hand, for the FastAPI app:

```powershell
cd examples\fastapi-login
$env:VAYUNX_VARIANT="md5";      ..\..\.venv\Scripts\vayunx-run.exe -- ..\..\.venv\Scripts\python.exe -m uvicorn app:app --port 8101
$env:VAYUNX_VARIANT="argon2id"; ..\..\.venv\Scripts\vayunx-run.exe -- ..\..\.venv\Scripts\python.exe -m uvicorn app:app --port 8101
python ..\loadgen.py --url http://127.0.0.1:8101 --seconds 20      # against each, then pick both runs under My app
```

The Next.js app starts the SDK from `instrumentation.ts`; `npm start` with `VAYUNX_VARIANT` set is enough.
