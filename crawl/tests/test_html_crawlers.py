"""Unit tests for the three plain-``requests`` crawlers landed in
the Milestone-A2 gap-closure: ``ted_worklife``, ``deutsche_bank``,
and ``thomson_reuters``.

Tests never touch the network. Each crawler's
:meth:`BaseCrawler.fetch` is monkey-patched with a stub that
serves URL → ``_FakeResponse`` from a hand-rolled fixture map
mirroring the live page structure observed during the
Milestone-A2 reconnaissance.

If a publisher changes its HTML or sitemap shape, the
corresponding test below is the unit-test layer that catches it;
the integration crawl in ``packs/`` is the operational backstop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from crawl.crawlers.base import CrawlerConfig
from crawl.crawlers.deutsche_bank import DeutscheBankCrawler
from crawl.crawlers.ted_worklife import TedWorklifeCrawler
from crawl.crawlers.thomson_reuters import ThomsonReutersCrawler


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


class _FakeResponse:
    """Minimal stand-in for ``requests.Response``. We need only
    ``.text`` and ``.content`` because the three crawlers under
    test use those exclusively. Headers are not consulted on the
    HTML/PDF paths because content-type is hard-coded by the
    crawler (transcript HTML or transcript PDF).
    """

    def __init__(self, body: bytes, *, encoding: str = "utf-8") -> None:
        self.content = body
        self._encoding = encoding

    @property
    def text(self) -> str:
        return self.content.decode(self._encoding, errors="replace")


class _FetchStub:
    """URL → fixture-bytes map that masquerades as
    ``BaseCrawler.fetch``. Any unmapped URL raises ``KeyError`` so
    test failures are loud.

    ``accept`` is recorded so tests can assert PDF fetches go via
    the explicit ``accept="application/pdf"`` call site (matches
    BCG / the new Thomson Reuters paths).
    """

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self._mapping = mapping
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        if url not in self._mapping:
            raise KeyError(f"_FetchStub: no fixture for {url!r}")
        return _FakeResponse(self._mapping[url])


# ---------------------------------------------------------------------------
# TED WorkLife
# ---------------------------------------------------------------------------


_TED_HUB = "https://www.ted.com/podcasts/worklife-transcripts"
_TED_EPISODE_URL = "https://www.ted.com/podcasts/sample-slug-transcript"

_TED_HUB_HTML = b"""<!doctype html><html><body>
<a href="/podcasts/first-episode-transcript">First</a>
<a href="https://www.ted.com/podcasts/second-episode-transcript">Second abs</a>
<a href="https://ted.com/podcasts/third-episode-transcript">Third abs no www</a>
<a href="ted.com/podcasts/fourth-episode-transcript">Fourth bare-host (NextJS quirk)</a>
<a href="/podcasts/first-episode-transcript">Dup of first</a>
<a href="/podcasts/worklife-transcripts">Hub self-link - skip</a>
<a href="/podcasts/worklife">Show landing - skip</a>
<a href="/podcasts/subscribe">Subscribe - skip</a>
<a href="/podcasts/foo-transcript?utm_source=tw">Trailing query string</a>
</body></html>"""

_TED_EPISODE_HTML = b"""<!doctype html>
<html><head>
<title>Sample episode (Transcript)</title>
<meta property="og:title" content="Sample episode">
<meta property="og:description" content="OG description.">
<meta name="description" content="HTML meta description.">
<meta property="article:published_time" content="2026-04-18T08:40:00Z">
</head><body>
<header><nav>Site chrome nav.</nav></header>
<main>
<p>Sample episode May 19, 2026</p>
<p>Please note the following transcript may not exactly match the final audio.</p>
<p>Speaker A: hello.</p>
<p>Speaker B: hi.</p>
</main>
<aside>Sidebar that must be stripped.</aside>
<footer>Footer that must be stripped.</footer>
</body></html>"""


@pytest.fixture
def ted_crawler(tmp_path: Path) -> TedWorklifeCrawler:
    return TedWorklifeCrawler(
        _config("ted_worklife", "https://www.ted.com/podcasts", "cc_by_nc_nd"),
        tmp_path,
    )


def test_ted_discovers_transcript_permalinks_and_filters_chrome(
    monkeypatch: pytest.MonkeyPatch, ted_crawler: TedWorklifeCrawler
) -> None:
    """Hub-page discovery extracts the episode slug from each
    transcript permalink (the slug is what sits between
    ``/podcasts/`` and ``-transcript``), dedups across casing /
    query-string variants, and rejects the hub self-link,
    the show landing page and TED-wide CTAs.
    """
    stub = _FetchStub({_TED_HUB: _TED_HUB_HTML})
    monkeypatch.setattr(ted_crawler, "fetch", stub)
    slugs = ted_crawler._discover_episode_slugs()
    assert slugs == [
        "first-episode",
        "second-episode",
        "third-episode",
        "fourth-episode",
        "foo",
    ]
    assert len(stub.calls) == 1
    assert stub.calls[0][0] == _TED_HUB


def test_ted_discovery_returns_empty_on_fetch_error(
    monkeypatch: pytest.MonkeyPatch, ted_crawler: TedWorklifeCrawler
) -> None:
    """Discovery is best-effort. If the hub fetch raises (rate
    limit, robots block, transient network blip), we return an
    empty list so ``BaseCrawler.initial_sync`` can fall back to
    the seed list without crashing the publisher run.
    """

    def boom(*_a: Any, **_kw: Any) -> _FakeResponse:
        raise RuntimeError("network error")

    monkeypatch.setattr(ted_crawler, "fetch", boom)
    assert ted_crawler._discover_episode_slugs() == []


def test_ted_fetch_transcript_strips_transcript_suffix_from_title(
    monkeypatch: pytest.MonkeyPatch, ted_crawler: TedWorklifeCrawler
) -> None:
    """The stored title should be the human-readable episode
    title — not the page's ``<title>`` which TED suffixes with
    ``" (Transcript)"``. The crawler prefers ``og:title`` (which
    TED keeps clean), and as a defence-in-depth strips a trailing
    ``"(Transcript)"`` from any other title source.
    """
    stub = _FetchStub({_TED_EPISODE_URL: _TED_EPISODE_HTML})
    monkeypatch.setattr(ted_crawler, "fetch", stub)
    raw = ted_crawler.fetch_transcript("sample-slug")
    assert raw.title == "Sample episode"
    assert raw.publication_date == "2026-04-18"
    assert raw.primary_url == _TED_EPISODE_URL
    assert raw.content_type == "text/html"
    # OG description wins over the bare HTML meta name="description".
    assert raw.summary == "OG description."


def test_ted_normalises_only_main_subtree(
    ted_crawler: TedWorklifeCrawler,
) -> None:
    """The normaliser pins to ``<main>`` so site chrome (nav,
    sidebar, footer) is dropped. Without this anchor the
    sidebar's promo CTAs and the footer's social rail would
    leak into the transcript and break content-hash dedup.
    """
    text = ted_crawler._normalize_html_bytes(_TED_EPISODE_HTML)
    assert "Site chrome nav" not in text
    assert "Sidebar that must be stripped" not in text
    assert "Footer that must be stripped" not in text
    assert "Speaker A: hello" in text
    assert "Speaker B: hi" in text


def test_ted_episode_url_round_trips_slug(
    ted_crawler: TedWorklifeCrawler,
) -> None:
    """The discovered slug + ``_episode_url`` must round-trip
    back to the canonical transcript URL — this is the invariant
    that lets ``initial_sync`` feed ``fetch_transcript`` directly.
    """
    assert (
        ted_crawler._episode_url("how-to-make-ai-worth-your-time-with-max-mullen")
        == "https://www.ted.com/podcasts/how-to-make-ai-worth-your-time-with-max-mullen-transcript"
    )


# ---------------------------------------------------------------------------
# Deutsche Bank — flow InCorporate Treasury
# ---------------------------------------------------------------------------


_DB_HUB = "https://flow.db.com/media/flow-incorporatetreasury-podcasts/"
_DB_EP_URL = (
    "https://flow.db.com/media/flow-incorporatetreasury-podcasts/episode-11-merck-iso"
)
_DB_TRANSCRIPT_URL = (
    "https://flow.db.com/media/flow-incorporatetreasury-podcasts/"
    "episode-11-merck-iso-transcript"
)

_DB_HUB_HTML = b"""<!doctype html><html><body>
<a href="/media/flow-incorporatetreasury-podcasts/episode-10-rwe">Ep 10</a>
<a href="/media/flow-incorporatetreasury-podcasts/episode-11-merck-iso">Ep 11</a>
<a href="https://flow.db.com/media/flow-incorporatetreasury-podcasts/episode-12-payments">Ep 12 abs</a>
<a href="/media/flow-incorporatetreasury-podcasts/episode-10-rwe-transcript">Ep 10 transcript - skip</a>
<a href="/media/flow-incorporatetreasury-podcasts/episode-10-rwe">Dup of ep 10</a>
<a href="/media/flow-incorporatetreasury-podcasts/">Hub self-link - skip (no slug)</a>
<a href="/media/flow-magazine">Sibling series - skip (different prefix)</a>
</body></html>"""

_DB_EP_HTML = b"""<!doctype html>
<html><head>
<title>Episode 11: Merck switches to ISO 20022</title>
<meta name="description" content="DB episode summary.">
</head><body>
<header><nav>Site nav</nav></header>
<article>
<h1>Episode 11: Merck switches to ISO 20022</h1>
<p>Episode summary paragraph.</p>
<p>For an accessible version with transcription please click
<a href="/media/flow-incorporatetreasury-podcasts/episode-11-merck-iso-transcript"
   title="Transcript - Episode 11">here</a>.</p>
</article>
<footer>Site footer</footer>
</body></html>"""

_DB_TRANSCRIPT_HTML = b"""<!doctype html>
<html><head>
<title>Episode 11 transcript - Deutsche Bank</title>
</head><body>
<header><nav>Site nav must be stripped</nav></header>
<article>
<h1>Episode 11 transcript page header</h1>
<p>Some metadata text that lives above the transcript anchor.</p>
<h2>Transcript - Episode 11: Merck switches to ISO 20022</h2>
<p>Clarissa Dann (CD): Hello and welcome to the flow InCorporate Treasury podcast.</p>
<p>Uwe Reinemer (UR): Thanks for having me on the show.</p>
<h2>Subscribe</h2>
<p>This is the subscribe footer that must NOT leak into the transcript chunks.</p>
</article>
<footer>Footer that must be stripped</footer>
</body></html>"""

_DB_EP_NO_TRANSCRIPT_HTML = b"""<!doctype html>
<html><head>
<title>Episode 12: Payments</title>
<meta name="description" content="No transcript yet.">
</head><body>
<article>
<h1>Episode 12: Payments</h1>
<p>This episode's transcript hasn't been published yet.</p>
</article>
</body></html>"""


@pytest.fixture
def db_crawler(tmp_path: Path) -> DeutscheBankCrawler:
    return DeutscheBankCrawler(
        _config(
            "deutsche_bank",
            "https://flow.db.com/media/flow-incorporatetreasury-podcasts",
            "free_access_copyrighted",
        ),
        tmp_path,
    )


def test_db_discovers_episodes_excluding_transcript_companions(
    monkeypatch: pytest.MonkeyPatch, db_crawler: DeutscheBankCrawler
) -> None:
    """Hub discovery enumerates the per-episode permalinks while
    skipping the matching ``-transcript`` companion pages
    (those are reached via the episode-page link, never as
    standalone episodes) and the hub self-link (no slug
    component). Dedup across absolute / relative href shapes.
    """
    stub = _FetchStub({_DB_HUB: _DB_HUB_HTML})
    monkeypatch.setattr(db_crawler, "fetch", stub)
    slugs = db_crawler._discover_episode_slugs()
    assert slugs == [
        "episode-10-rwe",
        "episode-11-merck-iso",
        "episode-12-payments",
    ]


def test_db_discovery_returns_empty_on_fetch_error(
    monkeypatch: pytest.MonkeyPatch, db_crawler: DeutscheBankCrawler
) -> None:
    def boom(*_a: Any, **_kw: Any) -> _FakeResponse:
        raise RuntimeError("network error")

    monkeypatch.setattr(db_crawler, "fetch", boom)
    assert db_crawler._discover_episode_slugs() == []


def test_db_fetch_transcript_resolves_companion_page(
    monkeypatch: pytest.MonkeyPatch, db_crawler: DeutscheBankCrawler
) -> None:
    """The episode landing page links to its ``-transcript``
    companion. ``fetch_transcript`` must (1) GET the landing
    page, (2) follow the transcript link, (3) return the
    transcript page's bytes as ``raw_bytes`` with
    ``primary_url`` pointing at the transcript (so citation
    anchors land on the actual content, not the audio-only
    landing).
    """
    stub = _FetchStub(
        {_DB_EP_URL: _DB_EP_HTML, _DB_TRANSCRIPT_URL: _DB_TRANSCRIPT_HTML}
    )
    monkeypatch.setattr(db_crawler, "fetch", stub)
    raw = db_crawler.fetch_transcript("episode-11-merck-iso")
    assert raw.title == "Episode 11: Merck switches to ISO 20022"
    assert raw.primary_url == _DB_TRANSCRIPT_URL
    assert raw.raw_bytes == _DB_TRANSCRIPT_HTML
    assert raw.content_type == "text/html"
    # Both pages should have been fetched, in order.
    assert [c[0] for c in stub.calls] == [_DB_EP_URL, _DB_TRANSCRIPT_URL]


def test_db_fetch_transcript_falls_back_to_landing_when_no_companion(
    monkeypatch: pytest.MonkeyPatch, db_crawler: DeutscheBankCrawler
) -> None:
    """If the episode page has no transcript link (most recent
    episode while editorial finalises the transcript), the
    crawler still emits a row backed by the landing page —
    ``primary_url`` falls back to the landing URL, ``raw_bytes``
    holds the landing HTML. This keeps the metadata + governance
    row recorded so a future re-crawl picks up the transcript
    when it appears.
    """
    ep12_url = (
        "https://flow.db.com/media/flow-incorporatetreasury-podcasts/episode-12-payments"
    )
    stub = _FetchStub({ep12_url: _DB_EP_NO_TRANSCRIPT_HTML})
    monkeypatch.setattr(db_crawler, "fetch", stub)
    raw = db_crawler.fetch_transcript("episode-12-payments")
    assert raw.primary_url == ep12_url
    assert raw.raw_bytes == _DB_EP_NO_TRANSCRIPT_HTML
    assert raw.title == "Episode 12: Payments"


def test_db_normalises_only_transcript_subtree(
    db_crawler: DeutscheBankCrawler,
) -> None:
    """The normaliser pins to the ``<h2>Transcript - Episode N: …</h2>``
    heading and collects subsequent siblings up to the next H1/H2.
    The result must include the transcript dialogue and exclude
    site chrome AND the "Subscribe" footer that follows the
    transcript section.
    """
    text = db_crawler._normalize_html_bytes(_DB_TRANSCRIPT_HTML)
    assert "Clarissa Dann (CD): Hello and welcome" in text
    assert "Uwe Reinemer (UR): Thanks for having me on the show" in text
    assert "Site nav must be stripped" not in text
    assert "Footer that must be stripped" not in text
    assert "subscribe footer that must NOT leak" not in text


# ---------------------------------------------------------------------------
# Thomson Reuters
# ---------------------------------------------------------------------------


_TR_SITEMAP_1 = "https://www.thomsonreuters.com/en-us/posts/post-sitemap.xml"
_TR_SITEMAP_2 = "https://www.thomsonreuters.com/en-us/posts/post-sitemap2.xml"
_TR_SITEMAP_3 = "https://www.thomsonreuters.com/en-us/posts/post-sitemap3.xml"

_TR_POST_URL = (
    "https://www.thomsonreuters.com/en-us/posts/legal/podcast-coo-cfo-forum/"
)
_TR_PDF_URL = (
    "https://www.thomsonreuters.com/en-us/posts/wp-content/uploads/sites/20/2021/10/"
    "Thomson-Reuters-Market-Insights-20-years-of-the-Law-Firm-COO-CFO-Forum.pdf"
)

_TR_SITEMAP_1_XML = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.thomsonreuters.com/en-us/posts/legal/podcast-coo-cfo-forum/</loc></url>
<url><loc>https://www.thomsonreuters.com/en-us/posts/legal/some-non-podcast-post/</loc></url>
<url><loc>https://www.thomsonreuters.com/en-us/posts/investigation-fraud-and-risk/podcast-fraud-report/</loc></url>
<url><loc>https://www.thomsonreuters.com/en-us/posts/legal/podcast-coo-cfo-forum/</loc></url>
</urlset>"""

_TR_SITEMAP_2_XML = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.thomsonreuters.com/en-us/posts/government/podcast-one-big-beautiful-bill-act/</loc></url>
</urlset>"""

_TR_SITEMAP_3_XML = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.thomsonreuters.com/en-us/posts/legal/podcast-coo-cfo-forum/</loc></url>
</urlset>"""

_TR_POST_HTML = b"""<!doctype html>
<html><head>
<title>Podcast: 20 years of the Law Firm COO &amp; CFO Forum - Thomson Reuters Institute</title>
<meta property="og:description" content="TR post description.">
</head><body>
<article>
<p>Episode summary paragraph.</p>
<p><a href="https://www.thomsonreuters.com/en-us/posts/wp-content/uploads/sites/20/2021/10/Thomson-Reuters-Market-Insights-20-years-of-the-Law-Firm-COO-CFO-Forum.pdf">Episode transcript.</a></p>
</article>
</body></html>"""

# Pretend the PDF bytes are arbitrary — the crawler doesn't open
# them in ``fetch_transcript``; the pipeline's
# ``_normalize_pdf_bytes`` does. The test only checks pass-through.
_TR_PDF_BYTES = b"%PDF-1.7\nSample transcript content for tests"


@pytest.fixture
def tr_crawler(tmp_path: Path) -> ThomsonReutersCrawler:
    return ThomsonReutersCrawler(
        _config(
            "thomson_reuters",
            "https://www.thomsonreuters.com/en-us/posts",
            "free_access_copyrighted",
        ),
        tmp_path,
    )


def test_tr_discovers_podcast_loc_entries_across_sitemaps(
    monkeypatch: pytest.MonkeyPatch, tr_crawler: ThomsonReutersCrawler
) -> None:
    """Discovery walks each ``post-sitemap{1,2,3}.xml`` and
    extracts loc entries matching ``/posts/{category}/podcast-*/``.
    Non-podcast posts and duplicate loc entries (Thomson Reuters
    occasionally lists the same canonical URL in multiple
    sitemaps) are filtered out. Category prefix is preserved in
    the stored slug.
    """
    stub = _FetchStub(
        {
            _TR_SITEMAP_1: _TR_SITEMAP_1_XML,
            _TR_SITEMAP_2: _TR_SITEMAP_2_XML,
            _TR_SITEMAP_3: _TR_SITEMAP_3_XML,
        }
    )
    monkeypatch.setattr(tr_crawler, "fetch", stub)
    slugs = tr_crawler._discover_episode_slugs()
    assert slugs == [
        "legal/podcast-coo-cfo-forum",
        "investigation-fraud-and-risk/podcast-fraud-report",
        "government/podcast-one-big-beautiful-bill-act",
    ]


def test_tr_discovery_skips_failing_sitemaps_but_keeps_others(
    monkeypatch: pytest.MonkeyPatch, tr_crawler: ThomsonReutersCrawler
) -> None:
    """If one of the post sitemaps 5xxs or times out, discovery
    must continue with the remaining ones — a single transient
    sitemap failure should not zero the whole publisher's
    discovery results. This is the same isolation contract
    BCG uses across its sitemap pair.
    """
    served = {_TR_SITEMAP_2: _TR_SITEMAP_2_XML}

    def fetch(url: str, **_kw: Any) -> _FakeResponse:
        if url == _TR_SITEMAP_1:
            raise RuntimeError("503 service unavailable")
        if url == _TR_SITEMAP_3:
            raise RuntimeError("timeout")
        return _FakeResponse(served[url])

    monkeypatch.setattr(tr_crawler, "fetch", fetch)
    slugs = tr_crawler._discover_episode_slugs()
    assert slugs == ["government/podcast-one-big-beautiful-bill-act"]


def test_tr_fetch_transcript_returns_pdf_bytes(
    monkeypatch: pytest.MonkeyPatch, tr_crawler: ThomsonReutersCrawler
) -> None:
    """The crawler fetches the post HTML, finds the "Episode
    transcript" PDF anchor, fetches the PDF, and returns the
    PDF bytes as the ingest payload with
    ``content_type='application/pdf'`` — that's what tells
    ``BaseCrawler.normalize`` to route through ``_normalize_pdf_bytes``.
    The slug is filename-safe (``/`` → ``_``) but
    ``primary_url`` keeps the canonical post URL.
    """
    stub = _FetchStub({_TR_POST_URL: _TR_POST_HTML, _TR_PDF_URL: _TR_PDF_BYTES})
    monkeypatch.setattr(tr_crawler, "fetch", stub)
    raw = tr_crawler.fetch_transcript("legal/podcast-coo-cfo-forum")
    assert raw.episode_slug == "legal_podcast-coo-cfo-forum"
    assert raw.primary_url == _TR_POST_URL
    assert raw.content_type == "application/pdf"
    assert raw.raw_bytes == _TR_PDF_BYTES
    # Title was extracted from <title>.
    assert "20 years of the Law Firm COO" in raw.title
    # PDF fetch passed accept=application/pdf so an origin that
    # serves an HTML wrapper would 406.
    pdf_call = next(c for c in stub.calls if c[0] == _TR_PDF_URL)
    assert pdf_call[1].get("accept") == "application/pdf"


def test_tr_fetch_transcript_falls_back_to_landing_when_no_pdf(
    monkeypatch: pytest.MonkeyPatch, tr_crawler: ThomsonReutersCrawler
) -> None:
    """Older Thomson Reuters posts predate the transcript-PDF
    convention. For those, the crawler still emits a row backed
    by the landing-page HTML so the metadata + governance entry
    is recorded; the rights gate + summary still pass through.
    """
    no_pdf_html = b"""<!doctype html>
<html><head>
<title>Podcast: an older episode without a transcript PDF</title>
<meta name="description" content="Older episode.">
</head><body>
<article>
<p>This episode has no transcript PDF link.</p>
</article>
</body></html>"""
    url = "https://www.thomsonreuters.com/en-us/posts/legal/podcast-old-no-pdf/"
    stub = _FetchStub({url: no_pdf_html})
    monkeypatch.setattr(tr_crawler, "fetch", stub)
    raw = tr_crawler.fetch_transcript("legal/podcast-old-no-pdf")
    assert raw.episode_slug == "legal_podcast-old-no-pdf"
    assert raw.content_type == "text/html"
    assert raw.raw_bytes == no_pdf_html


def test_tr_episode_url_round_trips_category_slug(
    tr_crawler: ThomsonReutersCrawler,
) -> None:
    """``_episode_url`` round-trips the ``{category}/{post-slug}``
    shape back to the canonical post URL (with trailing slash).
    """
    assert (
        tr_crawler._episode_url("legal/podcast-coo-cfo-forum")
        == "https://www.thomsonreuters.com/en-us/posts/legal/podcast-coo-cfo-forum/"
    )
