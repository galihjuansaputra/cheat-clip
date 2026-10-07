from typing import Optional
from pydantic import BaseModel

class RawVideoDownloadRequest(BaseModel):
    video_url: str
    video_id: str
    title: Optional[str] = None

class RawClipDownloadRequest(BaseModel):
    video_url: str
    video_id: str
    start_time: float
    end_time: float
    title: Optional[str] = None

class CookiesSaveRequest(BaseModel):
    cookies: Optional[str] = None
    cookies_content: Optional[str] = None
