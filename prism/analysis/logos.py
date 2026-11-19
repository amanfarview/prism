"""Company logos for the dashboards: each tracked company's own site icon, fetched once and served by Prism.

Free (plain web requests, no AI). The worker fills in missing logos every few hours; Command Centre > Brands can
fetch again or take an upload. Every request goes through prism/netguard.py (public addresses only, re-checked on
each redirect), like the site audit.

Safety: only raster images (PNG, JPEG, GIF, WebP, ICO) are kept, recognised by their first bytes rather than by what
the site claims. SVG is refused: it can carry script, and the logo URL is on the same site as Orbit and Flo.
"""
from __future__ import annotations

import html as htmllib
import logging
import re
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select

from ..models import Brand, Competitor, SiteLogo, now
from ..netguard import safe_client

log = logging.getLogger("prism")
MAX_IMAGE = 512 * 1024
MAX_PAGE = 1024 * 1024
RETRY_AFTER = timedelta(days=7)  # a site with nothing usable is tried again after a week
UA = "Mozilla/5.0 (compatible; FarviewPrism/1.0; logo fetch)"
LINK = re.compile(r"<link\b[^>]*>", re.I)
ATTR = re.compile(r'([a-zA-Z:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))')


def normalise(domain: str | None) -> str:
    """'https://www.Amplience.com/x' -> 'amplience.com'. Empty when there's no usable host."""
    d = (domain or "").strip().lower()
    if not d:
        return ""
    if "//" not in d:
        d = "//" + d
    host = (urlparse(d).hostname or "").strip(".")
    host = host[4:] if host.startswith("www.") else host
    return host if re.fullmatch(r"[a-z0-9.-]{3,200}", host or "") and "." in host else ""


def sniff(data: bytes) -> str | None:
    """Content type from the file's first bytes; None for anything that isn't a raster image we accept."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    return None


def _attrs(tag: str) -> dict:
    return {m.group(1).lower(): htmllib.unescape(m.group(3) or m.group(4) or m.group(5) or "") for m in ATTR.finditer(tag)}


def candidates(page: str, page_url: str) -> list[str]:
    """Icon URLs declared by the page, best first: apple-touch-icon (usually 180 px), then the largest other icon.
    SVG icons are skipped. The conventional /apple-touch-icon.png and /favicon.ico come last."""
    scored = []
    for tag in LINK.findall(page or ""):
        a = _attrs(tag)
        rel, href = a.get("rel", "").lower(), a.get("href", "").strip()
        if not href or "icon" not in rel or "mask-icon" in rel:
            continue
        if href.lower().split("?")[0].endswith(".svg") or "svg" in a.get("type", "").lower() or href.startswith("data:"):
            continue
        sizes = [int(x) for x in re.findall(r"(\d+)x\d+", a.get("sizes", ""))]
        score = (2 if "apple-touch-icon" in rel else 1, max(sizes) if sizes else 0)
        scored.append((score, urljoin(page_url, href)))
    out = [u for _, u in sorted(scored, key=lambda x: x[0], reverse=True)]
    root = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    for extra in ("/apple-touch-icon.png", "/favicon.ico"):
        if root + extra not in out:
            out.append(root + extra)
    seen, uniq = set(), []
    for u in out:
        if u not in seen and urlparse(u).scheme in ("http", "https"):
            seen.add(u)
            uniq.append(u)
    return uniq[:6]


def _get(client: httpx.Client, url: str, limit: int) -> tuple[bytes, str] | None:
    with client.stream("GET", url) as r:
        if r.status_code != 200:
            return None
        buf = b""
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > limit:
                return None
        return buf, str(r.url)


def fetch(domain: str) -> tuple[bytes, str, str] | None:
    """(image bytes, content type, source URL) for the domain's best icon, or None."""
    d = normalise(domain)
    if not d:
        return None
    with safe_client(timeout=8, headers={"User-Agent": UA, "Accept": "text/html,image/*;q=0.8"}) as c:
        page, page_url = "", f"https://{d}/"
        for start in (f"https://{d}/", f"https://www.{d}/"):
            try:
                got = _get(c, start, MAX_PAGE)
            except httpx.HTTPError:
                got = None
            if got:
                page, page_url = got[0].decode("utf-8", "ignore"), got[1]
                break
        for url in candidates(page, page_url):
            try:
                got = _get(c, url, MAX_IMAGE)
            except httpx.HTTPError:
                continue
            if got and (ctype := sniff(got[0])) and len(got[0]) >= 64:
                return got[0], ctype, url
    return None


def tracked_domains(s) -> dict[str, str]:
    """{normalised domain: company name} for active brands and their active competitors."""
    out = {}
    for b in s.scalars(select(Brand).where(Brand.active == True)):  # noqa: E712
        if (d := normalise(b.domain)):
            out[d] = b.name
        for c in b.competitors:
            if c.active and (d := normalise(c.domain)):
                out.setdefault(d, c.name)
    return out


def refresh(s, domains: list[str] | None = None, force: bool = False, limit: int = 40) -> dict:
    """Fetch missing logos (or, with force, every non-uploaded one) for the given domains or every tracked one.
    Uploads are never replaced by a fetch. Returns counts; never raises for one bad site."""
    want = [normalise(d) for d in (domains if domains is not None else tracked_domains(s))]
    rows = {r.domain: r for r in s.scalars(select(SiteLogo).where(SiteLogo.domain.in_([d for d in want if d])))}
    done = found = 0
    for d in [d for d in want if d][:limit]:
        row = rows.get(d)
        if row and row.status == "upload":
            continue
        if row and not force and (row.status == "ok" or now() - row.fetched_at < RETRY_AFTER):
            continue
        try:
            got = fetch(d)
        except Exception as e:  # noqa: BLE001  one site's problem must not stop the others
            log.warning("logo fetch failed for %s: %s", d, type(e).__name__)
            got = None
        row = row or SiteLogo(domain=d)
        if got:
            row.data, row.content_type, row.source, row.status = got[0], got[1], got[2][:2000], "ok"
            found += 1
        elif row.status != "ok":
            row.status, row.data, row.content_type = "none", None, ""
        row.fetched_at = now()
        s.merge(row)
        done += 1
    return {"checked": done, "found": found}


def store_upload(s, domain: str, data: bytes, who: str) -> str | None:
    """Save an uploaded logo. Returns an error message, or None when stored."""
    d = normalise(domain)
    if not d:
        return "Set the company's domain first."
    if len(data) > MAX_IMAGE:
        return f"Too big ({len(data) // 1024} KB); the limit is {MAX_IMAGE // 1024} KB."
    ctype = sniff(data)
    if not ctype:
        return "Use a PNG, JPEG, WebP, GIF or ICO file (SVG isn't accepted)."
    s.merge(SiteLogo(domain=d, status="upload", content_type=ctype, data=data, source=f"upload by {who}"[:2000],
                     fetched_at=now()))
    return None
