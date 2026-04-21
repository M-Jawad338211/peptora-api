import hashlib
from fastapi import Request


def extract_fingerprint(request: Request) -> str:
    """Extract device fingerprint from request headers or generate a fallback."""
    fp = request.headers.get("X-Device-Fingerprint")
    if fp:
        return fp[:255]
    # Fallback: hash of IP + User-Agent
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("User-Agent", "unknown")
    return hashlib.sha256(f"{ip}:{ua}".encode()).hexdigest()
