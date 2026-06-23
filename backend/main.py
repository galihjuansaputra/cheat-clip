import os
import sys

# Windows Python 3.14 compatibility hotfix for unix RTLD flags and uname used in yt-dlp plugins
for flag in ('RTLD_LAZY', 'RTLD_NOW', 'RTLD_GLOBAL', 'RTLD_LOCAL', 'RTLD_NODELETE', 'RTLD_NOLOAD', 'RTLD_DEEPBIND'):
    if not hasattr(os, flag):
        setattr(os, flag, 1)

if not hasattr(os, 'uname'):
    from collections import namedtuple
    UnameResult = namedtuple('UnameResult', ['sysname', 'nodename', 'release', 'version', 'machine'])
    os.uname = lambda: UnameResult('Windows', 'localhost', '10', '10.0', 'AMD64')

import re
import logging
import asyncio
import json
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.genai import types
# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cheat-clip")

app = FastAPI(title="CHEAT CLIP API", description="AI-powered YouTube Viral Hotspot Finder")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins in development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------
# Pydantic Schemas for Gemini Structured Output
# ----------------------------------------------------------------

class ViralClip(BaseModel):
    title: str = Field(description="Catchy clip title, max 8 words")
    start_time: float = Field(description="Clip start in seconds, aligned to a sentence boundary")
    end_time: float = Field(description="Clip end in seconds, aligned to a sentence boundary")
    virality_score: int = Field(description="Virality score 1-100")
    key_quotes: List[str] = Field(description="1-2 key quotes from the clip")
    transcript: str = Field(description="Spoken text of the clip")

class ViralClipGemini(BaseModel):
    title: str = Field(description="Catchy clip title, max 8 words")
    start_time: float = Field(description="Clip start in seconds, aligned to a sentence boundary")
    end_time: float = Field(description="Clip end in seconds, aligned to a sentence boundary")
    virality_score: int = Field(description="Virality score 1-100")
    key_quotes: List[str] = Field(description="1-2 key quotes from the clip")

class VideoAnalysis(BaseModel):
    summary: str = Field(description="1-2 sentence video summary")
    clips: List[ViralClipGemini] = Field(description="List of viral clip candidates (10-30 for shorter videos, or 15-60 for videos longer than 1 hour), sorted by virality_score desc")

# ----------------------------------------------------------------
# API Request/Response Schemas
# ----------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    duration: str = Field("30s", description="Target clip duration: '30s', '60s', or '1m+'")
    api_key: Optional[str] = Field(None, description="Optional custom Gemini API key provided by the user")
    range_start: Optional[float] = Field(None, description="Search range start in seconds")
    range_end: Optional[float] = Field(None, description="Search range end in seconds")

class HeatmapPoint(BaseModel):
    start_time: float
    end_time: float
    value: float

class AnalyzeResponse(BaseModel):
    video_id: str
    title: str
    duration: float
    heatmap: List[HeatmapPoint]
    summary: str
    clips: List[ViralClip]

# ----------------------------------------------------------------
# Helper Functions
# ----------------------------------------------------------------

def extract_video_id(url: str) -> Optional[str]:
    """Extracts the 11-character YouTube video ID from various URL formats."""
    # Handle shorts, embed, watch?v=, youtu.be, etc.
    patterns = [
        r"(?:v=|\/v\/|embed\/|shorts\/|youtu\.be\/|\/embed\/|\/watch\?v=|\/watch\?.+&v=)([^#\&\?]{11})",
        r"^(?:https?:\/\/)?(?:www\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=)?([^#\&\?]{11})"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    # Simple length check fallback if the user just pasted the ID
    if len(url.strip()) == 11:
        return url.strip()
    return None

def fetch_video_metadata(url: str):
    """Fetches video title, duration, and viewer retention heatmap using yt-dlp."""
    proxy_url = os.environ.get("YOUTUBE_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    ydl_opts = {
        'skip_download': True,
        'youtube_include_dash_manifest': False,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True
    }
    if proxy_url:
        ydl_opts['proxy'] = proxy_url
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise Exception("yt-dlp returned empty info dict")
            return {
                "title": info.get('title') or 'Unknown YouTube Video',
                "duration": float(info.get('duration') or 0.0),
                "heatmap": info.get('heatmap') or []
            }
        except Exception as e:
            logger.error(f"Error extracting metadata with yt-dlp: {e}")
            # Try parsing from video URL ID fallback
            video_id = extract_video_id(url)
            if video_id:
                return {
                    "title": f"YouTube Video ({video_id})",
                    "duration": 0.0,
                    "heatmap": []
                }
            raise HTTPException(status_code=400, detail=f"Failed to retrieve YouTube video details: {str(e)}")


def fetch_transcript(video_id: str) -> List[dict]:
    """Retrieves subtitles with multiple fallback strategies."""

    def to_dict_list(fetched) -> List[dict]:
        return [
            {
                "text": getattr(line, "text", ""),
                "start": getattr(line, "start", 0.0),
                "duration": getattr(line, "duration", 0.0)
            }
            for line in fetched
        ]

    proxy_url = os.environ.get("YOUTUBE_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy_url:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        session = requests.Session()
        session.proxies = {"http": proxy_url, "https": proxy_url}
        session.verify = False
        api = YouTubeTranscriptApi(http_client=session)
    else:
        api = YouTubeTranscriptApi()

    # ── Strategy 1: direct fetch by language priority ────────────────────────
    priority_langs = ['id', 'en', 'es', 'pt', 'fr', 'de', 'ja', 'ko', 'zh-Hans', 'zh-Hant', 'ar', 'hi', 'ru']
    for lang in priority_langs:
        try:
            data = to_dict_list(api.fetch(video_id, languages=[lang]))
            if data:
                logger.info(f"Transcript fetched via direct fetch (lang={lang})")
                return data
        except Exception:
            continue

    # ── Strategy 2: list all and try manual transcripts first ─────────────────
    try:
        all_transcripts = list(api.list(video_id))
        manual    = [t for t in all_transcripts if not getattr(t, 'is_generated', False)]
        generated = [t for t in all_transcripts if     getattr(t, 'is_generated', False)]

        for transcript in (manual + generated):
            try:
                data = to_dict_list(transcript.fetch())
                if data:
                    logger.info(
                        f"Transcript fetched via list: {transcript.language} "
                        f"({'auto' if getattr(transcript, 'is_generated', False) else 'manual'})"
                    )
                    return data
            except Exception as e:
                logger.warning(f"Failed ({transcript.language_code}): {e}")
                continue
    except Exception as e:
        logger.warning(f"Could not list transcripts: {e}")

    # ── All strategies exhausted ──────────────────────────────────────────────
    raise HTTPException(
        status_code=400,
        detail=(
            "No subtitles could be retrieved for this video. "
            "The video may have subtitles disabled, be age-restricted, private, or require a sign-in. "
            "Try a different video."
        )
    )





def get_average_heatmap_value(start: float, end: float, heatmap: List[dict]) -> float:
    """Calculates the average retention score from the heatmap for a transcript time segment."""
    if not heatmap:
        return 0.0
    
    overlaps = []
    for point in heatmap:
        p_start = point.get('start_time', 0.0)
        p_end = point.get('end_time', 0.0)
        p_val = point.get('value', 0.0)
        
        # Check if heatmap point overlaps with transcript segment
        if max(start, p_start) < min(end, p_end):
            overlaps.append(p_val)
            
    if overlaps:
        return sum(overlaps) / len(overlaps)
        
    # Fallback to closest point if no direct overlap matches
    closest_val = 0.0
    min_dist = float('inf')
    mid_time = (start + end) / 2.0
    for point in heatmap:
        p_mid = (point.get('start_time', 0.0) + point.get('end_time', 0.0)) / 2.0
        dist = abs(p_mid - mid_time)
        if dist < min_dist:
            min_dist = dist
            closest_val = point.get('value', 0.0)
    return closest_val

# ----------------------------------------------------------------
# Routes
# ----------------------------------------------------------------

def _sse(data: dict) -> str:
    """Format a dict as a Server-Sent Event string."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

@app.get("/api/health")
def health_check():
    return {"status": "ok", "message": "CHEAT CLIP API is active"}

@app.post("/api/analyze")
async def analyze_video(request: AnalyzeRequest):
    """Stream real-time progress via Server-Sent Events, then deliver the final result."""

    async def stream():
        gemini_key = (request.api_key or '').strip()
        is_mock = gemini_key.lower() == "mock"

        if not gemini_key:
            yield _sse({"error": "Gemini API Key is required. Enter it in the web interface.", "status": 400})
            return

        # ── Step 1: Extract video ID & metadata ─────────────────────────────
        video_id = extract_video_id(request.url)
        if not video_id:
            if not is_mock:
                yield _sse({"error": "Invalid YouTube URL. Please check the link and try again.", "status": 400})
                return
            video_id = "dQw4w9WgXcQ"

        yield _sse({"step": 1, "message": "Connecting to YouTube — fetching video title and duration..."})

        try:
            metadata = await asyncio.to_thread(fetch_video_metadata, request.url)
            title    = metadata["title"]
            duration = metadata["duration"]
            heatmap  = metadata.get("heatmap") or []
        except Exception as e:
            if is_mock:
                title = "Mock YouTube Video"
                duration = 212.0
                heatmap = []
            else:
                msg = e.detail if isinstance(e, HTTPException) else str(e)
                yield _sse({"error": f"Failed to fetch video details: {msg}", "status": 500})
                return

        logger.info(f"Metadata fetched: title='{title}', duration={duration}s, heatmap_pts={len(heatmap)}")

        # ── Step 2: Heatmap ──────────────────────────────────────────────────
        if heatmap:
            yield _sse({"step": 2, "message": f"Viewer retention heatmap loaded — {len(heatmap)} data points scraped."})
        else:
            yield _sse({"step": 2, "message": "No heatmap available for this video — will rely on transcript content analysis."})

        # ── Step 3: Transcript ───────────────────────────────────────────────
        yield _sse({"step": 3, "message": "Fetching subtitles — trying video's original language..."})

        try:
            transcript_lines = await asyncio.to_thread(fetch_transcript, video_id)
            yield _sse({"step": 3, "message": f"Subtitles loaded — {len(transcript_lines)} lines parsed successfully."})
        except Exception as e:
            if is_mock:
                transcript_lines = [
                    {"text": "Hello and welcome to this video.",            "start":  0.0, "duration": 3.0},
                    {"text": "Today we are looking at how this app works.",  "start":  3.0, "duration": 4.0},
                    {"text": "It finds viral hotspots and highlights them.",  "start":  7.0, "duration": 4.0},
                    {"text": "Most people think it's magic.",               "start": 11.0, "duration": 3.0},
                    {"text": "But it uses YouTube player heatmaps.",         "start": 14.0, "duration": 4.0},
                    {"text": "And processes them with Gemini AI models.",    "start": 18.0, "duration": 4.0},
                    {"text": "This is changing how editors crop videos.",    "start": 22.0, "duration": 5.0},
                    {"text": "If you want to grow on TikTok, try it.",      "start": 27.0, "duration": 5.0},
                    {"text": "We will explore the code next.",               "start": 32.0, "duration": 3.0},
                ]
                yield _sse({"step": 3, "message": "Mock mode — using sample transcript."})
            else:
                msg = e.detail if isinstance(e, HTTPException) else str(e)
                yield _sse({"error": msg, "status": 400})
                return

        # Estimate duration from transcript if missing
        if duration == 0.0 and transcript_lines:
            last = transcript_lines[-1]
            duration = last.get("start", 0.0) + last.get("duration", 0.0)

        # Slice transcript based on custom search range if provided
        start_bound = 0.0
        end_bound = duration
        if request.range_start is not None or request.range_end is not None:
            start_bound = request.range_start if request.range_start is not None else 0.0
            end_bound = request.range_end if request.range_end is not None else duration

            if start_bound < 0.0:
                start_bound = 0.0
            if end_bound > duration:
                end_bound = duration

            if start_bound >= end_bound:
                yield _sse({"error": "Invalid search range: start time must be less than end time.", "status": 400})
                return

            filtered_lines = []
            for line in transcript_lines:
                ls = line.get("start", 0.0)
                le = ls + line.get("duration", 0.0)
                if max(ls, start_bound) < min(le, end_bound):
                    filtered_lines.append(line)
            
            transcript_lines = filtered_lines
            if not transcript_lines:
                yield _sse({"error": f"No subtitles found in the specified range {start_bound}s to {end_bound}s.", "status": 400})
                return
            
            duration = end_bound - start_bound
            logger.info(f"Filtered transcript to custom range: {start_bound}s to {end_bound}s (duration: {duration}s)")

        # Enrich transcript with heatmap engagement scores
        enriched_transcript = []
        for line in transcript_lines:
            ls   = line.get("start", 0.0)
            ld   = line.get("duration", 0.0)
            le   = ls + ld
            score = get_average_heatmap_value(ls, le, heatmap)
            enriched_transcript.append({
                "start":      round(ls, 2),
                "end":        round(le, 2),
                "text":       line.get("text", ""),
                "engagement": round(score, 3)
            })

        # ── Mock short-circuit ───────────────────────────────────────────────
        if is_mock:
            yield _sse({"step": 4, "message": "Mock mode — generating sample clip data..."})
            mock_clips = [
                ViralClip(title="Finding hotspots using heatmaps",  start_time=11.0, end_time=22.0, virality_score=95,
                          key_quotes=["Uses YouTube player heatmaps.", "Processes using Gemini AI."],
                          transcript="Most people think it's magic. But it uses YouTube player heatmaps."),
                ViralClip(title="Grow on TikTok or Reels",          start_time=22.0, end_time=32.0, virality_score=88,
                          key_quotes=["Changing how editors crop videos.", "If you want to grow on TikTok, try it."],
                          transcript="This is changing how editors crop videos. If you want to grow on TikTok, try it."),
                ViralClip(title="Introductory overview of the tool", start_time=0.0,  end_time=11.0, virality_score=72,
                          key_quotes=["Hello and welcome.", "Finds viral hotspots."],
                          transcript="Hello and welcome. It finds viral hotspots and highlights them."),
            ]
            mock_heatmap = [
                HeatmapPoint(start_time=i*10.0, end_time=(i+1)*10.0,
                             value=0.2 + (0.6 if i in [2,5,8,12,16] else 0.1))
                for i in range(20)
            ] if not heatmap else [
                HeatmapPoint(start_time=float(pt.get('start_time',0.0)),
                             end_time=float(pt.get('end_time',0.0)),
                             value=float(pt.get('value',0.0)))
                for pt in heatmap
            ]
            result = AnalyzeResponse(
                video_id=video_id, title=title, duration=duration or 200.0,
                heatmap=mock_heatmap,
                summary="Mock analysis: this video explains how CHEAT CLIP works.",
                clips=mock_clips
            )
            yield _sse({"done": True, "result": result.model_dump()})
            return

        is_long_video = duration > 3600
        clip_range = "15-60" if is_long_video else "10-30"

        # ── Step 4: Build prompt ─────────────────────────────────────────────
        transcript_dump = []
        for line in enriched_transcript:
            eng = f"|{line['engagement']:.2f}" if heatmap and line['engagement'] > 0 else ""
            transcript_dump.append(f"{line['start']:.1f}|{line['end']:.1f}{eng} {line['text']}")

        MAX_LINES = 2500 if is_long_video else 800
        if len(transcript_dump) > MAX_LINES:
            logger.warning(f"Transcript {len(transcript_dump)} lines — truncating to {MAX_LINES}.")
            transcript_dump = transcript_dump[:MAX_LINES]

        transcript_text = "\n".join(transcript_dump)
        dur_range   = {"30s": "20-40s", "60s": "45-75s", "1m+": "60-180s"}.get(request.duration, "20-40s")
        heatmap_note = (
            "Columns: start|end|audience_interest(0-1). Prioritise high-interest peaks."
            if heatmap else
            "No audience interest data. Use content hooks, energy, and story arcs."
        )
        prompt = (
            f"You are a viral video clip finder.\n"
            f"Find {clip_range} short-form clip candidates from this YouTube transcript for TikTok/Reels/Shorts.\n\n"
            f"Title: {title}\n"
            f"Duration Range: {int(start_bound)}s to {int(end_bound)}s (Length: {int(duration)}s) | Target clip length: {dur_range}\n"
            f"{heatmap_note}\n"
            f"Match output language to transcript language.\n\n"
            f"Transcript (start|end[|interest] text):\n---\n{transcript_text}\n---\n\n"
            f"Rules: use exact seconds from transcript; clips must start/end at sentence boundaries; do not overlap.\n"
            f"Return {clip_range} clips sorted by virality_score desc."
        )

        yield _sse({"step": 4, "message": f"Prompt built — sending {len(transcript_dump)} transcript lines to Gemini..."})

        # ── Step 4: Gemini API call with fallback models and retry ───────────
        client = genai.Client(api_key=gemini_key)
        models_to_try = ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-1.5-flash']
        response = None
        last_error = None

        for model_name in models_to_try:
            MAX_RETRIES = 2
            RETRY_DELAYS = [3]  # If a model fails, wait 3 seconds before the single retry, then try next model
            
            for attempt in range(MAX_RETRIES):
                if attempt > 0:
                    wait = RETRY_DELAYS[attempt - 1]
                    yield _sse({"step": 4, "message": f"{model_name} is busy — waiting {wait}s before retry {attempt + 1}/{MAX_RETRIES}..."})
                    await asyncio.sleep(wait)
                
                yield _sse({"step": 4, "message": f"Calling {model_name} (attempt {attempt + 1}/{MAX_RETRIES})..."})
                try:
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=VideoAnalysis,
                            temperature=0.2,
                        )
                    )
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    err_str = str(e).lower()
                    is_retryable = any(x in err_str for x in ('503', '429', 'unavailable', 'resource exhausted', 'overloaded', 'rate limit'))
                    if not is_retryable:
                        # Non-retryable error (e.g. invalid API key) - break out of retries for this model
                        break
            
            # If we successfully got a response, stop trying other models
            if last_error is None and response is not None:
                break
            
            # Otherwise, log the model failure and prepare for fallback if available
            logger.warning(f"Model {model_name} failed with error: {last_error}")
            if model_name != models_to_try[-1]:
                yield _sse({"step": 4, "message": f"{model_name} failed (high demand or rate limit) — falling back to next model..."})

        if last_error is not None:
            err_str = str(last_error).lower()
            if any(x in err_str for x in ('503', 'unavailable', 'overloaded')):
                yield _sse({"error": "All Gemini models are experiencing high demand. Please link a billing account to your API key or wait a moment and try again.", "status": 503})
            elif any(x in err_str for x in ('429', 'quota', 'resource exhausted', 'rate limit')):
                yield _sse({"error": "Your API key quota/rate limit was exceeded on all fallback models. Link a billing account in Google AI Studio for higher limits.", "status": 429})
            elif any(x in err_str for x in ('401', '403', 'api_key', 'invalid', 'permission')):
                yield _sse({"error": "Invalid Gemini API key. Please double-check it at aistudio.google.com.", "status": 401})
            else:
                logger.error(f"Gemini error after all fallback models: {last_error}")
                yield _sse({"error": f"AI analysis failed: {str(last_error)}", "status": 500})
            return

        if response is None:
            yield _sse({"error": "Gemini returned no response.", "status": 500})
            return

        yield _sse({"step": 4, "message": "Gemini responded — parsing clip candidates..."})

        # Parse structured response
        analysis_data = None
        if hasattr(response, 'parsed') and response.parsed is not None:
            parsed = response.parsed
            analysis_data = {
                "summary": getattr(parsed, 'summary', ''),
                "clips": [
                    {
                        "title":         getattr(c, 'title', ''),
                        "start_time":    getattr(c, 'start_time', 0.0),
                        "end_time":      getattr(c, 'end_time', 0.0),
                        "virality_score": getattr(c, 'virality_score', 0),
                        "key_quotes":    getattr(c, 'key_quotes', []),
                    }
                    for c in (getattr(parsed, 'clips', []) or [])
                ]
            }

        if analysis_data is None:
            if not response.text:
                finish_reason = None
                try:
                    finish_reason = response.candidates[0].finish_reason if response.candidates else None
                except Exception:
                    pass
                if finish_reason and str(finish_reason) in ('SAFETY','RECITATION','OTHER'):
                    yield _sse({"error": f"Gemini blocked this content (reason={finish_reason}).", "status": 422})
                    return
                yield _sse({"error": "Gemini returned an empty response. Try a shorter video or different duration setting.", "status": 500})
                return
            analysis_data = json.loads(response.text)

        clip_count = len(analysis_data.get('clips', []))
        yield _sse({"step": 4, "message": f"Found {clip_count} viral clip candidates — reconstructing transcripts..."})
        logger.info(f"Gemini analysis complete. Found {clip_count} clips.")

        # Reconstruct clip transcripts from enriched_transcript
        final_clips = []
        for raw_clip in analysis_data.get('clips', []):
            start = raw_clip.get('start_time', 0.0)
            end   = raw_clip.get('end_time', 0.0)
            clip_lines = [
                line.get("text", "")
                for line in enriched_transcript
                if max(line.get("start", 0.0), start) < min(line.get("end", 0.0), end)
            ]
            final_clips.append(ViralClip(
                title=raw_clip.get('title', ''),
                start_time=start,
                end_time=end,
                virality_score=raw_clip.get('virality_score', 0),
                key_quotes=raw_clip.get('key_quotes') or [],
                transcript=" ".join(clip_lines)
            ))

        response_heatmap = [
            HeatmapPoint(
                start_time=float(pt.get('start_time', 0.0)),
                end_time=float(pt.get('end_time', 0.0)),
                value=float(pt.get('value', 0.0))
            )
            for pt in (heatmap or [])
        ]

        final_result = AnalyzeResponse(
            video_id=video_id,
            title=title,
            duration=duration,
            heatmap=response_heatmap,
            summary=analysis_data.get("summary", ""),
            clips=final_clips
        )

        yield _sse({"done": True, "result": final_result.model_dump()})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

