"""Regression tests for Milestone D — recover IMD and Masters of
Scale episodes that the content-hash dedup gate previously
collapsed because the crawler picked the wrong DOM subtree.

These tests hit the ``_normalize_html_bytes`` extraction directly
with hand-rolled HTML fixtures that mirror the live page
structure observed during the Milestone-D reconnaissance:

* **IMD**: the post body lives under
  ``<div data-elementor-type="wp-post">``. The page also renders
  several ``<article>`` blocks for the "Related Articles" widget
  that sit outside that wrapper. The pre-Milestone-D crawler
  picked ``soup.find("article")`` and consequently extracted the
  same recent-articles list on every episode page → 22 of 25
  discovered episodes hashed identically and got deduped.

* **Masters of Scale**: the transcript ``<h2>`` is nested inside
  a sticky wrapper ``<div>`` whose only direct sibling is a
  chevron-toggle ``<div>``. The actual transcript ``<p>``
  paragraphs are siblings of *that wrapper*, one level up. The
  pre-Milestone-D crawler walked ``target.next_siblings`` and
  consequently only ever extracted the chevron-toggle text →
  24 of 25 discovered episodes hashed identically and got
  deduped.

The tests below would have caught both regressions: each
asserts (a) the crawler extracts material from the correct
subtree, AND (b) two structurally different episodes produce
distinct normalised output (the content-hash dedup invariant).
"""

from __future__ import annotations

from pathlib import Path

from crawl.crawlers.base import CrawlerConfig
from crawl.crawlers.imd import ImdCrawler
from crawl.crawlers.masters_of_scale import MastersOfScaleCrawler


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


# ---------------------------------------------------------------------------
# IMD — Elementor wp-post wrapper
# ---------------------------------------------------------------------------


# Mirrors the live page structure: ``<div data-elementor-type
# ="wp-post">`` wraps the canonical post; multiple ``<article>``
# blocks for the "Related Articles" sidebar widget render at the
# top of the body, outside the wp-post container.
_IMD_EP_A = b"""<!doctype html>
<html><body>
<article class="card">RELATED CARD: this is the recent-articles widget.</article>
<article class="card">RELATED CARD: another one.</article>
<div data-elementor-type="wp-post">
<h1>Niels Christiansen, how did LEGO regain momentum?</h1>
<p>LEGO CEO Niels Christiansen on simplification, sustainability, and competing for children's attention.</p>
<p>When Christiansen took over as CEO in 2017, he stepped into a company that had stalled.</p>
<article class="in-body-related">RELATED CARD inside wp-post - also strip.</article>
</div>
</body></html>"""

_IMD_EP_B = b"""<!doctype html>
<html><body>
<article class="card">RELATED CARD: this is the recent-articles widget.</article>
<article class="card">RELATED CARD: another one.</article>
<div data-elementor-type="wp-post">
<h1>Kati ter Horst on being a first-time CEO when the old rules are gone</h1>
<p>Outokumpu CEO Kati ter Horst on steel, free trade, Europe's competitiveness.</p>
<p>What does it mean to lead a heavy-industry company through a structural transformation?</p>
</div>
</body></html>"""


def test_imd_normalises_canonical_wp_post_not_related_cards(tmp_path: Path) -> None:
    """Two IMD episodes with different post bodies but identical
    related-cards widgets must produce *different* normalised
    text. The pre-Milestone-D ``soup.find("article")`` selected
    the related-cards widget; that path is the dedup-collapse
    regression we're fixing.
    """
    crawler = ImdCrawler(_config("imd", "https://www.imd.org", "fair_use_review"), tmp_path)
    text_a = crawler._normalize_html_bytes(_IMD_EP_A)
    text_b = crawler._normalize_html_bytes(_IMD_EP_B)

    # The episode body must surface in the extraction.
    assert "LEGO regain momentum" in text_a
    assert "Niels Christiansen" in text_a
    assert "Kati ter Horst" in text_b
    assert "Outokumpu" in text_b

    # The related-cards widget (outside wp-post AND inside wp-post)
    # must NOT surface — it is identical across every episode and
    # is exactly what made the dedup gate collapse 22/25 IMDs.
    assert "RELATED CARD" not in text_a
    assert "RELATED CARD" not in text_b

    # Most importantly: the two episodes must produce distinct
    # normalised text. If they don't, the dedup gate folds them.
    assert text_a != text_b


def test_imd_falls_back_to_soup_when_wp_post_marker_missing(tmp_path: Path) -> None:
    """If a future page revision drops the
    ``data-elementor-type="wp-post"`` marker, the crawler should
    still extract *something* (better to ship a noisy episode
    than to silently drop one). The fallback is the whole soup,
    which is the same defensive posture as the existing
    ``container = soup.find(...) or soup`` pattern across the
    other HTML crawlers.
    """
    crawler = ImdCrawler(_config("imd", "https://www.imd.org", "fair_use_review"), tmp_path)
    text = crawler._normalize_html_bytes(
        b"""<!doctype html><html><body>
<p>Fallback episode body - no Elementor marker.</p>
</body></html>"""
    )
    assert "Fallback episode body" in text


# ---------------------------------------------------------------------------
# Masters of Scale — sticky transcript-heading wrapper
# ---------------------------------------------------------------------------


# Mirrors the live MoS page structure: the transcript heading is
# inside a sticky wrapper ``<div>``; the only direct sibling of
# the heading inside that wrapper is the chevron-toggle ``<div>``.
# Real transcript paragraphs are siblings of the *wrapper*, one
# level up — so ``target.next_siblings`` only ever yields the
# chevron-toggle text.
_MOS_EP_A = b"""<!doctype html>
<html><body>
<div class="content">
<div class="sticky-wrapper">
<h2>Transcript: Imperfect is perfect</h2>
<div>Open chevron-down Close chevron-up</div>
</div>
<p>REID HOFFMAN: Mark Zuckerberg famously launched Facebook as an undergraduate.</p>
<p>MARK ZUCKERBERG: When I was 10, 11, or 12 years old, I used to mostly build games.</p>
<p>HOFFMAN: A lot of tech entrepreneurs tell stories about the basic games they built as a kid.</p>
</div>
</body></html>"""

_MOS_EP_B = b"""<!doctype html>
<html><body>
<div class="content">
<div class="sticky-wrapper">
<h2>Transcript: Lead, lead again</h2>
<div>Open chevron-down Close chevron-up</div>
</div>
<p>REID HOFFMAN: When technologies become ubiquitous and essential.</p>
<p>SHERYL SANDBERG: I learned to lead by leading. And then leading again.</p>
</div>
</body></html>"""

_MOS_EP_NO_TRANSCRIPT = b"""<!doctype html>
<html><body>
<div class="content">
<h1>An older episode without a transcript</h1>
<p>Audio-only episode, no transcript shipped.</p>
</div>
</body></html>"""


def test_mos_extracts_paragraphs_after_sticky_transcript_heading(
    tmp_path: Path,
) -> None:
    """The real transcript ``<p>`` paragraphs sit outside the
    sticky wrapper that holds the transcript ``<h2>``; the
    pre-Milestone-D ``target.next_siblings`` walk missed them all
    and only captured the chevron-toggle text. ``find_all_next``
    descends the entire forward DOM and is the architectural fix.
    """
    crawler = MastersOfScaleCrawler(
        _config("masters_of_scale", "https://mastersofscale.com", "free_access_copyrighted"),
        tmp_path,
    )
    text_a = crawler._normalize_html_bytes(_MOS_EP_A)
    text_b = crawler._normalize_html_bytes(_MOS_EP_B)

    # Real transcript content surfaces.
    assert "REID HOFFMAN" in text_a
    assert "MARK ZUCKERBERG" in text_a
    assert "SHERYL SANDBERG" in text_b

    # Chevron-toggle UI chrome (only direct sibling of the heading
    # inside the sticky wrapper) is excluded because the
    # ``find_all_next`` filter restricts to ``<p>/<li>/<blockquote>``
    # — the chevron text is inside a ``<div>``.
    assert "chevron-down" not in text_a
    assert "chevron-up" not in text_a

    # Most importantly: two episodes produce distinct text.
    assert text_a != text_b


def test_mos_returns_empty_when_no_transcript_heading(tmp_path: Path) -> None:
    """MoS episodes published before the show started shipping
    transcripts must produce empty extraction so the
    ``content_hash`` dedup gate folds them all together rather
    than emitting one ``header-only`` row per audio-only episode.
    """
    crawler = MastersOfScaleCrawler(
        _config("masters_of_scale", "https://mastersofscale.com", "free_access_copyrighted"),
        tmp_path,
    )
    text = crawler._normalize_html_bytes(_MOS_EP_NO_TRANSCRIPT)
    assert text == ""


def test_mos_extraction_stops_at_next_top_level_heading(tmp_path: Path) -> None:
    """If a future MoS layout reintroduces a top-level heading
    after the transcript (e.g., a "Related Episodes" section),
    the extraction must stop there so the related-episode promo
    text doesn't leak into the transcript dedup-key.
    """
    crawler = MastersOfScaleCrawler(
        _config("masters_of_scale", "https://mastersofscale.com", "free_access_copyrighted"),
        tmp_path,
    )
    html = b"""<!doctype html>
<html><body>
<div class="content">
<div class="sticky-wrapper">
<h2>Transcript: episode title</h2>
<div>chevron toggle</div>
</div>
<p>HOFFMAN: real transcript content goes here.</p>
<h2>Related Episodes</h2>
<p>This is a related-episode promo - must NOT appear in transcript.</p>
</div>
</body></html>"""
    text = crawler._normalize_html_bytes(html)
    assert "real transcript content" in text
    assert "related-episode promo" not in text


def test_mos_does_not_duplicate_nested_blockquote_text(tmp_path: Path) -> None:
    """``find_all_next()`` yields elements in document order
    including descendants, so a transcript paragraph wrapped in
    a ``<blockquote>`` would be matched twice — once for the
    blockquote and once for the inner ``<p>`` — and the dedup
    key would carry the doubled text. The extraction must
    suppress descendants of an already-captured candidate so
    each on-page paragraph contributes exactly once.
    """
    crawler = MastersOfScaleCrawler(
        _config("masters_of_scale", "https://mastersofscale.com", "free_access_copyrighted"),
        tmp_path,
    )
    html = b"""<!doctype html>
<html><body>
<div class="content">
<h2>Transcript: with a quote</h2>
<p>HOFFMAN: a leading paragraph.</p>
<blockquote><p>ZUCKERBERG: a quoted paragraph that must not double.</p></blockquote>
<ul><li>Bullet point one.</li><li>Bullet point two.</li></ul>
<p>HOFFMAN: a trailing paragraph.</p>
</div>
</body></html>"""
    text = crawler._normalize_html_bytes(html)
    # Each on-page paragraph contributes exactly once.
    assert text.count("a quoted paragraph that must not double") == 1
    assert text.count("a leading paragraph") == 1
    assert text.count("Bullet point one") == 1
    assert text.count("Bullet point two") == 1
    assert text.count("a trailing paragraph") == 1


def test_mos_extracts_sub_headings_as_markdown(tmp_path: Path) -> None:
    """If a future MoS layout introduces structural sub-headings
    inside the transcript (e.g. ``Part 1``, ``Act II``), they
    must surface in the normalised output so the chunker can use
    them as section boundaries. The transcript-section stop
    condition still uses h1/h2; intermediate h3-h6 are body
    content. This mirrors the markdown-style rendering the
    original Tranche-1 extractor used.
    """
    crawler = MastersOfScaleCrawler(
        _config("masters_of_scale", "https://mastersofscale.com", "free_access_copyrighted"),
        tmp_path,
    )
    html = b"""<!doctype html>
<html><body>
<div class="content">
<h2>Transcript: structured episode</h2>
<h3>Part 1</h3>
<p>HOFFMAN: this is part one.</p>
<h4>Aside</h4>
<p>HOFFMAN: a brief aside.</p>
<h3>Part 2</h3>
<p>HOFFMAN: this is part two.</p>
</div>
</body></html>"""
    text = crawler._normalize_html_bytes(html)
    assert "Part 1" in text
    assert "Part 2" in text
    assert "Aside" in text
    assert "this is part one" in text
    assert "this is part two" in text


# ---------------------------------------------------------------------------
# IMD — wp-post fallback robustness
# ---------------------------------------------------------------------------


def test_imd_fallback_does_not_strip_article_when_no_elementor_marker(
    tmp_path: Path,
) -> None:
    """If a future IMD layout drops the
    ``data-elementor-type="wp-post"`` marker and instead uses a
    bare HTML5 ``<article>`` as the post container, the
    extractor must NOT decompose that article — otherwise the
    fallback silently discards the episode body. Targeting the
    related-cards strip on the specific
    ``class*="card"`` selector keeps the related-widget removal
    focused while the fallback remains robust.
    """
    crawler = ImdCrawler(_config("imd", "https://www.imd.org", "fair_use_review"), tmp_path)
    text = crawler._normalize_html_bytes(
        b"""<!doctype html><html><body>
<article class="card">RELATED CARD: must be stripped.</article>
<article><h1>Future layout post body</h1><p>Real episode body content.</p></article>
</body></html>"""
    )
    assert "Real episode body content" in text
    assert "Future layout post body" in text
    assert "RELATED CARD" not in text
