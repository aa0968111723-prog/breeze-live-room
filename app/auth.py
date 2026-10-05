from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def new_host_token() -> str:
    return secrets.token_urlsafe(32)


def client_host(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host or ""


def require_local_host(request: Request, allow_testclient: bool) -> None:
    host = client_host(request)
    if host in LOCAL_HOSTS:
        return
    if allow_testclient and host == "testclient":
        return
    raise HTTPException(status_code=403, detail="請在主持這台電腦的瀏覽器開啟，不要從別的裝置拿主持權杖")


def require_host(request: Request, token: str) -> None:
    header = request.headers.get("authorization", "")
    supplied = ""
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="沒有主持權限")
    origin = request.headers.get("origin")
    if origin and not _origin_ok(origin, request):
        raise HTTPException(status_code=403, detail="這個頁面不能操作主持端")


def _origin_ok(origin: str, request: Request) -> bool:
    origin = origin.rstrip("/")
    host = request.headers.get("host", "")
    allowed = {
        f"http://127.0.0.1:{request.url.port}",
        f"http://localhost:{request.url.port}",
        "http://127.0.0.1",
        "http://localhost",
    }
    if host:
        allowed.add(f"http://{host}")
        allowed.add(f"https://{host}")
    if request.url.port:
        allowed.add(f"http://127.0.0.1:{request.url.port}")
        allowed.add(f"http://localhost:{request.url.port}")
    return origin in allowed or origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")
