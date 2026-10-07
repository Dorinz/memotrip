"""What search engines are told: robots.txt + sitemap list only the public
pages, those pages carry a canonical URL, and per-user pages say noindex."""

from __future__ import annotations

import config


def test_robots_txt_blocks_private_paths_and_points_to_sitemap(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    for path in ("/oauth/", "/reset-password"):
        assert f"Disallow: {path}" in r.text
    # trip pages must stay crawlable so their noindex is actually seen
    assert "/data/" not in r.text and "/trips/" not in r.text
    assert f"Sitemap: {config.PUBLIC_BASE_URL.rstrip('/')}/sitemap.xml" in r.text


def test_sitemap_lists_only_public_pages(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    base = config.PUBLIC_BASE_URL.rstrip("/")
    for path in ("/", "/privacy", "/terms"):
        assert f"<loc>{base}{path}</loc>" in r.text
    assert "/trips" not in r.text and "/login" not in r.text


def test_public_pages_have_canonical(client):
    base = config.PUBLIC_BASE_URL.rstrip("/")
    for path in ("/", "/privacy", "/terms", "/login"):
        html = client.get(path).text
        assert f'<link rel="canonical" href="{base}{path}" />' in html
        assert "{{BASE_URL}}" not in html


def test_reset_pages_are_noindex(client):
    assert '<meta name="robots" content="noindex" />' in client.get("/forgot-password").text


def test_trip_page_sends_noindex_header(client, tmp_path, monkeypatch):
    import webapp

    tid = "b" * 32
    monkeypatch.setattr(webapp, "DATA", tmp_path / "data")
    (tmp_path / "data" / tid).mkdir(parents=True)
    (tmp_path / "data" / tid / "page.html").write_text("<html></html>", encoding="utf-8")
    r = client.get(f"/data/{tid}/page.html")
    assert r.status_code == 200
    assert "noindex" in r.headers["x-robots-tag"]
