from backend.routers.analyze import router as analyze_router
from backend.routers.cookies import router as cookies_router
from backend.routers.downloads import router as downloads_router
from backend.routers.media import router as media_router
from backend.routers.render import router as render_router
from backend.routers.system import router as system_router

__all__ = [
    "analyze_router",
    "cookies_router",
    "downloads_router",
    "media_router",
    "render_router",
    "system_router",
]
