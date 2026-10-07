import os
import sys
import logging
from pathlib import Path

# Windows Python 3.14 compatibility hotfix for unix RTLD flags and uname used in yt-dlp plugins
for flag in ('RTLD_LAZY', 'RTLD_NOW', 'RTLD_GLOBAL', 'RTLD_LOCAL', 'RTLD_NODELETE', 'RTLD_NOLOAD', 'RTLD_DEEPBIND'):
    if not hasattr(os, flag):
        setattr(os, flag, 1)

if not hasattr(os, 'uname'):
    from collections import namedtuple
    UnameResult = namedtuple('UnameResult', ['sysname', 'nodename', 'release', 'version', 'machine'])
    os.uname = lambda: UnameResult('Windows', 'localhost', '10', '10.0', 'AMD64')

from dotenv import load_dotenv

# Path & Environment safety utilities
def get_system_path() -> str:
    """Safely retrieves system PATH regardless of environment variable case."""
    return os.environ.get("PATH") or os.environ.get("Path") or ""

def prepend_to_system_path(*directories):
    """Safely prepends existing directories to system PATH without duplicates."""
    current = get_system_path()
    parts = current.split(os.pathsep) if current else []
    for d in reversed(directories):
        if not d:
            continue
        p_str = str(Path(d).resolve()) if isinstance(d, (str, Path)) else str(d)
        if os.path.exists(p_str) and p_str not in parts:
            parts.insert(0, p_str)
    new_path = os.pathsep.join(parts)
    os.environ["PATH"] = new_path
    os.environ["Path"] = new_path

# Ensure python executable directory and Scripts are in PATH
try:
    py_dir = Path(sys.executable).parent
    prepend_to_system_path(py_dir, py_dir / "Scripts", py_dir / "bin")
except Exception:
    pass

# Automatically load environment variables from backend/.env or root .env
_base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base_dir, ".env"))
load_dotenv(os.path.join(_base_dir, "..", ".env"))
load_dotenv()

ROOT_DIR = Path(_base_dir).parent

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cheat-clip-pro")


try:
    from backend.video_engine import (
        TEMP_DIR,
        EXPORTS_DIR,
        FONTS_DIR,
        COOKIES_PATH,
        ROOT_COOKIES_PATH,
        get_effective_cookies_path,
        ACTIVE_ENCODER_NAME,
        ACTIVE_ENCODER_ARGS,
        detect_hardware_support,
        is_valid_mp4,
        download_clip_segment,
        download_full_raw_video,
        transcribe_clip_words,
        has_emoji,
        render_title_overlay_png,
        generate_ass_file,
        render_clip_to_mp4,
        extract_clip_frame,
        detect_speaker_face_box,
        get_video_file_metadata,
        compute_audio_energy_heatmap,
        transcribe_local_video_file,
    )
except ImportError:
    from video_engine import (
        TEMP_DIR,
        EXPORTS_DIR,
        FONTS_DIR,
        COOKIES_PATH,
        ROOT_COOKIES_PATH,
        get_effective_cookies_path,
        ACTIVE_ENCODER_NAME,
        ACTIVE_ENCODER_ARGS,
        detect_hardware_support,
        is_valid_mp4,
        download_clip_segment,
        download_full_raw_video,
        transcribe_clip_words,
        has_emoji,
        render_title_overlay_png,
        generate_ass_file,
        render_clip_to_mp4,
        extract_clip_frame,
        detect_speaker_face_box,
        get_video_file_metadata,
        compute_audio_energy_heatmap,
        transcribe_local_video_file,
    )

UPLOADS_DIR = TEMP_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
