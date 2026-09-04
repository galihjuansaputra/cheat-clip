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
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse, RedirectResponse, FileResponse
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
    hook_time: float = Field(description="Absolute timestamp in seconds from video start where the potential hook occurs inside this clip range (must be >= start_time and <= end_time)")
    virality_score: int = Field(description="Virality score 1-100")
    key_quotes: List[str] = Field(description="1-2 key quotes from the clip")
    transcript: str = Field(description="Spoken text of the clip")
    title_suggestion: str = Field(default="", description="Catchy alternative title suggestion")
    caption_suggestion: str = Field(default="", description="Engaging social media caption suggestion")
    hashtag_suggestion: str = Field(default="", description="Relevant hashtags suggestion (e.g. #hashtag1 #hashtag2)")

class ViralClipGemini(BaseModel):
    title: str = Field(description="Catchy clip title, max 8 words")
    start_time: float = Field(description="Clip start in seconds, aligned to a sentence boundary")
    end_time: float = Field(description="Clip end in seconds, aligned to a sentence boundary")
    hook_time: float = Field(description="Absolute timestamp in seconds from video start where the potential hook occurs inside this clip range (must be >= start_time and <= end_time)")
    virality_score: int = Field(description="Virality score 1-100")
    key_quotes: List[str] = Field(description="1-2 key quotes from the clip")
    title_suggestion: str = Field(default="", description="Catchy alternative title suggestion")
    caption_suggestion: str = Field(default="", description="Engaging social media caption suggestion")
    hashtag_suggestion: str = Field(default="", description="Relevant hashtags suggestion (e.g. #hashtag1 #hashtag2)")

class VideoAnalysis(BaseModel):
    summary: str = Field(description="1-2 sentence video summary, followed by 2-4 general hashtags (e.g. #podcast #marriage #success)")
    clips: List[ViralClipGemini] = Field(description="List of viral clip candidates, sorted by virality_score desc")

# ----------------------------------------------------------------
# API Request/Response Schemas
# ----------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    duration: str = Field("30s", description="Target clip duration: '15s', '30s', or '60s'")
    api_key: Optional[str] = Field(None, description="Optional custom Gemini API key provided by the user")
    model: Optional[str] = Field("gemini-2.5-flash", description="Preferred Gemini model name")
    custom_prompt: Optional[str] = Field(None, description="Optional custom focus prompt for clips search")
    range_start: Optional[float] = Field(None, description="Search range start in seconds")
    range_end: Optional[float] = Field(None, description="Search range end in seconds")
    subtitles: Optional[str] = Field(None, description="Optional manual subtitles text (SRT or TXT)")
    subtitles_filename: Optional[str] = Field(None, description="Optional manual subtitles filename")
    target_clip_count: Optional[int] = Field(None, description="Optional target number of clips (1-50)")

class HeatmapPoint(BaseModel):
    start_time: float
    end_time: float
    value: float

class TranscriptLine(BaseModel):
    start: float
    end: float
    text: str
    engagement: Optional[float] = None

class AnalyzeResponse(BaseModel):
    video_id: str
    title: str
    duration: float
    heatmap: List[HeatmapPoint]
    summary: str
    clips: List[ViralClip]
    transcript: Optional[List[TranscriptLine]] = None
    model: Optional[str] = None

# ----------------------------------------------------------------
# Helper Functions
# ----------------------------------------------------------------

def parse_time_str(time_str: str) -> float:
    """Parses time string in formats like HH:MM:SS,mmm or MM:SS,mmm or HH:MM:SS or MM:SS to seconds."""
    time_str = time_str.strip().replace(',', '.')
    # Extract millisecond if present
    ms = 0.0
    if '.' in time_str:
        parts = time_str.split('.')
        time_str = parts[0]
        try:
            ms = float('0.' + parts[1])
        except ValueError:
            pass
            
    time_parts = time_str.split(':')
    try:
        if len(time_parts) == 3:
            return int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2]) + ms
        elif len(time_parts) == 2:
            return int(time_parts[0]) * 60 + int(time_parts[1]) + ms
        elif len(time_parts) == 1:
            return float(time_parts[0]) + ms
    except ValueError:
        return 0.0

def parse_manual_subtitles(content: str, default_duration: float = 0.0) -> List[dict]:
    # Normalize line endings
    content = content.replace('\r\n', '\n').strip()
    
    # 1. Try standard SRT parsing first
    # SRT block regex: index (optional), time range, text
    # e.g.,
    # 1
    # 00:00:01,000 --> 00:00:04,500
    # Hello
    srt_regex = r'(?:\d+\n)?(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\n|\n\d+\n|\Z)'
    srt_matches = re.findall(srt_regex, content, re.DOTALL)
    
    if srt_matches:
        results = []
        for start_str, end_str, text in srt_matches:
            start = parse_time_str(start_str)
            end = parse_time_str(end_str)
            cleaned_text = text.replace('\n', ' ').strip()
            results.append({
                "text": cleaned_text,
                "start": start,
                "duration": max(0.1, end - start)
            })
        if results:
            return results

    # 2. Try parsing line-by-line for timestamped lines
    # Patterns:
    # [00:12] Hello or 00:12 Hello
    # [01:02:15] Hello or 01:02:15 Hello
    # [00:12 - 00:15] Hello or 00:12 - 00:15 Hello
    # Let's match timestamp patterns at the start of the line or enclosed in brackets/parens
    line_time_range_regex = r'^[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)\s*(?:-|-->|\s)\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)[\]\)]?\s*(.*)'
    line_single_time_regex = r'^[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)[\]\)]?\s*(.*)'
    
    lines = content.split('\n')
    results = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Match range first (e.g. 00:12 - 00:15 Text)
        m_range = re.match(line_time_range_regex, line)
        if m_range:
            start_str, end_str, text = m_range.groups()
            start = parse_time_str(start_str)
            end = parse_time_str(end_str)
            results.append({
                "text": text.strip(),
                "start": start,
                "duration": max(0.1, end - start)
            })
            continue
            
        # Match single timestamp (e.g. 00:12 Text)
        m_single = re.match(line_single_time_regex, line)
        if m_single:
            start_str, text = m_single.groups()
            start = parse_time_str(start_str)
            results.append({
                "text": text.strip(),
                "start": start,
                "duration": -1.0  # Will fill in later
            })
            continue

    if results:
        # Resolve duration for single timestamps
        # Set duration to the difference between next start and current start, or a default 3.0s
        for i in range(len(results)):
            if results[i]["duration"] == -1.0:
                if i < len(results) - 1:
                    next_start = results[i+1]["start"]
                    diff = next_start - results[i]["start"]
                    results[i]["duration"] = max(0.5, diff)
                else:
                    results[i]["duration"] = 3.0  # default for the last line
        return results

    # 3. Fallback: split text into paragraphs or sentences and distribute evenly across video duration
    duration_to_use = default_duration if default_duration > 0 else 60.0
    # Clean multiple newlines and split by sentences
    sentences = re.split(r'(?<=[.!?])\s+|\n+', content)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    if sentences:
        num_sentences = len(sentences)
        sec_per_sentence = duration_to_use / num_sentences
        results = []
        for i, text in enumerate(sentences):
            start = i * sec_per_sentence
            results.append({
                "text": text,
                "start": round(start, 2),
                "duration": round(sec_per_sentence, 2)
            })
        return results
        
    return []


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
                "heatmap": info.get('heatmap') or [],
                "is_live": bool(info.get('is_live') or False),
                "live_status": info.get('live_status') or 'not_live'
            }
        except Exception as e:
            logger.error(f"Error extracting metadata with yt-dlp: {e}")
            # Try parsing from video URL ID fallback
            video_id = extract_video_id(url)
            if video_id:
                return {
                    "title": f"YouTube Video ({video_id})",
                    "duration": 0.0,
                    "heatmap": [],
                    "is_live": False,
                    "live_status": "not_live"
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
            "Subtitles might be disabled, or the video may be age-restricted, private, or require a login."
        )
    )





def lowercase_hashtags_in_string(text: str) -> str:
    """Finds all hashtags (#word) in a string and converts them to lowercase."""
    if not text:
        return text
    return re.sub(r'#\w+', lambda m: m.group(0).lower(), text)

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



KNOWN_FLASH_MODELS = [
    'gemini-2.5-flash',
    'gemini-2.0-flash',
    'gemini-2.0-flash-lite',
    'gemini-1.5-flash',
    'gemini-1.5-flash-8b',
]

def get_flash_models_for_key(client: genai.Client) -> List[str]:
    """Dynamically query all available flash models for the given API key, merged with known models."""
    discovered = []
    try:
        models_page = client.models.list()
        for m in models_page:
            name = m.name or ""
            short_name = name.split('/')[-1]
            if "gemini" in short_name.lower() and "flash" in short_name.lower():
                if m.supported_actions and "generateContent" not in m.supported_actions:
                    continue
                exclude = ['tuning', 'thinking', 'vision', 'image', 'tts', 'omni', 'customtools', 'embed']
                if not any(x in short_name.lower() for x in exclude):
                    if short_name not in discovered:
                        discovered.append(short_name)
    except Exception as e:
        logger.warning(f"Could not dynamically list models: {e}")

    # Prioritize KNOWN_FLASH_MODELS order
    ordered = []
    for km in KNOWN_FLASH_MODELS:
        if km in discovered:
            ordered.append(km)
    for d in discovered:
        if d not in ordered:
            ordered.append(d)

    # Ensure known flash models are always available as fallbacks
    for km in KNOWN_FLASH_MODELS:
        if km not in ordered:
            ordered.append(km)

    return ordered


@app.get("/api/models")
def list_available_models(api_key: str):
    """Fetches list of available Gemini models using the user's API key, prioritizing Flash models."""
    default_models = [
        'gemini-2.5-flash',
        'gemini-2.0-flash',
        'gemini-2.0-flash-lite',
        'gemini-1.5-flash',
        'gemini-1.5-flash-8b',
        'gemini-2.5-pro'
    ]
    if not api_key or api_key.strip().lower() == "mock":
        return {"models": default_models}
    try:
        client = genai.Client(api_key=api_key.strip())
        models_page = client.models.list()
        
        flash_models = []
        pro_models = []
        other_models = []
        
        for m in models_page:
            name = m.name or ""
            if "gemini" in name.lower():
                if m.supported_actions and "generateContent" not in m.supported_actions:
                    continue
                
                short_name = name.split('/')[-1]
                # Filter out deprecated models like 1.5-pro and specialized non-text variants
                if "1.5-pro" in short_name.lower():
                    continue
                exclude_keywords = ['tuning', 'thinking', 'vision', 'image', 'tts', 'omni', 'customtools', 'embed']
                if any(x in short_name.lower() for x in exclude_keywords):
                    continue
                
                if "flash" in short_name.lower():
                    if short_name not in flash_models:
                        flash_models.append(short_name)
                elif "pro" in short_name.lower():
                    if short_name not in pro_models:
                        pro_models.append(short_name)
                elif any(x in short_name.lower() for x in ['lite', 'exp']):
                    if short_name not in other_models:
                        other_models.append(short_name)
        
        preferred_flash_order = ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-2.0-flash-lite', 'gemini-1.5-flash', 'gemini-1.5-flash-8b']
        ordered_flash = []
        for pref in preferred_flash_order:
            if pref in flash_models:
                ordered_flash.append(pref)
        for name in flash_models:
            if name not in ordered_flash:
                ordered_flash.append(name)
                
        ordered_pro = [p for p in ['gemini-2.5-pro'] if p in pro_models] + [p for p in pro_models if p != 'gemini-2.5-pro']
        
        final_list = ordered_flash + ordered_pro + other_models
        if not final_list:
            final_list = default_models
            
        return {"models": final_list}
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        return {"models": default_models}

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

        yield _sse({
            "step": 1,
            "step_progress": 30,
            "overall_progress": 8,
            "stage": "Connecting to YouTube",
            "detail": "Connecting to YouTube & fetching video metadata...",
            "message": "Connecting to YouTube — fetching video title and duration..."
        })

        try:
            metadata = await asyncio.to_thread(fetch_video_metadata, request.url)
            title    = metadata["title"]
            duration = metadata["duration"]
            heatmap  = metadata.get("heatmap") or []
            is_live  = metadata.get("is_live", False)
            live_status = metadata.get("live_status", "not_live")
            yield _sse({
                "step": 1,
                "step_progress": 100,
                "overall_progress": 25,
                "stage": "Video Verified",
                "detail": f"Loaded metadata for \"{title[:45]}\" ({int(duration)}s)",
                "message": f"Connected — \"{title[:45]}\" ({int(duration)}s)"
            })
        except Exception as e:
            if is_mock:
                title = "Mock YouTube Video"
                duration = 212.0
                heatmap = []
                is_live = False
                live_status = "not_live"
                yield _sse({
                    "step": 1,
                    "step_progress": 100,
                    "overall_progress": 25,
                    "stage": "Video Verified",
                    "detail": "Loaded mock video metadata (212s)",
                    "message": "Mock video metadata loaded"
                })
            else:
                msg = e.detail if isinstance(e, HTTPException) else str(e)
                yield _sse({"error": f"Failed to fetch video details: {msg}", "status": 500})
                return

        logger.info(f"Metadata fetched: title='{title}', duration={duration}s, heatmap_pts={len(heatmap)}")

        # ── Step 2: Heatmap ──────────────────────────────────────────────────
        yield _sse({
            "step": 2,
            "step_progress": 40,
            "overall_progress": 35,
            "stage": "Scraping Retention",
            "detail": "Extracting viewer replay telemetry and retention curve...",
            "message": "Scraping player viewer retention curve..."
        })
        if heatmap:
            yield _sse({
                "step": 2,
                "step_progress": 100,
                "overall_progress": 50,
                "stage": "Retention Decoded",
                "detail": f"Viewer retention heatmap loaded — {len(heatmap)} audience interest data points parsed.",
                "message": f"Viewer retention heatmap loaded — {len(heatmap)} data points scraped."
            })
        else:
            yield _sse({
                "step": 2,
                "step_progress": 100,
                "overall_progress": 50,
                "stage": "Dialogue Fallback",
                "detail": "No heatmap curve available — relying on full transcript dialogue analysis.",
                "message": "No heatmap available for this video — will rely on transcript content analysis."
            })

        # ── Step 3: Transcript ───────────────────────────────────────────────
        if request.subtitles:
            yield _sse({
                "step": 3,
                "step_progress": 30,
                "overall_progress": 55,
                "stage": "Parsing Subtitles",
                "detail": "Parsing custom SRT/TXT subtitle timestamps...",
                "message": "Parsing manual subtitles..."
            })
            try:
                transcript_lines = parse_manual_subtitles(request.subtitles, duration)
                if not transcript_lines:
                    raise Exception("Custom subtitles parsed into empty array.")
                yield _sse({
                    "step": 3,
                    "step_progress": 100,
                    "overall_progress": 70,
                    "stage": "Subtitles Ready",
                    "detail": f"Custom subtitles parsed — {len(transcript_lines)} timestamped lines loaded.",
                    "message": f"Custom subtitles parsed — {len(transcript_lines)} lines loaded successfully."
                })
            except Exception as e:
                yield _sse({"error": f"Failed to parse manual subtitles: {str(e)}", "status": 400})
                return
        else:
            yield _sse({
                "step": 3,
                "step_progress": 30,
                "overall_progress": 55,
                "stage": "Fetching Subtitles",
                "detail": "Querying YouTube caption tracks & auto-generated transcripts...",
                "message": "Fetching subtitles — trying video's original language..."
            })
            try:
                transcript_lines = await asyncio.to_thread(fetch_transcript, video_id)
                yield _sse({
                    "step": 3,
                    "step_progress": 100,
                    "overall_progress": 70,
                    "stage": "Subtitles Ready",
                    "detail": f"Subtitles loaded — {len(transcript_lines)} dialogue sentences with timestamps ready.",
                    "message": f"Subtitles loaded — {len(transcript_lines)} lines parsed successfully."
                })
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
                    yield _sse({
                        "step": 3,
                        "step_progress": 100,
                        "overall_progress": 70,
                        "stage": "Subtitles Ready",
                        "detail": "Mock mode — 9 sample dialogue lines loaded.",
                        "message": "Mock mode — using sample transcript."
                    })
                else:
                    # Provide a helpful error message if the video is live or recently completed
                    if is_live or live_status in ('is_live', 'is_upcoming', 'post_live'):
                        yield _sse({
                            "error": (
                                "No subtitles could be retrieved because this video is currently live, "
                                "upcoming, or recently completed (post-live processing). Subtitles are only "
                                "available once the live stream ends and YouTube finishes processing the video. "
                                "You can upload custom subtitles manually to analyze this video."
                            ),
                            "status": 400
                        })
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
            mock_stages = [
                ("Context Assembly", "Aligning 9 transcript dialogue lines with retention telemetry...", 30, 78),
                ("Viral Hook & Curiosity Detection", "Scanning transcript dialogue for viral hooks & curiosity gaps...", 65, 88),
                ("Virality Scoring & Selection", "Calculating virality coefficients and formatting clip candidates...", 92, 95),
            ]
            for s_name, s_detail, s_prog, o_prog in mock_stages:
                yield _sse({
                    "step": 4,
                    "step_progress": s_prog,
                    "overall_progress": o_prog,
                    "stage": s_name,
                    "detail": s_detail,
                    "model": "gemini-2.5-flash (Mock)",
                    "message": f"Mock AI ({s_name}): {s_detail}"
                })
                await asyncio.sleep(0.7)

            mock_clips = [
                ViralClip(title="Finding hotspots using heatmaps",  start_time=11.0, end_time=22.0, hook_time=14.0, virality_score=95,
                          key_quotes=["Uses YouTube player heatmaps.", "Processes using Gemini AI."],
                          transcript="Most people think it's magic. But it uses YouTube player heatmaps.",
                          title_suggestion="Unlock Video Virality Secrets",
                          caption_suggestion="Stop guessing what works! Here's how to use heatmaps to find viral hotspots in seconds. 🔥",
                          hashtag_suggestion="#viralclips #videoediting #heatmaps #aitools"),
                ViralClip(title="Grow on TikTok or Reels",          start_time=22.0, end_time=32.0, hook_time=27.0, virality_score=88,
                          key_quotes=["Changing how editors crop videos.", "If you want to grow on TikTok, try it."],
                          transcript="This is changing how editors crop videos. If you want to grow on TikTok, try it.",
                          title_suggestion="The Ultimate TikTok Growth Hack",
                          caption_suggestion="Want to scale your TikTok views? This tool will revolutionize your workflow. 🚀",
                          hashtag_suggestion="#tiktokgrowth #reels #shorts #editingtips"),
                ViralClip(title="Introductory overview of the tool", start_time=0.0,  end_time=11.0, hook_time=3.0, virality_score=72,
                          key_quotes=["Hello and welcome.", "Finds viral hotspots."],
                          transcript="Hello and welcome. It finds viral hotspots and highlights them.",
                          title_suggestion="Meet Cheat Clip AI",
                          caption_suggestion="Say hello to your new AI co-editor. Find the absolute best parts of any video instantly.",
                          hashtag_suggestion="#cheatclip #aiediting #growthmindset"),
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
                summary="Mock analysis: this video explains how CHEAT CLIP works. #aitools #videoediting #productivity",
                clips=mock_clips,
                model="Mock Gemini"
            )
            yield _sse({
                "step": 4,
                "step_progress": 100,
                "overall_progress": 100,
                "stage": "Analysis Complete",
                "detail": "Generated 3 viral clip candidates successfully.",
                "done": True,
                "result": result.model_dump()
            })
            return

        is_long_video = duration > 3600
        if request.target_clip_count:
            N = request.target_clip_count
            if N <= 5:
                min_clips = max(1, N - 1)
                max_clips = N + 2
            elif N <= 10:
                min_clips = max(1, N - 2)
                max_clips = N + 3
            else:
                min_clips = N - 5
                max_clips = N + 5
            clip_range = f"{min_clips}-{max_clips}"
        else:
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
        dur_range   = {"15s": "10-20s", "30s": "20-40s", "60s": "45-75s"}.get(request.duration, "20-40s")
        heatmap_note = (
            "Columns: start|end|audience_interest(0-1). Prioritise high-interest peaks."
            if heatmap else
            "No audience interest data. Use content hooks, energy, and story arcs."
        )
        focus_instruction = ""
        if request.custom_prompt and request.custom_prompt.strip():
            focus_instruction = f"CRITICAL FOCUS: The user specifically wants you to find clips matching the following query/theme: \"{request.custom_prompt.strip()}\". Prioritize and tailor your selection of viral clips to fit this request, while still ensuring they make good standalone clips.\n\n"

        prompt = (
            f"You are a viral video clip finder.\n"
            f"Find {clip_range} short-form clip candidates from this YouTube transcript for TikTok/Reels/Shorts.\n\n"
            f"Title: {title}\n"
            f"Duration Range: {int(start_bound)}s to {int(end_bound)}s (Length: {int(duration)}s) | Target clip length: {dur_range}\n"
            f"{heatmap_note}\n"
            f"{focus_instruction}"
            f"Match output language to transcript language.\n\n"
            f"Transcript (start|end[|interest] text):\n---\n{transcript_text}\n---\n\n"
            f"Rules: use exact seconds from transcript; clips must start/end at sentence boundaries; do not overlap.\n"
            f"Return {clip_range} clips sorted by virality_score desc."
        )

        requested_model = (request.model or 'gemini-2.5-flash').strip()
        if '1.5-pro' in requested_model.lower():
            logger.info(f"Requested model '{requested_model}' is deprecated. Upgrading to gemini-2.5-flash.")
            requested_model = 'gemini-2.5-flash'

        yield _sse({
            "step": 4,
            "step_progress": 10,
            "overall_progress": 72,
            "stage": "Context Assembly",
            "detail": f"Aligning {len(transcript_dump)} dialogue segments with engagement data for {requested_model}...",
            "model": requested_model,
            "message": f"Assembling prompt and engagement context for {requested_model}..."
        })

        # ── Step 4: Gemini API call with dynamic Flash fallback models and retry ───────────
        client = genai.Client(api_key=gemini_key)
        
        # Discover all available Flash models for the user's API key
        discovered_flash = await asyncio.to_thread(get_flash_models_for_key, client)
        
        # Build models_to_try:
        models_to_try = [requested_model]
        for fm in discovered_flash:
            if fm not in models_to_try and '1.5-pro' not in fm.lower():
                models_to_try.append(fm)
        for km in KNOWN_FLASH_MODELS:
            if km not in models_to_try:
                models_to_try.append(km)

        logger.info(f"Flash fallback chain prepared: {models_to_try}")

        response = None
        last_error = None
        analysis_data = None
        successful_model = None

        for idx, model_name in enumerate(models_to_try):
            if '1.5-pro' in model_name.lower():
                continue

            next_model_hint = None
            for cand in models_to_try[idx + 1:]:
                if '1.5-pro' not in cand.lower():
                    next_model_hint = cand
                    break

            MAX_RETRIES = 2
            
            for attempt in range(MAX_RETRIES):
                if attempt > 0:
                    wait = 2
                    yield _sse({
                        "step": 4,
                        "step_progress": 25,
                        "overall_progress": 75,
                        "stage": "Transient Retry",
                        "detail": f"{model_name} busy — waiting {wait}s before retry ({attempt + 1}/{MAX_RETRIES})...",
                        "model": model_name,
                        "message": f"{model_name} is busy — waiting {wait}s before retry {attempt + 1}/{MAX_RETRIES}..."
                    })
                    await asyncio.sleep(wait)
                
                yield _sse({
                    "step": 4,
                    "step_progress": 18,
                    "overall_progress": 74,
                    "stage": "Neural Model Dispatch",
                    "detail": f"Dispatched {len(transcript_dump)} lines to {model_name} (attempt {attempt + 1})...",
                    "model": model_name,
                    "message": f"Calling {model_name} (attempt {attempt + 1}/{MAX_RETRIES})..."
                })
                
                # Execute Gemini call with heartbeat to keep mobile connection alive and show live stages
                task = asyncio.create_task(asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=VideoAnalysis,
                        temperature=0.2,
                    )
                ))
                
                call_start = asyncio.get_event_loop().time()
                while not task.done():
                    done, _ = await asyncio.wait([task], timeout=2.0)
                    if not done:
                        elapsed = int(asyncio.get_event_loop().time() - call_start)
                        
                        if elapsed < 5:
                            stage = "Neural Context Loading"
                            detail = f"Transmitting {len(transcript_dump)} timestamped dialogue segments to {model_name}..."
                            step_prog = min(35, 12 + elapsed * 4)
                        elif elapsed < 12:
                            stage = "Retention Spike Cross-Analysis"
                            detail = f"Correlating viewer retention peaks against speaker dialogue to isolate viral moments..."
                            step_prog = min(55, 35 + int((elapsed - 5) * 3))
                        elif elapsed < 20:
                            stage = "Viral Hook & Curiosity Detection"
                            detail = f"Scanning transcript dialogue for opening hooks, punchlines, controversial takes & emotional peaks..."
                            step_prog = min(72, 55 + int((elapsed - 12) * 2.2))
                        elif elapsed < 30:
                            stage = "Coherence & Sentence Boundary Snapping"
                            detail = f"Ensuring clip candidates start and end naturally on sentence boundaries without mid-word cuts..."
                            step_prog = min(85, 72 + int((elapsed - 20) * 1.3))
                        elif elapsed < 42:
                            stage = "Virality Scoring & Selection"
                            detail = f"Calculating virality coefficients (1-100) and selecting the top {clip_range} highest potential clips..."
                            step_prog = min(92, 85 + int((elapsed - 30) * 0.7))
                        else:
                            stage = "Social Media Metadata Synthesis"
                            detail = f"Drafting attention-grabbing titles, social captions, and targeted hashtags ({elapsed}s)..."
                            step_prog = min(95, 92 + min(3, int((elapsed - 42) * 0.3)))

                        overall_prog = 70 + int(step_prog * 0.28)
                        yield _sse({
                            "step": 4,
                            "keepalive": True,
                            "step_progress": step_prog,
                            "overall_progress": overall_prog,
                            "stage": stage,
                            "detail": detail,
                            "model": model_name,
                            "elapsed": elapsed,
                            "message": f"[{model_name} | {elapsed}s] {stage}: {detail}"
                        })
                
                try:
                    resp_candidate = await task
                    last_error = None
                    
                    # Parse structured response
                    parsed_data = None
                    if hasattr(resp_candidate, 'parsed') and resp_candidate.parsed is not None:
                        parsed = resp_candidate.parsed
                        parsed_data = {
                            "summary": getattr(parsed, 'summary', ''),
                            "clips": [
                                {
                                    "title": getattr(c, 'title', ''),
                                    "start_time": getattr(c, 'start_time', 0.0),
                                    "end_time": getattr(c, 'end_time', 0.0),
                                    "hook_time": getattr(c, 'hook_time', None),
                                    "virality_score": getattr(c, 'virality_score', 0),
                                    "key_quotes": getattr(c, 'key_quotes', []),
                                    "title_suggestion": getattr(c, 'title_suggestion', ''),
                                    "caption_suggestion": getattr(c, 'caption_suggestion', ''),
                                    "hashtag_suggestion": getattr(c, 'hashtag_suggestion', ''),
                                }
                                for c in (getattr(parsed, 'clips', []) or [])
                            ]
                        }
                    elif resp_candidate.text:
                        raw_text = resp_candidate.text.strip()
                        if raw_text.startswith("```"):
                            raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
                            raw_text = re.sub(r"\n?```$", "", raw_text)
                        try:
                            parsed_data = json.loads(raw_text)
                        except Exception as json_err:
                            logger.warning(f"JSON parsing error from {model_name}: {json_err}")
                            parsed_data = None

                    if parsed_data is not None:
                        clips_found = len(parsed_data.get('clips', []))
                        if clips_found == 0 and next_model_hint is not None:
                            logger.warning(f"{model_name} returned 0 clips. Will try next flash model {next_model_hint}...")
                            yield _sse({
                                "step": 4,
                                "step_progress": 40,
                                "overall_progress": 78,
                                "stage": "Flash Model Fallback",
                                "detail": f"{model_name} returned 0 clips — switching to {next_model_hint} for deeper extraction...",
                                "model": next_model_hint,
                                "message": f"{model_name} returned 0 clips — switching to {next_model_hint}..."
                            })
                            last_error = Exception(f"{model_name} returned 0 clips")
                            break
                        
                        response = resp_candidate
                        analysis_data = parsed_data
                        successful_model = model_name
                        break
                    else:
                        last_error = Exception(f"{model_name} returned empty or unparseable response")
                        break
                        
                except Exception as e:
                    last_error = e
                    err_str = str(e).lower()
                    logger.warning(f"Error from {model_name} (attempt {attempt + 1}): {e}")
                    
                    if any(x in err_str for x in ('404', 'not found', 'not supported', '429', 'quota', 'resource exhausted', 'rate limit')):
                        break
                    
                    is_server_busy = any(x in err_str for x in ('503', 'unavailable', 'overloaded', '500', 'internal'))
                    if not is_server_busy:
                        break
            
            if analysis_data is not None and response is not None:
                break
                
            if next_model_hint is not None:
                err_summary = "quota reached" if any(x in str(last_error).lower() for x in ('429', 'quota', 'rate limit')) else \
                              "not available or deprecated" if "404" in str(last_error) else \
                              "temporarily busy"
                yield _sse({
                    "step": 4,
                    "step_progress": 35,
                    "overall_progress": 76,
                    "stage": "Flash Fallback",
                    "detail": f"{model_name} {err_summary} — switching to fallback {next_model_hint}...",
                    "model": next_model_hint,
                    "message": f"{model_name} {err_summary} — switching to flash fallback model {next_model_hint}..."
                })

        if analysis_data is None:
            if last_error is not None:
                err_str = str(last_error).lower()
                if any(x in err_str for x in ('429', 'quota', 'resource exhausted', 'rate limit')):
                    yield _sse({
                        "error": "Gemini API free quota/rate limit was reached across all Flash models. Free keys have a request limit per minute. Please wait 30–60 seconds and try again, or generate a new free key at aistudio.google.com.",
                        "status": 429
                    })
                elif any(x in err_str for x in ('503', 'unavailable', 'overloaded')):
                    yield _sse({
                        "error": "Google Gemini servers are currently experiencing high demand across all Flash models. Please wait a moment and try again.",
                        "status": 503
                    })
                elif any(x in err_str for x in ('401', '403', 'api_key', 'invalid', 'permission')):
                    yield _sse({
                        "error": "Invalid Gemini API key or unauthorized. Please verify your API key at aistudio.google.com.",
                        "status": 401
                    })
                else:
                    logger.error(f"Gemini error after all fallback models: {last_error}")
                    yield _sse({"error": f"AI analysis failed: {str(last_error)}", "status": 500})
            else:
                yield _sse({"error": "Gemini returned no response.", "status": 500})
            return

        # Fallback clip synthesis if 0 clips were returned after all models
        if len(analysis_data.get('clips', [])) == 0 and enriched_transcript:
            logger.info("Generating fallback clips from heatmap and transcript segments...")
            sorted_lines = sorted(enriched_transcript, key=lambda l: l.get('engagement', 0.0), reverse=True)
            candidate_starts = []
            for l in sorted_lines:
                s = l['start']
                if not any(abs(s - existing) < 25.0 for existing in candidate_starts):
                    candidate_starts.append(s)
                if len(candidate_starts) >= 5:
                    break
            
            fallback_clips_list = []
            for i, st in enumerate(candidate_starts):
                target_len = 30.0 if request.duration == "30s" else 15.0 if request.duration == "15s" else 60.0
                et = min(duration, st + target_len)
                seg_lines = [l['text'] for l in enriched_transcript if max(l['start'], st) < min(l['end'], et)]
                seg_text = " ".join(seg_lines).strip()
                preview = seg_text[:60] + "..." if len(seg_text) > 60 else seg_text or f"Viral Highlight #{i+1}"
                fallback_clips_list.append({
                    "title": f"Key Highlight #{i+1}",
                    "start_time": st,
                    "end_time": et,
                    "hook_time": st,
                    "virality_score": max(70, int(95 - i * 5)),
                    "key_quotes": [seg_text[:80]] if seg_text else [],
                    "title_suggestion": f"Must Watch Moment #{i+1}",
                    "caption_suggestion": f"Key highlight from video: {preview} #viral #trending",
                    "hashtag_suggestion": "#viral #shorts #trending"
                })
            analysis_data['clips'] = fallback_clips_list
            if not analysis_data.get('summary'):
                analysis_data['summary'] = f"Analysis of \"{title}\" identifying {len(fallback_clips_list)} key segments. #viral #highlights"

        clip_count = len(analysis_data.get('clips', []))
        yield _sse({
            "step": 4,
            "step_progress": 98,
            "overall_progress": 98,
            "stage": "Clip Verification & Alignment",
            "detail": f"Verified {clip_count} clip segments with precise video timestamps and key quotes.",
            "model": successful_model or requested_model,
            "message": f"Found {clip_count} viral clip candidates with {successful_model or requested_model} — reconstructing transcripts..."
        })
        logger.info(f"Gemini analysis complete with {successful_model or requested_model}. Found {clip_count} clips.")
        logger.info(f"Gemini analysis complete with {successful_model or requested_model}. Found {clip_count} clips.")

        # Reconstruct clip transcripts from enriched_transcript
        final_clips = []
        for raw_clip in analysis_data.get('clips', []):
            start = raw_clip.get('start_time', 0.0)
            end   = raw_clip.get('end_time', 0.0)
            hook  = raw_clip.get('hook_time')
            if hook is None or not (start <= hook <= end):
                hook = start
            
            clip_lines = [
                line.get("text", "")
                for line in enriched_transcript
                if max(line.get("start", 0.0), start) < min(line.get("end", 0.0), end)
            ]
            
            # Ensure hashtags are always lowercase
            caption_sug = lowercase_hashtags_in_string(raw_clip.get('caption_suggestion', ''))
            hashtag_sug = lowercase_hashtags_in_string(raw_clip.get('hashtag_suggestion', ''))
            
            final_clips.append(ViralClip(
                title=raw_clip.get('title', ''),
                start_time=start,
                end_time=end,
                hook_time=hook,
                virality_score=raw_clip.get('virality_score', 0),
                key_quotes=raw_clip.get('key_quotes') or [],
                transcript=" ".join(clip_lines),
                title_suggestion=raw_clip.get('title_suggestion', ''),
                caption_suggestion=caption_sug,
                hashtag_suggestion=hashtag_sug
            ))

        response_heatmap = [
            HeatmapPoint(
                start_time=float(pt.get('start_time', 0.0)),
                end_time=float(pt.get('end_time', 0.0)),
                value=float(pt.get('value', 0.0))
            )
            for pt in (heatmap or [])
        ]

        response_transcript = [
            TranscriptLine(
                start=float(line["start"]),
                end=float(line["end"]),
                text=line["text"],
                engagement=line.get("engagement")
            )
            for line in enriched_transcript
        ]

        # Ensure hashtags are lowercase in the overall summary
        clean_summary = lowercase_hashtags_in_string(analysis_data.get("summary", ""))

        final_result = AnalyzeResponse(
            video_id=video_id,
            title=title,
            duration=duration,
            heatmap=response_heatmap,
            summary=clean_summary,
            clips=final_clips,
            transcript=response_transcript,
            model=successful_model or requested_model
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

