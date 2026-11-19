"""Company logos: fetched from each company's own site, raster only, served by Prism in a sandbox."""
import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def test_normalise():
    from prism.analysis.logos import normalise
    assert normalise("https://www.Amplience.com/x?y") == "amplience.com"
    assert normalise("amplience.com") == "amplience.com"
    assert normalise("") == "" and normalise("localhost") == "" and normalise("javascript:alert(1)") == ""


def test_sniff_accepts_raster_only():
    from prism.analysis.logos import sniff
    assert sniff(PNG) == "image/png"
    assert sniff(b"\xff\xd8\xff\xe0" + b"0" * 10) == "image/jpeg"
    assert sniff(b"\x00\x00\x01\x00" + b"0" * 10) == "image/x-icon"
    assert sniff(SVG) is None and sniff(b"<html>") is None


def test_candidates_prefer_apple_touch_icon_and_skip_svg():
    from prism.analysis.logos import candidates
    page = ('<link rel="icon" href="/f.svg" type="image/svg+xml"><link rel="icon" sizes="32x32" href="/f32.png">'
            '<link rel="apple-touch-icon" href="/apple.png"><link rel="icon" sizes="192x192" href="/f192.png">')
    c = candidates(page, "https://example.com/")
    assert c[0] == "https://example.com/apple.png" and c[1] == "https://example.com/f192.png"
    assert not any(u.endswith(".svg") for u in c)
    assert c[-1] == "https://example.com/favicon.ico"


@respx.mock
def test_refresh_stores_png_and_rejects_svg(db):
    from prism.analysis import logos
    from prism.models import SiteLogo
    respx.get("https://good.example/").mock(return_value=httpx.Response(200, text='<link rel="apple-touch-icon" href="/a.png">'))
    respx.get("https://good.example/a.png").mock(return_value=httpx.Response(200, content=PNG))
    respx.get("https://bad.example/").mock(return_value=httpx.Response(200, text='<link rel="icon" href="/x.png">'))
    respx.get("https://bad.example/x.png").mock(return_value=httpx.Response(200, content=SVG))  # lies about its type
    respx.get(url__regex=r"https://bad\.example/(apple-touch-icon\.png|favicon\.ico)").mock(return_value=httpx.Response(404))
    with db() as s:
        res = logos.refresh(s, ["good.example", "bad.example"])
    assert res == {"checked": 2, "found": 1}
    with db() as s:
        rows = {r.domain: r for r in s.scalars(select(SiteLogo))}
    assert rows["good.example"].status == "ok" and rows["good.example"].data == PNG
    assert rows["bad.example"].status == "none" and rows["bad.example"].data is None


def test_upload_rules(db):
    from prism.analysis import logos
    with db() as s:
        assert "SVG" in logos.store_upload(s, "amplience.com", SVG, "a@farviewequity.com")
        assert "Too big" in logos.store_upload(s, "amplience.com", PNG + b"0" * logos.MAX_IMAGE, "a@farviewequity.com")
        assert logos.store_upload(s, "amplience.com", PNG, "a@farviewequity.com") is None
    with db() as s:  # a fetch never replaces an upload
        assert logos.refresh(s, ["amplience.com"], force=True) == {"checked": 0, "found": 0}


def test_logo_route_is_sandboxed_and_pages_use_it(db):
    from prism.analysis import logos
    with db() as s:
        logos.store_upload(s, "amplience.com", PNG, "a@farviewequity.com")
    from prism.web.app import create_app
    with TestClient(create_app()) as c:
        r = c.get("/prism/logo/amplience.com")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
        assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
        assert c.get("/prism/logo/unknown.example").status_code == 404
        home = c.get("/prism/").text
        assert '/prism/logo/amplience.com' in home      # portfolio card and sidebar
        assert 'class="lg lg-md ini"' in home           # companies without a logo show their initial


def test_logo_route_needs_sign_in(db, monkeypatch):
    monkeypatch.setenv("PRISM_AUTH_MODE", "entra")
    monkeypatch.setenv("PRISM_PUBLIC_URL", "https://jarvis.farviewequity.com/prism")
    from prism.config import get_settings
    get_settings.cache_clear()
    from prism.web.app import create_app
    with TestClient(create_app()) as c:
        assert c.get("/prism/logo/amplience.com", follow_redirects=False).status_code == 303
