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


# ---------------------------------------------------------------------------
# Browser transport behaviours (cache key, JSON-LD shapes, retry)
# ---------------------------------------------------------------------------


_WEF_EPISODE_HTML_ARRAY = b"""<!doctype html>
<html><head>
<title>Array shape test</title>
<script type="application/ld+json">
[
  {"@type": "BreadcrumbList", "name": "ignored"},
  {
    "@context": "http://schema.org",
    "@type": "PodcastEpisode",
    "headline": "Array-shape title",
    "datePublished": "2026-05-01T00:00:00Z"
  }
]
</script>
</head><body>
<div data-gtm-section="Podcast transcript">
  <p>Robin Pomeroy: Hello array shape.</p>
</div>
</body></html>"""

_WEF_EPISODE_HTML_GRAPH = b"""<!doctype html>
<html><head>
<title>@graph shape test</title>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {"@type": "WebPage"},
    {
      "@type": "Article",
      "headline": "Graph-shape title",
      "datePublished": "2026-05-02T00:00:00Z"
    }
  ]
}
</script>
</head><body>
<div data-gtm-section="Podcast transcript">
  <p>Robin Pomeroy: Hello graph shape.</p>
</div>
</body></html>"""


def test_wef_jsonld_handles_top_level_array(
    monkeypatch: pytest.MonkeyPatch, wef_crawler: WefRadioDavosCrawler
) -> None:
    """Array-shaped JSON-LD payloads (``[{...}, {...}]``) must
    parse like the single-object shape. Without
    ``_iter_jsonld_objects`` this regressed silently into a
    JSON-decode error: a greedy ``{.*}`` regex captures from the
    first ``{`` to the last ``}``, spanning two adjacent
    objects, and produces invalid JSON. This regression test
    catches a future revert.
    """
    stub = _RenderStub({_WEF_EPISODE_URL: _WEF_EPISODE_HTML_ARRAY})
    monkeypatch.setattr(wef_crawler, "fetch_rendered", stub)
    raw = wef_crawler.fetch_transcript("sample-slug")
    assert raw.title == "Array-shape title"
    assert raw.publication_date == "2026-05-01"


def test_wef_jsonld_handles_graph_envelope(
    monkeypatch: pytest.MonkeyPatch, wef_crawler: WefRadioDavosCrawler
) -> None:
    """``@graph``-wrapped JSON-LD (WordPress's preferred shape)
    must yield the same episode metadata as a top-level object.
    """
    stub = _RenderStub({_WEF_EPISODE_URL: _WEF_EPISODE_HTML_GRAPH})
    monkeypatch.setattr(wef_crawler, "fetch_rendered", stub)
    raw = wef_crawler.fetch_transcript("sample-slug")
    assert raw.title == "Graph-shape title"
    assert raw.publication_date == "2026-05-02"


def test_browser_cache_key_separates_render_parameters() -> None:
    """The browser-transport LRU is keyed by
    ``(url, wait_for_selector, wait_for_states)`` not URL alone.
    Without this, a second caller asking for stricter wait
    semantics would silently get the first caller's partially
    rendered snapshot \u2014 and a SPA that needs ``networkidle``
    to settle would return an empty shell to BeautifulSoup.
    Exercises ``_BrowserState`` directly so we don't have to
    spin up Chromium to validate cache semantics.
    """
    from crawl.crawlers import _browser

    state = _browser._BrowserState()
    weak_key = ("https://example.com/x", None, ("domcontentloaded",))
    strict_key = (
        "https://example.com/x",
        None,
        ("domcontentloaded", "networkidle"),
    )
    state.cache_put(weak_key, (b"<weak/>", "text/html"))
    # Same URL, stricter wait spec \u2014 must NOT alias to the weak
    # response.
    assert state.cache_get(strict_key) is None
    # Identical parameters must hit (the WEF discovery + per
    # episode fetch reuse the same hub key, so the cache has to
    # serve a repeated lookup).
    assert state.cache_get(weak_key) == (b"<weak/>", "text/html")
    # A third key with a different selector also misses.
    sel_key = ("https://example.com/x", "div.transcript", ("domcontentloaded",))
    assert state.cache_get(sel_key) is None


def test_browser_cache_lru_eviction_respects_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LRU eviction is what keeps the page cache bounded across
    long Tranche-N runs. Lower the cap to 2 entries and confirm
    the oldest is dropped first when a third entry is inserted.
    """
    from crawl.crawlers import _browser

    monkeypatch.setattr(_browser, "_PAGE_CACHE_MAX", 2)
    state = _browser._BrowserState()
    state.cache_put(("a", None, ()), (b"A", "text/html"))
    state.cache_put(("b", None, ()), (b"B", "text/html"))
    state.cache_put(("c", None, ()), (b"C", "text/html"))
    assert state.cache_get(("a", None, ())) is None  # evicted
    assert state.cache_get(("b", None, ())) == (b"B", "text/html")
    assert state.cache_get(("c", None, ())) == (b"C", "text/html")


def test_fetch_rendered_retries_on_transient_playwright_error(
    monkeypatch: pytest.MonkeyPatch, wef_crawler: WefRadioDavosCrawler
) -> None:
    """A transient Playwright error on the first attempt must be
    retried, and a subsequent success must surface to the caller.
    Mirrors :meth:`BaseCrawler.fetch`'s retry shape for 5xx /
    408 / 429. Without retry, a single flaky navigation would
    drop the whole episode \u2014 expensive on a 30+ s SPA fetch.
    """
    from playwright.sync_api import Error as PlaywrightError

    from crawl.crawlers import _browser

    attempts = {"n": 0}

    def flaky_fetch(*_a: Any, **_kw: Any) -> tuple[bytes, str]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise PlaywrightError("transient navigation timeout")
        return (b"<html>ok</html>", "text/html")

    monkeypatch.setattr(_browser, "fetch_rendered", flaky_fetch)
    # Stub robots + rate-limit so the test stays network-free.
    monkeypatch.setattr(wef_crawler, "_robots_parser", lambda _u: _AllowAll())
    monkeypatch.setattr(wef_crawler, "_respect_rate_limit", lambda: None)
    # Defang the linear backoff so the test stays fast.
    import crawl.crawlers.base as _base

    monkeypatch.setattr(_base.time, "sleep", lambda _s: None)
    # Reach the real ``BaseCrawler.fetch_rendered``, not the stub
    # the other tests install.
    from crawl.crawlers.base import BaseCrawler

    out = BaseCrawler.fetch_rendered(wef_crawler, "https://example.com/x")
    assert out == b"<html>ok</html>"
    assert attempts["n"] == 2


def test_fetch_rendered_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch, wef_crawler: WefRadioDavosCrawler
) -> None:
    """When every attempt fails, the final Playwright error must
    surface to the caller. The pipeline relies on this to record
    the failure in the per-episode error log; swallowing it would
    silently drop episodes.
    """
    from playwright.sync_api import Error as PlaywrightError

    from crawl.crawlers import _browser

    attempts = {"n": 0}

    def always_fails(*_a: Any, **_kw: Any) -> tuple[bytes, str]:
        attempts["n"] += 1
        raise PlaywrightError(f"attempt {attempts['n']} failed")

    monkeypatch.setattr(_browser, "fetch_rendered", always_fails)
    monkeypatch.setattr(wef_crawler, "_robots_parser", lambda _u: _AllowAll())
    monkeypatch.setattr(wef_crawler, "_respect_rate_limit", lambda: None)
    import crawl.crawlers.base as _base

    monkeypatch.setattr(_base.time, "sleep", lambda _s: None)
    from crawl.crawlers.base import BaseCrawler

    with pytest.raises(PlaywrightError, match="attempt 3 failed"):
        BaseCrawler.fetch_rendered(
            wef_crawler, "https://example.com/x", max_attempts=3
        )
    assert attempts["n"] == 3


def test_fetch_rendered_does_not_retry_permission_error(
    monkeypatch: pytest.MonkeyPatch, wef_crawler: WefRadioDavosCrawler
) -> None:
    """A robots.txt disallow is a contract failure, not a
    transient. Must raise immediately without consuming retry
    budget \u2014 retrying would only delay the inevitable and
    burn another full Chromium navigation.
    """
    from crawl.crawlers.base import BaseCrawler

    class _DenyAll:
        def can_fetch(self, _ua: str, _url: str) -> bool:
            return False

    monkeypatch.setattr(wef_crawler, "_robots_parser", lambda _u: _DenyAll())
    monkeypatch.setattr(wef_crawler, "_respect_rate_limit", lambda: None)
    with pytest.raises(PermissionError):
        BaseCrawler.fetch_rendered(wef_crawler, "https://example.com/x")


class _AllowAll:
    def can_fetch(self, _ua: str, _url: str) -> bool:
        return True


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


# ---------------------------------------------------------------------------
# Shared JSON-LD helper (`_jsonld`)
# ---------------------------------------------------------------------------


def test_jsonld_helper_handles_single_object() -> None:
    """Single-object payload yields exactly that object."""
    from crawl.crawlers import _jsonld

    out = list(_jsonld.iter_jsonld_objects('{"@type": "Article", "headline": "x"}'))
    assert out == [{"@type": "Article", "headline": "x"}]


def test_jsonld_helper_handles_top_level_array() -> None:
    """Array payload yields every dict in order; non-dicts are
    filtered out so the caller can iterate without ``isinstance``
    checks of its own.
    """
    from crawl.crawlers import _jsonld

    out = list(
        _jsonld.iter_jsonld_objects(
            '[{"@type": "BreadcrumbList"}, "junk", {"@type": "Article", "headline": "x"}]'
        )
    )
    assert out == [
        {"@type": "BreadcrumbList"},
        {"@type": "Article", "headline": "x"},
    ]


def test_jsonld_helper_handles_graph_envelope() -> None:
    """``@graph`` envelope is walked into; the outer wrapper is
    not yielded (it carries no episode-level fields).
    """
    from crawl.crawlers import _jsonld

    raw = (
        '{"@context": "https://schema.org", '
        '"@graph": [{"@type": "WebPage"}, {"@type": "Article", "headline": "x"}]}'
    )
    out = list(_jsonld.iter_jsonld_objects(raw))
    assert out == [{"@type": "WebPage"}, {"@type": "Article", "headline": "x"}]


def test_jsonld_helper_strips_cdata_shell() -> None:
    """CDATA-wrapped payloads (WEF) parse identically to bare
    payloads — the wrapper is a template-engine artefact, not
    part of the JSON contract.
    """
    from crawl.crawlers import _jsonld

    raw = (
        '// <![CDATA[\n'
        '{"@type": "PodcastEpisode", "headline": "cdata"}\n'
        '// ]]>\n'
    )
    out = list(_jsonld.iter_jsonld_objects(raw))
    assert out == [{"@type": "PodcastEpisode", "headline": "cdata"}]


def test_jsonld_helper_yields_nothing_on_malformed_payload() -> None:
    """A payload that isn't valid JSON yields an empty iterator
    rather than raising — the caller iterates without try/except
    and just gets no metadata.
    """
    from crawl.crawlers import _jsonld

    assert list(_jsonld.iter_jsonld_objects("not json at all {[}")) == []
    assert list(_jsonld.iter_jsonld_objects("")) == []
    assert list(_jsonld.iter_jsonld_objects("42")) == []


def test_jsonld_type_matches_handles_list_type() -> None:
    """``@type`` can be a list per the JSON-LD spec; matching is
    set-membership against any element.
    """
    from crawl.crawlers import _jsonld

    wanted = frozenset({"PodcastEpisode", "Article"})
    assert _jsonld.type_matches({"@type": "Article"}, wanted)
    assert _jsonld.type_matches({"@type": ["NewsArticle", "PodcastEpisode"]}, wanted)
    assert not _jsonld.type_matches({"@type": "BreadcrumbList"}, wanted)
    assert not _jsonld.type_matches({"@type": ["WebPage", "Organization"]}, wanted)
    assert not _jsonld.type_matches({}, wanted)


def test_rbc_extractor_handles_top_level_array_jsonld() -> None:
    """After the shared-helper refactor, RBC's extractor inherits
    array-shape support — a regression-proof for the case where
    WordPress's plugin starts emitting arrays per the
    ``@type``-as-list note above.
    """
    from bs4 import BeautifulSoup

    from crawl.crawlers.rbc_disruptors import _extract_jsonld_metadata

    html = (
        '<html><head><script type="application/ld+json">'
        '[{"@type": "BreadcrumbList"}, '
        '{"@type": "Article", "headline": "RBC array shape", '
        '"datePublished": "2026-05-15"}]'
        '</script></head><body></body></html>'
    )
    soup = BeautifulSoup(html, "html.parser")
    md = _extract_jsonld_metadata(soup)
    assert md["headline"] == "RBC array shape"
    assert md["datePublished"] == "2026-05-15"


# ---------------------------------------------------------------------------
# _browser.fetch_rendered: iterable contract
# ---------------------------------------------------------------------------


def test_fetch_rendered_accepts_generator_wait_for_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signature declares ``Iterable[str]`` so passing a
    generator must work. Pre-fix the implementation drained the
    generator into the cache key and then unpacked it a second
    time at the goto step, raising ``ValueError: not enough
    values to unpack``.
    """
    from crawl.crawlers import _browser

    captured: dict[str, Any] = {}

    class _Page:
        def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
            captured["first_state"] = wait_until
            captured["url"] = url

        def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            captured.setdefault("rest_states", []).append(state)

        def content(self) -> str:
            return "<html></html>"

        def close(self) -> None:
            pass

    class _Ctx:
        def new_page(self) -> _Page:
            return _Page()

    monkeypatch.setattr(_browser._STATE, "context", lambda: _Ctx())
    # Empty the LRU so we exercise the fetch path, not the cache.
    monkeypatch.setattr(_browser._STATE, "_cache", type(_browser._STATE._cache)())

    def _states():
        yield "domcontentloaded"
        yield "networkidle"

    html_bytes, _ct = _browser.fetch_rendered(
        "https://example.com/", wait_for_states=_states(), use_cache=False
    )
    assert html_bytes == b"<html></html>"
    assert captured["first_state"] == "domcontentloaded"
    assert captured["rest_states"] == ["networkidle"]


# ---------------------------------------------------------------------------
# _browser.context partial-init cleanup
# ---------------------------------------------------------------------------


def test_browser_context_rolls_back_on_partial_init_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Chromium fails to launch midway through
    ``_BrowserState.context()``, the singleton must NOT retain
    the partially-built Playwright handle — otherwise a follow-up
    call would overwrite it with a fresh start() and leak the
    previous Playwright host process, and the atexit shutdown
    would try to ``.stop()`` a torn-down handle.
    """
    from crawl.crawlers import _browser

    state = _browser._BrowserState()

    closed = {"playwright": False, "browser": False}

    class _Pw:
        def stop(self) -> None:
            closed["playwright"] = True

        class chromium:  # noqa: N801 - mirrors Playwright API shape
            @staticmethod
            def launch(**_kw: Any) -> Any:
                raise RuntimeError("simulated chromium launch failure")

    class _PwFactory:
        def start(self) -> _Pw:
            return _Pw()

    # Intercept sync_playwright() at import time inside context().
    import sys
    import types

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _PwFactory()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    with pytest.raises(RuntimeError, match="simulated chromium launch failure"):
        state.context()

    # All three handles must be back to None so a subsequent
    # retry starts from a clean slate.
    assert state._playwright is None
    assert state._browser is None
    assert state._context is None
    # And the partial Playwright handle was stopped during rollback.
    assert closed["playwright"] is True
