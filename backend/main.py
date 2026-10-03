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

from dotenv import load_dotenv

# Automatically load environment variables from backend/.env or root .env
_base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base_dir, ".env"))
load_dotenv(os.path.join(_base_dir, "..", ".env"))
load_dotenv()

import re
import logging
import asyncio
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Callable, Any, Dict, Union
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Header, Body, Request
from fastapi.responses import StreamingResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import yt_dlp
import requests
import subprocess
import html
from urllib.parse import urlsplit
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import JSONFormatter
from google import genai
from google.genai import types

# ----------------------------------------------------------------
# Security & Privacy Redaction Engine for Logs & Traces
# ----------------------------------------------------------------

SENSITIVE_PATTERNS = [
    # URL query parameter tokens: ?api_key=..., &key=..., etc.
    (re.compile(r'([?&](?:api_key|apikey|gemini_key|key|token|access_token|secret|password|auth|authorization)=)[^&\s"\'`\)]+', re.IGNORECASE), r'\1[REDACTED]'),
    # Google AI Studio / Gemini API key pattern (AIza...)
    (re.compile(r'AIza[0-9A-Za-z\-_]{20,}'), '[REDACTED_API_KEY]'),
    # JSON field values: "api_key": "...", etc.
    (re.compile(r'("(?:api_key|apikey|gemini_key|key|token|access_token|secret|password|auth|authorization)"\s*:\s*)"[^"]*"', re.IGNORECASE), r'\1"[REDACTED]"'),
    # Python/Code/String assignments: api_key='...', api_key="...", key=...
    (re.compile(r'(\b(?:api_key|apikey|gemini_key|secret|password|access_token|authorization)\s*=\s*[\'"])[^\'"]+([\'"])', re.IGNORECASE), r'\1[REDACTED]\2'),
    # Authorization header tokens: Bearer ..., Basic ..., key=...
    (re.compile(r'(\b(?:Bearer|Basic|key=)\s+)[a-zA-Z0-9_\-\.]{8,}', re.IGNORECASE), r'\1[REDACTED]'),
    # Proxy passwords in URLs: http://user:pass@host:port
    (re.compile(r'((?:https?|socks4|socks5)://[^:\s/@]+:)[^@\s/]+(@)', re.IGNORECASE), r'\1***\2'),
]

def sanitize_sensitive_data(val: Any) -> Any:
    """Recursively redacts API keys, credentials, and sensitive tokens from strings, containers, or objects."""
    if val is None:
        return None
    if isinstance(val, str):
        res = val
        for pattern, replacement in SENSITIVE_PATTERNS:
            res = pattern.sub(replacement, res)
        return res
    elif isinstance(val, (list, tuple)):
        sanitized = [sanitize_sensitive_data(item) for item in val]
        return tuple(sanitized) if isinstance(val, tuple) else sanitized
    elif isinstance(val, dict):
        sanitized_dict = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ('api_key', 'apikey', 'gemini_key', 'secret', 'password', 'token', 'authorization')):
                sanitized_dict[k] = '[REDACTED]' if v else v
            else:
                sanitized_dict[k] = sanitize_sensitive_data(v)
        return sanitized_dict
    elif isinstance(val, Exception):
        return sanitize_sensitive_data(str(val))
    return val

_original_log_record_factory = logging.getLogRecordFactory()

def sensitive_log_record_factory(*args, **kwargs):
    record = _original_log_record_factory(*args, **kwargs)
    try:
        if isinstance(record.msg, str):
            record.msg = sanitize_sensitive_data(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = sanitize_sensitive_data(record.args)
            elif isinstance(record.args, (tuple, list)):
                record.args = tuple(sanitize_sensitive_data(arg) for arg in record.args)
            else:
                record.args = sanitize_sensitive_data(str(record.args))
        if getattr(record, 'exc_text', None):
            record.exc_text = sanitize_sensitive_data(record.exc_text)
        if getattr(record, 'stack_info', None):
            record.stack_info = sanitize_sensitive_data(record.stack_info)
    except Exception:
        pass
    return record

logging.setLogRecordFactory(sensitive_log_record_factory)

class SensitiveDataFilter(logging.Filter):
    """Logging filter that intercepts and sanitizes sensitive keys, tokens, and credentials in all log records."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = sanitize_sensitive_data(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = sanitize_sensitive_data(record.args)
                elif isinstance(record.args, (tuple, list)):
                    record.args = tuple(sanitize_sensitive_data(arg) for arg in record.args)
                else:
                    record.args = sanitize_sensitive_data(str(record.args))
            if record.exc_text:
                record.exc_text = sanitize_sensitive_data(record.exc_text)
            if record.stack_info:
                record.stack_info = sanitize_sensitive_data(record.stack_info)
        except Exception:
            pass
        return True

def apply_security_logging_filters():
    """Applies the sensitive data filter to all standard and uvicorn loggers and handlers."""
    sensitive_filter = SensitiveDataFilter()
    target_loggers = [
        logging.getLogger(),
        logging.getLogger("cheat-clip"),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.asgi"),
        logging.getLogger("fastapi"),
    ]
    for lgr in target_loggers:
        if not any(isinstance(f, SensitiveDataFilter) for f in lgr.filters):
            lgr.addFilter(sensitive_filter)
        for handler in lgr.handlers:
            if not any(isinstance(f, SensitiveDataFilter) for f in handler.filters):
                handler.addFilter(sensitive_filter)

# Setup logging
logging.basicConfig(level=logging.INFO)
apply_security_logging_filters()
logger = logging.getLogger("cheat-clip")

app = FastAPI(title="CHEAT CLIP API", description="AI-powered YouTube Viral Hotspot Finder")

@app.on_event("startup")
def on_startup_security():
    apply_security_logging_filters()

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
    title: str = Field(description="Catchy clip title (max 8 words). MANDATORY: Never use first-person pronouns ('I', 'me', 'my', 'mine', 'saya', 'aku', 'gue'). Frame objectively using speaker name, host, role, or video context.")
    start_time: float = Field(description="Clip start in seconds, aligned to a sentence boundary")
    end_time: float = Field(description="Clip end in seconds, aligned to a sentence boundary")
    hook_time: float = Field(description="Absolute timestamp in seconds from video start where the potential hook occurs inside this clip range (must be >= start_time and <= end_time)")
    virality_score: int = Field(description="Virality score 1-100")
    key_quotes: List[str] = Field(description="1-2 key quotes from the clip")
    transcript: str = Field(description="Spoken text of the clip")
    title_suggestion: str = Field(default="", description="Catchy alternative title suggestion in third-person (no 'I'/'me'/'my'/'saya', frame around speaker or topic)")
    caption_suggestion: str = Field(default="", description="Engaging social media caption suggestion framed around what the speaker discusses")
    hashtag_suggestion: str = Field(default="", description="Relevant hashtags suggestion (e.g. #hashtag1 #hashtag2)")

class ViralClipGemini(BaseModel):
    title: str = Field(description="Catchy clip title (max 8 words). MANDATORY: Never use first-person pronouns ('I', 'me', 'my', 'mine', 'saya', 'aku', 'gue'). Frame objectively using speaker name, host, role, or video context.")
    start_time: float = Field(description="Clip start in seconds, aligned to a sentence boundary")
    end_time: float = Field(description="Clip end in seconds, aligned to a sentence boundary")
    hook_time: float = Field(description="Absolute timestamp in seconds from video start where the potential hook occurs inside this clip range (must be >= start_time and <= end_time)")
    virality_score: int = Field(description="Virality score 1-100")
    key_quotes: List[str] = Field(description="1-2 key quotes from the clip")
    title_suggestion: str = Field(default="", description="Catchy alternative title suggestion in third-person (no 'I'/'me'/'my'/'saya', frame around speaker or topic)")
    caption_suggestion: str = Field(default="", description="Engaging social media caption suggestion framed around what the speaker discusses")
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
    proxy: Optional[str] = Field(None, description="Optional custom HTTP/HTTPS/SOCKS proxy URL")

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
    # Handle shorts, live, embed, watch?v=, youtu.be, etc.
    patterns = [
        r"(?:v=|\/v\/|embed\/|shorts\/|live\/|youtu\.be\/|\/embed\/|\/watch\?v=|\/watch\?.+&v=)([\w-]{11})",
        r"^(?:https?:\/\/)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=)?([\w-]{11})"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    # Simple length check fallback if the user just pasted the ID
    trimmed = url.strip()
    if len(trimmed) == 11 and re.match(r"^[\w-]{11}$", trimmed):
        return trimmed
    return None

class TimeoutSession(requests.Session):
    """requests.Session that enforces a default timeout to avoid hanging indefinitely on slow proxies."""
    def __init__(self, timeout: float = 12.0):
        super().__init__()
        self._default_timeout = timeout

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(*args, **kwargs)

_proxy_index = 0
_proxy_lock = threading.Lock()

def get_all_proxy_urls() -> List[str]:
    """Retrieves all configured proxy URLs from environment variables, splitting comma-separated lists."""
    raw_candidates = [
        os.environ.get("PROXY_URL", ""),
        os.environ.get("WEBSHARE_PROXY", ""),
        os.environ.get("PROXIES", ""),
        os.environ.get("HTTPS_PROXY", ""),
        os.environ.get("HTTP_PROXY", ""),
        os.environ.get("ALL_PROXY", "")
    ]
    urls: List[str] = []
    for raw in raw_candidates:
        if raw and raw.strip():
            # Support comma-separated proxies: proxy1,proxy2,proxy3
            for part in raw.split(","):
                p = part.strip().strip("'\"")
                if p and p not in urls:
                    urls.append(p)

    # Synthesize URL from explicit Webshare credentials if provided and not already in list
    ws_user = os.environ.get("WEBSHARE_USERNAME", "").strip()
    ws_pass = os.environ.get("WEBSHARE_PASSWORD", "").strip()
    if ws_user and ws_pass:
        ws_locations_raw = os.environ.get("WEBSHARE_LOCATIONS", "").strip()
        loc_suffix = "".join(f"-{loc.strip().upper()}" for loc in ws_locations_raw.split(",") if loc.strip())
        user_clean = ws_user[:-7] if ws_user.endswith("-rotate") else ws_user
        synthesized = f"http://{user_clean}{loc_suffix}-rotate:{ws_pass}@p.webshare.io:80"
        if synthesized not in urls:
            urls.append(synthesized)

    return urls

def get_proxy_url() -> Optional[str]:
    """Retrieves next rotating proxy URL from configured pool (round-robin)."""
    urls = get_all_proxy_urls()
    if not urls:
        return None
    global _proxy_index
    with _proxy_lock:
        selected = urls[_proxy_index % len(urls)]
        _proxy_index = (_proxy_index + 1) % len(urls)
        return selected

def get_youtube_transcript_proxy_config(custom_proxy: Optional[str] = None):
    """
    Constructs a ProxyConfig (WebshareProxyConfig or GenericProxyConfig)
    for YouTubeTranscriptApi per official recommendations:
    https://github.com/jdepoix/youtube-transcript-api#working-around-ip-bans-requestblocked-or-ipblocked-exception
    """
    from youtube_transcript_api.proxies import WebshareProxyConfig, GenericProxyConfig
    from urllib.parse import urlsplit

    ws_locations_raw = os.environ.get("WEBSHARE_LOCATIONS", "").strip()
    ws_locations = [loc.strip() for loc in ws_locations_raw.split(",") if loc.strip()] if ws_locations_raw else None
    try:
        ws_retries = int(os.environ.get("WEBSHARE_RETRIES", "5"))
    except ValueError:
        ws_retries = 5

    # 1. Explicit Webshare credentials from environment
    ws_user = os.environ.get("WEBSHARE_USERNAME", "").strip()
    ws_pass = os.environ.get("WEBSHARE_PASSWORD", "").strip()
    if not custom_proxy and ws_user and ws_pass:
        return WebshareProxyConfig(
            proxy_username=ws_user,
            proxy_password=ws_pass,
            filter_ip_locations=ws_locations,
            retries_when_blocked=ws_retries
        )

    # 2. Check full proxy URL (custom_proxy or from get_proxy_url())
    proxy_url = (custom_proxy or get_proxy_url() or "").strip()
    if not proxy_url:
        return None

    # Check if this proxy URL points to Webshare
    if "webshare.io" in proxy_url.lower():
        try:
            parsed = urlsplit(proxy_url)
            if parsed.username and parsed.password:
                domain = parsed.hostname or "p.webshare.io"
                port = parsed.port or 80
                return WebshareProxyConfig(
                    proxy_username=parsed.username,
                    proxy_password=parsed.password,
                    domain_name=domain,
                    proxy_port=port,
                    filter_ip_locations=ws_locations,
                    retries_when_blocked=ws_retries
                )
        except Exception as e:
            logger.warning(f"Failed parsing Webshare URL for WebshareProxyConfig: {e}")

    # 3. GenericProxyConfig fallback for non-Webshare proxies
    try:
        return GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
    except Exception as e:
        logger.warning(f"Failed creating GenericProxyConfig: {e}")
        return None

# ── Shared Cookie Jar & HTTP Session Factory ──────────────────────────────────
# Per https://github.com/jdepoix/youtube-transcript-api#overwriting-request-defaults
# Caching cookies across requests (consent screens, visitor tokens) and setting
# realistic browser headers minimizes automated bot blocks on YouTube.
_shared_cookie_jar = requests.cookies.RequestsCookieJar()

def create_http_client(timeout: float = 15.0) -> TimeoutSession:
    """Creates a requests.Session pre-configured with realistic browser headers,
    shared YouTube cookies (consent/tokens), and optional CA bundle per documentation:
    https://github.com/jdepoix/youtube-transcript-api#overwriting-request-defaults
    """
    session = TimeoutSession(timeout=timeout)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1"
    })
    # Inherit verified session cookies across requests
    session.cookies.update(_shared_cookie_jar)

    # SSL verification certificate override if defined
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca_bundle and os.path.exists(ca_bundle):
        session.verify = ca_bundle

    return session

def normalize_transcript(fetched_data) -> List[dict]:
    """Normalizes FetchedTranscript objects (using JSONFormatter or to_raw_data()),
    raw JSON strings, or dict lists into standard timestamped segment dictionaries.
    Per https://github.com/jdepoix/youtube-transcript-api#using-formatters
    """
    if not fetched_data:
        return []

    items = []
    if hasattr(fetched_data, "snippets") or hasattr(fetched_data, "to_raw_data"):
        try:
            formatter = JSONFormatter()
            json_str = formatter.format_transcript(fetched_data)
            items = json.loads(json_str)
        except Exception:
            items = fetched_data.to_raw_data() if hasattr(fetched_data, "to_raw_data") else list(fetched_data)
    elif isinstance(fetched_data, str):
        try:
            parsed = json.loads(fetched_data)
            items = parsed[0] if isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], list) else parsed
        except Exception:
            return []
    elif isinstance(fetched_data, list):
        items = fetched_data
    else:
        try:
            items = list(fetched_data)
        except Exception:
            return []

    results = []
    for item in items:
        if isinstance(item, dict):
            text = html.unescape(str(item.get("text", ""))).strip()
            start = float(item.get("start", 0.0))
            dur = float(item.get("duration", 0.0))
        else:
            text = html.unescape(getattr(item, "text", "")).strip()
            start = float(getattr(item, "start", 0.0))
            dur = float(getattr(item, "duration", 0.0))

        if text:
            results.append({
                "text": text,
                "start": round(start, 2),
                "duration": max(0.1, round(dur, 2))
            })

    return results

def fetch_transcript_cli(
    video_id: str,
    priority_langs: List[str],
    proxy_url: Optional[str] = None,
    custom_proxy: Optional[str] = None,
    use_proxy: bool = True,
    timeout: int = 20
) -> List[dict]:
    """Attempts subtitle extraction using youtube_transcript_api CLI subprocess.
    Provides an isolated process environment with independent network & proxy stack.
    Handles hyphenated video IDs (e.g. \"\\-abc\") per documentation:
    https://github.com/jdepoix/youtube-transcript-api#cli
    """
    escaped_id = f"\\{video_id}" if video_id.startswith("-") else video_id
    cmd = [sys.executable, "-m", "youtube_transcript_api", escaped_id, "--format", "json"]

    if priority_langs:
        cmd.extend(["--languages"] + priority_langs)

    env = os.environ.copy()

    if use_proxy:
        ws_user = os.environ.get("WEBSHARE_USERNAME", "").strip()
        ws_pass = os.environ.get("WEBSHARE_PASSWORD", "").strip()
        effective_proxy = custom_proxy or proxy_url or get_proxy_url()

        if ws_user and ws_pass and not custom_proxy:
            cmd.extend(["--webshare-proxy-username", ws_user, "--webshare-proxy-password", ws_pass])
        elif effective_proxy:
            cmd.extend(["--http-proxy", effective_proxy, "--https-proxy", effective_proxy])
    else:
        # Strip proxy environment variables for pure direct execution
        for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(proxy_var, None)

    logger.info(f"Executing CLI transcript extraction for {video_id} (proxy={'yes' if use_proxy else 'no'})...")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        stdout_clean = (proc.stdout or "").strip()
        stderr_clean = (proc.stderr or "").strip()

        if proc.returncode == 0 and stdout_clean:
            # youtube_transcript_api CLI exits with code 0 even when it fails to retrieve transcripts,
            # printing a plain-text error message to stdout. Check if output is actually JSON.
            if stdout_clean.startswith(("[", "{")):
                try:
                    raw = json.loads(stdout_clean)
                    items = raw[0] if isinstance(raw, list) and len(raw) > 0 and isinstance(raw[0], list) else raw
                    result = normalize_transcript(items)
                    if result:
                        logger.info(f"Transcript fetched via CLI fallback: {len(result)} lines")
                        return result
                except json.JSONDecodeError:
                    pass

        # Extract the meaningful error message from stdout or stderr
        output_text = stdout_clean or stderr_clean
        if output_text:
            non_empty_lines = [line.strip() for line in output_text.splitlines() if line.strip()]
            if non_empty_lines:
                summary_lines = []
                for line in non_empty_lines:
                    if "If you are sure" in line or "please create an issue" in line:
                        break
                    summary_lines.append(line)
                first_err = " - ".join(summary_lines[:2])
                raise Exception(first_err)
        raise Exception(f"CLI returned exit code {proc.returncode} with no transcript output")
    except Exception as e:
        logger.warning(f"CLI transcript extraction failed: {e}")
        raise



def get_youtube_oembed_title(video_id_or_url: str) -> Optional[str]:
    """Fetches video title directly from YouTube's public oEmbed API.
    Fast (<300ms), requires no authentication or cookies, and works reliably when yt-dlp is blocked."""
    import requests
    video_id = extract_video_id(video_id_or_url) if ("youtube" in video_id_or_url or "youtu.be" in video_id_or_url or "/" in video_id_or_url) else video_id_or_url
    if not video_id:
        return None
    
    # 1. Try direct HTTP GET to oEmbed endpoint
    try:
        resp = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=5
        )
        if resp.status_code == 200:
            title = resp.json().get("title")
            if title and title.strip():
                return title.strip()
    except Exception as e:
        logger.warning(f"Direct oEmbed title fetch failed for {video_id}: {e}")

    # 2. Try via proxy if configured
    proxy = get_proxy_url()
    if proxy:
        try:
            resp = requests.get(
                f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
                proxies={"http": proxy, "https": proxy},
                timeout=5
            )
            if resp.status_code == 200:
                title = resp.json().get("title")
                if title and title.strip():
                    return title.strip()
        except Exception as e:
            logger.warning(f"Proxy oEmbed title fetch failed for {video_id}: {e}")

    # 3. Direct HTML title scraping fallback
    try:
        resp = requests.get(f"https://www.youtube.com/watch?v={video_id}", timeout=5)
        if resp.status_code == 200:
            m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', resp.text)
            if m and m.group(1).strip():
                return m.group(1).strip()
            m2 = re.search(r'<title>(.*?)(?:\s*-\s*YouTube)?</title>', resp.text)
            if m2 and m2.group(1).strip():
                return m2.group(1).strip()
    except Exception:
        pass

    return None

def fetch_video_metadata(url: str, custom_proxy: Optional[str] = None):
    """Fetches video title, duration, and viewer retention heatmap using yt-dlp with oEmbed title fallback."""
    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    proxy = custom_proxy or get_proxy_url()
    video_id = extract_video_id(url)
    target_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    
    # On Vercel, YouTube blocks direct datacenter IPs, so try proxy first if configured; locally try direct first
    attempts = [proxy, None] if (is_vercel and proxy) else [None, proxy] if proxy else [None]
    
    for attempt_proxy in attempts:
        ydl_opts = {
            'skip_download': True,
            'youtube_include_dash_manifest': False,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'proxy': attempt_proxy,
            'socket_timeout': 10
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target_url, download=False)
                if not info:
                    raise Exception("yt-dlp returned empty info dict")
                title = info.get('title')
                if not title or title.lower() == 'unknown youtube video':
                    if video_id:
                        title = get_youtube_oembed_title(video_id) or title
                return {
                    "title": title or 'Unknown YouTube Video',
                    "duration": float(info.get('duration') or 0.0),
                    "heatmap": info.get('heatmap') or [],
                    "is_live": bool(info.get('is_live') or False),
                    "live_status": info.get('live_status') or 'not_live'
                }
        except Exception as e:
            logger.warning(f"yt-dlp metadata extraction failed (proxy={'yes' if attempt_proxy else 'no'}): {e}")
            continue

    # Fallback to oEmbed and URL video ID parsing if yt-dlp fails
    if video_id:
        fallback_title = get_youtube_oembed_title(video_id)
        return {
            "title": fallback_title or f"YouTube Video ({video_id})",
            "duration": 0.0,
            "heatmap": [],
            "is_live": False,
            "live_status": "not_live"
        }
    raise HTTPException(status_code=400, detail="Failed to retrieve YouTube video details from URL.")


_supadata_key_index = 0

def get_supadata_keys() -> List[str]:
    """Retrieves list of Supadata API keys from environment variables."""
    raw = os.environ.get("SUPADATA_API_KEYS") or os.environ.get("SUPADATA_API_KEY") or ""
    # Extract keys starting with sd_ or split by comma/whitespace/quotes
    keys = re.findall(r'sd_[a-zA-Z0-9]+', raw)
    if not keys:
        keys = [k.strip('\"\' ') for k in re.split(r'[,\s\n]+', raw) if k.strip('\"\' ')]
    return keys

def fetch_transcript_supadata(video_id: str, error_collector: Optional[List[str]] = None) -> List[dict]:
    """Fetches transcript from Supadata API, rotating through available keys if rate limits/quotas occur."""
    global _supadata_key_index
    import requests
    
    keys = get_supadata_keys()
    if not keys:
        if error_collector is not None:
            error_collector.append("Supadata API: No keys configured (SUPADATA_API_KEYS is empty in .env)")
        return []

    # Round-robin key rotation to evenly distribute load across keys
    start_idx = _supadata_key_index % len(keys)
    rotated_keys = keys[start_idx:] + keys[:start_idx]
    _supadata_key_index = (_supadata_key_index + 1) % len(keys)

    quota_exhausted_count = 0
    not_found = False
    last_error = ""

    for key in rotated_keys:
        masked_key = f"{key[:7]}...{key[-4:]}" if len(key) >= 11 else "***"
        try:
            logger.info(f"Attempting Supadata transcript fetch with key {masked_key}")
            response = requests.get(
                "https://api.supadata.ai/v1/youtube/transcript",
                headers={"x-api-key": key},
                params={"videoId": video_id},
                timeout=12
            )
            if response.status_code == 200:
                data = response.json()
                content = data.get("content") or []
                if content:
                    result = []
                    for seg in content:
                        text = seg.get("text", "").strip()
                        if text:
                            start = float(seg.get("offset", 0)) / 1000.0
                            dur = float(seg.get("duration", 0)) / 1000.0
                            result.append({"text": text, "start": start, "duration": dur})
                    if result:
                        logger.info(f"Successfully retrieved {len(result)} transcript lines via Supadata ({masked_key})")
                        # Invalidate usage cache so next check fetches fresh quota data
                        global _supadata_usage_cache
                        _supadata_usage_cache["timestamp"] = 0
                        return result
            elif response.status_code in (429, 402):
                quota_exhausted_count += 1
                logger.warning(f"Supadata key {masked_key} returned status {response.status_code} (quota/limit). Rotating to next key...")
                continue
            elif response.status_code == 404:
                not_found = True
                last_error = "HTTP 404 (No subtitles found for this video on YouTube)"
                logger.warning(f"Supadata key {masked_key} returned status 404: No subtitles found")
                break
            else:
                last_error = f"HTTP {response.status_code}: {response.text[:100]}"
                logger.warning(f"Supadata key {masked_key} returned status {response.status_code}: {response.text[:100]}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Supadata request with key {masked_key} failed: {e}")
            continue

    if error_collector is not None:
        if quota_exhausted_count == len(keys):
            error_collector.append(f"Supadata API: All {len(keys)} configured API keys exhausted (HTTP 429/402 Monthly Quota Exceeded)")
        elif not_found:
            error_collector.append(f"Supadata API: {last_error}")
        elif last_error:
            error_collector.append(f"Supadata API: Requests failed across all keys ({last_error})")
        else:
            error_collector.append("Supadata API: Transcript content was empty")

    return []


_supadata_usage_cache = {
    "data": None,
    "timestamp": 0
}
_CACHE_TTL_SECONDS = 30

def check_single_supadata_key(key: str, index: int) -> dict:
    masked = f"{key[:7]}...{key[-4:]}" if len(key) >= 11 else "***"
    import requests
    try:
        resp = requests.get(
            "https://api.supadata.ai/v1/me",
            headers={"x-api-key": key},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            max_credits = int(data.get("maxCredits", 100))
            used_credits = int(data.get("usedCredits", 0))
            remaining = max(0, max_credits - used_credits)
            status = "exhausted" if remaining == 0 else "active"
            return {
                "index": index,
                "masked_key": masked,
                "status": status,
                "max_credits": max_credits,
                "used_credits": used_credits,
                "remaining_credits": remaining,
                "plan": data.get("plan", "Free (100/mo)")
            }
        elif resp.status_code in (429, 402):
            return {
                "index": index,
                "masked_key": masked,
                "status": "exhausted",
                "max_credits": 100,
                "used_credits": 100,
                "remaining_credits": 0,
                "plan": "Limit Exceeded"
            }
        else:
            return {
                "index": index,
                "masked_key": masked,
                "status": "error",
                "max_credits": 100,
                "used_credits": 0,
                "remaining_credits": 100,
                "plan": f"HTTP {resp.status_code}"
            }
    except Exception as e:
        logger.warning(f"Error checking Supadata key {masked}: {e}")
        return {
            "index": index,
            "masked_key": masked,
            "status": "error",
            "max_credits": 100,
            "used_credits": 0,
            "remaining_credits": 100,
            "plan": "Timeout/Error"
        }

def get_supadata_usage_data(force: bool = False) -> dict:
    """Aggregates quota metrics across all configured Supadata API keys in parallel with 30s cache."""
    global _supadata_usage_cache
    now = time.time()
    if not force and _supadata_usage_cache["data"] and (now - _supadata_usage_cache["timestamp"] < _CACHE_TTL_SECONDS):
        cached_res = dict(_supadata_usage_cache["data"])
        cached_res["cached"] = True
        return cached_res

    keys = get_supadata_keys()
    if not keys:
        empty_res = {
            "total_keys": 0,
            "total_limit": 0,
            "total_used": 0,
            "total_remaining": 0,
            "usage_percent": 0.0,
            "active_keys": 0,
            "exhausted_keys": 0,
            "keys_detail": [],
            "cached": False,
            "timestamp": now
        }
        _supadata_usage_cache = {"data": empty_res, "timestamp": now}
        return empty_res

    with ThreadPoolExecutor(max_workers=min(len(keys), 12)) as executor:
        futures = [executor.submit(check_single_supadata_key, k, i + 1) for i, k in enumerate(keys)]
        details = [f.result() for f in futures]

    total_limit = sum(k["max_credits"] for k in details)
    total_used = sum(k["used_credits"] for k in details)
    total_remaining = sum(k["remaining_credits"] for k in details)
    active_count = sum(1 for k in details if k["remaining_credits"] > 0)
    exhausted_count = sum(1 for k in details if k["remaining_credits"] == 0 and k["status"] != "error")
    usage_percent = round((total_used / total_limit * 100.0), 1) if total_limit > 0 else 0.0

    res = {
        "total_keys": len(keys),
        "total_limit": total_limit,
        "total_used": total_used,
        "total_remaining": total_remaining,
        "usage_percent": usage_percent,
        "active_keys": active_count,
        "exhausted_keys": exhausted_count,
        "keys_detail": details,
        "cached": False,
        "timestamp": now
    }
    _supadata_usage_cache = {"data": res, "timestamp": now}
    return res


def fetch_transcript_ytdlp(video_id: str, proxy: Optional[str] = None) -> List[dict]:
    """Attempts to extract captions using yt-dlp's player response directly (free, no quota used).
    Can be run direct (proxy=None) or routed through a proxy."""
    import requests
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'proxy': proxy,
        'socket_timeout': 10
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if not info:
                return []
            
            subtitles = info.get('subtitles') or {}
            auto_subtitles = info.get('automatic_captions') or {}
            
            priority_langs = ['id', 'en', 'es', 'pt', 'fr', 'de', 'ja', 'ko', 'zh-Hans', 'zh-Hant', 'ar', 'hi', 'ru']
            # Search manual first, then automatic captions
            for lang_dict, is_auto in [(subtitles, False), (auto_subtitles, True)]:
                langs_to_try = [l for l in priority_langs if l in lang_dict] + [l for l in lang_dict if l not in priority_langs]
                for lang in langs_to_try:
                    formats = lang_dict.get(lang) or []
                    json3_entry = next((f['url'] for f in formats if f.get('ext') == 'json3'), None)
                    if json3_entry:
                        # Try proxy if provided, then direct fallback (or direct only if no proxy)
                        proxies_dict = {'http': proxy, 'https': proxy} if proxy else None
                        attempts = [proxies_dict, None] if proxy else [None]
                        for p in attempts:
                            try:
                                r = requests.get(json3_entry, proxies=p, timeout=8)
                                if r.status_code == 200:
                                    events = r.json().get('events', [])
                                    result = []
                                    for ev in events:
                                        segs = ev.get('segs', [])
                                        text = ''.join(s.get('utf8', '') for s in segs).strip()
                                        if text:
                                            start = ev.get('tStartMs', 0) / 1000.0
                                            dur = ev.get('dDurationMs', 0) / 1000.0
                                            result.append({'text': text, 'start': start, 'duration': dur})
                                    if result:
                                        logger.info(f"Transcript fetched via yt-dlp (lang={lang}, auto={is_auto}, proxy={'yes' if p else 'no'})")
                                        return result
                            except Exception:
                                continue
    except Exception as e:
        logger.warning(f"yt-dlp subtitle extraction failed (proxy={'yes' if proxy else 'no'}): {e}")
    return []


def fetch_transcript(
    video_id: str,
    custom_proxy: Optional[str] = None,
    on_progress: Optional[Callable[[str, str, int], None]] = None
) -> List[dict]:
    """Retrieves subtitles using a comprehensive multi-tier fallback pipeline:
      Tier 1: Supadata API (if keys configured) — cloud residential rotation
      Tier 2: YouTubeTranscriptApi Python API (Proxy + Shared Session + Browser Headers + Translation fallback)
      Tier 3: YouTubeTranscriptApi CLI Subprocess (Proxy)
      Tier 4: yt-dlp Native Extraction (Proxy)
      Tier 5: Direct YouTubeTranscriptApi Python API (Direct, Shared Session + Browser Headers)
      Tier 6: Direct YouTubeTranscriptApi CLI Subprocess (Direct)
      Tier 7: Direct yt-dlp Native Extraction (Direct)
    If all tiers fail, raises detailed HTTPException with full diagnostics and solutions.
    """
    def notify(stage: str, detail: str, pct: int):
        if on_progress:
            try:
                on_progress(stage, detail, pct)
            except Exception:
                pass

    priority_langs = ['id', 'en', 'es', 'pt', 'fr', 'de', 'ja', 'ko', 'zh-Hans', 'zh-Hant', 'ar', 'hi', 'ru']
    keys = get_supadata_keys()
    proxy_url = custom_proxy or get_proxy_url()
    proxy_cfg = get_youtube_transcript_proxy_config(custom_proxy)
    attempt_history: List[str] = []

    # ── Tier 1: Supadata API (if keys configured) ─────────────────────────────
    if keys:
        logger.info(f"[Tier 1] Attempting transcript retrieval via Supadata API ({len(keys)} keys configured)...")
        notify("Tier 1/7: Supadata Cloud API", f"Trying Method 1/7: Supadata Cloud API ({len(keys)} keys rotation)...", 30)
        supadata_data = fetch_transcript_supadata(video_id, error_collector=attempt_history)
        if supadata_data:
            return normalize_transcript(supadata_data)
        logger.info("[Tier 1] Supadata API unsuccessful — proceeding to proxy fallback tiers...")
    else:
        attempt_history.append("Tier 1 (Supadata API): Not configured (no keys in SUPADATA_API_KEYS)")

    # ── Proxy Tiers (Tier 2 - 4) ──────────────────────────────────────────────
    if proxy_cfg or proxy_url:
        masked_proxy = proxy_url.split('@')[-1] if (proxy_url and '@' in proxy_url) else (proxy_url or "Configured Proxy")
        logger.info(f"[Tier 2-4] Attempting proxy fallback pipeline ({masked_proxy})...")

        # ── Tier 2: YouTubeTranscriptApi Python API with Proxy & Shared Session ───
        notify("Tier 2/7: Proxy Python API", f"Trying Method 2/7: YouTubeTranscriptApi via rotating proxy ({masked_proxy})...", 45)
        try:
            client = create_http_client(timeout=15.0)
            proxy_api = YouTubeTranscriptApi(proxy_config=proxy_cfg, http_client=client)

            # 2a. Direct language match
            try:
                data = proxy_api.fetch(video_id, languages=priority_langs)
                res = normalize_transcript(data)
                if res:
                    _shared_cookie_jar.update(client.cookies)
                    logger.info(f"[Tier 2a] Transcript fetched via proxy Python API direct: {len(res)} lines")
                    return res
            except Exception as direct_err:
                logger.info(f"[Tier 2a] Proxy direct language fetch missed: {direct_err}")

            # 2b. List all transcripts & try fetching manual, then auto
            try:
                transcripts = list(proxy_api.list(video_id))
                manual = [t for t in transcripts if not getattr(t, 'is_generated', False)]
                generated = [t for t in transcripts if getattr(t, 'is_generated', False)]
                for t in (manual + generated):
                    try:
                        data = t.fetch()
                        res = normalize_transcript(data)
                        if res:
                            _shared_cookie_jar.update(client.cookies)
                            logger.info(f"[Tier 2b] Transcript fetched via proxy Python API list ({t.language}): {len(res)} lines")
                            return res
                    except Exception:
                        continue

                # 2c. Translation fallback: translate any translatable track to 'id' or 'en'
                for t in transcripts:
                    if getattr(t, 'is_translatable', False):
                        for target_lang in ['id', 'en']:
                            try:
                                notify("Tier 2/7: Translating Captions", f"Translating available {t.language} track to {target_lang} via proxy...", 52)
                                translated = t.translate(target_lang)
                                data = translated.fetch()
                                res = normalize_transcript(data)
                                if res:
                                    _shared_cookie_jar.update(client.cookies)
                                    logger.info(f"[Tier 2c] Transcript translated via proxy Python API ({t.language} -> {target_lang}): {len(res)} lines")
                                    return res
                            except Exception:
                                continue

                attempt_history.append(f"Tier 2 (Proxy Python API): No accessible track in {len(transcripts)} tracks")
            except Exception as list_err:
                err_type = type(list_err).__name__
                err_msg = str(list_err).strip().split('\n')[0]
                attempt_history.append(f"Tier 2 (Proxy Python API): {err_type} ({err_msg})")
        except Exception as init_err:
            attempt_history.append(f"Tier 2 (Proxy Python API setup): {type(init_err).__name__} ({init_err})")

        # ── Tier 3: YouTubeTranscriptApi CLI Subprocess with Proxy ───────────────
        notify("Tier 3/7: Proxy CLI Subprocess", "Trying Method 3/7: Isolated CLI subprocess via proxy...", 60)
        try:
            cli_data = fetch_transcript_cli(
                video_id,
                priority_langs,
                proxy_url=proxy_url,
                custom_proxy=custom_proxy,
                timeout=20
            )
            if cli_data:
                logger.info(f"[Tier 3] Transcript fetched via proxy CLI subprocess: {len(cli_data)} lines")
                return cli_data
        except Exception as cli_err:
            attempt_history.append(f"Tier 3 (Proxy CLI Subprocess): {type(cli_err).__name__} ({str(cli_err)[:150]})")

        # ── Tier 4: yt-dlp Native Extraction with Proxy ──────────────────────────
        notify("Tier 4/7: Proxy yt-dlp Native", "Trying Method 4/7: yt-dlp native caption extraction via proxy...", 70)
        try:
            ytdlp_proxy_data = fetch_transcript_ytdlp(video_id, proxy=proxy_url)
            if ytdlp_proxy_data:
                res = normalize_transcript(ytdlp_proxy_data)
                if res:
                    logger.info(f"[Tier 4] Transcript fetched via proxy yt-dlp: {len(res)} lines")
                    return res
            attempt_history.append("Tier 4 (Proxy yt-dlp): No subtitle streams found or extraction empty")
        except Exception as ytdlp_err:
            attempt_history.append(f"Tier 4 (Proxy yt-dlp): {type(ytdlp_err).__name__} ({str(ytdlp_err)[:150]})")

    else:
        attempt_history.append("Tier 2-4 (Proxy Fallbacks): No proxy configured in .env (WEBSHARE_PROXY, WEBSHARE_USERNAME, or PROXY_URL)")

    # ── Direct Tiers (Tier 5 - 7: Localhost / Residential IP fallback) ─────────
    logger.info("[Tier 5-7] Attempting direct YouTube retrieval (no proxy)...")

    # ── Tier 5: Direct YouTubeTranscriptApi Python API ────────────────────────
    notify("Tier 5/7: Direct YouTube API", "Trying Method 5/7: Direct YouTubeTranscriptApi (localhost / residential)...", 80)
    try:
        direct_client = create_http_client(timeout=10.0)
        direct_api = YouTubeTranscriptApi(http_client=direct_client)

        try:
            data = direct_api.fetch(video_id, languages=priority_langs)
            res = normalize_transcript(data)
            if res:
                _shared_cookie_jar.update(direct_client.cookies)
                logger.info(f"[Tier 5a] Transcript fetched via direct Python API: {len(res)} lines")
                return res
        except Exception:
            pass

        try:
            all_transcripts = list(direct_api.list(video_id))
            manual = [t for t in all_transcripts if not getattr(t, 'is_generated', False)]
            generated = [t for t in all_transcripts if getattr(t, 'is_generated', False)]
            for transcript in (manual + generated):
                try:
                    data = transcript.fetch()
                    res = normalize_transcript(data)
                    if res:
                        _shared_cookie_jar.update(direct_client.cookies)
                        logger.info(f"[Tier 5b] Transcript fetched via direct list ({transcript.language}): {len(res)} lines")
                        return res
                except Exception:
                    continue

            # Direct translation fallback
            for t in all_transcripts:
                if getattr(t, 'is_translatable', False):
                    for target_lang in ['id', 'en']:
                        try:
                            notify("Tier 5/7: Translating Captions", f"Translating available {t.language} track to {target_lang} directly...", 84)
                            translated = t.translate(target_lang)
                            data = translated.fetch()
                            res = normalize_transcript(data)
                            if res:
                                _shared_cookie_jar.update(direct_client.cookies)
                                logger.info(f"[Tier 5c] Transcript translated via direct list ({t.language} -> {target_lang}): {len(res)} lines")
                                return res
                        except Exception:
                            continue

            attempt_history.append(f"Tier 5 (Direct Python API): No accessible track in {len(all_transcripts)} tracks")
        except Exception as list_err:
            err_type = type(list_err).__name__
            err_msg = str(list_err).strip().split('\n')[0]
            attempt_history.append(f"Tier 5 (Direct Python API): {err_type} ({err_msg})")
    except Exception as api_err:
        attempt_history.append(f"Tier 5 (Direct Python API setup): {type(api_err).__name__} ({str(api_err)[:150]})")

    # ── Tier 6: Direct YouTubeTranscriptApi CLI Subprocess ────────────────────
    notify("Tier 6/7: Direct CLI Subprocess", "Trying Method 6/7: Direct isolated CLI subprocess...", 88)
    try:
        direct_cli_data = fetch_transcript_cli(
            video_id,
            priority_langs,
            use_proxy=False,
            timeout=15
        )
        if direct_cli_data:
            logger.info(f"[Tier 6] Transcript fetched via direct CLI subprocess: {len(direct_cli_data)} lines")
            return direct_cli_data
    except Exception as cli_err:
        attempt_history.append(f"Tier 6 (Direct CLI Subprocess): {type(cli_err).__name__} ({str(cli_err)[:150]})")

    # ── Tier 7: Direct yt-dlp Native Extraction ──────────────────────────────
    notify("Tier 7/7: Direct yt-dlp Native", "Trying Method 7/7: Direct yt-dlp native caption extraction...", 94)
    try:
        direct_ytdlp_data = fetch_transcript_ytdlp(video_id, proxy=None)
        if direct_ytdlp_data:
            res = normalize_transcript(direct_ytdlp_data)
            if res:
                logger.info(f"[Tier 7] Transcript fetched via direct yt-dlp: {len(res)} lines")
                return res
        attempt_history.append("Tier 7 (Direct yt-dlp): No subtitle streams found")
    except Exception as ytdlp_err:
        attempt_history.append(f"Tier 7 (Direct yt-dlp): {type(ytdlp_err).__name__} ({str(ytdlp_err)[:150]})")



    # ── All Tiers Exhausted: Construct User-Friendly Limit / Quota Error ─────
    combined_history = " ".join(attempt_history)
    
    # Internal diagnostic log for debugging
    diag_lines = [f"Unable to retrieve subtitles for YouTube video ID '{video_id}'."]
    for h in attempt_history:
        diag_lines.append(f"  • {h}")
    logger.error("Subtitle retrieval exhausted all methods:\n" + "\n".join(diag_lines))

    # Construct clean user-facing error message with Supadata usage metrics
    if "TranscriptsDisabled" in combined_history:
        user_error = "Subtitles are disabled for this video by the creator. You can upload custom subtitles (.srt or .txt) to analyze this video."
    elif "AgeRestricted" in combined_history:
        user_error = "This video is age-restricted and requires YouTube authentication. You can upload custom subtitles (.srt or .txt) to analyze this video."
    elif "VideoUnavailable" in combined_history:
        user_error = "This video is private or unavailable."
    else:
        if keys:
            try:
                usage = get_supadata_usage_data(force=False)
                total_used = usage.get("total_used", 0)
                total_limit = usage.get("total_limit", len(keys) * 100)
                total_keys = usage.get("total_keys", len(keys))
                keys_word = "key" if total_keys == 1 else "keys"
                user_error = (
                    f"Limit exhausted: {total_used}/{total_limit} Supadata API credits used this month across {total_keys} {keys_word}. "
                    "Unable to retrieve subtitles automatically. Please upload custom subtitles manually (.srt or .txt) to analyze this video."
                )
            except Exception:
                user_error = (
                    f"Limit exhausted across {len(keys)} Supadata API keys. "
                    "Unable to retrieve subtitles automatically. Please upload custom subtitles manually (.srt or .txt) to analyze this video."
                )
        else:
            user_error = (
                "Limit exhausted: No Supadata API keys configured. "
                "Unable to retrieve subtitles automatically. Please upload custom subtitles manually (.srt or .txt) to analyze this video."
            )

    raise HTTPException(status_code=400, detail=user_error)





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
        "message": "CHEAT CLIP API is active",
        "is_vercel": is_vercel,
        "proxy_configured": bool(proxy),
        "gemini_env_configured": has_gemini,
        "supadata_keys_count": len(keys),
        "supadata": supadata_info
    }

@app.get("/api/supadata-usage")
def get_supadata_usage_endpoint(refresh: bool = False):
    """Retrieves dynamic usage and credit limits for all Supadata API keys."""
    return get_supadata_usage_data(force=refresh)

@app.get("/api/video-title")
def get_video_title_endpoint(video_id: str):
    """Retrieves real video title using YouTube oEmbed or fallback."""
    title = get_youtube_oembed_title(video_id)
    if not title:
        title = f"YouTube Video ({video_id})"
    return {"video_id": video_id, "title": title}



def parse_gemini_model_sort_key(name: str):
    """Sort key for Gemini models: parses major and minor versions (e.g. 3.7, 3.6, 3.5, 2.5, 2.0, 1.5),
    tier (standard > lite/8b > preview/exp), so newest and most capable models come first."""
    name_clean = (name or "").split('/')[-1].lower()
    m = re.search(r'(\d+)(?:\.(\d+))?', name_clean)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2)) if m.group(2) is not None else 0
    else:
        major, minor = 0, 0

    if 'lite' in name_clean or '8b' in name_clean:
        tier = 2
    elif 'exp' in name_clean or 'preview' in name_clean:
        tier = 1
    else:
        tier = 3

    return (major, minor, tier, name_clean)


KNOWN_FLASH_MODELS = [
    'gemini-2.5-flash',
    'gemini-2.5-flash-lite',
    'gemini-2.0-flash',
    'gemini-2.0-flash-lite',
    'gemini-1.5-flash',
    'gemini-1.5-flash-8b',
]

def get_flash_models_for_key(client: genai.Client) -> List[str]:
    """Dynamically query all available flash models for the given API key.
    Discovers newer versions (e.g., 3.7, 3.6, 3.5) and earlier versions (2.5, 2.0, 1.5),
    merging with known fallback models and sorting in descending order of version/capability."""
    discovered = []
    try:
        models_page = client.models.list()
        for m in models_page:
            name = m.name or ""
            short_name = name.split('/')[-1]
            if "gemini" in short_name.lower() and "flash" in short_name.lower():
                if m.supported_actions and "generateContent" not in m.supported_actions:
                    continue
                # Exclude non-text, specialized, or non-generative tasks
                exclude_keywords = [
                    'tuning', 'thinking', 'vision', 'image', 'tts',
                    'omni', 'customtools', 'embed', 'realtime', 'robotics'
                ]
                if not any(x in short_name.lower() for x in exclude_keywords):
                    if short_name not in discovered:
                        discovered.append(short_name)
    except Exception as e:
        logger.warning(f"Could not dynamically list models: {e}")

    # Combine discovered with known flash models, preserving uniqueness
    combined_pool = list(dict.fromkeys(discovered + KNOWN_FLASH_MODELS))
    # Sort descending so newest versions (3.7, 3.6, 3.5, 2.5, 2.0, 1.5) are prioritized
    ordered = sorted(combined_pool, key=parse_gemini_model_sort_key, reverse=True)
    return ordered


@app.get("/api/models")
@app.post("/api/models")
def list_available_models(
    api_key: Optional[str] = Query(None),
    x_gemini_api_key: Optional[str] = Header(None, alias="x-gemini-api-key"),
    authorization: Optional[str] = Header(None),
    payload: Optional[Dict[str, Any]] = Body(None)
):
    """Fetches list of available Gemini models using the user's API key, prioritizing Flash models (newest first).
    Securely accepts API key via 'x-gemini-api-key' header, Authorization bearer header, POST body, or query param."""
    default_models = [
        'gemini-2.5-flash',
        'gemini-2.5-flash-lite',
        'gemini-2.0-flash',
        'gemini-2.0-flash-lite',
        'gemini-1.5-flash',
        'gemini-2.5-pro'
    ]
    auth_key = ""
    if authorization:
        auth_key = authorization.replace("Bearer ", "").replace("bearer ", "").strip()
    
    body_key = payload.get("api_key") if (payload and isinstance(payload, dict)) else None
    key_to_use = (x_gemini_api_key or auth_key or body_key or api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    
    if not key_to_use or key_to_use.lower() == "mock":
        return {"models": default_models}
    try:
        client = genai.Client(api_key=key_to_use)
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
                exclude_keywords = [
                    'tuning', 'thinking', 'vision', 'image', 'tts',
                    'omni', 'customtools', 'embed', 'realtime', 'robotics'
                ]
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
        
        # Sort flash models by version descending (e.g. 3.7, 3.6, 3.5, 2.5, 2.0, 1.5)
        ordered_flash = sorted(
            list(dict.fromkeys(flash_models + KNOWN_FLASH_MODELS)),
            key=parse_gemini_model_sort_key,
            reverse=True
        )
        ordered_pro = sorted(pro_models, key=parse_gemini_model_sort_key, reverse=True)
        ordered_other = sorted(other_models, key=parse_gemini_model_sort_key, reverse=True)
        
        final_list = ordered_flash + ordered_pro + ordered_other
        if not final_list:
            final_list = default_models
            
        return {"models": final_list}
    except Exception as e:
        logger.error(f"Error listing models: {sanitize_sensitive_data(str(e))}")
        return {"models": default_models}

@app.post("/api/analyze")
async def analyze_video(
    request: AnalyzeRequest,
    x_gemini_api_key: Optional[str] = Header(None, alias="x-gemini-api-key"),
    authorization: Optional[str] = Header(None)
):
    """Stream real-time progress via Server-Sent Events, then deliver the final result."""

    async def stream():
        try:
            auth_key = authorization.replace("Bearer ", "").replace("bearer ", "").strip() if authorization else ""
            gemini_key = (x_gemini_api_key or auth_key or request.api_key or os.environ.get("GEMINI_API_KEY") or '').strip()
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
                metadata = await asyncio.to_thread(fetch_video_metadata, request.url, request.proxy)
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
                loop = asyncio.get_running_loop()
                progress_queue = asyncio.Queue()

                def progress_callback(stage: str, detail: str, step_pct: int = 30):
                    loop.call_soon_threadsafe(progress_queue.put_nowait, {
                        "step": 3,
                        "step_progress": step_pct,
                        "overall_progress": min(68, 50 + int(step_pct * 0.2)),
                        "stage": stage,
                        "detail": detail,
                        "message": detail
                    })

                # Initial stage event
                yield _sse({
                    "step": 3,
                    "step_progress": 25,
                    "overall_progress": 55,
                    "stage": "Fetching Subtitles",
                    "detail": "Initializing multi-tier subtitle extraction pipeline...",
                    "message": "Initializing multi-tier subtitle extraction pipeline..."
                })

                try:
                    task = asyncio.create_task(
                        asyncio.to_thread(fetch_transcript, video_id, request.proxy, progress_callback)
                    )

                    while not task.done():
                        try:
                            evt = await asyncio.wait_for(progress_queue.get(), timeout=0.2)
                            yield _sse(evt)
                        except asyncio.TimeoutError:
                            pass

                    while not progress_queue.empty():
                        yield _sse(progress_queue.get_nowait())

                    transcript_lines = await task
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
                f"You are an expert viral video clip finder for TikTok, YouTube Shorts, and Instagram Reels.\n"
                f"Find {clip_range} high-performing short-form clip candidates from this YouTube transcript.\n\n"
                f"Source Video Title: {title}\n"
                f"Duration Range: {int(start_bound)}s to {int(end_bound)}s (Length: {int(duration)}s) | Target clip length: {dur_range}\n"
                f"{heatmap_note}\n"
                f"{focus_instruction}"
                f"Match output language to the primary language of the transcript.\n\n"
                f"Transcript (start|end[|interest] text):\n---\n{transcript_text}\n---\n\n"
                f"CRITICAL RULES & PERSPECTIVE GUIDELINES (MANDATORY):\n"
                f"1. Timestamps: Use exact seconds from the transcript; clips must start and end at natural sentence boundaries; clips must not overlap.\n"
                f"2. Objective Third-Person Perspective (STRICT - NO FIRST-PERSON IN TITLES):\n"
                f"   - NEVER generate titles or title_suggestions using first-person pronouns such as 'I', 'me', 'my', 'mine', 'myself' (or Indonesian: 'saya', 'aku', 'gue', 'ku').\n"
                f"   - Clip titles must NOT sound like the clipper's or user's personal opinion (e.g. NEVER write 'Why I Quit', 'My Biggest Mistake', 'Kenapa Saya Keluar', 'Opini Saya').\n"
                f"   - ALWAYS frame titles objectively using the context of the video: refer to the person speaking by their name (from the video title or transcript), their role (e.g. 'The Host', 'The Guest', 'The Founder', 'The CEO'), or describe the topic/story objectively (e.g. 'Why [Speaker Name] Quit', 'How [Name] Scaled A Startup', 'The Shocking Truth About [Topic]').\n"
                f"   - If the speaker's name is not explicitly mentioned, use contextual descriptors like 'The Host', 'The Guest', 'The Expert', or direct topic phrasing.\n"
                f"3. Titles & Hook: Maximum 8 words, punchy, curiosity-inducing, and optimized for high click-through and viewer retention.\n"
                f"4. Captions & Hashtags: Make caption_suggestion engaging and framed around what the speaker discusses or reveals, and provide 3-5 relevant hashtags in hashtag_suggestion.\n"
                f"5. Quality & Ranking: Return {clip_range} clips sorted by virality_score descending."
            )

            requested_model = (request.model or 'gemini-2.5-flash').strip()
            if any(dep in requested_model.lower() for dep in ['gemini-1.0', 'gemini-pro-vision']):
                logger.info(f"Requested model '{requested_model}' is outdated. Upgrading to gemini-2.5-flash.")
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
            # 1. Start with the requested model
            # 2. Append all discovered and known flash models in version descending order (e.g. 3.7, 3.6, 3.5, 2.5, 2.0, 1.5)
            #    so all available flash models are tried before giving up
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
                        )
                    ))
                
                    call_start = asyncio.get_event_loop().time()
                    while not task.done():
                        done, _ = await asyncio.wait([task], timeout=2.0)
                        if not done:
                            elapsed = int(asyncio.get_event_loop().time() - call_start)
                        
                            if elapsed < 8:
                                stage = "Neural Context Loading"
                                detail = f"Transmitting {len(transcript_dump)} timestamped dialogue segments to {model_name}..."
                                step_prog = min(35, 12 + elapsed * 3)
                            elif elapsed < 20:
                                stage = "Retention Spike Cross-Analysis"
                                detail = f"Correlating viewer retention peaks against speaker dialogue to isolate viral moments..."
                                step_prog = min(55, 35 + int((elapsed - 8) * 1.6))
                            elif elapsed < 40:
                                stage = "Viral Hook & Curiosity Detection"
                                detail = f"Scanning transcript dialogue for opening hooks, punchlines, controversial takes & emotional peaks..."
                                step_prog = min(72, 55 + int((elapsed - 20) * 0.85))
                            elif elapsed < 65:
                                stage = "Coherence & Sentence Boundary Snapping"
                                detail = f"Ensuring clip candidates start and end naturally on sentence boundaries without mid-word cuts..."
                                step_prog = min(85, 72 + int((elapsed - 40) * 0.52))
                            elif elapsed < 80:
                                stage = "Virality Scoring & Selection"
                                detail = f"Calculating virality coefficients (1-100) and selecting the top {clip_range} highest potential clips..."
                                step_prog = min(92, 85 + int((elapsed - 65) * 0.46))
                            else:
                                stage = "Social Media Metadata Synthesis"
                                detail = f"Drafting attention-grabbing titles, social captions, and targeted hashtags ({elapsed}s)..."
                                step_prog = min(96, 92 + min(4, int((elapsed - 80) * 0.4)))

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

                            if elapsed > 90:
                                task.cancel()
                                logger.warning(f"Model {model_name} execution timed out (>90s). Advancing to fallback model...")
                                last_error = f"{model_name} execution timed out (>90s)"
                                break
                
                    if task.cancelled():
                        continue

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
                        clean_err = sanitize_sensitive_data(str(error_to_report))
                        logger.error(f"Gemini error after all fallback models: {clean_err}")
                        yield _sse({
                            "error": f"AI analysis failed across all available Flash models ({clean_err}). Please change your Gemini API key or try again in a few moments.",
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
        except asyncio.CancelledError:
            logger.info('Client disconnected from SSE analyze stream.')
            return
        except Exception as exc:
            logger.error(f'Unhandled error in analyze stream: {exc}', exc_info=True)
            yield _sse({'error': f'Internal Server Error: {str(exc)}', 'status': 500})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

