import os
import re
import logging
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cheat-clip")

# Load environment variables
load_dotenv()

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
    title: str = Field(description="A highly engaging, clickbaity title for this clip (max 6-8 words)")
    start_time: float = Field(description="Exact start time of the clip in seconds (must align with a sentence beginning in the transcript)")
    end_time: float = Field(description="Exact end time of the clip in seconds (must align with a sentence ending in the transcript)")
    hook_analysis: str = Field(description="Detailed explanation of why this segment is viral and engaging, referencing the conversation flow, hooks, or viewer retention peaks")
    virality_score: int = Field(description="Estimated virality rating from 1 to 100, where 90+ represents extremely viral potential")
    key_quotes: List[str] = Field(description="1-3 key punchy or memorable quotes spoken during this clip")
    transcript: str = Field(description="The exact spoken text within this clip segment")

class ViralClipGemini(BaseModel):
    title: str = Field(description="A highly engaging, clickbaity title for this clip (max 6-8 words)")
    start_time: float = Field(description="Exact start time of the clip in seconds (must align with a sentence beginning in the transcript)")
    end_time: float = Field(description="Exact end time of the clip in seconds (must align with a sentence ending in the transcript)")
    hook_analysis: str = Field(description="Detailed explanation of why this segment is viral and engaging, referencing the conversation flow, hooks, or viewer retention peaks")
    virality_score: int = Field(description="Estimated virality rating from 1 to 100, where 90+ represents extremely viral potential")
    key_quotes: List[str] = Field(description="1-3 key punchy or memorable quotes spoken during this clip")

class VideoAnalysis(BaseModel):
    summary: str = Field(description="A brief overall summary of the video content, main theme, and target audience")
    clips: List[ViralClipGemini] = Field(description="List of 10 to 30 proposed viral clips, sorted by virality_score in descending order")

# ----------------------------------------------------------------
# API Request/Response Schemas
# ----------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    duration: str = Field("30s", description="Target clip duration: '30s', '60s', or '1m+'")
    api_key: Optional[str] = Field(None, description="Optional custom Gemini API key provided by the user")

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
    ydl_opts = {
        'skip_download': True,
        'youtube_include_dash_manifest': False,
        'quiet': True,
        'no_warnings': True
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get('title', 'Unknown YouTube Video'),
                "duration": info.get('duration', 0.0),
                "heatmap": info.get('heatmap', [])
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
    """Retrieves subtitles using youtube-transcript-api in their original language."""
    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)
    except Exception as e:
        logger.error(f"Error listing transcripts: {e}")
        raise HTTPException(
            status_code=400, 
            detail="Could not retrieve transcripts for this video. Subtitles may be disabled or unavailable."
        )

    # Helper to convert dataclass FetchedTranscriptSnippet elements to dicts
    def to_dict_list(fetched_transcript) -> List[dict]:
        return [
            {
                "text": getattr(line, "text", ""),
                "start": getattr(line, "start", 0.0),
                "duration": getattr(line, "duration", 0.0)
            }
            for line in fetched_transcript
        ]

    # Try to get the original/default transcript of the video (no translation needed)
    try:
        transcript = next(iter(transcript_list))
        logger.info(f"Retrieving native transcript in {transcript.language} ({transcript.language_code})...")
        return to_dict_list(transcript.fetch())
    except Exception as e:
        logger.error(f"Failed fetching native transcript: {e}")
        raise HTTPException(
            status_code=400, 
            detail="Subtitles are available but could not be parsed."
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

@app.get("/api/health")
def health_check():
    return {"status": "ok", "message": "CHEAT CLIP API is active"}

@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze_video(request: AnalyzeRequest):
    logger.info(f"Received request for URL: {request.url}, preferred duration: {request.duration}")
    
    # Determine Gemini API Key to use
    env_key = os.getenv("GEMINI_API_KEY")
    logger.info(f"Debug: request.api_key = {repr(request.api_key)}, env GEMINI_API_KEY = {repr(env_key)}")
    gemini_key = request.api_key or env_key
    is_mock = gemini_key is not None and gemini_key.lower().strip() == "mock"
    logger.info(f"Debug: gemini_key = {repr(gemini_key)}, is_mock = {is_mock}")
    
    # 1. Extract video ID
    video_id = extract_video_id(request.url)
    if not video_id:
        if is_mock:
            video_id = "dQw4w9WgXcQ"
        else:
            raise HTTPException(status_code=400, detail="Invalid YouTube URL. Please check the link and try again.")
    
    # 2. Fetch metadata & heatmap
    try:
        metadata = fetch_video_metadata(request.url)
        title = metadata["title"]
        duration = metadata["duration"]
        heatmap = metadata["heatmap"] or []  # normalize None → []
    except Exception as e:
        if is_mock:
            title = "Mock YouTube Video (RickRoll fallback)"
            duration = 212.0
            heatmap = []
        else:
            raise e
    
    # 3. Fetch transcript
    try:
        transcript_lines = fetch_transcript(video_id)
    except Exception as e:
        if is_mock:
            transcript_lines = [
                {"text": "Hello and welcome to this video.", "start": 0.0, "duration": 3.0},
                {"text": "Today we are looking at how this application works.", "start": 3.0, "duration": 4.0},
                {"text": "It finds viral hotspots and highlights them.", "start": 7.0, "duration": 4.0},
                {"text": "Most people think it's magic.", "start": 11.0, "duration": 3.0},
                {"text": "But it actually uses YouTube player heatmaps.", "start": 14.0, "duration": 4.0},
                {"text": "And it processes it using Gemini AI models.", "start": 18.0, "duration": 4.0},
                {"text": "This is completely changing how editors crop videos.", "start": 22.0, "duration": 5.0},
                {"text": "If you want to grow on TikTok or Reels, try it.", "start": 27.0, "duration": 5.0},
                {"text": "We will explore the code next.", "start": 32.0, "duration": 3.0}
            ]
        else:
            raise e
    
    # If video duration is 0, estimate it from the last transcript line
    if duration == 0.0 and transcript_lines:
        last_line = transcript_lines[-1]
        duration = last_line.get("start", 0.0) + last_line.get("duration", 0.0)
        
    # 4. Map transcript lines to heatmap scores
    enriched_transcript = []
    for line in transcript_lines:
        line_start = line.get("start", 0.0)
        line_dur = line.get("duration", 0.0)
        line_end = line_start + line_dur
        line_text = line.get("text", "")
        
        heatmap_score = get_average_heatmap_value(line_start, line_end, heatmap)
        
        enriched_transcript.append({
            "start": round(line_start, 2),
            "end": round(line_end, 2),
            "text": line_text,
            "engagement": round(heatmap_score, 3)
        })

    if is_mock:
        logger.info("Mock mode bypass: generating mock clips data.")
        mock_clips = [
            ViralClip(
                title="Finding hotspots using heatmaps",
                start_time=11.0,
                end_time=22.0,
                hook_analysis="High interest segment explaining the core concept of utilizing player heatmaps for hotspot detection.",
                virality_score=95,
                key_quotes=["But it actually uses YouTube player heatmaps.", "And it processes it using Gemini AI models."],
                transcript="Most people think it's magic. But it actually uses YouTube player heatmaps. And it processes it using Gemini AI models."
            ),
            ViralClip(
                title="Grow on TikTok or Reels",
                start_time=22.0,
                end_time=32.0,
                hook_analysis="Engaging call to action telling the viewer how this tool will help them grow on TikTok or Reels.",
                virality_score=88,
                key_quotes=["This is completely changing how editors crop videos.", "If you want to grow on TikTok or Reels, try it."],
                transcript="This is completely changing how editors crop videos. If you want to grow on TikTok or Reels, try it."
            ),
            ViralClip(
                title="Introductory overview of the tool",
                start_time=0.0,
                end_time=11.0,
                hook_analysis="Clean intro that catches attention by welcoming the audience and setting the premise.",
                virality_score=72,
                key_quotes=["Hello and welcome to this video.", "It finds viral hotspots and highlights them."],
                transcript="Hello and welcome to this video. Today we are looking at how this application works. It finds viral hotspots and highlights them."
            )
        ]
        
        # Make a mock heatmap if none exists
        response_heatmap = []
        if not heatmap:
            for i in range(20):
                response_heatmap.append(HeatmapPoint(
                    start_time=i * 10.0,
                    end_time=(i + 1) * 10.0,
                    value=0.2 + (0.6 if i in [2, 5, 8, 12, 16] else 0.1)
                ))
        else:
            response_heatmap = [
                HeatmapPoint(
                    start_time=float(pt.get('start_time', 0.0)),
                    end_time=float(pt.get('end_time', 0.0)),
                    value=float(pt.get('value', 0.0))
                )
                for pt in heatmap
            ]
            
        return AnalyzeResponse(
            video_id=video_id,
            title=title,
            duration=duration or 200.0,
            heatmap=response_heatmap,
            summary="This is a mock video analysis summarizing the core concepts of this video. Generated using MOCK mode fallback.",
            clips=mock_clips
        )

    if not gemini_key:
        raise HTTPException(
            status_code=400, 
            detail="Gemini API Key is missing. Please configure it in the .env file or input it in the web interface."
        )

    # 5. Formulate prompt for Gemini
    # Build a summarized transcript dump to keep the context window highly efficient
    # We will format it as timestamped lines with engagement scores:
    # [125.50s - 130.20s] (Engagement: 0.85) Hello welcome to the show
    transcript_dump = []
    for line in enriched_transcript:
        engagement_indicator = f" (Audience Interest: {line['engagement']})" if heatmap else ""
        transcript_dump.append(
            f"[{line['start']:.2f}s - {line['end']:.2f}s]{engagement_indicator} {line['text']}"
        )

    # Safeguard: truncate if the transcript is extremely long (> 1500 lines ~ very long videos)
    # to avoid hitting the model's context window limits silently
    MAX_LINES = 1500
    if len(transcript_dump) > MAX_LINES:
        logger.warning(f"Transcript has {len(transcript_dump)} lines — truncating to {MAX_LINES} for Gemini safety.")
        transcript_dump = transcript_dump[:MAX_LINES]

    transcript_text = "\n".join(transcript_dump)
    
    duration_prompt = ""
    if request.duration == "30s":
        duration_prompt = "Each clip MUST be approximately 20 to 40 seconds long."
    elif request.duration == "60s":
        duration_prompt = "Each clip MUST be approximately 45 to 75 seconds long."
    else:  # "1m+"
        duration_prompt = "Each clip MUST be longer than 60 seconds (usually 60 to 180 seconds)."

    heatmap_guidance = ""
    if heatmap:
        heatmap_guidance = (
            "The transcript lines include 'Audience Interest' scores ranging from 0.0 to 1.0 (with 1.0 being the absolute highest spike). "
            "Prioritize segments containing audience interest peaks, as these represent moments where viewers replayed or watched the most. "
            "However, ensure the selected segment has cohesive context, starting with an attention-grabbing hook and ending cleanly."
        )
    else:
        heatmap_guidance = (
            "No audience interest data is available for this video. Analyze the conversation flow, energy, and semantics "
            "to identify high-impact hooks, emotional peaks, major value reveals, jokes, or stories that stand on their own."
        )

    prompt = f"""
You are the ultimate AI Video Clipper and Viral Hacker.
Your task is to analyze the following YouTube video transcript and find 10 to 30 highly engaging segments (timestamps) that are perfect for TikTok, YouTube Shorts, or Instagram Reels.

Video Title: "{title}"
Total Video Duration: {duration} seconds (approx. {int(duration // 60)} minutes)
Clip Length Preference: {request.duration}. {duration_prompt}

{heatmap_guidance}

CRITICAL: You MUST write the clip titles, hook analyses, key quotes, and overall summary in the same language as the provided transcript (e.g. if the transcript is in Indonesian, write all clip details, quotes, and summary in Indonesian). Do not translate the spoken words or key quotes; keep them exactly as they appear in the original text.

For each recommended clip:
1. Identify exact start and end times in seconds (e.g., start_time: 125.5, end_time: 155.0) matching the absolute second numbers (in seconds, ending with 's') in the transcript. Do NOT format them as MM:SS or represent them in minutes (e.g., do not output 6.43 if you mean 385.8 seconds; use the exact decimal seconds values from the transcript, such as 385.8).
2. The clip MUST be self-contained. It should start with a strong hook (a question, a shocking statement, or the beginning of a story) and end cleanly (a punchline, a resolution of a point, or a natural pause). Do not cut in the middle of a word or sentence!
3. Ensure the duration matches the requested length.
4. Estimate a "virality_score" (1-100) based on content structure, hook strength, and retention.
5. Provide a catchy, clickable title for the clip.
6. Provide a detailed analysis explaining why it will perform well (referencing hooks, drama, information value, or heatmap peaks).
7. List 1-3 key quotes spoken.

Here is the timestamped transcript data:
---
{transcript_text}
---

Generate exactly 10 to 30 clips (as many high-quality ones as you can find up to 30) matching the schema, sorted by virality_score in descending order.
"""

    # 6. Call Gemini API
    try:
        # Initialize client with provided key
        client = genai.Client(api_key=gemini_key)
        
        # We will use gemini-2.5-flash which is extremely capable and fast, with structured JSON output support.
        logger.info("Calling Gemini API...")
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VideoAnalysis,
                temperature=0.2,
            ),
        )
        
        # Enforce parsing response text as JSON
        # Gemini structured output may come back as response.parsed (Pydantic object) OR
        # as response.text (raw JSON string). Handle both gracefully.
        import json
        
        analysis_data = None
        
        # Strategy 1: Use response.parsed if the SDK returned a structured object directly
        if hasattr(response, 'parsed') and response.parsed is not None:
            parsed = response.parsed
            analysis_data = {
                "summary": getattr(parsed, 'summary', ''),
                "clips": [
                    {
                        "title": getattr(c, 'title', ''),
                        "start_time": getattr(c, 'start_time', 0.0),
                        "end_time": getattr(c, 'end_time', 0.0),
                        "hook_analysis": getattr(c, 'hook_analysis', ''),
                        "virality_score": getattr(c, 'virality_score', 0),
                        "key_quotes": getattr(c, 'key_quotes', []),
                    }
                    for c in (getattr(parsed, 'clips', []) or [])
                ]
            }
            logger.info("Parsed response via response.parsed (structured output).")
        
        # Strategy 2: Fall back to parsing response.text as JSON
        if analysis_data is None:
            if not response.text:
                # Detect if model was blocked by safety filters
                finish_reason = None
                try:
                    finish_reason = response.candidates[0].finish_reason if response.candidates else None
                except Exception:
                    pass
                if finish_reason and str(finish_reason) in ('SAFETY', 'RECITATION', 'OTHER'):
                    raise HTTPException(
                        status_code=422,
                        detail=f"Gemini blocked this content (finish_reason={finish_reason}). The video may contain restricted topics."
                    )
                raise HTTPException(
                    status_code=500,
                    detail="Gemini returned an empty response. The video transcript may be too long or the model hit a processing limit. Try a shorter video or different duration setting."
                )
            analysis_data = json.loads(response.text)
            logger.info("Parsed response via response.text JSON.")

        logger.info(f"Gemini analysis complete. Found {len(analysis_data.get('clips', []))} clips.")
        
        # Programmatically reconstruct the clip transcripts to avoid output token limits
        final_clips = []
        for raw_clip in analysis_data.get('clips', []):
            start = raw_clip.get('start_time', 0.0)
            end = raw_clip.get('end_time', 0.0)
            
            # Find and concatenate lines overlapping with the clip time range
            clip_lines = []
            for line in enriched_transcript:
                l_start = line.get("start", 0.0)
                l_end = line.get("end", 0.0)
                if max(l_start, start) < min(l_end, end):
                    clip_lines.append(line.get("text", ""))
            
            clip_transcript = " ".join(clip_lines)
            
            final_clips.append(
                ViralClip(
                    title=raw_clip.get('title', ''),
                    start_time=start,
                    end_time=end,
                    hook_analysis=raw_clip.get('hook_analysis', ''),
                    virality_score=raw_clip.get('virality_score', 0),
                    key_quotes=raw_clip.get('key_quotes') or [],
                    transcript=clip_transcript
                )
            )
        
        # Map heat points to response schema
        response_heatmap = [
            HeatmapPoint(
                start_time=float(pt.get('start_time', 0.0)),
                end_time=float(pt.get('end_time', 0.0)),
                value=float(pt.get('value', 0.0))
            )
            for pt in (heatmap or [])
        ]
        
        return AnalyzeResponse(
            video_id=video_id,
            title=title,
            duration=duration,
            heatmap=response_heatmap,
            summary=analysis_data.get("summary", ""),
            clips=final_clips
        )
        
    except Exception as e:
        logger.error(f"Gemini API or Parsing error: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"AI Analysis failed: {str(e)}. Please check your API key and network connection."
        )
