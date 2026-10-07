import json

def _sse(data: dict) -> str:
    """Format a dict as a Server-Sent Event string."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
