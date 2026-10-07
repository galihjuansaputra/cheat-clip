import asyncio
import json
import logging
import os
import re
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from google import genai
from google.genai import types
from pathlib import Path

from backend.config import (
    TEMP_DIR,
    logger,
)
from backend.schemas.analyze import (
    AnalyzeRequest,
    AnalyzeResponse,
    HeatmapPoint,
    TranscriptLine,
    VideoAnalysis,
    ViralClip,
)
from backend.services.ai_service import (
    KNOWN_FLASH_MODELS,
    get_flash_models_for_key,
    list_available_gemini_models,
)
from backend.services.youtube_service import (
    fetch_transcript,
    fetch_video_metadata,
    get_supadata_keys,
    get_supadata_usage_data,
)
from backend.utils.heatmap import get_average_heatmap_value
from backend.utils.proxy import get_proxy_url
from backend.utils.sse import _sse
from backend.utils.text import (
    detect_transcript_language,
    extract_video_id,
    lowercase_hashtags_in_string,
    parse_manual_subtitles,
    sanitize_first_person_title,
)

router = APIRouter(tags=["Analyze"])


@router.get("/api/health")
def health_check(refresh: bool = False):
    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    keys = get_supadata_keys()
    proxy = get_proxy_url()
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    supadata_info = get_supadata_usage_data(force=refresh) if keys else {
        "total_keys": 0,
        "total_limit": 0,
        "total_used": 0,
        "total_remaining": 0,
        "usage_percent": 0.0,
        "active_keys": 0,
        "exhausted_keys": 0,
        "keys_detail": [],
        "status": "not_configured"
    }
    return {
        "status": "ok",
        "message": "CHEAT CLIP PRO API is active",
        "is_vercel": is_vercel,
        "proxy_configured": bool(proxy),
        "gemini_env_configured": has_gemini,
        "supadata_keys_count": len(keys),
        "supadata": supadata_info
    }


@router.get("/api/supadata-usage")
def supadata_usage_endpoint(refresh: bool = False):
    """Returns real-time usage and remaining credit quota across all configured Supadata API keys."""
    return get_supadata_usage_data(force=refresh)


@router.get("/api/models")
def list_available_models(api_key: str = ""):
    """Fetches list of available Gemini models using the user's API key, prioritizing Flash models (newest first)."""
    models = list_available_gemini_models(api_key)
    return {"models": models}


@router.post("/api/analyze")
async def analyze_video(request: AnalyzeRequest):
    """Stream real-time progress via Server-Sent Events, then deliver the final result."""

    async def stream():
        gemini_key = (request.api_key or os.environ.get("GEMINI_API_KEY") or '').strip()
        is_mock = gemini_key.lower() == "mock"

        if not gemini_key:
            yield _sse({"error": "Gemini API Key is required. Enter it in the web interface.", "status": 400})
            return

        # ── Step 1: Detect Source Type & Extract Metadata ───────────────────
        source_type = "youtube"
        video_url = None
        video_id = extract_video_id(request.url)
        if not video_id:
            if not is_mock:
                yield _sse({"error": "Invalid YouTube URL. Please check and try again.", "status": 400})
                return
            video_id = "dQw4w9WgXcQ"

        canonical_url = f"https://www.youtube.com/watch?v={video_id}"

        yield _sse({
            "step": 1,
            "step_progress": 30,
            "overall_progress": 8,
            "stage": "Connecting to YouTube",
            "detail": "Connecting to YouTube & fetching video metadata...",
            "message": "Connecting to YouTube — fetching video title and duration..."
        })

        channel = ""
        try:
            metadata = await asyncio.to_thread(fetch_video_metadata, canonical_url, request.proxy)
            title    = metadata["title"]
            channel  = metadata.get("channel", "")
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
                channel = "Cheat Clip Pro"
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
                "stage": "Fetching Transcript",
                "detail": "Extracting YouTube subtitles & timestamps...",
                "message": "Fetching YouTube transcript..."
            })
            try:
                transcript_lines = await asyncio.to_thread(fetch_transcript, video_id, request.proxy)
                yield _sse({
                    "step": 3,
                    "step_progress": 100,
                    "overall_progress": 70,
                    "stage": "Transcript Ready",
                    "detail": f"YouTube transcript loaded — {len(transcript_lines)} dialogue segments parsed.",
                    "message": f"Transcript loaded — {len(transcript_lines)} lines."
                })
            except Exception as e:
                if is_mock:
                    transcript_lines = [
                        {"text": "Hello and welcome to this video.",            "start":  0.0, "duration": 3.0},
                        {"text": "Today we are looking at how this app works.",  "start":  3.0, "duration": 4.0},
                        {"text": "It finds viral hotspots and highlights them.",  "start":  7.0, "duration": 4.0},
                        {"text": "You can paste any YouTube URL to try it.",     "start": 11.0, "duration": 3.0},
                        {"text": "Thanks for watching and see you next time.",   "start": 14.0, "duration": 3.0},
                    ]
                    yield _sse({
                        "step": 3,
                        "step_progress": 100,
                        "overall_progress": 70,
                        "stage": "Transcript Ready",
                        "detail": f"Mock transcript generated — {len(transcript_lines)} dialogue segments.",
                        "message": "Mock transcript loaded."
                    })
                else:
                    if is_live or live_status in ("is_live", "is_upcoming", "was_live", "post_live"):
                        yield _sse({
                            "error": (
                                "This video appears to be a YouTube Live stream. Automatic captions/transcripts will become "
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
                          title_suggestion="Meet Cheat Clip Pro AI",
                          caption_suggestion="Say hello to your new AI co-editor. Find the absolute best parts of any video instantly.",
                          hashtag_suggestion="#cheatclippro #aiediting #growthmindset"),
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
                video_id=video_id, title=title, channel=channel, duration=duration or 200.0,
                heatmap=mock_heatmap,
                summary="Mock analysis: this video explains how CHEAT CLIP PRO works. #aitools #videoediting #productivity",
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
        is_auto_clip_count = False
        target_count_num = None
        if request.target_clip_count is not None:
            if isinstance(request.target_clip_count, str) and request.target_clip_count.lower() == 'auto':
                is_auto_clip_count = True
            else:
                try:
                    target_count_num = int(request.target_clip_count)
                    if target_count_num <= 0:
                        is_auto_clip_count = True
                except (ValueError, TypeError):
                    is_auto_clip_count = True
        else:
            is_auto_clip_count = True

        if not is_auto_clip_count and target_count_num:
            N = target_count_num
            if N <= 5:
                min_clips = max(1, N - 1)
                max_clips = N + 2
            elif N <= 10:
                min_clips = max(1, N - 2)
                max_clips = N + 3
            else:
                min_clips = max(1, N - 5)
                max_clips = N + 5
            clip_range = f"{min_clips}-{max_clips}"
            clip_count_instruction = (
                f"TARGET CLIP COUNT: Approximately {clip_range} clips. "
                f"Identify the highest-quality, most viral segments within this range."
            )
        else:
            clip_range = "up to around 200 (AI-determined based on interesting topics)"
            clip_count_instruction = (
                "DYNAMIC AUTO CLIP COUNT & TOPIC CURATION RULES:\n"
                "- You (the AI editor) decide the total number of clips to extract based on how many genuinely interesting, high-value, and viral topics exist in this video.\n"
                "- Upper Constraint Ceiling: Extract up to around 200 clips maximum (no need to reach exactly 200; extract as many as the video's content genuinely justifies, up to approximately 200 clips).\n"
                "- High-Interest Standalone Topics Required: Every single clip MUST focus on an interesting, distinct, and compelling topic, idea, debate, story, funny moment, or revelation. Do NOT produce repetitive, weak, or trivial filler clips just to inflate the count. Only create clips for moments that would actually captivate an audience."
            )

        # ── Step 4: Build prompt & Detect Language ─────────────────────────────
        detected_lang = detect_transcript_language(enriched_transcript, title)
        lang_code = detected_lang.get('code', 'en')
        lang_name = detected_lang.get('name', 'English')
        logger.info(f"Detected video language: {lang_name} ({lang_code}) - confidence {detected_lang.get('confidence', 0.0)}")

        transcript_dump = []
        for line in enriched_transcript:
            eng = f"|{line['engagement']:.2f}" if heatmap and line['engagement'] > 0 else ""
            transcript_dump.append(f"{line['start']:.1f}|{line['end']:.1f}{eng} {line['text']}")

        MAX_LINES = 8000 if is_long_video else 4000
        if len(transcript_dump) > MAX_LINES:
            logger.warning(f"Transcript {len(transcript_dump)} lines — truncating to {MAX_LINES}.")
            transcript_dump = transcript_dump[:MAX_LINES]

        transcript_text = "\n".join(transcript_dump)

        is_auto_duration = str(request.duration).lower() == "auto"
        if is_auto_duration:
            dur_range = "Auto dynamic length (~15s to ~90s max)"
            duration_instruction = (
                "DYNAMIC AUTO CLIP DURATION GUIDELINES:\n"
                "- Intelligently adjust the duration of each individual clip based on its natural narrative flow and context so that NO CONTEXT, setup, premise, punchline, or explanation is cut off mid-thought.\n"
                "- Generate variant clip durations naturally: ranging flexibly from approximately 15 seconds up to around 90 seconds (1 minute 30 seconds maximum). Do NOT force all clips to be the same length (some can be ~20-30s, some ~40-60s, and deeper discussions or complex stories can be up to around 90s).\n"
                "- Strict maximum constraint: No clip should ever exceed 90 seconds (1 minute 30 seconds). Always ensure clips begin and end cleanly at complete sentence boundaries without mid-word or mid-sentence cuts."
            )
        else:
            dur_target = {"15s": "10-20s", "30s": "20-40s", "60s": "45-75s"}.get(request.duration, "20-40s")
            dur_range = dur_target
            duration_instruction = f"Target clip length: {dur_target}. Keep clips tightly focused around this duration."

        heatmap_note = (
            "Columns: start|end|audience_interest(0-1). Prioritise high-interest peaks."
            if heatmap else
            "No audience interest data. Use content hooks, energy, and story arcs."
        )
        focus_instruction = ""
        if request.custom_prompt and request.custom_prompt.strip():
            focus_instruction = (
                f"CRITICAL USER SEARCH FOCUS:\n"
                f"The user specifically wants you to find clips matching the following query/theme: \"{request.custom_prompt.strip()}\".\n"
                f"Prioritize and tailor your selection of viral clips to fit this request, while still ensuring they make good standalone clips.\n"
                f"IMPORTANT: Regardless of the language this query was typed in, your generated titles, summaries, quotes, and captions MUST REMAIN IN THE VIDEO'S SPOKEN LANGUAGE ({lang_name}). DO NOT translate the video's content to match the language of the search query.\n\n"
            )

        channel_context = f"- Channel / Host / Creator: {channel}\n" if channel else ""

        prompt = (
            f"You are an expert viral video clip editor finding top clip candidates for TikTok, Instagram Reels, and YouTube Shorts.\n"
            f"Analyze this YouTube video transcript and find {clip_range} high-performing, standalone clip candidates.\n\n"
            f"VIDEO CONTEXT:\n"
            f"- Video Title: {title}\n"
            f"{channel_context}"
            f"- Video Duration: {int(start_bound)}s to {int(end_bound)}s (Total: {int(duration)}s) | Target clip length: {dur_range}\n"
            f"- Heatmap: {heatmap_note}\n"
            f"- DETECTED VIDEO SPOKEN LANGUAGE: {lang_name} (Code: '{lang_code}')\n\n"
            f"================================================================================\n"
            f"CRITICAL MANDATORY LANGUAGE RULE (ZERO TRANSLATION):\n"
            f"================================================================================\n"
            f"THE MAIN SPOKEN LANGUAGE OF THIS VIDEO IS: {lang_name.upper()} (Language Code: '{lang_code}').\n"
            f"YOU MUST GENERATE 100% OF ALL CONTENT STRICTLY AND EXCLUSIVELY IN {lang_name.upper()}.\n\n"
            f"1. NEVER TRANSLATE TO ANY OTHER LANGUAGE!\n"
            f"   - If the video is in English -> ALL titles, title suggestions, summaries, captions, hashtags, and quotes MUST BE 100% IN ENGLISH. Do NOT write Indonesian, Spanish, or any other language!\n"
            f"   - If the video is in Indonesian -> ALL titles, title suggestions, summaries, captions, hashtags, and quotes MUST BE 100% IN INDONESIAN. Do NOT write English, Arabic, or any other language!\n"
            f"   - CRITICAL GUARD FOR INDONESIAN VIDEOS: If speakers use occasional Arabic loanwords or Islamic greetings/phrases (e.g., 'Assalamu\'alaikum', 'Bismillah', 'Alhamdulillah', 'Insya Allah', or Arabic quotes), DO NOT TRANSLATE INTO ARABIC! The video language is 100% BAHASA INDONESIA. All titles, suggestions, and captions MUST be written strictly in BAHASA INDONESIA.\n"
            f"   - If the video is in another language -> ALL output MUST strictly match that language.\n"
            f"   - Even if the user's prompt or search query was written in another language, your output MUST REMAIN 100% IN {lang_name.upper()}.\n"
            f"2. Strict field requirements in {lang_name.upper()}:\n"
            f"   - `summary`: In {lang_name.upper()}.\n"
            f"   - `title`: Catchy title in {lang_name.upper()}, max 8 words.\n"
            f"   - `title_suggestion`: Alternative title in {lang_name.upper()}.\n"
            f"   - `caption_suggestion`: Engaging social caption in {lang_name.upper()}.\n"
            f"   - `hashtag_suggestion`: Relevant hashtags in {lang_name.upper()}.\n"
            f"   - `key_quotes`: MUST be verbatim spoken quotes directly from the transcript in {lang_name.upper()}.\n"
            f"================================================================================\n\n"
            f"{duration_instruction}\n\n"
            f"{clip_count_instruction}\n\n"
            f"{focus_instruction}"
            f"CRITICAL TITLE & ATTRIBUTION RULES (NO FIRST-PERSON 'I' OR 'ME'):\n"
            f"1. NEVER write clip titles or title suggestions using first-person pronouns ('I', 'me', 'my', 'mine', 'myself', or equivalents in other languages such as 'saya', 'aku', 'gue')!\n"
            f"2. The user posting or curating these clips is a third-party editor, NOT the person speaking in the video. Titles must NEVER make it look like the clip is expressing the user's personal opinion, story, or reaction (e.g. NEVER write 'Why I think this is bad', 'How I made $100K', 'My biggest mistake', or 'I was shocked' / in Indonesian: NEVER 'Kenapa saya...', 'Cara aku...', 'Opini gue...').\n"
            f"3. ALWAYS attribute statements to the context of the video and the actual person speaking:\n"
            f"   - Use the actual name of the speaker, host, or guest from the video title, channel name ({channel or 'Host'}), or transcript dialogue (e.g. '{channel or 'Speaker'} Explains...', 'Why {channel or 'Host'} Shocked Fans', '[Name] Reveals The Truth' / in Indonesian: '{channel or 'Host'} Menjelaskan...', 'Alasan {channel or 'Host'} Mengungkapkan...').\n"
            f"   - If the speaker's name is not explicitly stated, use their role or descriptive title (e.g. 'Host Reacts...', 'CEO Reveals...', 'Expert Explains...', 'Guest Breaks Down...' / in Indonesian: 'Host Menjelaskan...', 'Pakar Membongkar...').\n"
            f"   - Or use objective, curiosity-driven framing (e.g. 'The Real Truth About...', 'How To Master...', 'Why Most People Fail At...', 'The Harsh Reality of...' / in Indonesian: 'Fakta Sebenarnya Tentang...', 'Cara Menguasai...').\n"
            f"4. Keep titles snappy, viral, engaging, and under 8 words.\n\n"
            f"Transcript (start|end[|interest] text):\n---\n{transcript_text}\n---\n\n"
            f"Rules: use exact seconds from transcript; clips must start/end at sentence boundaries; do not overlap.\n"
            f"Return clips sorted by virality_score desc."
        )

        requested_model = (request.model or 'gemini-2.5-flash').strip()
        if any(dep in requested_model.lower() for dep in ['gemini-1.0', 'gemini-pro-vision']):
            logger.info(f"Requested model '{requested_model}' is outdated. Upgrading to gemini-2.5-flash.")
            requested_model = 'gemini-2.5-flash'

        yield _sse({
            "step": 4,
            "step_progress": 10,
            "overall_progress": 72,
            "stage": "Language & Context Assembly",
            "detail": f"Source language: {lang_name} ({lang_code}). Aligning {len(transcript_dump)} dialogue segments for {requested_model}...",
            "model": requested_model,
            "message": f"Verified language: {lang_name}. Zero-translation rule enforced for {requested_model}."
        })

        # ── Step 4: Gemini API call with dynamic Flash fallback models and retry ───────────
        client = genai.Client(api_key=gemini_key)
        
        # Discover all available Flash models for the user's API key
        discovered_flash = await asyncio.to_thread(get_flash_models_for_key, client)
        
        # Build models_to_try:
        # 1. Start with the requested model
        # 2. Append all discovered and known flash models in version descending order (e.g. 3.7, 3.6, 3.5, 2.5, 2.0, 1.5)
        models_to_try = [requested_model]
        for fm in discovered_flash:
            if fm not in models_to_try:
                models_to_try.append(fm)
        for km in KNOWN_FLASH_MODELS:
            if km not in models_to_try:
                models_to_try.append(km)

        logger.info(f"Flash fallback chain prepared: {models_to_try}")

        response = None
        last_error = None
        encountered_quota_error = None
        analysis_data = None
        successful_model = None

        for idx, model_name in enumerate(models_to_try):
            next_model_hint = models_to_try[idx + 1] if idx + 1 < len(models_to_try) else None
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
                        max_output_tokens=65536 if any(v in model_name for v in ['2.0', '2.5', '3.']) else 8192,
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
                            display_clip_count = "all high-value" if is_auto_clip_count else f"the top {clip_range}"
                            detail = f"Calculating virality coefficients (1-100) and selecting {display_clip_count} highest potential clips..."
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
                                    "title": sanitize_first_person_title(getattr(c, 'title', ''), channel, lang=lang_code),
                                    "start_time": getattr(c, 'start_time', 0.0),
                                    "end_time": getattr(c, 'end_time', 0.0),
                                    "hook_time": getattr(c, 'hook_time', None),
                                    "virality_score": getattr(c, 'virality_score', 0),
                                    "key_quotes": getattr(c, 'key_quotes', []),
                                    "title_suggestion": sanitize_first_person_title(getattr(c, 'title_suggestion', ''), channel, lang=lang_code),
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
                    
                    if any(x in err_str for x in ('429', 'quota', 'resource exhausted', 'rate limit')):
                        encountered_quota_error = e
                        break

                    if any(x in err_str for x in ('404', 'not found', 'not supported')):
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
            # If any model in the fallback chain suffered quota exhaustion, prioritize showing the quota explanation
            error_to_report = encountered_quota_error or last_error
            if error_to_report is not None:
                err_str = str(error_to_report).lower()
                if any(x in err_str for x in ('429', 'quota', 'resource exhausted', 'rate limit')):
                    yield _sse({
                        "error": "Quota limit reached across all available Gemini Flash models for this API key. Free keys have a request limit per minute. Please change your API key, generate a fresh free key at aistudio.google.com, or wait 30–60 seconds before trying again.",
                        "status": 429
                    })
                elif any(x in err_str for x in ('503', 'unavailable', 'overloaded')):
                    yield _sse({
                        "error": "Google Gemini servers are currently experiencing high demand across all Flash models. Please change to a different Gemini API key or wait a few moments and try again.",
                        "status": 503
                    })
                elif any(x in err_str for x in ('401', '403', 'api_key', 'invalid', 'permission')):
                    yield _sse({
                        "error": "Invalid or restricted Gemini API key. Please change your API key or generate a new free key at aistudio.google.com.",
                        "status": 401
                    })
                elif any(x in err_str for x in ('404', 'not found', 'not supported')):
                    models_preview = ', '.join(models_to_try[:3])
                    yield _sse({
                        "error": f"All tested Gemini Flash models ({models_preview}...) were unavailable or not supported for this API key. Please change your Gemini API key or generate a new one at aistudio.google.com.",
                        "status": 404
                    })
                else:
                    logger.error(f"Gemini error after all fallback models: {error_to_report}")
                    yield _sse({
                        "error": f"AI analysis failed across all available Flash models ({str(error_to_report)}). Please change your Gemini API key or try again in a few moments.",
                        "status": 500
                    })
            else:
                yield _sse({
                    "error": "No response received after trying all available Gemini Flash models. Please change your Gemini API key or try again in a few moments.",
                    "status": 500
                })
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
                if request.duration == "15s":
                    target_len = 15.0
                elif request.duration == "60s":
                    target_len = 60.0
                elif request.duration == "auto":
                    auto_lens = [25.0, 45.0, 75.0, 35.0, 60.0, 85.0, 20.0, 50.0]
                    target_len = auto_lens[i % len(auto_lens)]
                else:
                    target_len = 30.0
                et = min(duration, st + min(90.0, target_len))
                seg_lines = [l['text'] for l in enriched_transcript if max(l['start'], st) < min(l['end'], et)]
                seg_text = " ".join(seg_lines).strip()
                preview = seg_text[:60] + "..." if len(seg_text) > 60 else seg_text or f"Viral Highlight #{i+1}"
                if lang_code == 'id':
                    fallback_clips_list.append({
                        "title": f"Momen Menarik #{i+1}",
                        "start_time": st,
                        "end_time": et,
                        "hook_time": st,
                        "virality_score": max(70, int(95 - i * 5)),
                        "key_quotes": [seg_text[:80]] if seg_text else [],
                        "title_suggestion": f"Cuplikan Pilihan #{i+1}",
                        "caption_suggestion": f"Momen terbaik dari video: {preview} #viral #trending",
                        "hashtag_suggestion": "#viral #shorts #trending"
                    })
                elif lang_code == 'es':
                    fallback_clips_list.append({
                        "title": f"Momento Destacado #{i+1}",
                        "start_time": st,
                        "end_time": et,
                        "hook_time": st,
                        "virality_score": max(70, int(95 - i * 5)),
                        "key_quotes": [seg_text[:80]] if seg_text else [],
                        "title_suggestion": f"Momento Imperdible #{i+1}",
                        "caption_suggestion": f"Momento clave del video: {preview} #viral #trending",
                        "hashtag_suggestion": "#viral #shorts #trending"
                    })
                else:
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
                if lang_code == 'id':
                    analysis_data['summary'] = f"Analisis video \"{title}\" menemukan {len(fallback_clips_list)} segmen cuplikan pilihan. #viral #highlights"
                elif lang_code == 'es':
                    analysis_data['summary'] = f"Análisis de \"{title}\" identificando {len(fallback_clips_list)} segmentos clave. #viral #highlights"
                else:
                    analysis_data['summary'] = f"Analysis of \"{title}\" identifying {len(fallback_clips_list)} key segments. #viral #highlights"

        clip_count = len(analysis_data.get('clips', []))
        yield _sse({
            "step": 4,
            "step_progress": 98,
            "overall_progress": 98,
            "stage": "Clip Verification & Alignment",
            "detail": f"Verified {clip_count} clip segments with precise video timestamps and key quotes in {lang_name}.",
            "model": successful_model or requested_model,
            "message": f"Found {clip_count} viral clip candidates ({lang_name}) with {successful_model or requested_model} — reconstructing transcripts..."
        })
        logger.info(f"Gemini analysis complete with {successful_model or requested_model}. Found {clip_count} clips in {lang_name}.")

        # Reconstruct clip transcripts from enriched_transcript
        final_clips = []
        for raw_clip in analysis_data.get('clips', []):
            start = raw_clip.get('start_time', 0.0)
            end   = raw_clip.get('end_time', 0.0)
            hook  = raw_clip.get('hook_time')
            if hook is None or not (start <= hook <= end):
                hook = start
            
            # Enforce max 90 seconds (1m 30s) duration constraint for auto duration mode
            if is_auto_duration and (end - start) > 90.0:
                max_end = start + 90.0
                best_end = max_end
                for l in enriched_transcript:
                    l_end = l.get('end', 0.0)
                    if start + 15.0 <= l_end <= max_end:
                        best_end = l_end
                end = best_end
            
            clip_lines = [
                line.get("text", "")
                for line in enriched_transcript
                if max(line.get("start", 0.0), start) < min(line.get("end", 0.0), end)
            ]
            
            # Ensure hashtags are always lowercase
            caption_sug = lowercase_hashtags_in_string(raw_clip.get('caption_suggestion', ''))
            hashtag_sug = lowercase_hashtags_in_string(raw_clip.get('hashtag_suggestion', ''))
            
            final_clips.append(ViralClip(
                title=sanitize_first_person_title(raw_clip.get('title', ''), channel, lang=lang_code),
                start_time=start,
                end_time=end,
                hook_time=hook,
                virality_score=raw_clip.get('virality_score', 0),
                key_quotes=raw_clip.get('key_quotes') or [],
                transcript=" ".join(clip_lines),
                title_suggestion=sanitize_first_person_title(raw_clip.get('title_suggestion', ''), channel, lang=lang_code),
                caption_suggestion=caption_sug,
                hashtag_suggestion=hashtag_sug
            ))

        # Enforce max constraint of ~200 clips for auto clip count
        if is_auto_clip_count and len(final_clips) > 200:
            final_clips = final_clips[:200]

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
            channel=channel,
            duration=duration,
            heatmap=response_heatmap,
            summary=clean_summary,
            clips=final_clips,
            transcript=response_transcript,
            model=successful_model or requested_model,
            video_url=video_url,
            source_type=source_type
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
