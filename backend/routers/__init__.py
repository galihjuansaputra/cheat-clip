from backend.routers.analyze import router as analyze_router
from backend.routers.cookies import router as cookies_router
from backend.routers.system import router as system_router

__all__ = [
    "analyze_router",
    "cookies_router",
    "system_router",
]
