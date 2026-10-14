"""Download caller media and upload it through an authenticated Nim browser."""

from __future__ import annotations

import base64
import ipaddress
import mimetypes
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests



MAX_MEDIA_BYTES = 200 * 1024 * 1024
MIME_EXTENSIONS = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "image/jpeg": ".jpg",
                   "image/png": ".png", "video/mp4": ".mp4", "video/quicktime": ".mov"}
_MEDIA_ROUTE_LOCK = threading.RLock()
_DIRECT_MEDIA_UNTIL: dict[str, float] = {}
_DIRECT_FIRST_MEDIA_HOSTS: frozenset[str] = frozenset()


class MediaError(Exception):
    pass


def _allow_direct_media_fallback(host: str | None) -> bool:
    """Keep browser/upstream and local URLs on the account proxy."""
    if not host or host == "nim.video" or host.endswith(".nim.video"):
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


def _download(source: str, kind: str, proxy_url: str = "") -> tuple[bytes, str, str]:
    if source.startswith("data:"):
        header, separator, payload = source.partition(",")
        if not separator or ";base64" not in header:
            raise MediaError("media data URL must be base64 encoded")
        mime = header[5:].split(";", 1)[0].lower()
        try:
            data = base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise MediaError("media data URL contains invalid base64") from exc
        filename = kind + MIME_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or ".bin")
    elif source.startswith(("https://", "http://")):
        parsed = urlsplit(source)
        if not parsed.hostname or parsed.username or parsed.password:
            raise MediaError("media URL is invalid")
        if not proxy_url:
            raise MediaError("account proxy is required for media download")
        # Requests' socks5h form resolves the destination through the same
        # account proxy that Chromium uses for Nim traffic.
        proxy = proxy_url.replace("socks5://", "socks5h://", 1)
        account_route = {"http": proxy, "https": proxy}
        routes = [("account proxy", account_route), ("account proxy", account_route)]
        # Caller-owned public media can use a direct route if the account proxy
        # repeatedly fails; Nim and local hosts never use that route.
        direct_allowed = _allow_direct_media_fallback(parsed.hostname)
        direct_first = False
        if direct_allowed:
            with _MEDIA_ROUTE_LOCK:
                direct_first = (parsed.hostname in _DIRECT_FIRST_MEDIA_HOSTS or
                                _DIRECT_MEDIA_UNTIL.get(parsed.hostname, 0) > time.monotonic())
            direct_route = ("direct media fallback", {})
            # A short direct-route interruption must not leave a caller-owned
            # media host at the mercy of a consistently timing-out proxy.
            routes = ([direct_route] + routes + [direct_route]) if direct_first else (routes + [direct_route])
        for index, (route_name, route_proxy) in enumerate(routes):
            session = requests.Session()
            session.trust_env = False
            try:
                with session.get(source, timeout=(10, 45) if route_proxy else (10, 60), stream=True,
                                 proxies=route_proxy,
                                 allow_redirects=bool(route_proxy),
                                 headers={"User-Agent": "Mozilla/5.0"}) as response:
                    if not route_proxy and 300 <= response.status_code < 400:
                        raise MediaError("direct media fallback redirected; account proxy is required")
                    response.raise_for_status()
                    chunks = []
                    total = 0
                    for chunk in response.iter_content(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_MEDIA_BYTES:
                            raise MediaError("reference media exceeds 200 MB")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    mime = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].lower()
                    filename = Path(parsed.path).name or kind + MIME_EXTENSIONS.get(mime, ".bin")
                    if direct_allowed:
                        with _MEDIA_ROUTE_LOCK:
                            if route_proxy:
                                _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                            else:
                                _DIRECT_MEDIA_UNTIL[parsed.hostname] = time.monotonic() + 120
                    break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                if direct_allowed and not route_proxy:
                    with _MEDIA_ROUTE_LOCK:
                        _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                if index == len(routes) - 1:
                    raise MediaError(f"media download failed via {route_name}: {type(exc).__name__}") from exc
                time.sleep(0.5 * (index + 1))
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if direct_allowed and not route_proxy and index < len(routes) - 1:
                    with _MEDIA_ROUTE_LOCK:
                        _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                    continue
                if status not in {429, 500, 502, 503, 504} or index == len(routes) - 1:
                    raise MediaError(f"media download failed via {route_name}: HTTP {status}") from exc
                time.sleep(0.5 * (index + 1))
            except requests.RequestException as exc:
                if direct_allowed and not route_proxy and index < len(routes) - 1:
                    with _MEDIA_ROUTE_LOCK:
                        _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                    continue
                raise MediaError(f"media download failed via {route_name}: {type(exc).__name__}") from exc
            except MediaError as exc:
                if direct_first and index < len(routes) - 1 and "direct media fallback redirected" in str(exc):
                    with _MEDIA_ROUTE_LOCK:
                        _DIRECT_MEDIA_UNTIL.pop(parsed.hostname, None)
                    continue
                raise
            finally:
                session.close()
    else:
        raise MediaError("media must be an HTTPS URL, HTTP URL, data URL, or Nim asset ID")
    if not data:
        raise MediaError("reference media is empty")
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaError("reference media exceeds 200 MB")
    if not mime.startswith({"images": "image/", "videos": "video/", "audios": "audio/"}[kind]):
        guessed = mimetypes.guess_type(filename)[0] or ""
        if not guessed.startswith({"images": "image/", "videos": "video/", "audios": "audio/"}[kind]):
            raise MediaError(f"{kind} reference has the wrong content type")
        mime = guessed
    return data, mime, filename
