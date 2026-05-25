"""BCG Featured Insights podcast crawler.

BCG (Boston Consulting Group) publishes podcast episodes under
``https://www.bcg.com/featured-insights/podcasts``. Many of the
episodes link to a transcript PDF on ``web-assets.bcg.com``. We
crawl both the landing page (for metadata) and the PDF (for the
transcript body).

The episode slug in the source registry is the path under
``featured-insights/podcasts`` (e.g.
``"so-what-from-bcg/episode-name"``).
"""

from __future__ import annotations

import logging
import re
import urllib.parse

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

LOG = logging.getLogger(__name__)

# BCG ships a Google-flavoured XML sitemap. Each entry is a full
# canonical URL — the show pages, the episode pages, and a few
# series-landing pages share the ``/featured-insights/podcasts/``
# prefix, so we keep only paths with at least one ``<show>/<slug>``
# component (i.e. two segments after the prefix).
_SITEMAP_URLS = (
    "https://www.bcg.com/google_sitemap-content.xml",
    "https://www.bcg.com/google_sitemap-latest.xml",
)
_EPISODE_LOC_RE = re.compile(
    r"<loc>\s*https?://(?:www\.)?bcg\.com/featured-insights/podcasts/"
    r"([a-z0-9\-]+/[a-z0-9\-]+)/?\s*</loc>"
)


class BcgCrawler(BaseCrawler):
    publisher_id = "bcg"
    publisher_name = "BCG"
    DISCOVER_CAP = 25

    BASE_URL = "https://www.bcg.com"

    def _episode_url(self, slug: str) -> str:
        return f"{self.BASE_URL}/featured-insights/podcasts/{slug}"

    def _discover_episode_slugs(self) -> list[str]:
        slugs: list[str] = []
        seen: set[str] = set()
        for sitemap_url in _SITEMAP_URLS:
            try:
                resp = self.fetch(sitemap_url)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("bcg: %s failed: %s", sitemap_url, exc)
                continue
            for m in _EPISODE_LOC_RE.finditer(resp.text):
                slug = m.group(1)
                if slug in seen:
                    continue
                seen.add(slug)
                slugs.append(slug)
                if len(slugs) >= self.DISCOVER_CAP:
                    return slugs
        return slugs

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        # Step 1: landing page.
        landing_url = self._episode_url(episode_slug)
        resp = self.fetch(landing_url)
        soup = BeautifulSoup(resp.text, "lxml")
        title = self._extract_title(resp.text) or episode_slug
        publication_date = self._extract_publication_date(resp.text)

        # Step 2: locate the transcript PDF.
        pdf_url = _find_transcript_pdf_url(soup, landing_url)
        if pdf_url:
            pdf_resp = self.fetch(pdf_url, accept="application/pdf")
            return RawEpisode(
                episode_slug=episode_slug,
                title=title,
                primary_url=landing_url,
                publication_date=publication_date,
                raw_bytes=pdf_resp.content,
                content_type="application/pdf",
                hosts=_extract_hosts(soup),
                guests=_extract_guests(soup),
                asset_urls=[pdf_url, *_extract_companion_assets(soup, landing_url)],
                summary=_extract_summary(soup),
            )

        # Step 2b: no PDF — capture the landing page as HTML and let
        # the HTML normaliser do its best.
        return RawEpisode(
            episode_slug=episode_slug,
            title=title,
            primary_url=landing_url,
            publication_date=publication_date,
            raw_bytes=resp.content,
            content_type="text/html",
            hosts=_extract_hosts(soup),
            guests=_extract_guests(soup),
            asset_urls=_extract_companion_assets(soup, landing_url),
            summary=_extract_summary(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        # BCG's content area is a div with class containing
        # "podcast-transcript" or "rich-text" depending on layout.
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        container = soup.find(
            lambda tag: tag.name == "div"
            and any(
                ("podcast-transcript" in c.lower() or "rich-text" in c.lower())
                for c in tag.get("class", [])
            )
        )
        if container is None:
            container = soup.find("main") or soup
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _find_transcript_pdf_url(soup: BeautifulSoup, base: str) -> str | None:
    """Walk anchor tags looking for a PDF link whose anchor text or
    URL suggests it's the transcript. BCG hosts these on
    ``web-assets.bcg.com``.
    """
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        text = (a.get_text(strip=True) or "").lower()
        if any(kw in text for kw in ("transcript", "transcripts", "read the transcript")):
            return urllib.parse.urljoin(base, href)
        if "web-assets.bcg.com" in href and "transcript" in href.lower():
            return urllib.parse.urljoin(base, href)
    return None


def _extract_summary(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    return ""


def _extract_hosts(soup: BeautifulSoup) -> list[str]:
    return _extract_byline(soup, r"Host(?:ed by)?")


def _extract_guests(soup: BeautifulSoup) -> list[str]:
    return _extract_byline(soup, r"Guests?|Featured")


def _extract_byline(soup: BeautifulSoup, label_pattern: str) -> list[str]:
    pat = re.compile(rf"^({label_pattern})\s*[:\u2014\u2013-]\s*(.+)$", re.I)
    for tag in soup.find_all(["p", "div", "span"]):
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        m = pat.match(text)
        if m:
            return [n.strip() for n in re.split(r",| and ", m.group(2)) if n.strip()]
    return []


def _extract_companion_assets(soup: BeautifulSoup, base: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf"):
            full = urllib.parse.urljoin(base, href)
            if full not in seen:
                seen.add(full)
                out.append(full)
    return out[:20]
