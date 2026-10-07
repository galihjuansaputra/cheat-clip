import os
import sys
import logging
from pathlib import Path
from typing import Optional

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
_base_dir = Path(__file__).resolve().parent
load_dotenv(os.path.join(_base_dir, ".env"))
load_dotenv(os.path.join(_base_dir.parent, ".env"))
load_dotenv()

ROOT_DIR = _base_dir.parent

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cheat-clip-pro")

# Standard paths
COOKIES_PATH = _base_dir / "cookies.txt"
ROOT_COOKIES_PATH = ROOT_DIR / "cookies.txt"
TEMP_DIR = _base_dir / "temp"

# Safe temp directory creation (e.g. in /tmp for Vercel / serverless if read-only)
try:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    import tempfile
    TEMP_DIR = Path(tempfile.gettempdir()) / "cheat_clip_temp"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def get_effective_cookies_path() -> Optional[Path]:
    """Returns path to the active cookies.txt file if present."""
    if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0:
        return COOKIES_PATH
    if ROOT_COOKIES_PATH.exists() and ROOT_COOKIES_PATH.stat().st_size > 0:
        return ROOT_COOKIES_PATH
    return None
