"""Unit tests for the two Tranche 1 crawlers that fetch via the
headless-browser transport (``wef_radio_davos`` and
``rbc_disruptors``).

These tests never spin up Chromium — they monkey-patch the
crawler instance's :meth:`fetch_rendered` to return hand-rolled
HTML fixtures so we can exercise discovery + extraction + the
publisher-specific normalisers without paying for a real browser.
The live-network behaviour is exercised by the manual
``python -m crawl.pipeline`` runs documented in ``docs/SOURCES.md``.

The fixtures intentionally mirror the live-page structure we
observed during reconnaissance (see PR description for evidence):

* WEF embeds the transcript under a stable
  ``<div data-gtm-section="Podcast transcript">`` attribute and
  ships ``schema.org/CreativeWork`` JSON-LD with ``headline`` and
  ``datePublished``.
* RBC's archive is fully client-rendered React but every episode
  page exposes an ``<h2>Transcript</h2>`` heading followed by
  the transcript paragraphs, and ships ``schema.org/Article``
  JSON-LD inside an ``@graph`` payload.

If WEF or RBC change either contract, the corresponding test
suite below is the unit-test layer that catches it; the
integration crawl in ``packs/`` is the operational backstop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from crawl.crawlers.base import CrawlerConfig
from crawl.crawlers.rbc_disruptors import RbcDisruptorsCrawler
from crawl.crawlers.wef_radio_davos import WefRadioDavosCrawler

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _config(publisher_id: str, base_url: str, rights_code: str) -> CrawlerConfig:
    return CrawlerConfig(
        publisher_id=publisher_id,
        publisher_name=publisher_id,
        base_url=base_url,
        rights_code=rights_code,
        rights_summary="test",
        country_region=["global"],
        industry_tags=[],
        function_tags=[],
        business_model_tags=[],
        source_type="podcast_transcript_html",
        language="en",
        host="",
    )


class _RenderStub:
    """Drop-in replacement for ``BaseCrawler.fetch_rendered`` that
    serves a URL → bytes map. Any URL not in the map raises
    ``KeyError`` so test failures are loud.

    Captures every call so tests can assert the discovery /
    fetch_transcript code path actually called the browser
    transport with the expected URL.
    """

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self._mapping = mapping
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> bytes:
        self.calls.append((url, kwargs))
        if url not in self._mapping:
            raise KeyError(f"_RenderStub: no fixture for {url!r}")
        return self._mapping[url]


# ---------------------------------------------------------------------------
# WEF Radio Davos
# ---------------------------------------------------------------------------


_WEF_HUB = "https://www.weforum.org/podcasts/radio-davos/"
_WEF_EPISODE_URL = "https://www.weforum.org/podcasts/radio-davos/episodes/sample-slug/"

_WEF_HUB_HTML = b"""<!doctype html><html><body>
<a href="/podcasts/radio-davos/episodes/first-episode/">First</a>
<a href="https://www.weforum.org/podcasts/radio-davos/episodes/second-episode/">Second</a>
<a href="/podcasts/radio-davos/episodes/first-episode/">Dup of first</a>
<a href="/podcasts/radio-davos/episodes/?page=2">Pagination noise</a>
<a href="/podcasts/radio-davos/">Hub self-link</a>
<a href="/podcasts/other-show/episodes/should-be-ignored/">Other show</a>
</body></html>"""

_WEF_EPISODE_HTML = b"""<!doctype html>
<html><head>
<title>Sample episode | World Economic Forum</title>
<meta property="og:description" content="Sample summary.">
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "@type": "CreativeWork",
  "headline": "Sample WEF episode title",
  "datePublished": "2026-04-16T08:40:00Z",
  "description": "JSON-LD description override.",
  "author": [{"@type": "Person", "name": "Robin Pomeroy"}]
}
</script>
</head><body>
<div id="CybotCookiebotDialog">Cookie banner that must be stripped.</div>
<div class="wef-13hre6c">Site chrome wrapper.</div>
<div data-gtm-section="Podcast transcript">
  <h2>Transcript</h2>
  <p>Robin Pomeroy: Welcome to Radio Davos.</p>
  <p>Guest: Adam Grant - thanks for having me.</p>
  <p>Robin Pomeroy: Today we discuss intelligence and skills.</p>
</div>
</body></html>"""


@pytest.fixture
def wef_crawler(tmp_path: Path) -> WefRadioDavosCrawler:
    return WefRadioDavosCrawler(
        _config("wef_radio_davos", "https://www.weforum.org/podcasts/radio-davos", "cc_by_nc_nd"),
        tmp_path,
    )


def test_wef_discovers_episode_slugs_dedup_and_filters(
    monkeypatch: pytest.MonkeyPatch, wef_crawler: WefRadioDavosCrawler
) -> None:
    """The hub-page regex pulls episode slugs in document order,
    dedups, and rejects pagination / hub-self / other-show
    hrefs. Production hubs ship the same hrefs duplicated by the
    sidebar so dedup is load-bearing — without it ``initial_sync``
    would refetch each episode N times.
    """
    stub = _RenderStub({_WEF_HUB: _WEF_HUB_HTML})
    monkeypatch.setattr(wef_crawler, "fetch_rendered", stub)
    slugs = wef_crawler._discover_episode_slugs()
    assert slugs == ["first-episode", "second-episode"]
    # Only one browser call (the hub), not one per discovered slug.
    assert len(stub.calls) == 1
    assert stub.calls[0][0] == _WEF_HUB


def test_wef_discovery_returns_empty_on_browser_error(
    monkeypatch: pytest.MonkeyPatch, wef_crawler: WefRadioDavosCrawler
) -> None:
    """``_discover_episode_slugs`` is best-effort. When the hub
    page errors (Chromium navigation timeout, WAF block,
    network blip), we return an empty list so
    :meth:`BaseCrawler.initial_sync` can fall back to the seed
    list without crashing the run.
    """

    def boom(*_a: Any, **_kw: Any) -> bytes:
        raise RuntimeError("chromium navigation timeout")

    monkeypatch.setattr(wef_crawler, "fetch_rendered", boom)
    assert wef_crawler._discover_episode_slugs() == []


def test_wef_fetch_transcript_extracts_jsonld_metadata(
    monkeypatch: pytest.MonkeyPatch, wef_crawler: WefRadioDavosCrawler
) -> None:
    """``fetch_transcript`` prefers JSON-LD ``headline`` over the
    HTML ``<title>`` (the canonical title), trims the ISO-8601
    date down to ``YYYY-MM-DD`` (matching the ``publication_date``
    schema used by every other crawler), and pulls the author
    list out of ``schema.org/Person`` blocks. The summary falls
    back to the ``og:description`` only when the JSON-LD
    ``description`` is absent — in this fixture it isn't, so
    JSON-LD wins.
    """
    stub = _RenderStub({_WEF_EPISODE_URL: _WEF_EPISODE_HTML})
    monkeypatch.setattr(wef_crawler, "fetch_rendered", stub)
    raw = wef_crawler.fetch_transcript("sample-slug")
    assert raw.title == "Sample WEF episode title"
    assert raw.publication_date == "2026-04-16"
    assert raw.hosts == ["Robin Pomeroy"]
    assert raw.summary == "JSON-LD description override."
    assert raw.primary_url == _WEF_EPISODE_URL
    assert raw.content_type == "text/html"
    assert raw.raw_bytes == _WEF_EPISODE_HTML


def test_wef_normalises_only_transcript_subtree(
    wef_crawler: WefRadioDavosCrawler,
) -> None:
    """The normaliser pins to the stable
    ``<div data-gtm-section="Podcast transcript">`` anchor —
    Cookiebot markup and the surrounding site chrome must not
    leak into the chunked text. Without this, the cookie banner
    (~60 KB on the live page) dominates every chunk and breaks
    content-hash dedup across episodes.
    """
    text = wef_crawler._normalize_html_bytes(_WEF_EPISODE_HTML)
    assert "Cookie banner that must be stripped." not in text
    assert "Site chrome wrapper." not in text
    assert "Welcome to Radio Davos" in text
    assert "Today we discuss intelligence and skills" in text


# ---------------------------------------------------------------------------
# RBC Disruptors
# ---------------------------------------------------------------------------


_RBC_HUB = "https://www.rbc.com/en/thought-leadership/disruptors/"
_RBC_EPISODE_URL = (
    "https://www.rbc.com/en/thought-leadership/disruptors/sample-slug/"
)

_RBC_HUB_HTML = b"""<!doctype html><html><body>
<a href="/en/thought-leadership/disruptors/first-episode/">First</a>
<a href="https://www.rbc.com/en/thought-leadership/disruptors/second-episode/">Second</a>
<a href="/en/thought-leadership/disruptors/season-9/">Season hub - skip</a>
<a href="/en/thought-leadership/disruptors/subscribe/">Subscribe - skip</a>
<a href="/en/thought-leadership/disruptors/first-episode/">Dup of first</a>
<a href="/en/thought-leadership/other-section/some-page/">Other section</a>
</body></html>"""

_RBC_EPISODE_HTML = b"""<!doctype html>
<html><head>
<title>RBC episode | RBC</title>
<meta name="description" content="HTML meta description used as summary.">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Article",
      "headline": "AI&#8217;s Power, Pitfalls and Potential",
      "datePublished": "2026-04-14T14:11:28+00:00",
      "wordCount": 6425
    },
    {
      "@type": "WebPage"
    }
  ]
}
</script>
</head><body>
<article>
<h1>AI Power and Potential</h1>
<p>Intro paragraph that lives above the transcript section.</p>
<h2>Transcript</h2>
<p>John Stackhouse: Welcome to Disruptors.</p>
<p>Guest: Lavanyakaleeswaran - thanks.</p>
<h2>Subscribe</h2>
<p>This is the subscribe section that must not leak into the chunked text.</p>
</article>
</body></html>"""


@pytest.fixture
def rbc_crawler(tmp_path: Path) -> RbcDisruptorsCrawler:
    return RbcDisruptorsCrawler(
        _config(
            "rbc_disruptors",
            "https://www.rbc.com/en/thought-leadership/disruptors",
            "free_access_copyrighted",
        ),
        tmp_path,
    )


def test_rbc_discovers_episodes_excluding_season_and_subscribe(
    monkeypatch: pytest.MonkeyPatch, rbc_crawler: RbcDisruptorsCrawler
) -> None:
    """RBC's archive page is a single ``<ul>`` of cards mixed
    with cross-links to ``/season-N/`` summary pages and a
    ``/subscribe/`` CTA. Discovery has to skip both shapes or
    every initial_sync would also crawl the meaningless season
    hubs and waste a fetch slot. The fixture exercises the
    ``_NON_EPISODE_SLUGS`` allow-list.
    """
    stub = _RenderStub({_RBC_HUB: _RBC_HUB_HTML})
    monkeypatch.setattr(rbc_crawler, "fetch_rendered", stub)
    slugs = rbc_crawler._discover_episode_slugs()
    assert slugs == ["first-episode", "second-episode"]


def test_rbc_discovery_returns_empty_on_browser_error(
    monkeypatch: pytest.MonkeyPatch, rbc_crawler: RbcDisruptorsCrawler
) -> None:
    def boom(*_a: Any, **_kw: Any) -> bytes:
        raise RuntimeError("chromium navigation timeout")

    monkeypatch.setattr(rbc_crawler, "fetch_rendered", boom)
    assert rbc_crawler._discover_episode_slugs() == []


def test_rbc_fetch_transcript_decodes_html_entities_in_title(
    monkeypatch: pytest.MonkeyPatch, rbc_crawler: RbcDisruptorsCrawler
) -> None:
    """RBC's JSON-LD ``headline`` is double-escaped (the
    WordPress JSON-LD plugin emits ``&#8217;`` even though JSON
    would have rendered the literal Unicode just fine). We
    decode entities post-extraction so the title that lands in
    the JSONL matches what a human reader sees on the live page.
    """
    stub = _RenderStub({_RBC_EPISODE_URL: _RBC_EPISODE_HTML})
    monkeypatch.setattr(rbc_crawler, "fetch_rendered", stub)
    raw = rbc_crawler.fetch_transcript("sample-slug")
    # &#8217; is a right single quote — make sure it round-trips.
    assert raw.title == "AI\u2019s Power, Pitfalls and Potential"
    assert raw.publication_date == "2026-04-14"
    assert raw.hosts == ["John Stackhouse"]
    assert raw.summary == "HTML meta description used as summary."


def test_rbc_normalises_only_transcript_section(
    rbc_crawler: RbcDisruptorsCrawler,
) -> None:
    """The RBC normaliser narrows to the sibling nodes after the
    ``<h2>Transcript</h2>`` heading and stops at the next H1/H2.
    Without this we'd include the page-intro blurb (above the
    heading) and the subscribe CTA (after the heading) in the
    chunked text — which would muddy chunk content and is
    obviously not transcript material.
    """
    text = rbc_crawler._normalize_html_bytes(_RBC_EPISODE_HTML)
    assert "Welcome to Disruptors" in text
    assert "Intro paragraph that lives above the transcript" not in text
    assert "Subscribe" not in text or "## Subscribe" not in text
    assert "must not leak into the chunked text" not in text
