import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from backend.config import (
    EXPORTS_DIR,
    TEMP_DIR,
    UPLOADS_DIR,
    FONTS_DIR,
    detect_speaker_face_box,
    extract_clip_frame,
    get_video_file_metadata,
    is_valid_mp4,
    logger,
)

router = APIRouter(tags=["Media"])


MAX_VIDEO_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024   # 4 GB
MAX_AUDIO_UPLOAD_BYTES = 100 * 1024 * 1024        # 100 MB
MAX_SFX_UPLOAD_BYTES = 50 * 1024 * 1024           # 50 MB
MAX_IMAGE_UPLOAD_BYTES = 25 * 1024 * 1024         # 25 MB
MAX_FONT_UPLOAD_BYTES = 50 * 1024 * 1024          # 50 MB


def _is_safe_path(target_path: Path) -> bool:
    """Ensures the resolved file path is strictly located within allowed media directories."""
    try:
        resolved = target_path.resolve()
        allowed_roots = [UPLOADS_DIR.resolve(), TEMP_DIR.resolve(), EXPORTS_DIR.resolve(), FONTS_DIR.resolve()]
        return any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots)
    except Exception:
        return False


async def _save_uploaded_file_chunked(file: UploadFile, save_path: Path, max_bytes: int) -> int:
    """
    Streams upload content in 1MB chunks to disk while enforcing a strict maximum size limit
    to prevent memory exhaustion (OOM) and disk flood attacks.
    """
    total_written = 0
    chunk_size = 1024 * 1024  # 1 MB
    try:
        with open(save_path, "wb") as f:
            while chunk := await file.read(chunk_size):
                total_written += len(chunk)
                if total_written > max_bytes:
                    f.close()
                    if save_path.exists():
                        save_path.unlink()
                    limit_mb = round(max_bytes / (1024 * 1024))
                    raise HTTPException(
                        status_code=413,
                        detail=f"Uploaded file exceeds the maximum allowed size limit of {limit_mb} MB."
                    )
                f.write(chunk)
        return total_written
    except HTTPException:
        raise
    except Exception as e:
        if save_path.exists():
            try:
                save_path.unlink()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Failed to save upload: {str(e)}")


@router.post("/api/upload-video")
async def upload_video(file: UploadFile = File(...)):
    """
    Handles local video file uploads (.mp4, .mov, .mkv, .webm, .avi, etc.).
    Extracts video metadata (duration, resolution, fps) and saves to uploads folder.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    
    ext = os.path.splitext(file.filename)[1].lower()
    allowed = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv"]
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported video format. Allowed: {', '.join(allowed)}")
    
    clean_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)
    unique_id = f"upload_{uuid.uuid4().hex[:10]}"
    unique_name = f"{unique_id}_{clean_name}"
    save_path = UPLOADS_DIR / unique_name
    
    try:
        bytes_written = await _save_uploaded_file_chunked(file, save_path, MAX_VIDEO_UPLOAD_BYTES)
        meta = await asyncio.to_thread(get_video_file_metadata, save_path)
        logger.info(f"Uploaded video '{file.filename}' -> saved as '{unique_name}' ({meta.get('duration')}s, {meta.get('width')}x{meta.get('height')})")
        
        return {
            "success": True,
            "video_id": unique_id,
            "filename": file.filename,
            "saved_name": unique_name,
            "file_path": str(save_path),
            "video_url": f"/api/video/{unique_name}",
            "duration": meta.get("duration", 0.0),
            "width": meta.get("width", 1920),
            "height": meta.get("height", 1080),
            "fps": meta.get("fps", 30.0),
            "has_audio": meta.get("has_audio", True),
            "size_bytes": bytes_written
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload video: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process video upload: {str(e)}")


def _find_video_file_on_disk(file_name: str) -> Optional[Path]:
    import urllib.parse
    clean_name = urllib.parse.unquote(os.path.basename(file_name.split("?")[0])).strip()
    
    # Check direct paths
    for base in [UPLOADS_DIR, TEMP_DIR, EXPORTS_DIR]:
        candidate = base / clean_name
        if candidate.exists() and candidate.is_file() and _is_safe_path(candidate):
            return candidate

    # Search directory listings without regex/glob pitfalls
    all_files: list[Path] = []
    for d in [UPLOADS_DIR, TEMP_DIR, EXPORTS_DIR]:
        if d.exists():
            all_files.extend([f for f in d.iterdir() if f.is_file() and _is_safe_path(f)])

    clean_lower = clean_name.lower()
    
    # 1. Exact case-insensitive match
    for f in all_files:
        if f.name.lower() == clean_lower:
            return f

    # 2. Match without extension
    stem_lower = os.path.splitext(clean_lower)[0]
    for f in all_files:
        if f.stem.lower() == stem_lower or f.stem.lower() == clean_lower:
            return f

    # 3. Match Google Drive or Upload ID substring
    drive_match = re.search(r'([a-zA-Z0-9_-]{20,})', clean_name)
    if drive_match:
        fid = drive_match.group(1)
        for f in all_files:
            if fid in f.name:
                return f

    upload_match = re.search(r'(upload_[a-zA-Z0-9]{6,})', clean_name)
    if upload_match:
        uid = upload_match.group(1)
        for f in all_files:
            if uid in f.name:
                return f

    # 4. Containment matching
    for f in all_files:
        if clean_lower in f.name.lower() or f.stem.lower() in clean_lower:
            return f

    return None


@router.get("/api/video/{file_name:path}")
def get_video_file(file_name: str, request: Request):
    """
    Streams local video files with HTTP byte ranges support (HTTP 206 Partial Content)
    for smooth seeking, immediate playback start, and previewing without loading the whole file.
    """
    file_path = _find_video_file_on_disk(file_name)
    if not file_path or not file_path.exists() or not _is_safe_path(file_path):
        raise HTTPException(status_code=404, detail="Video file not found")

    file_size = file_path.stat().st_size
    ext = os.path.splitext(file_path.name)[1].lower()
    media_type = "video/webm" if ext == ".webm" else "video/quicktime" if ext == ".mov" else "video/x-matroska" if ext == ".mkv" else "video/mp4"

    range_header = request.headers.get("range")
    if not range_header:
        # Full file streaming response with Accept-Ranges
        def iter_full():
            with open(file_path, "rb") as f:
                while chunk := f.read(1024 * 512):
                    yield chunk

        return StreamingResponse(
            iter_full(),
            status_code=200,
            media_type=media_type,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'inline; filename="{file_path.name}"'
            }
        )

    # Parse Range: bytes=start-end
    range_match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not range_match:
        return Response(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE)

    start = int(range_match.group(1))
    end = int(range_match.group(2)) if range_match.group(2) else file_size - 1

    if start >= file_size or end >= file_size or start > end:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"}
        )

    content_length = end - start + 1

    def iter_range():
        with open(file_path, "rb") as f:
            f.seek(start)
            bytes_left = content_length
            while bytes_left > 0:
                chunk_size = min(1024 * 512, bytes_left)
                data = f.read(chunk_size)
                if not data:
                    break
                bytes_left -= len(data)
                yield data

    return StreamingResponse(
        iter_range(),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Disposition": f'inline; filename="{file_path.name}"'
        }
    )


@router.post("/api/upload-bgm")
async def upload_bgm(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    ext = os.path.splitext(file.filename)[1].lower()
    allowed = [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"]
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported audio format. Allowed: {', '.join(allowed)}")
    
    clean_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)
    unique_name = f"bgm_{uuid.uuid4().hex[:8]}_{clean_name}"
    save_path = UPLOADS_DIR / unique_name
    
    try:
        bytes_written = await _save_uploaded_file_chunked(file, save_path, MAX_AUDIO_UPLOAD_BYTES)
        return {
            "success": True,
            "filename": file.filename,
            "saved_name": unique_name,
            "file_path": str(save_path),
            "url": f"/api/audio/{unique_name}",
            "size_bytes": bytes_written
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload BGM: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/audio/{file_name}")
def get_audio_file(file_name: str):
    clean_name = os.path.basename(file_name)
    file_path = UPLOADS_DIR / clean_name
    if not file_path.exists() or not _is_safe_path(file_path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    media_type = "audio/mpeg" if clean_name.endswith(".mp3") else "audio/wav" if clean_name.endswith(".wav") else "application/octet-stream"
    return FileResponse(file_path, media_type=media_type, filename=clean_name)


@router.post("/api/upload-sfx")
async def upload_hook_sfx(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    ext = os.path.splitext(file.filename)[1].lower()
    allowed = [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"]
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported audio format. Allowed: {', '.join(allowed)}")

    clean_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)
    unique_name = f"sfx_{uuid.uuid4().hex[:8]}_{clean_name}"
    save_path = UPLOADS_DIR / unique_name

    try:
        bytes_written = await _save_uploaded_file_chunked(file, save_path, MAX_SFX_UPLOAD_BYTES)
        return {
            "success": True,
            "filename": file.filename,
            "saved_name": unique_name,
            "file_path": str(save_path),
            "url": f"/api/audio/{unique_name}",
            "size_bytes": bytes_written
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload Hook SFX: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/upload-watermark")
async def upload_watermark(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    ext = os.path.splitext(file.filename)[1].lower()
    allowed = [".png", ".jpg", ".jpeg", ".webp", ".svg"]
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported image format. Allowed: {', '.join(allowed)}")
    
    clean_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)
    unique_name = f"wm_{uuid.uuid4().hex[:8]}_{clean_name}"
    save_path = UPLOADS_DIR / unique_name
    
    try:
        bytes_written = await _save_uploaded_file_chunked(file, save_path, MAX_IMAGE_UPLOAD_BYTES)
        return {
            "success": True,
            "filename": file.filename,
            "saved_name": unique_name,
            "file_path": str(save_path),
            "url": f"/api/watermark/{unique_name}",
            "size_bytes": bytes_written
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload watermark: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/watermark/{file_name}")
def get_watermark_file(file_name: str):
    clean_name = os.path.basename(file_name)
    file_path = UPLOADS_DIR / clean_name
    if not file_path.exists() or not _is_safe_path(file_path):
        raise HTTPException(status_code=404, detail="Watermark file not found")
    media_type = "image/png" if clean_name.endswith(".png") else "image/jpeg" if (clean_name.endswith(".jpg") or clean_name.endswith(".jpeg")) else "image/webp"
    return FileResponse(file_path, media_type=media_type, filename=clean_name)


@router.get("/api/clip-frame")
async def get_clip_frame(video_id: str, timestamp: float = 0.0, video_url: Optional[str] = None):
    """
    Returns an extracted real video frame at timestamp for the real video preview.
    Guarantees returning a real video frame, never a promotional thumbnail.
    """
    try:
        frame_path = await asyncio.to_thread(extract_clip_frame, video_url or "", video_id, timestamp)
        if frame_path and os.path.exists(frame_path):
            return FileResponse(frame_path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        logger.warning(f"Failed to serve extracted frame for {video_id}: {e}")

    raise HTTPException(status_code=404, detail="Real video frame could not be extracted yet")


@router.get("/api/detect-face")
async def detect_face(
    video_id: str,
    timestamp: float = 0.0,
    video_url: Optional[str] = None,
    facecam_position: Optional[str] = "auto",
    streamer_preset: Optional[str] = "none"
):
    """
    Detects speaker face coordinates (cx, cy, w, h) on the video at timestamp.
    Returns normalized coordinates and the frame URL.
    """
    is_streamer = (streamer_preset or "none") in ["split_top_cam", "pip_corner"]
    default_cx = 0.85 if is_streamer else 0.5
    default_cy = 0.78 if is_streamer else 0.35
    default_res = {
        "found": False,
        "cx": default_cx,
        "cy": default_cy,
        "w": 0.22,
        "h": 0.25,
        "frame_url": f"/api/clip-frame?video_id={video_id}&timestamp={timestamp}"
    }
    try:
        # First try to extract or get the cached frame
        frame_path = await asyncio.to_thread(extract_clip_frame, video_url or "", video_id, timestamp)
        if frame_path and os.path.exists(frame_path):
            box = await asyncio.to_thread(
                detect_speaker_face_box,
                frame_path,
                facecam_position or "auto",
                streamer_preset or "none"
            )
            box["frame_url"] = f"/api/clip-frame?video_id={video_id}&timestamp={timestamp}"
            return box

        # If frame extract didn't complete, check local candidates
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', video_id)
        local_candidates = list(TEMP_DIR.glob(f"*{safe_id}*.mp4")) + list(EXPORTS_DIR.glob(f"*{safe_id}*.mp4"))
        for candidate in local_candidates:
            if candidate.exists() and candidate.stat().st_size > 10000 and "slice_" not in candidate.name and is_valid_mp4(candidate):
                box = await asyncio.to_thread(
                    detect_speaker_face_box,
                    str(candidate),
                    facecam_position or "auto",
                    streamer_preset or "none"
                )
                box["frame_url"] = f"/api/clip-frame?video_id={video_id}&timestamp={timestamp}"
                return box
    except Exception as e:
        logger.warning(f"Face detection API error: {e}")

    return default_res


@router.post("/api/upload-font")
async def upload_font(file: UploadFile = File(...)):
    """
    Handles custom font file uploads (.ttf, .otf, .woff, .woff2).
    Saves to fonts directory so preview and libass / ffmpeg can immediately render with it.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    
    ext = os.path.splitext(file.filename)[1].lower()
    allowed = [".ttf", ".otf", ".woff", ".woff2"]
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported font format. Allowed: {', '.join(allowed)}")

    clean_stem = re.sub(r'[^a-zA-Z0-9_\-\s]', '_', os.path.splitext(file.filename)[0]).strip()
    if not clean_stem:
        clean_stem = f"CustomFont_{uuid.uuid4().hex[:6]}"
    
    saved_filename = f"{clean_stem}{ext}"
    save_path = FONTS_DIR / saved_filename

    try:
        bytes_written = await _save_uploaded_file_chunked(file, save_path, MAX_FONT_UPLOAD_BYTES)
        
        # Extract font family name using PIL ImageFont
        font_family = clean_stem
        try:
            from PIL import ImageFont
            loaded_f = ImageFont.truetype(str(save_path), 24)
            names = loaded_f.getname()
            if names and names[0]:
                font_family = str(names[0]).strip()
        except Exception as font_err:
            logger.info(f"Could not read font table name ({font_err}); using stem '{clean_stem}'")

        logger.info(f"Uploaded custom font: '{file.filename}' -> saved as '{saved_filename}' (Family: '{font_family}', {bytes_written} bytes)")

        return {
            "success": True,
            "font_name": font_family,
            "filename": saved_filename,
            "url": f"/api/font-file/{saved_filename}",
            "size_bytes": bytes_written
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload font: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save custom font: {str(e)}")


@router.get("/api/fonts")
def list_available_fonts():
    """
    Returns the complete list of built-in and uploaded custom fonts.
    """
    default_builtin = [
        "Outfit",
        "Montserrat",
        "Inter",
        "Impact",
        "Bebas Neue",
        "Anton",
        "Poppins",
        "Arial Black",
    ]
    fonts_map = {}
    for name in default_builtin:
        fonts_map[name.lower()] = {
            "name": name,
            "is_custom": False,
            "filename": None,
            "url": None
        }

    # Discover fonts on disk
    if FONTS_DIR.exists():
        for f in FONTS_DIR.iterdir():
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in [".ttf", ".otf", ".woff", ".woff2"]:
                continue
            # Skip emoji helper fonts
            if f.name.lower() in ["seguiemj.ttf", "notocoloremoji.ttf"]:
                continue

            font_family = f.stem
            try:
                from PIL import ImageFont
                loaded = ImageFont.truetype(str(f), 24)
                names = loaded.getname()
                if names and names[0]:
                    font_family = str(names[0]).strip()
            except Exception:
                pass

            key = font_family.lower()
            is_builtin = key in fonts_map and not fonts_map[key]["is_custom"]
            fonts_map[key] = {
                "name": font_family,
                "is_custom": not is_builtin,
                "filename": f.name,
                "url": f"/api/font-file/{f.name}"
            }

    return {"success": True, "fonts": list(fonts_map.values())}


@router.get("/api/font-file/{file_name}")
def get_font_file(file_name: str):
    """
    Serves font binary with correct MIME type for frontend FontFace and CSS loading.
    """
    clean_name = os.path.basename(file_name)
    file_path = FONTS_DIR / clean_name
    if not file_path.exists() or not _is_safe_path(file_path):
        raise HTTPException(status_code=404, detail="Font file not found")

    ext = os.path.splitext(clean_name)[1].lower()
    media_type = "font/woff2" if ext == ".woff2" else "font/woff" if ext == ".woff" else "font/otf" if ext == ".otf" else "font/ttf"
    return FileResponse(file_path, media_type=media_type, filename=clean_name)
