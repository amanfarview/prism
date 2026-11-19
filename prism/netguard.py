"""Guarded HTTP client for fetching other people's websites (site audit, Amplience content lookups).

Why: a sitemap entry, a cited URL or a redirect on a site we audit decides what Prism fetches next. Without
a guard that can be steered at this VM's own services (Orbit, Flo, ... on 127.0.0.1), the private network,
or Azure's metadata endpoint (169.254.169.254, which hands out the VM identity's tokens).

Every request, including each redirect hop, must be http(s) on port 80/443 to a host that resolves only to
public addresses. systemd also blocks 169.254.169.254 for the Prism services (IPAddressDeny), so this is the
second of two locks. Provider API calls (OpenAI, Anthropic, ...) don't go through here: their hosts are fixed.
"""
from __future__ import annotations

import ipaddress
import socket

import httpx

from .config import get_settings

MAX_BYTES = 5 * 1024 * 1024
# public addresses that are still Azure plumbing: the WireServer / Azure DNS
BLOCKED_PUBLIC = {ipaddress.ip_address("168.63.129.16")}


class BlockedURL(httpx.RequestError):
    """Refused before connecting. Subclasses httpx.RequestError so existing `except httpx.HTTPError` handles it."""


def check_url(url: httpx.URL) -> None:
    if url.scheme not in ("http", "https"):
        raise BlockedURL(f"refused: scheme {url.scheme!r}")
    if url.port not in (None, 80, 443):
        raise BlockedURL(f"refused: port {url.port}")
    host = url.host
    if not host:
        raise BlockedURL("refused: no host")
    if not get_settings().block_private_fetch:
        return
    try:
        infos = socket.getaddrinfo(host, url.port or (443 if url.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise BlockedURL(f"refused: can't resolve {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip in BLOCKED_PUBLIC:
            raise BlockedURL(f"refused: {host} resolves to a non-public address")


def _on_request(request: httpx.Request) -> None:
    check_url(request.url)


def _on_response(response: httpx.Response) -> None:
    n = response.headers.get("content-length")
    if n and n.isdigit() and int(n) > MAX_BYTES:
        response.close()
        raise BlockedURL(f"refused: response of {int(n) // 1024} KB is over the {MAX_BYTES // 1024} KB limit")


def safe_client(timeout: float = 20, **kw) -> httpx.Client:
    return httpx.Client(follow_redirects=True, max_redirects=5, timeout=timeout,
                        event_hooks={"request": [_on_request], "response": [_on_response]}, **kw)


def safe_get(url: str, timeout: float = 10, **kw) -> httpx.Response:
    with safe_client(timeout=timeout) as c:
        return c.get(url, **kw)
