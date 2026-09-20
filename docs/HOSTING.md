# Hosting on your own machine, published through Cloudflare Tunnel + Zero Trust Access

The Profiler is not a serverless app: it needs real Python with native crypto libraries (argon2, bcrypt),
starts a fresh worker process for every Lab trial, and measures CPU time and memory. Cloudflare Workers and
Pages cannot run that, and shared serverless CPU would make the measurements meaningless anyway - the whole
point is "these numbers came from this machine". So the app runs on a machine you control, and Cloudflare
publishes and guards it.

```
browser ──HTTPS──> Cloudflare (Access policy) ──tunnel──> cloudflared on this PC
                                                            └─> 127.0.0.1:3000  UI (Next.js, production)
                                                                  └─ /api/* ──> 127.0.0.1:8010  Service (FastAPI)
```

Both processes listen on **127.0.0.1 only**, so nothing on your network or the internet reaches them except
through the tunnel.

## On this machine (already set up)

| Piece | Command | Notes |
|---|---|---|
| Service | `.venv\Scripts\python.exe -m profiler_service` | port 8010, database `profiler.db` |
| UI | `npx next start -p 3000 -H 127.0.0.1` in `web\` | production build (`npx next build`) |
| Both | `powershell -File ops\start-vayunx.ps1` | starts whatever is not already running; logs in `ops\logs\` |
| At sign-in | Scheduled task **VAYUNX Profiler** | runs the script above; remove with `Unregister-ScheduledTask -TaskName "VAYUNX Profiler"` |
| Every 2 min | Scheduled task **VAYUNX Tunnel Watchdog** | runs `ops\tunnel-watchdog.ps1`; see below |

Rebuild the UI after changing it: `cd web; npx next build`, then restart the UI process.

## Without a domain: the Quick Tunnel and its watchdog

With no zone of your own, the app is published by a Cloudflare **Quick Tunnel**, which has no stable hostname.
If Cloudflare drops the registration - sleep, a network blip, or no visible reason - `cloudflared` keeps retrying
a tunnel that no longer exists, DNS for that name goes NXDOMAIN, and the URL is dead for good. Only a restart
fixes it, and every restart hands out a new random name. That happened on 20 September 2026, which is why
`ops\tunnel-watchdog.ps1` exists.

Each run does one check:

1. the UI answers on 127.0.0.1:3000 - if not, it runs `ops\start-vayunx.ps1`;
2. the recorded public URL answers 200 - if not, it replaces the tunnel and records the new URL.

It tests the **URL**, not the process, because a live `cloudflared` retrying a dead registration looks healthy.
Only the Quick Tunnel is ever stopped: it is recognised by `--url` on its command line, so the token-managed
`Benchmark_Application` service is never touched. A global mutex keeps two runs from overlapping.

**Where the current URL is** (it changes on every restart - never reuse an old one):

| Place | What it holds |
|---|---|
| Desktop shortcut **VAYUNX Profiler.url** | double-click to open the live site |
| `ops\logs\tunnel-url.txt` | the URL, one line |
| `ops\logs\watchdog.log` | every restart with a timestamp and why |
| `ops\logs\quick-tunnel.log` | cloudflared's own log (previous one kept as `quick-tunnel.prev.log`) |

Run it by hand any time: `powershell -ExecutionPolicy Bypass -File ops\tunnel-watchdog.ps1`
(`-Force` replaces a healthy tunnel, `-NoShortcut` leaves the desktop alone).
Stop the automation with `schtasks /Delete /TN "VAYUNX Tunnel Watchdog" /F`.

**What it does not fix:** the address still changes, so a link you sent yesterday may be dead today, and a Quick
Tunnel still cannot be protected by Zero Trust Access. A domain on Cloudflare solves both - see the section below.

## In the Cloudflare dashboard

1. **Publish the hostname.** Zero Trust → Networks → Tunnels → your running tunnel → **Public Hostname** → Add:
   - Subdomain and domain: for example `profiler` + your zone;
   - Type **HTTP**, URL **127.0.0.1:3000**;
   - Save. Cloudflare creates the DNS record for you.
2. **Guard it - do this first, or immediately after.** Until a policy exists, that hostname is open to anyone
   who knows it. Zero Trust → Access → Applications → Add an application → **Self-hosted**:
   - Application domain: the same hostname;
   - Session duration: 24 hours is a reasonable start;
   - Policy: *Allow* with Include → *Emails ending in* `@vayunx.com` (or named emails, or a group);
   - Save.
3. **Test** in a private window: you should get the Cloudflare login, then the Compare screen.

## What to expect

- **The site is up only while this PC is awake, online and signed in** (the task starts at sign-in). Sleep or
  shutdown takes it offline; the tunnel reconnects by itself afterwards.
- **Every Lab number describes this machine** (i5-8400H, Windows 11) and is affected by whatever else it is
  doing. The compare screen already flags noisy trials.
- **One Lab experiment at a time** - a second request gets a clear 409 while one is running.
- **Cloudflare drops an origin response that takes more than 100 seconds.** Nothing here does: starting a Lab
  run returns immediately and the page polls progress; exports are generated in well under a second.
- **App sign-in stays off.** Access decides who gets in; inside the app everyone is an administrator of the
  Default project. To get per-person roles later, turn on `VAYUNX_AUTH=oidc` and register VAYUNX as an OIDC
  application in Access (Zero Trust → Access → Applications → SaaS → OIDC), then set `VAYUNX_PUBLIC_URL` to
  `https://<your hostname>/api`.

## If something does not work

| Symptom | Check |
|---|---|
| Cloudflare shows 502 / 1033 | Is anything listening? `Get-NetTCPConnection -LocalPort 3000,8010 -State Listen`. Run `ops\start-vayunx.ps1`. |
| The public URL does not resolve at all | The Quick Tunnel was dropped and its name is gone. `ops\tunnel-watchdog.ps1` replaces it within ~2 minutes; the new URL is in `ops\logs\tunnel-url.txt`. |
| Page loads, data does not | The UI proxies `/api` to 8010: `curl http://127.0.0.1:3000/api/v2/projects` should answer 200. |
| Everyone can open the site | The Access application is missing or its policy does not cover that hostname. |
| Tunnel offline | `Get-Service Cloudflared`; the tunnel is token-managed from the dashboard. |
