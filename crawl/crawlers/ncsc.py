"""UK NCSC Toolkit-for-Boards audio-transcript crawler.

The UK National Cyber Security Centre publishes OGL v3 transcripts
under ``https://www.ncsc.gov.uk/information/toolkit-for-boards-audio-transcripts``.
The page links to per-section transcript pages which carry the
canonical OGL v3 licence statement.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class NcscCrawler(BaseCrawler):
    publisher_id = "ncsc"
    publisher_name = "UK National Cyber Security Centre"

    BASE = "https://www.ncsc.gov.uk"

    def _episode_url(self, slug: str) -> str:
        # `slug` may be a relative path (e.g.
        # "information/toolkit-for-boards-audio-transcripts/...") or
        # the empty string (hub page).
        return f"{self.BASE}/{slug.lstrip('/')}" if slug else f"{self.BASE}/information/toolkit-for-boards-audio-transcripts"

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        url = self._episode_url(episode_slug)
        resp = self.fetch(url)
        soup = BeautifulSoup(resp.text, "lxml")
        return RawEpisode(
            episode_slug=(episode_slug or "hub").replace("/", "_"),
            title=self._extract_title(resp.text) or "NCSC Toolkit for Boards",
            primary_url=url,
            publication_date=self._extract_publication_date(resp.text),
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
        container = soup.find("main") or soup.find("article") or soup
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _meta_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    return ""
