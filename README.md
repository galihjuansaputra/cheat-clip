# 🎬 CHEAT CLIP

> **AI-powered YouTube Viral Hotspot Finder** — Instantly discover the most re-watched, highest-engagement moments in any YouTube video and turn them into viral-ready clips for TikTok, YouTube Shorts, and Instagram Reels.

---

## ✨ What It Does

CHEAT CLIP analyzes any YouTube video using:

- 📊 **YouTube Viewer Retention Heatmaps** — Real audience re-watch data scraped via `yt-dlp` to identify the exact seconds viewers rewound to most.
- 🧠 **Google Gemini 2.5 Flash AI** — Processes the timestamped transcript enriched with engagement scores to intelligently identify viral clip candidates with hooks, punchlines, and story arcs.
- ⚡ **Instant Results** — Returns 10–30 ranked clips with virality scores, key quotes, hook analyses, and precise timestamps — ready to cut.

---

## 🖥️ Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | React 19 + TypeScript + Vite |
| **Backend** | Python · FastAPI · Uvicorn |
| **AI** | Google Gemini 2.5 Flash (via `google-genai`) |
| **YouTube Data** | `yt-dlp` (metadata + heatmap) · `youtube-transcript-api` (subtitles) |
| **Dev Tooling** | `concurrently` · ESLint · TypeScript |

---

## 🚀 Getting Started

### Prerequisites

- [Node.js](https://nodejs.org/) v18+
- [Python](https://www.python.org/) 3.10+
- A **Google Gemini API Key** → [Get one free at Google AI Studio](https://aistudio.google.com/)

---

### 1. Clone the repository

```bash
git clone https://github.com/your-username/cheat-clip.git
cd cheat-clip
```

### 2. Configure environment variables

```bash
# Copy the template and fill in your Gemini API key
cp backend/.env.template backend/.env
```

Open `backend/.env` and replace the placeholder:

```env
GEMINI_API_KEY=your_gemini_api_key_here
```

> **Tip:** You can also skip this and enter your API key directly in the app's UI at runtime.

### 3. Install frontend dependencies

```bash
npm install
```

### 4. Install backend dependencies

```bash
pip install -r backend/requirements.txt
```

### 5. Run the full stack (frontend + backend)

```bash
npm run dev
```

This starts:
- **Frontend** → `http://localhost:5173`
- **Backend API** → `http://localhost:8000`

---

## 🔑 API Key Options

CHEAT CLIP supports two ways to provide your Gemini API key:

| Method | How |
|---|---|
| **Environment file** | Set `GEMINI_API_KEY` in `backend/.env` |
| **In-app input** | Enter the key directly in the web interface at runtime |

> ⚠️ **Never commit your `.env` file** — it is already excluded in `.gitignore`.

---

## 📡 API Reference

The backend exposes a simple REST API at `http://localhost:8000`.

### `GET /api/health`
Health check — returns `{ "status": "ok" }`.

### `POST /api/analyze`

Analyzes a YouTube video and returns ranked viral clip candidates.

**Request body:**
```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "duration": "30s",
  "api_key": "optional_override_key"
}
```

| Field | Type | Options | Description |
|---|---|---|---|
| `url` | `string` | — | YouTube video URL |
| `duration` | `string` | `"30s"` · `"60s"` · `"1m+"` | Target clip length |
| `api_key` | `string` (optional) | — | Override the server's API key. Use `"mock"` for test mode. |

**Response:** A ranked list of clip objects with `title`, `start_time`, `end_time`, `virality_score`, `hook_analysis`, `key_quotes`, and `transcript`.

---

## 🧪 Mock Mode

Want to test the UI without spending API credits? Pass `"mock"` as the API key either in-app or in the request. CHEAT CLIP will return realistic sample clip data without calling Gemini.

---

## 📁 Project Structure

```
cheat-clip/
├── backend/
│   ├── main.py              # FastAPI app — all API routes & Gemini logic
│   ├── requirements.txt     # Python dependencies
│   ├── .env.template        # Environment variable template (safe to commit)
│   └── .env                 # ⛔ Your secrets — never commit this
├── src/
│   ├── App.tsx              # Main React application
│   ├── components/
│   │   └── HeatmapTimeline.tsx  # Viewer retention heatmap visualizer
│   ├── types.ts             # Shared TypeScript interfaces
│   └── index.css            # Global styles
├── public/                  # Static assets
├── index.html               # App entry point
├── vite.config.ts           # Vite configuration
└── package.json             # Node.js dependencies & scripts
```

---

## 📜 Available Scripts

| Command | Description |
|---|---|
| `npm run dev` | Start frontend + backend concurrently |
| `npm run dev-frontend` | Start only the Vite dev server |
| `npm run dev-backend` | Start only the FastAPI server |
| `npm run build` | Build the frontend for production |
| `npm run lint` | Run ESLint |
| `npm run preview` | Preview the production build |

---

## 🛡️ Security Notes

- `backend/.env` and `.env` are git-ignored — your API keys are safe locally.
- The backend accepts API keys at request time for multi-user flexibility.
- CORS is open (`*`) in development mode — restrict `allow_origins` before deploying to production.

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.

---

<p align="center">
  Built with ❤️ using React, FastAPI, and Google Gemini
</p>
