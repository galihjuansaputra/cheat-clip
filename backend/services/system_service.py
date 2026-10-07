import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from backend.config import COOKIES_PATH, ROOT_COOKIES_PATH, TEMP_DIR, _base_dir, logger


def get_dir_size_and_count(dir_path) -> Tuple[int, int]:
    p = Path(dir_path)
    total_bytes = 0
    total_files = 0
    if p.exists() and p.is_dir():
        for item in p.rglob("*"):
            if item.is_file():
                try:
                    total_bytes += item.stat().st_size
                    total_files += 1
                except Exception:
                    pass
    return total_files, total_bytes


def get_temp_storage_summary() -> dict:
    """Returns total files, bytes, and formatted size of temp download storage."""
    base_dir = Path(_base_dir)
    f1, b1 = get_dir_size_and_count(TEMP_DIR)
    f2, b2 = get_dir_size_and_count(base_dir / "temp")
    tot_files = f1 + f2
    tot_bytes = b1 + b2
    tot_mb = round(tot_bytes / (1024 * 1024), 2)
    formatted = f"{tot_mb} MB" if tot_mb < 1024 else f"{round(tot_mb / 1024, 2)} GB"
    return {
        "total_files": tot_files,
        "total_bytes": tot_bytes,
        "total_mb": tot_mb,
        "formatted_size": formatted
    }


def clear_temp_files() -> dict:
    """
    Clears all temporary downloaded video clips, audio slices, ASS files, and frames
    from TEMP_DIR and backend/temp. Re-creates empty directories.
    PROTECTED: cookies.txt and any cookie files are strictly PRESERVED and NEVER deleted.
    """
    base_dir = Path(_base_dir)
    cleared_files = 0
    cleared_bytes = 0

    PROTECTED_COOKIE_NAMES = {"cookies.txt", ".cookies", "youtube_cookies.txt", "cookie.txt"}

    def is_protected_cookie(p: Path) -> bool:
        if p.name.lower() in PROTECTED_COOKIE_NAMES:
            return True
        try:
            if COOKIES_PATH.exists() and p.resolve() == COOKIES_PATH.resolve():
                return True
        except Exception:
            pass
        return False

    target_dirs = [TEMP_DIR, base_dir / "temp"]
    for d in target_dirs:
        if d.exists() and d.is_dir():
            for item in list(d.iterdir()):
                try:
                    if is_protected_cookie(item):
                        logger.info(f"Preserving protected cookie file: {item}")
                        continue

                    if item.is_file() or item.is_symlink():
                        sz = item.stat().st_size
                        item.unlink()
                        cleared_files += 1
                        cleared_bytes += sz
                    elif item.is_dir():
                        has_cookie = False
                        for sub in list(item.rglob("*")):
                            if is_protected_cookie(sub):
                                has_cookie = True
                                logger.info(f"Preserving protected cookie file inside folder: {sub}")
                                continue
                            if sub.is_file() or sub.is_symlink():
                                try:
                                    cleared_files += 1
                                    cleared_bytes += sub.stat().st_size
                                    sub.unlink()
                                except Exception:
                                    pass
                        if not has_cookie:
                            shutil.rmtree(item, ignore_errors=True)
                except Exception as e:
                    logger.warning(f"Could not delete temp item {item}: {e}")
        d.mkdir(parents=True, exist_ok=True)

    # Ensure frames directory inside TEMP_DIR exists
    (TEMP_DIR / "frames").mkdir(parents=True, exist_ok=True)

    cleared_mb = round(cleared_bytes / (1024 * 1024), 2)
    formatted = f"{cleared_mb} MB" if cleared_mb < 1024 else f"{round(cleared_mb / 1024, 2)} GB"
    cookies_present = bool(COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0)
    logger.info(f"Cleared temp folder: {cleared_files} files, {formatted} (Cookies preserved: {cookies_present})")
    return {
        "success": True,
        "cleared_files": cleared_files,
        "cleared_bytes": cleared_bytes,
        "cleared_mb": cleared_mb,
        "cookies_preserved": True,
        "has_cookies": cookies_present,
        "message": f"Successfully cleared {cleared_files} temporary files ({formatted}). Stored YouTube cookies preserved."
    }


def cleanup_expired_temp_files(max_age_hours: int = 48) -> dict:
    """
    Cleans up temporary video frames, audio slices, and ASS files that are older than max_age_hours.
    Strictly preserves cookies.txt and active files.
    """
    import time
    base_dir = Path(_base_dir)
    cutoff_time = time.time() - (max_age_hours * 3600)
    cleaned_count = 0
    cleaned_bytes = 0

    PROTECTED_COOKIE_NAMES = {"cookies.txt", ".cookies", "youtube_cookies.txt", "cookie.txt"}

    for target in [TEMP_DIR, base_dir / "temp"]:
        if not target.exists() or not target.is_dir():
            continue
        for item in list(target.rglob("*")):
            if item.is_file() and item.name.lower() not in PROTECTED_COOKIE_NAMES:
                try:
                    mtime = item.stat().st_mtime
                    if mtime < cutoff_time:
                        sz = item.stat().st_size
                        item.unlink()
                        cleaned_count += 1
                        cleaned_bytes += sz
                except Exception:
                    pass

    return {
        "success": True,
        "cleaned_files": cleaned_count,
        "cleaned_bytes": cleaned_bytes,
        "max_age_hours": max_age_hours
    }


def find_git_executable() -> str:
    if shutil.which("git"):
        return "git"
    candidates = [
        Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("USERPROFILE", "")) / "scoop" / "apps" / "git" / "current" / "bin" / "git.exe",
        Path(os.environ.get("USERPROFILE", "")) / "scoop" / "shims" / "git.exe",
    ]
    for c in candidates:
        if c and c.exists():
            return str(c.resolve())
    return "git"


def run_git_command(args: List[str], cwd: Optional[Path] = None, timeout: int = 15) -> Tuple[int, str, str]:
    """Runs a git command safely and returns (returncode, stdout, stderr)."""
    target_cwd = cwd or Path(_base_dir).parent
    git_bin = find_git_executable()
    try:
        proc = subprocess.run(
            [git_bin] + args,
            cwd=str(target_cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def get_current_git_info() -> dict:
    """Retrieves current commit, branch, and remote URL information."""
    root_dir = Path(_base_dir).parent
    rc, commit_info, _ = run_git_command(["log", "-1", "--pretty=format:%h|%H|%s|%cd", "--date=short"], cwd=root_dir)
    rc_branch, branch, _ = run_git_command(["branch", "--show-current"], cwd=root_dir)
    rc_remote, remote_url, _ = run_git_command(["remote", "get-url", "origin"], cwd=root_dir)

    commit_hash = "unknown"
    commit_full = ""
    commit_msg = "Unknown commit"
    commit_date = ""
    if rc == 0 and commit_info:
        parts = commit_info.split("|")
        if len(parts) >= 4:
            commit_hash = parts[0]
            commit_full = parts[1]
            commit_msg = parts[2]
            commit_date = parts[3]

    return {
        "current_commit": commit_hash,
        "current_commit_full": commit_full,
        "commit_message": commit_msg,
        "commit_date": commit_date,
        "branch": branch if rc_branch == 0 and branch else "master",
        "remote_url": remote_url if rc_remote == 0 and remote_url else "https://github.com/galihjuansaputra/cheat-clip-pro.git"
    }


def trigger_detached_restart(delay: float = 2.5):
    """Launches backend/restart_runner.py in a fully detached background process."""
    root_dir = Path(_base_dir).parent
    runner_script = root_dir / "backend" / "restart_runner.py"
    
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == 'nt' else 0
    subprocess.Popen(
        [sys.executable, str(runner_script), "--delay", str(delay), "--cwd", str(root_dir)],
        cwd=str(root_dir),
        creationflags=flags,
        start_new_session=True if os.name != 'nt' else False,
        close_fds=True
    )
    logger.info(f"Detached restart runner spawned with delay={delay}s")
