# 🎬 CHEAT CLIP

> **AI-powered YouTube Viral Hotspot Finder** — Instantly discover the most re-watched, highest-engagement moments in any YouTube video and turn them into viral-ready clips for TikTok, YouTube Shorts, and Instagram Reels.

---

## ✨ Features

- 📊 **Audience Retention Heatmaps** — Maps the exact moments viewers rewound and re-watched most using `yt-dlp` player interaction data.
- 🧠 **Google Gemini 2.5 Flash AI** — Processes transcripts enriched with retention scores to identify hook points, punchlines, and high-energy story arcs.
- 🕒 **Custom Search Range** — Target clip searches on the full video or a custom timestamp range (e.g., `29:00–31:15`).
- 🎬 **Smart Long-Video Handling** — Dynamically scales clip count (15–60 clips for videos >1 hour, 10–30 for shorter).
- 🕓 **Persistent Analysis History** — Local storage caches previous analyses (thumbnails, timestamps, duration prefs) for instant reload.
- 🔍 **Interactive Filter** — Filter clips by virality level (`High 90%+`, Mid, Low) or search by title/transcript keyword.
- 📝 **Clip Checklist** — Check off clips as you create them; completed entries show a `✓ CREATED` badge.
- 📋 **Multi-Format Export** — Copy individual clips, download full dataset as JSON, or copy entire list as Markdown.
- 🧪 **Mock Mode** — Test the full UI without spending API quota by using `"mock"` as the API key.

---

## 🖥️ Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | React 19 · TypeScript · Vite |
| **Backend** | Python · FastAPI · Uvicorn |
| **AI** | Google Gemini 2.5 Flash (`google-genai`) |
| **YouTube Data** | `yt-dlp` (retention heatmap) · `youtube-transcript-api` (subtitles) |
| **Dev Tooling** | `concurrently` · ESLint · TypeScript |
| **Analytics** | `@vercel/analytics` |

---

## 🚀 Setup & Getting Started

### Prerequisites

Make sure you have the following installed:

| Tool | Version | Link |
|---|---|---|
| **Node.js** | v18+ | [nodejs.org](https://nodejs.org/) |
| **Python** | 3.10+ | [python.org](https://www.python.org/) |
| **pip** | (bundled with Python) | — |
| **yt-dlp** | Latest | Auto-installed via `requirements.txt` |

You will also need a **Google Gemini API Key** (free):
👉 [https://aistudio.google.com/](https://aistudio.google.com/)

---

### Step 1 — Clone the Repository

```bash
git clone https://github.com/your-username/cheat-clip.git
cd cheat-clip
```

---

### Step 2 — Configure Environment Variables

The backend reads your Gemini API key from `backend/.env`.

```bash
# Windows (PowerShell)
Copy-Item backend/.env.template backend/.env

# macOS / Linux
cp backend/.env.template backend/.env
```

Then open `backend/.env` and add your key:

```env
GEMINI_API_KEY=your_gemini_api_key_here
```

> **Tip:** You can also skip this step and enter the key directly in the app UI at runtime — it will be stored only in your browser's local storage.

---

### Step 3 — Install Frontend Dependencies

```bash
npm install
```

---

### Step 4 — Install Backend Dependencies

```bash
pip install -r backend/requirements.txt
```

> **Note for Windows users:** If `pip` is not recognized, try `python -m pip install -r backend/requirements.txt`.

---

### Step 5 — Run the Development Servers

```bash
npm run dev
```

This starts **both** servers concurrently:

| Service | URL |
|---|---|
| Frontend (Vite + React) | http://localhost:5173 |
| Backend (FastAPI) | http://localhost:8000 |

---

## 📜 Available Scripts

| Command | Description |
|---|---|
| `npm run dev` | Starts both frontend (Vite) and backend (FastAPI) concurrently |
| `npm run dev-frontend` | Starts only the Vite dev server |
| `npm run dev-backend` | Starts only the Python FastAPI server |
| `npm run build` | Compiles TypeScript and bundles the production frontend |
| `npm run preview` | Preview the production build locally |
| `npm run lint` | Run ESLint code quality checks |

---

## 🔑 API Key Configuration

> [!IMPORTANT]
> A Gemini API key is required for real AI-powered analysis. Without one, use **Mock Mode** for UI testing.

You can provide your key in two ways:

1. **Server-side** (recommended) — Set `GEMINI_API_KEY` in `backend/.env`
2. **In-app** — Enter your key in the app's setup UI; it's saved to browser local storage and is never sent to any server other than Google's API

---

## 🧪 Mock Mode

To test the UI without using API quota:

1. Leave the Gemini API key field **empty** or enter `mock`
2. Submit any YouTube URL
3. The app returns a realistic pre-built response instantly — no API calls made

---

## 📡 API Reference

The backend runs at `http://localhost:8000`.

### `GET /api/health`

Returns server status.

```json
{ "status": "ok", "message": "CHEAT CLIP API is active" }
```

### `POST /api/analyze`

Analyzes a YouTube video and streams real-time progress via **Server-Sent Events (SSE)**, followed by the final clip results.

**Request body:**

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "duration": "30s",
  "api_key": "your_gemini_api_key",
  "range_start": 1740.0,
  "range_end": 1875.0
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | `string` | ✅ | YouTube video URL |
| `duration` | `string` | ✅ | Target clip length: `"30s"`, `"60s"`, or `"1m+"` |
| `api_key` | `string` | ❌ | Overrides server key. Use `"mock"` for Mock Mode |
| `range_start` | `number` | ❌ | Analysis start bound in seconds |
| `range_end` | `number` | ❌ | Analysis end bound in seconds |

---

## 📁 Project Structure

```
cheat-clip/
├── backend/
│   ├── main.py              # FastAPI app — SSE endpoints & Gemini integration
│   ├── requirements.txt     # Python dependencies
│   ├── .env.template        # Environment variable template
│   └── .env                 # Your API key (git-ignored)
├── src/
│   ├── App.tsx              # Main React dashboard & clip checklist engine
│   ├── components/
│   │   └── HeatmapTimeline.tsx  # Interactive retention heatmap component
│   ├── types.ts             # TypeScript type definitions
│   ├── index.css            # Global dark theme & design system
│   └── main.tsx             # React entry point
├── public/                  # Static assets
├── index.html               # HTML root
├── vite.config.ts           # Vite configuration
├── tsconfig.json            # TypeScript configuration
├── package.json             # Node.js dependencies & npm scripts
└── vercel.json              # Vercel deployment configuration
```

---

## 🌐 Deployment

This project is configured for **Vercel** deployment via `vercel.json`.

For the backend, deploy the FastAPI app separately (e.g., Railway, Render, or a VPS), then update the frontend's API base URL accordingly.

---

## 🐛 Troubleshooting

| Issue | Solution |
|---|---|
| `python` not found | Use `python3` or ensure Python is added to your system `PATH` |
| `uvicorn` not found | Run `pip install uvicorn` or ensure your virtual environment is activated |
| Port 8000 already in use | Change port: `npm run dev-backend -- --port 8001` |
| yt-dlp errors | Update yt-dlp: `pip install -U yt-dlp` |
| CORS errors in browser | Ensure the backend is running at `http://localhost:8000` |
| Transcript not found | The video may have disabled subtitles; try a different video |

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.

---

<p align="center">
  Built with ❤️ using React, FastAPI, and Google Gemini
</p>
