# 🎬 CHEAT CLIP

> **AI-powered YouTube Viral Hotspot Finder** — Instantly discover the most re-watched, highest-engagement moments in any YouTube video and turn them into viral-ready clips for TikTok, YouTube Shorts, and Instagram Reels.

---

## ✨ Features & What It Does

CHEAT CLIP analyzes any YouTube video and offers the following advanced capabilities:

- 📊 **Audience Retention Heatmaps** — Scrapes real-time player interaction data via `yt-dlp` to map and highlight the exact moments viewers rewound and re-watched the most.
- 🧠 **Google Gemini 2.5 Flash AI** — Seamlessly processes transcripts enriched with retention scores to pick hook points, punchlines, and high-energy story arcs.
- 🕒 **Custom Search Range Selection** — Target recommended clip searches either on the entire video or a custom timestamp range (e.g., `29:00` to `31:15` or raw seconds).
- 🎬 **Smart Long-Video Handling** — Automatically scales clip generation counts dynamically (extracting 15–60 clips for videos longer than 1 hour, or 10–30 for shorter uploads).
- 🕓 **Persistent Analysis History** — Local storage caching preserves previously analyzed videos, complete with thumbnails, duration preferences, and timestamps, allowing instant loads without wasting API credits.
- 🔍 **Interactive Search & Filter** — Filter extracted clips by virality level (High 90%+, Mid/Low) or search terms inside the titles and transcript texts.
- 📝 **Clip Creation Checklist** — Check off clips as you generate them in your editor. Checkboxes cross out completed entries and mark them with a `✓ CREATED` badge.
- 📋 **Seamless Multi-Format Export** — Copy individual clips, download the complete dataset as a `JSON` file, or copy the entire list formatted as a clean `Markdown` summary.
- 📈 **Integrated Analytics** — Integrated with `@vercel/analytics` for traffic and performance monitoring.
- 🐈‍⬛ **Support Portal** — Built-in donation link supporting project development via Tako.

---

## 🖥️ Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | React 19 + TypeScript + Vite |
| **Backend** | Python · FastAPI · Uvicorn |
| **AI** | Google Gemini 2.5 Flash (via `google-genai`) |
| **YouTube Data** | `yt-dlp` (retention heatmap) · `youtube-transcript-api` (auto/manual subtitles) |
| **Dev Tooling** | `concurrently` · ESLint · TypeScript |
| **Analytics** | `@vercel/analytics` |
| **Compatibility** | Windows + Python 3.14 hotfixes built-in (`RTLD_*` flags and `os.uname` mocks) |

---

## 🚀 Getting Started

### Prerequisites

- [Node.js](https://nodejs.org/) v18+
- [Python](https://www.python.org/) 3.10+
- **Your own Google Gemini API Key** (Required for real analysis) → [Get one free at Google AI Studio](https://aistudio.google.com/)

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

> **Tip:** You can also skip setting it on the server and enter your key directly in the app's UI at runtime.

### 3. Install frontend dependencies

```bash
npm install
```

### 4. Install backend dependencies

```bash
pip install -r backend/requirements.txt
```

### 5. Run the dev servers (frontend + backend)

```bash
npm run dev
```

This starts:
- **Frontend** → `http://localhost:5173`
- **Backend API** → `http://localhost:8000`

---

## 🔑 API Key & Setup UI

> [!IMPORTANT]
> To run the actual AI-powered video analysis, you **must use your own Gemini API Key**. If you don't provide a key, you can still test the interface's features using **Mock Mode**.

CHEAT CLIP features a refined, secure setup interface:
- **Environment config**: Read directly from server-side `GEMINI_API_KEY`.
- **In-app config**: Input your key right next to the helpful `🔑 Get free key` link.
- **Show/Hide toggle**: Quickly reveal or obscure your key for privacy.
- **Local storage safety**: Keys provided in-app are stored locally in your browser's memory and are never transmitted to outside servers except for Google's API calls.

---

## 📡 API Reference

The backend exposes a streaming-capable FastAPI endpoint at `http://localhost:8000`.

### `GET /api/health`
Health check — returns `{ "status": "ok", "message": "CHEAT CLIP API is active" }`.

### `POST /api/analyze`
Analyzes a video and returns a real-time progress stream (Server-Sent Events) followed by the final results.

**Request body:**
```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "duration": "30s",
  "api_key": "optional_override_key",
  "range_start": 1740.0,
  "range_end": 1875.0
}
```

| Field | Type | Options | Description |
|---|---|---|---|
| `url` | `string` | — | YouTube video URL |
| `duration` | `string` | `"30s"` · `"60s"` · `"1m+"` | Target clip length preference |
| `api_key` | `string` (optional) | — | Override key. Pass `"mock"` to enter Mock Mode. |
| `range_start` | `number` (optional) | — | Start bound in seconds. |
| `range_end` | `number` (optional) | — | End bound in seconds. |

---

## 🧪 Mock Mode

To test UI features without spending API quota, pass `"mock"` as your Gemini API Key in the UI input box. The application will immediately bypass the Gemini API and render a realistic, pre-formed response.

---

## 📁 Project Structure

```
cheat-clip/
├── backend/
│   ├── main.py              # FastAPI app — SSE endpoints & dynamic hotfixes
│   ├── requirements.txt     # Python backend dependencies
│   ├── .env.template        # Sample environment variables
│   └── .env                 # API keys (git-ignored)
├── src/
│   ├── App.tsx              # Main dashboard React application & checklist engine
│   ├── components/
│   │   └── HeatmapTimeline.tsx  # Dynamic interactive retention heatmap component
│   ├── types.ts             # TypeScript definitions
│   └── index.css            # Premium dark style system
├── public/                  # Static assets
├── index.html               # Frontend HTML root
├── vite.config.ts           # Vite configuration
└── package.json             # Node dependencies and build scripts
```

---

## 📜 Available Scripts

| Command | Description |
|---|---|
| `npm run dev` | Runs both React Vite and FastAPI servers concurrently. |
| `npm run dev-frontend` | Starts only the Vite frontend dev server. |
| `npm run dev-backend` | Starts only the Python FastAPI server. |
| `npm run build` | Compiles the TypeScript code and bundles the frontend. |
| `npm run lint` | Runs ESLint syntax and code quality checks. |
| `npm run preview` | Runs a local web server to preview production builds. |

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.

---

<p align="center">
  Built with ❤️ using React, FastAPI, and Google Gemini
</p>
