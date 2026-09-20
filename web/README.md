# VAYUNX Crypto Profiler: web UI (Next.js + TypeScript)

The compare screen for the Profiler Service. It calls the Service's JSON API through `/api/*`, which
`next.config.ts` rewrites to the Service (default `http://127.0.0.1:8010`).

## Run

```powershell
# 1. the Profiler Service (from the repo root)
.\.venv\Scripts\python.exe -m profiler_service            # http://127.0.0.1:8010

# 2. the UI (from web/)
npm install
npm run dev                                                 # http://localhost:3000
# a service on another port:
$env:VAYUNX_API_URL = "http://127.0.0.1:8011"; npm run dev
```

The rewrite target is read when Next.js starts (dev) or builds (`npm run build`). Set `VAYUNX_API_URL`
before building if the Service is not on port 8010.

## Screens

| Screen | What it does |
|---|---|
| **Compare** (home) | "Lab test \| My app" switch. For a Lab test: pick 2–4 algorithms (chips with a security badge; quick pairs such as MD5 → Argon2id), click **Run**, watch live progress (trial, stage, ETA, cancel). The result, top to bottom: one-sentence verdict; scorecard (time per call, CPU cores busy, peak RAM, safe for passwords, change) with weak-data flags beside the affected numbers; tabs for App view, Machine view and Security; the what-if capacity box; a details drawer (IDs, machine fingerprint, per-trial data, method). |
| **Runs** | Search and filters (service, algorithm, source, dates) with pagination. Selecting several runs opens Compare: Lab trials of one experiment, or app runs. |
| **Algorithms** | The built-in Lab presets with parameters, library versions and security notes. |
| **Settings** | Default trials and seconds per trial (stored in this browser), and where the Service is. |

## Presentation mode (showing a result to an audience)

**Presentation** in the header (or `Esc` to leave) switches the screen to projector viewing: the left navigation
and the theme controls go away, type and tables grow, and a bar at the bottom walks the result in five steps.

| Step | What is on screen | The sentence to say |
|---|---|---|
| 1. Headline | Verdict and scorecard | "This is what the switch costs per call." |
| 2. In the code | App view tab | "Measured inside the code, per call, not averaged over a process." |
| 3. On the machine | Machine view tab | "And this is what the machine was doing while it ran." |
| 4. Security | Security tab | "The slow one is the one that is safe for passwords." |
| 5. At your traffic | The what-if box | "At our login rate that is this many cores and this much RAM." |

Step with the bar's **Back** / **Next**, or with the arrow keys (`PageUp` / `PageDown` works too, so a presenter
remote does). Keys are ignored while you are typing in the what-if boxes, so you can change the login rate live.
The mode is remembered in this browser and is applied before the first paint, so a reload mid-demo does not
flash the normal layout. It changes presentation only - no number, no API call and no stored result differs.

## Design rules (spec E + the dataviz reference palette)

- **Colour.**
  - The reference algorithm is always the neutral grey, and candidates take categorical slots 1–3 (blue, orange, aqua). The palette is validated in light and dark (`validate_palette.js`: CVD ΔE ≥ 9.2, normal-vision ΔE ≥ 26.5).
  - Aqua is below 3:1 contrast on the light surface, so every chart row is directly labelled and has a table twin.
  - Red and green appear only for security state, always with an icon and a label. "Slower" is never coloured.
- **Charts.**
  - App view: a dot plot on a log scale (median, p95 whisker, p99 ring, faint trial medians).
  - Machine view: small multiples, one metric each, on a shared time axis, with washes where crypto ran and no dual axis.
  - Every chart has a Table toggle and keyboard-reachable tooltips (the Machine view's crosshair moves with the arrow keys).
  - Flame graphs are not the default; for app runs, a link in the details drawer opens the classic flame graph report.
- **Accessibility.** WCAG 2.2 AA intent: visible focus rings, ARIA tabs with arrow keys, labelled form controls, live regions for progress and results, a skip link, light, dark and system themes, and `prefers-reduced-motion`.
- **States.**
  - Loading: a skeleton.
  - Empty: teaches the first comparison with one-click pairs.
  - Error: a plain cause, a fix, and Retry.

## Checks

```powershell
npm run lint          # ESLint (React 19 hooks rules included)
npx tsc --noEmit      # types
npm run build         # production build
node scripts/dod-check.mjs <out-dir> [base-url]   # drives installed Edge through the Phase 2 flow; needs an EMPTY service DB
```
