import os
import requests
from urllib.parse import urlsplit
from typing import Optional
from youtube_transcript_api.proxies import WebshareProxyConfig, GenericProxyConfig
from backend.config import logger

class TimeoutSession(requests.Session):
    """requests.Session that enforces a default timeout to avoid hanging indefinitely on slow proxies."""
    def __init__(self, timeout: float = 12.0):
        super().__init__()
        self._default_timeout = timeout

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(*args, **kwargs)

def mask_proxy_url(proxy_url: Optional[str]) -> str:
    """Masks basic authentication username/password in a proxy URL for safe logging and error reporting."""
    if not proxy_url:
        return ""
    try:
        parsed = urlsplit(proxy_url)
        if parsed.username or parsed.password:
            user = parsed.username or "user"
            host = parsed.hostname or ""
            port_str = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{user}:***@{host}{port_str}"
        return proxy_url
    except Exception:
        return "<proxy_configured>"


def get_proxy_url() -> Optional[str]:
    """Retrieves proxy URL from environment variables or synthesizes from Webshare credentials."""
    proxy = (
        os.environ.get("PROXY_URL")
        or os.environ.get("WEBSHARE_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or ""
    ).strip()
    if proxy:
        return proxy

    # Synthesize URL from explicit Webshare credentials if provided
    ws_user = os.environ.get("WEBSHARE_USERNAME", "").strip()
    ws_pass = os.environ.get("WEBSHARE_PASSWORD", "").strip()
    if ws_user and ws_pass:
        ws_locations_raw = os.environ.get("WEBSHARE_LOCATIONS", "").strip()
        loc_suffix = "".join(f"-{loc.strip().upper()}" for loc in ws_locations_raw.split(",") if loc.strip())
        user_clean = ws_user[:-7] if ws_user.endswith("-rotate") else ws_user
        return f"http://{user_clean}{loc_suffix}-rotate:{ws_pass}@p.webshare.io:80"

    return None

def get_youtube_transcript_proxy_config(custom_proxy: Optional[str] = None):
    """
    Constructs a ProxyConfig (WebshareProxyConfig or GenericProxyConfig)
    for YouTubeTranscriptApi per official recommendations:
    https://github.com/jdepoix/youtube-transcript-api#working-around-ip-bans-requestblocked-or-ipblocked-exception
    """
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

# Shared Cookie Jar & HTTP Session Factory
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
    return session
