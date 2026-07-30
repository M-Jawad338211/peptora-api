from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def client_ip(request: Request) -> str:
    """Rate-limit key: the real client IP, not the nearest proxy.

    The API always sits behind at least one proxy (Railway), and the web app
    reaches it through a Next.js rewrite, so `request.client.host` is an
    infrastructure IP shared by every user. Keying on it would put all users
    in a single bucket and let them exhaust each other's limits.

    X-Forwarded-For is appended to by each hop, so the leftmost entry is the
    original client. That value is client-controlled and therefore spoofable;
    it is only used for rate limiting, never for auth or authorisation.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return get_remote_address(request)


limiter = Limiter(key_func=client_ip)
