import html
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

import requests
import yt_dlp
from fastapi import HTTPException
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import JSONFormatter

from backend.config import get_effective_cookies_path, logger
from backend.utils.proxy import (
    TimeoutSession,
    _shared_cookie_jar,
    create_http_client,
    get_proxy_url,
    get_youtube_transcript_proxy_config,
)
from backend.utils.text import extract_video_id


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


def prioritize_transcripts(transcripts: list) -> list:
    """
    Sorts transcript tracks ensuring Indonesian (id) and English (en) are strictly prioritized
    over all other languages (preventing unintended Arabic, Russian, or translated tracks):
    1. Indonesian manual tracks ('id', 'in', 'id-id')
    2. Indonesian auto-generated tracks
    3. English manual tracks ('en', 'en-us', 'en-gb')
    4. English auto-generated tracks
    5. Native ASR generated track in video's original spoken language
    6. Other manual original tracks
    """
    def rank_track(t):
        code = (getattr(t, 'language_code', '') or '').lower()
        is_gen = getattr(t, 'is_generated', False)
        # Indonesian
        if code in ('id', 'in', 'id-id') or code.startswith('id-'):
            return 0 if not is_gen else 1
        # English
        if code in ('en', 'en-us', 'en-gb', 'en-ca') or code.startswith('en-'):
            return 2 if not is_gen else 3
        # Native ASR track
        if is_gen:
            return 4
        # Other manual track
        return 5

    return sorted(transcripts, key=rank_track)


def fetch_transcript_cli(
    video_id: str,
    priority_langs: Optional[List[str]] = None,
    proxy_url: Optional[str] = None,
    custom_proxy: Optional[str] = None,
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

    ws_user = os.environ.get("WEBSHARE_USERNAME", "").strip()
    ws_pass = os.environ.get("WEBSHARE_PASSWORD", "").strip()
    effective_proxy = custom_proxy or proxy_url or get_proxy_url()

    if ws_user and ws_pass and not custom_proxy:
        cmd.extend(["--webshare-proxy-username", ws_user, "--webshare-proxy-password", ws_pass])
    elif effective_proxy:
        cmd.extend(["--http-proxy", effective_proxy, "--https-proxy", effective_proxy])

    logger.info(f"Executing CLI transcript extraction for {video_id}...")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            raw = json.loads(proc.stdout)
            items = raw[0] if isinstance(raw, list) and len(raw) > 0 and isinstance(raw[0], list) else raw
            result = normalize_transcript(items)
            if result:
                logger.info(f"Transcript fetched via CLI fallback: {len(result)} lines")
                return result
        stderr_snippet = (proc.stderr or proc.stdout or "").strip()
        first_err_line = stderr_snippet.split("\n")[0] if stderr_snippet else f"exit code {proc.returncode}"
        raise Exception(f"CLI returned {proc.returncode}: {first_err_line}")
    except Exception as e:
        logger.warning(f"CLI transcript extraction failed: {e}")
        raise


def get_youtube_oembed_title(video_id_or_url: str) -> Optional[str]:
    """Fetches video title directly from YouTube's public oEmbed API.
    Fast (<300ms), requires no authentication or cookies, and works reliably when yt-dlp is blocked."""
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
        eff_cookies = get_effective_cookies_path()
        if eff_cookies:
            ydl_opts['cookiefile'] = str(eff_cookies)
        ydl_opts['extractor_args'] = {'youtube': {'player_client': ['default', 'web_embedded', 'ios']}}

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
                    "channel": info.get('channel') or info.get('uploader') or info.get('creator') or '',
                    "duration": float(info.get('duration') or 0.0),
                    "heatmap": info.get('heatmap') or [],
                    "is_live": bool(info.get('is_live') or False),
                    "live_status": info.get('live_status') or 'not_live'
                }
        except Exception as e:
            logger.warning(f"yt-dlp metadata extraction failed (proxy={'yes' if attempt_proxy else 'no'}): {e}")

    # Fallback: parse video ID and retrieve title from oEmbed API directly
    if video_id:
        fallback_title = get_youtube_oembed_title(video_id) or f"YouTube Video ({video_id})"
        return {
            "title": fallback_title,
            "channel": "",
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
    keys = re.findall(r'sd_[a-zA-Z0-9]+', raw)
    if not keys:
        keys = [k.strip('\"\' ') for k in re.split(r'[,\s\n]+', raw) if k.strip('\"\' ')]
    return keys

_supadata_usage_cache = {
    "data": None,
    "timestamp": 0
}
_CACHE_TTL_SECONDS = 30


def fetch_transcript_supadata(video_id: str, error_collector: Optional[List[str]] = None) -> List[dict]:
    """Fetches transcript from Supadata API, rotating through available keys if rate limits/quotas occur."""
    global _supadata_key_index, _supadata_usage_cache
    keys = get_supadata_keys()
    if not keys:
        if error_collector is not None:
            error_collector.append("Supadata API: No keys configured (SUPADATA_API_KEYS is empty in .env)")
        return []

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


def check_single_supadata_key(key: str, index: int) -> dict:
    masked = f"{key[:7]}...{key[-4:]}" if len(key) >= 11 else "***"
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
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'proxy': proxy,
        'socket_timeout': 10
    }
    eff_cookies = get_effective_cookies_path()
    if eff_cookies:
        ydl_opts['cookiefile'] = str(eff_cookies)
    ydl_opts['extractor_args'] = {'youtube': {'player_client': ['default', 'web_embedded', 'ios']}}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if not info:
                return []
            
            subtitles = info.get('subtitles') or {}
            auto_subtitles = info.get('automatic_captions') or {}
            
            # Identify the video's native audio language
            audio_lang = (info.get('audio_language') or info.get('language') or '').lower().split('-')[0]
            
            # Build priority: native language first, then whatever matches
            langs_to_try = []
            if audio_lang:
                langs_to_try.append(audio_lang)
            for l in ['en', 'id', 'es', 'pt', 'fr', 'de', 'ja', 'ko', 'zh-Hans', 'zh-Hant', 'ar', 'hi', 'ru']:
                if l not in langs_to_try:
                    langs_to_try.append(l)

            # Manual subtitles first, then auto captions in native language
            for lang_dict, is_auto in [(subtitles, False), (auto_subtitles, True)]:
                ordered_langs = [l for l in langs_to_try if l in lang_dict] + [l for l in lang_dict if l not in langs_to_try]
                for lang in ordered_langs:
                    formats = lang_dict.get(lang) or []
                    json3_entry = next((f['url'] for f in formats if f.get('ext') == 'json3'), None)
                    if json3_entry:
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

            # 2. List all transcripts and prioritize Indonesian and English
            try:
                all_transcripts = list(proxy_api.list(video_id))
                target_tracks = prioritize_transcripts(all_transcripts)
                
                for t in target_tracks:
                    try:
                        data = t.fetch()
                        res = normalize_transcript(data)
                        if res:
                            _shared_cookie_jar.update(client.cookies)
                            logger.info(f"[Tier 2] Transcript fetched via proxy Python API ({t.language_code} - {t.language}): {len(res)} lines")
                            return res
                    except Exception:
                        continue

                attempt_history.append(f"Tier 2 (Proxy Python API): No accessible track in {len(all_transcripts)} tracks")
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
                priority_langs=None,
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
            all_transcripts = list(direct_api.list(video_id))
            target_tracks = prioritize_transcripts(all_transcripts)
            
            for transcript in target_tracks:
                try:
                    data = transcript.fetch()
                    res = normalize_transcript(data)
                    if res:
                        _shared_cookie_jar.update(direct_client.cookies)
                        logger.info(f"[Tier 5] Transcript fetched via direct list ({transcript.language_code} - {transcript.language}): {len(res)} lines")
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
        direct_cli_data = fetch_transcript_cli(video_id, priority_langs=None, proxy_url=None, timeout=15)
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

    # ── All Tiers Exhausted: Construct Error ──────────────────────────────────
    all_supadata_keys_exhausted = bool(keys) and (
        any("configured API keys exhausted" in h or "Monthly Quota Exceeded" in h for h in attempt_history)
    )
    if not all_supadata_keys_exhausted and keys:
        try:
            usage = get_supadata_usage_data()
            if usage and usage.get("total_remaining", 1) == 0 and usage.get("total_limit", 0) > 0:
                all_supadata_keys_exhausted = True
        except Exception:
            pass

    if all_supadata_keys_exhausted:
        total_keys = len(keys)
        total_quota = total_keys * 100
        total_used = total_quota
        try:
            usage = get_supadata_usage_data()
            if usage and usage.get("total_limit"):
                total_quota = usage.get("total_limit", total_quota)
                total_used = usage.get("total_used", total_quota)
        except Exception:
            pass

        simplified_error = (
            f"Batas kuota transkrip otomatis telah habis ({total_used}/{total_quota} kuota terpakai bulan ini dari {total_keys} API key). "
            f"Seluruh metode fallback (Tier 1-7) juga tidak dapat mengambil subtitle otomatis dari YouTube. "
            f"Silakan gunakan opsi 'Unggah Subtitle Manual' dengan mengunduh berkas subtitle (.srt/.txt) secara gratis melalui DownSub.com."
        )
        logger.warning(f"Subtitle retrieval exhausted all methods with Supadata quota depleted:\n{simplified_error}")
        raise HTTPException(status_code=400, detail=simplified_error)

    combined_history = " ".join(attempt_history)
    root_cause = []
    if "TranscriptsDisabled" in combined_history:
        root_cause.append("Subtitles are disabled for this video by the creator.")
    elif "AgeRestricted" in combined_history:
        root_cause.append("Video is age-restricted and requires YouTube authentication.")
    elif "VideoUnavailable" in combined_history:
        root_cause.append("Video is private or unavailable.")
    elif "IpBlocked" in combined_history or "RequestBlocked" in combined_history:
        root_cause.append("YouTube blocked the IP address (datacenter IP ban / RequestBlocked).")

    error_lines = [
        f"Unable to retrieve subtitles for YouTube video ID '{video_id}'."
    ]
    if root_cause:
        error_lines.append(f"Probable Cause: {' '.join(root_cause)}")

    error_lines.append("\nMethods attempted and diagnostic results:")
    for h in attempt_history:
        error_lines.append(f"  • {h}")

    error_lines.append("\nRecommended solutions:")
    if not (proxy_cfg or proxy_url):
        error_lines.append("  1. Configure a proxy in backend/.env (e.g. WEBSHARE_USERNAME & WEBSHARE_PASSWORD, or WEBSHARE_PROXY / PROXY_URL) to bypass datacenter IP bans.")
    else:
        error_lines.append("  1. Verify your proxy quota/credentials in backend/.env or rotate your residential proxy IP.")
    error_lines.append("  2. If using Supadata, configure SUPADATA_API_KEYS in backend/.env for residential cloud extraction.")
    error_lines.append("  3. Upload custom subtitles manually (.srt or .txt file) using the 'Upload Custom Subtitle' setting above.")

    full_error_detail = "\n".join(error_lines)
    logger.error(f"Subtitle retrieval exhausted all {len(attempt_history)} methods:\n{full_error_detail}")
    raise HTTPException(status_code=400, detail=full_error_detail)
