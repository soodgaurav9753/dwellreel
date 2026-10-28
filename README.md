# DwellReel — Deployment edition

Property video generator using FastAPI, FFmpeg and a static HTML/CSS/JavaScript frontend. **It does not use a trained AI model**; promotional copy is generated from supplied data using template logic.

## Repo structure

- `frontend/` — Deploy on **Vercel** as a static website (Framework Preset: `Other`, Root Directory: `frontend`, Build Command: blank, Output Directory: `.`).
- `backend/` — Deploy on **Render** as a Docker Web Service (Root Directory: `backend`; runtime: Docker; instance: Free to test or a suitable paid tier for reliable rendering).
- `backend/music/` — Included original soundtrack files used for exporting MP4.
- `backend/sample_images/`, `backend/sample_output/` — Demo data and output.

## 1. Push to GitHub

Create a new GitHub repository, e.g. `dwellreel`, and upload both `frontend/` and `backend/` folders with this README. Do not commit `outputs/`, API keys or private photos.

## 2. Deploy Python + FFmpeg backend to Render

1. On https://dashboard.render.com click **New > Web Service** and connect the repository.
2. Set **Root Directory** to `backend`.
3. Select **Docker** runtime / Language. The included `backend/Dockerfile` installs Python, FFmpeg and fonts. No separate Build or Start Command is needed with Docker.
4. Choose an appropriate instance type; Free can be used for a demo, but low resources can cause slow renders or memory failures.
5. Deploy. Copy the final `https://YOUR-SERVICE.onrender.com` URL.
6. Visit `https://YOUR-SERVICE.onrender.com/health` and check for `{ "status": "ok", "ffmpeg": true }`.

## 3. Connect frontend and deploy to Vercel

1. In `frontend/config.js`, replace `https://YOUR-RENDER-SERVICE.onrender.com` with the actual Render URL. **Do not add a trailing slash**.
2. Commit and push that edit to GitHub.
3. On https://vercel.com/new import the same GitHub repository.
4. Use these settings: **Root Directory** `frontend`, **Framework Preset** `Other`, **Build Command** blank/disabled, **Output Directory** `.` (the directory containing `index.html`).
5. Deploy, and copy its production URL, e.g. `https://dwellreel.vercel.app`.
6. Back on Render, open **Environment** and create `CORS_ORIGINS` with value **your exact production Vercel URL** (no trailing slash), then save/redeploy/restart when prompted.

## 4. Test

- Vercel site opens: static dashboard and sample images/videos load.
- Select a specific music track: preview plays.
- Upload 4–8 sample photos, choose the correct room labels and select music/format.
- Click **Generate my video**. The Vercel page calls Render `/generate`, which returns a video URL from Render.
- Preview and download the finished MP4 promptly.
- If generation fails, see Render service **Logs** and your browser **Network/Console** tab.

## Local run (unchanged)

From `backend/` with Python dependencies installed and FFmpeg on PATH:

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000` directly; it still serves the full original app. The separate Vercel frontend needs `frontend/config.js` pointed to a running backend with its origin permitted in `CORS_ORIGINS`.

## Hosting limitations

- **Render Free** spins down after inactivity and the filesystem is ephemeral. Video exports may disappear on restart, redeploy or idle spin-down. Always **download your MP4** soon after generating it. The included `sample_output/` lives in source control and remains available.
- CPU/memory requirements for FFmpeg, upload size, concurrency and cold starts make a free service suitable mainly for testing. If generation crashes or takes too long, use a larger Render plan or a proper background-job architecture.
- Current MVP stores only one latest generated MP4 at a time across **all users**, and deletes older exports. It is **not suitable for multiple simultaneous users** without per-user isolated storage.
- No persistent user accounts or production authentication/rate-limiting are included; do not publicize widely before adding protections.
- If you create a new Vercel preview deployment on another domain, add that exact origin to `CORS_ORIGINS` (comma-separated) if you need it to call the API.
