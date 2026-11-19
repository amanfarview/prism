"""Trace cited pages back to the content behind them.

Connector config lives in AppSetting 'connector:<brand slug>':
  {"type": "amplience", "hub": "<hub name>", "strip_prefix": "", "locale_prefix": false}

For Amplience, a site page's path is usually its delivery key (e.g. /platform/dam
-> "platform/dam"), so we look it up on the Content Delivery 2 API (public CDN,
free, read-only) and store the content item id on the citation. That turns
"ChatGPT cited amplience.com/platform/dam" into "ChatGPT cited content item X",
which an editor can open and improve.
"""
from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import select

from ..common import get_setting
from ..models import Answer, Brand, Citation
from ..netguard import safe_get


@lru_cache(maxsize=4096)
def _amplience_lookup(hub: str, key: str) -> str:
    if not key:
        key = "home"
    # the hub name becomes part of a hostname and the key part of a path: accept only what Amplience allows
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", hub or "") or ".." in key:
        return ""
    url = f"https://{hub}.cdn.content.amplience.net/content/key/{quote(key, safe='/-_.~')}"
    try:
        r = safe_get(url, params={"depth": "root", "format": "inlined"}, timeout=10)
    except httpx.HTTPError:
        return ""
    if r.status_code != 200:
        return ""
    meta = (r.json().get("content") or {}).get("_meta") or {}
    cid = meta.get("deliveryId") or ""
    name = meta.get("name") or key
    schema = (meta.get("schema") or "").rsplit("/", 1)[-1]
    return f"amplience:{cid}|{name}|{schema}"[:300] if cid else ""


def delivery_key(path: str, cfg: dict) -> str:
    p = path.strip("/")
    pre = (cfg.get("strip_prefix") or "").strip("/")
    if pre and p.startswith(pre):
        p = p[len(pre):].strip("/")
    if cfg.get("locale_prefix") and "/" in p and len(p.split("/", 1)[0]) in (2, 5):
        p = p.split("/", 1)[1]
    return p


def map_brand_citations(s, brand: Brand, limit: int = 500) -> dict:
    cfg = get_setting(s, f"connector:{brand.slug}")
    if not cfg or cfg.get("type") != "amplience" or not cfg.get("hub"):
        return {"skipped": "no content connector configured"}
    rows = list(s.scalars(select(Citation).join(Answer, Answer.id == Citation.answer_id)
                          .where(Answer.brand_id == brand.id, Citation.owner_type == "own",
                                 Citation.content_ref == "").limit(limit)))
    mapped = 0
    for c in rows:
        ref = _amplience_lookup(cfg["hub"], delivery_key(urlparse(c.url).path, cfg))
        c.content_ref = ref or "unmatched"
        mapped += bool(ref)
    return {"checked": len(rows), "mapped": mapped}
