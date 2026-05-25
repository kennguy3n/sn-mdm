"""Thomson Reuters Institute podcast crawler.

Thomson Reuters publishes its podcast catalogue across several
WordPress categories on ``thomsonreuters.com/en-us/posts/``:

* ``/posts/legal/podcast-*``
* ``/posts/investigation-fraud-and-risk/podcast-*``
* ``/posts/government/podcast-*``
* ``/posts/international-trade-and-supply-chain/podcast-*``

Each post embeds an "Episode transcript" link that points at a
PDF transcript hosted under ``wp-content/uploads/...``. The PDF
is the canonical source of verbatim text — the post page itself
only carries a short summary plus the Apple Podcasts player.

History
-------

The original Tranche 1 crawler was configured against
``/posts/legal`` as a base URL and assumed an HTML transcript
section inside each post — neither of which is true. The current
implementation walks the post sitemaps (``post-sitemap{1,2,3}.xml``)
for permalinks matching the ``podcast-*`` shape, then resolves
the per-post transcript-PDF link.

Why a plain ``requests`` GET works
----------------------------------

Both the sitemaps and the post pages are server-rendered. No
headless browser is required — ``BaseCrawler.fetch`` (which
honours robots.txt and rate-limiting) is sufficient.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

LOG = logging.getLogger(__name__)

_SITEMAP_URLS = (
    "https://www.thomsonreuters.com/en-us/posts/post-sitemap.xml",
    "https://www.thomsonreuters.com/en-us/posts/post-sitemap2.xml",
    "https://www.thomsonreuters.com/en-us/posts/post-sitemap3.xml",
)

_POSTS_PREFIX = "https://www.thomsonreuters.com/en-us/posts/"

# Match ``/posts/{category}/podcast-{slug}/`` permalinks. The
# category segment lets us distinguish ``legal``, ``government``,
# ``investigation-fraud-and-risk``, ``international-trade-and-supply-chain``
# without hard-coding a per-category allow-list — any future
# Thomson Reuters category that hosts a ``podcast-*`` post
# is admitted automatically.
_EPISODE_LOC_RE = re.compile(
    r"<loc>\s*"
    r"https?://(?:www\.)?thomsonreuters\.com/en-us/posts/"
    r"([a-z0-9\-]+)/(podcast-[a-z0-9\-]+)/?"
    r"\s*</loc>"
)


class ThomsonReutersCrawler(BaseCrawler):
    publisher_id = "thomson_reuters"
    publisher_name = "Thomson Reuters"
    DISCOVER_CAP = 25

    def _episode_url(self, slug: str) -> str:
        # Slug is ``{category}/{post-slug}`` (e.g.
        # ``legal/podcast-coo-cfo-forum``). We keep the slash
        # in the slug to round-trip the canonical URL — the
        # JSONL ``episode_slug`` and the on-disk artifact path
        # both replace ``/`` with ``_`` at write time (see
        # ``BaseCrawler.normalize``).
        return f"{_POSTS_PREFIX}{slug}/"

    def _discover_episode_slugs(self) -> list[str]:
        slugs: list[str] = []
        seen: set[str] = set()
        for sitemap_url in _SITEMAP_URLS:
            try:
                resp = self.fetch(sitemap_url)
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "thomson_reuters: sitemap %s failed: %s",
                    sitemap_url,
                    exc,
                )
                continue
            for m in _EPISODE_LOC_RE.finditer(resp.text):
                category, post_slug = m.group(1), m.group(2)
                slug = f"{category}/{post_slug}"
                if slug in seen:
                    continue
                seen.add(slug)
                slugs.append(slug)
                if len(slugs) >= self.DISCOVER_CAP:
                    return slugs
        return slugs

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        landing_url = self._episode_url(episode_slug)
        landing_resp = self.fetch(landing_url)
        soup = BeautifulSoup(landing_resp.text, "lxml")
        title = self._extract_title(landing_resp.text) or episode_slug
        publication_date = self._extract_publication_date(landing_resp.text)
        summary = _meta_description(soup)
        # The on-disk slug uses ``_`` instead of ``/`` so it's a
        # safe filename. ``primary_url`` keeps the canonical
        # ``/`` shape.
        safe_slug = episode_slug.replace("/", "_")

        transcript_pdf = _find_transcript_pdf(soup, landing_url)
        if transcript_pdf:
            try:
                pdf_resp = self.fetch(transcript_pdf, accept="application/pdf")
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "thomson_reuters: PDF fetch failed for %s: %s",
                    safe_slug,
                    exc,
                )
            else:
                return RawEpisode(
                    episode_slug=safe_slug,
                    title=title,
                    primary_url=landing_url,
                    publication_date=publication_date,
                    raw_bytes=pdf_resp.content,
                    content_type="application/pdf",
                    hosts=[],
                    guests=[],
                    asset_urls=[transcript_pdf],
                    summary=summary,
                )

        return RawEpisode(
            episode_slug=safe_slug,
            title=title,
            primary_url=landing_url,
            publication_date=publication_date,
            raw_bytes=landing_resp.content,
            content_type="text/html",
            hosts=[],
            guests=[],
            asset_urls=[],
            summary=summary,
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        """Used only for the fallback path (when a post is missing
        its transcript PDF). Strip site chrome and surface the
        ``<article>`` / ``<main>`` body so the rights gate has
        something to score against.
        """
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(
            [
                "script",
                "style",
                "noscript",
                "iframe",
                "nav",
                "footer",
                "header",
                "form",
                "button",
                "aside",
            ]
        ):
            tag.decompose()
        container = soup.find("article") or soup.find("main") or soup
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _meta_description(soup: BeautifulSoup) -> str:
    for sel in (
        ("property", "og:description"),
        ("name", "description"),
    ):
        tag = soup.find("meta", attrs={sel[0]: sel[1]})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _find_transcript_pdf(soup: BeautifulSoup, base: str) -> str | None:
    """Locate the per-episode transcript PDF.

    Thomson Reuters posts the transcript with explicit anchor
    text ``"Episode transcript"`` (sometimes with a trailing
    period or whitespace). The href is a PDF under
    ``wp-content/uploads/`` — we use that path as a
    defence-in-depth check to avoid matching links that share
    the phrase but point at a non-PDF (e.g. an embed).
    """
    for a in soup.find_all("a", href=True):
        text = (a.get_text(strip=True) or "").lower().strip(".")
        if "episode transcript" not in text:
            continue
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        return urllib.parse.urljoin(base, href)
    # Defence-in-depth: some older posts label the link
    # ``Transcript`` only. Accept that if the href is a PDF
    # under ``wp-content/uploads/`` on the same origin.
    for a in soup.find_all("a", href=True):
        text = (a.get_text(strip=True) or "").lower().strip(".")
        if text != "transcript":
            continue
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        full = urllib.parse.urljoin(base, href)
        if "/wp-content/uploads/" in full:
            return full
    return None
