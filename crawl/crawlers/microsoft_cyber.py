"""Microsoft public sector cyber podcast crawler.

Microsoft's worldwide public sector ("WWPS") site hosts two
podcast shows:

* ``Public Sector Future`` — permalink prefix
  ``https://wwps.microsoft.com/episodes/{slug}/``
* ``Future of Infrastructure`` — permalink prefix
  ``https://wwps.microsoft.com/infrastructure-episodes/{slug}/``

Slugs in this crawler are namespaced as ``"<prefix>/<slug>"`` so
both shows can coexist in the same registry without ambiguity.
Many episode pages link to a transcript PDF; we follow that when
present, otherwise we extract from the HTML.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

LOG = logging.getLogger(__name__)

_SITEMAP_URLS = (
    "https://wwps.microsoft.com/post-sitemap1.xml",
    "https://wwps.microsoft.com/post-sitemap2.xml",
)
_EPISODE_LOC_RE = re.compile(
    r"<loc>\s*https?://wwps\.microsoft\.com/"
    r"((?:episodes|infrastructure-episodes)/[a-z0-9][a-z0-9\-]*)/?\s*</loc>"
)


class MicrosoftCyberCrawler(BaseCrawler):
    publisher_id = "microsoft_cyber"
    publisher_name = "Microsoft Worldwide Public Sector"
    DISCOVER_CAP = 25

    BASE = "https://wwps.microsoft.com"

    def _episode_url(self, slug: str) -> str:
        # Slug is "<prefix>/<slug>" where prefix is either
        # ``episodes`` or ``infrastructure-episodes``.
        return f"{self.BASE}/{slug}"

    def _discover_episode_slugs(self) -> list[str]:
        slugs: list[str] = []
        seen: set[str] = set()
        for sitemap_url in _SITEMAP_URLS:
            try:
                resp = self.fetch(sitemap_url)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("microsoft_cyber: %s failed: %s", sitemap_url, exc)
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
        url = self._episode_url(episode_slug)
        resp = self.fetch(url)
        soup = BeautifulSoup(resp.text, "lxml")
        title = self._extract_title(resp.text) or episode_slug
        publication_date = self._extract_publication_date(resp.text)

        pdf_url = _find_transcript_pdf(soup, url)
        if pdf_url:
            pdf_resp = self.fetch(pdf_url, accept="application/pdf")
            return RawEpisode(
                episode_slug=episode_slug,
                title=title,
                primary_url=url,
                publication_date=publication_date,
                raw_bytes=pdf_resp.content,
                content_type="application/pdf",
                hosts=[],
                guests=[],
                asset_urls=[pdf_url],
                summary=_meta_description(soup),
            )
        return RawEpisode(
            episode_slug=episode_slug,
            title=title,
            primary_url=url,
            publication_date=publication_date,
            raw_bytes=resp.content,
            content_type="text/html",
            hosts=[],
            guests=[],
            asset_urls=[],
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        container = soup.find("article") or soup.find("main") or soup
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _find_transcript_pdf(soup: BeautifulSoup, base: str) -> str | None:
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text(strip=True) or "").lower()
        if href.lower().endswith(".pdf") and (
            "transcript" in text or "transcript" in href.lower()
        ):
            return urllib.parse.urljoin(base, href)
    return None


def _meta_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    return ""
