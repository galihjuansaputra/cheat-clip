import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

import requests
import yt_dlp

from backend.config import TEMP_DIR, UPLOADS_DIR, get_video_file_metadata, is_valid_mp4, logger


def is_google_drive_url(url: str) -> bool:
    """Checks whether the given URL is a Google Drive share or file link."""
    if not url:
        return False
    u = url.strip()
    return bool(re.search(r'(?:drive\.google\.com|docs\.google\.com|drive\.usercontent\.google\.com)', u))


def extract_google_drive_file_id(url: str) -> Optional[str]:
    """Extracts the Google Drive file ID from various URL formats."""
    if not url:
        return None
    u = url.strip()

    patterns = [
        r'/file/d/([a-zA-Z0-9_-]{20,})',
        r'[?&]id=([a-zA-Z0-9_-]{20,})',
        r'drive\.google\.com/uc\?.*id=([a-zA-Z0-9_-]{20,})',
        r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]{20,})',
        r'docs\.google\.com/.*[?&]id=([a-zA-Z0-9_-]{20,})',
    ]
    for pattern in patterns:
        m = re.search(pattern, u)
        if m:
            return m.group(1)

    # In case the user pasted a raw Google Drive file ID
    if re.match(r'^[a-zA-Z0-9_-]{25,45}$', u):
        return u

    return None


def download_google_drive_video(
    url_or_id: str,
    on_progress: Optional[Callable[[str, str, int], None]] = None
) -> Path:
    """
    Downloads a video file from a Google Drive share link into UPLOADS_DIR.
    1. Checks if a file for this ID is already cached.
    2. Attempts yt-dlp GoogleDrive extractor (merges best video + audio).
    3. Falls back to direct HTTP chunked streaming download with confirmation handling.
    Returns the Path to the local video file.
    """
    file_id = extract_google_drive_file_id(url_or_id)
    if not file_id:
        raise ValueError("Could not extract a valid Google Drive file ID from the link. Please provide a link formatted like: https://drive.google.com/file/d/...")

    # 1. Cache check: Has this Google Drive file already been downloaded?
    existing = list(UPLOADS_DIR.glob(f"gdrive_{file_id}*.*"))
    for f in existing:
        if f.exists() and f.stat().st_size > 1024 * 1024:
            logger.info(f"Using cached Google Drive video: {f.name} ({f.stat().st_size} bytes)")
            if on_progress:
                on_progress("Video Found in Cache", f"Using cached Google Drive video: {f.name}", 100)
            return f

    target_filename = f"gdrive_{file_id}_{int(time.time())}.mp4"
    out_path = UPLOADS_DIR / target_filename

    if on_progress:
        on_progress("Connecting to Google Drive", "Requesting video stream from Google Drive...", 10)

    # 2. Strategy A: yt-dlp GoogleDrive extractor
    yt_success = False
    try:
        def ydl_hook(d):
            if d.get("status") == "downloading" and on_progress:
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                if total > 0:
                    pct = int(min(90, 10 + (downloaded / total) * 80))
                    mb_down = downloaded / (1024 * 1024)
                    mb_tot = total / (1024 * 1024)
                    on_progress("Downloading Video", f"Downloading from Google Drive: {mb_down:.1f} MB / {mb_tot:.1f} MB ({pct}%)", pct)
                else:
                    mb_down = downloaded / (1024 * 1024)
                    on_progress("Downloading Video", f"Downloading from Google Drive: {mb_down:.1f} MB...", 40)

        # Clean output template to avoid .mp4.mp4 double extension
        out_template = str(UPLOADS_DIR / f"gdrive_{file_id}_%(title).50B.%(ext)s")
        ydl_opts = {
            "outtmpl": out_template,
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "progress_hooks": [ydl_hook],
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://drive.google.com/file/d/{file_id}/view"])

        candidates = list(UPLOADS_DIR.glob(f"*gdrive_{file_id}*.*")) + [f for f in UPLOADS_DIR.iterdir() if file_id in f.name]
        for c in candidates:
            if c.exists() and c.stat().st_size > 1024 * 1024:
                # Fix double .mp4.mp4 extension if present
                if c.name.endswith(".mp4.mp4"):
                    fixed_name = c.name[:-4]
                    fixed_path = c.parent / fixed_name
                    try:
                        c.rename(fixed_path)
                        c = fixed_path
                    except Exception:
                        pass
                logger.info(f"yt-dlp successfully downloaded Google Drive video: {c.name}")
                if on_progress:
                    on_progress("Download Complete", f"Google Drive video ready: {c.name}", 100)
                return c
    except Exception as e:
        logger.warning(f"yt-dlp could not download Google Drive video ({e}). Switching to direct HTTP downloader...")

    # 3. Strategy B: Direct HTTP streaming download with confirmation handling
    try:
        if on_progress:
            on_progress("Direct Streaming", "Downloading video file directly from Google Drive...", 20)

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

        download_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        resp = session.get(download_url, stream=True, timeout=35)

        # Handle virus scan warning token for files > 100MB
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            confirm_token = None
            for k, v in session.cookies.items():
                if k.startswith("download_warning"):
                    confirm_token = v
                    break
            if not confirm_token:
                m = re.search(r'confirm=([0-9A-Za-z_]+)', resp.text)
                if m:
                    confirm_token = m.group(1)

            if confirm_token:
                download_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm={confirm_token}"
                resp = session.get(download_url, stream=True, timeout=35)
            else:
                download_url2 = f"https://docs.google.com/uc?export=download&id={file_id}&confirm=t"
                resp = session.get(download_url2, stream=True, timeout=35)

        # Detect original filename if available
        disposition = resp.headers.get("Content-Disposition", "")
        if disposition:
            fn_match = re.search(r'filename="?([^";]+)"?', disposition)
            if fn_match:
                extracted_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', fn_match.group(1))
                if not extracted_name.endswith(".mp4") and not extracted_name.endswith(".mov") and not extracted_name.endswith(".mkv"):
                    extracted_name += ".mp4"
                out_path = UPLOADS_DIR / f"gdrive_{file_id}_{extracted_name}"

        total_bytes = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        chunk_size = 1024 * 1024  # 1MB chunks

        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total_bytes > 0:
                        pct = int(min(95, 20 + (downloaded / total_bytes) * 75))
                        mb_down = downloaded / (1024 * 1024)
                        mb_tot = total_bytes / (1024 * 1024)
                        on_progress("Downloading Video", f"Downloading: {mb_down:.1f} MB / {mb_tot:.1f} MB ({pct}%)", pct)
                    elif on_progress:
                        mb_down = downloaded / (1024 * 1024)
                        on_progress("Downloading Video", f"Downloading: {mb_down:.1f} MB...", 45)

        if out_path.exists() and out_path.stat().st_size > 500 * 1024:
            logger.info(f"Direct HTTP download completed for Google Drive video: {out_path.name} ({out_path.stat().st_size} bytes)")
            if on_progress:
                on_progress("Download Complete", f"Google Drive video ready: {out_path.name}", 100)
            return out_path
        else:
            raise RuntimeError(
                "The downloaded file was too small or empty. Please ensure the Google Drive file permission is set to 'Anyone with the link can view'."
            )
    except Exception as e:
        if out_path.exists():
            try:
                out_path.unlink()
            except Exception:
                pass
        raise RuntimeError(
            f"Failed to download Google Drive video. Please verify the link is accessible ('Anyone with the link can view'). Details: {str(e)}"
        )
