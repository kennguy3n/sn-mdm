"""frog Design Mind frogcast crawler.

frog publishes ``design-mind-frogcast-{slug}`` posts under
``https://www.frog.co/designmind/``. Each post embeds the full
transcript inline with an introductory paragraph that flags it
("Read the full transcripts below").
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class FrogCrawler(BaseCrawler):
    publisher_id = "frog"
    publisher_name = "frog"

    BASE = "https://www.frog.co/designmind"

    def _episode_url(self, slug: str) -> str:
        if slug.startswith("design-mind-frogcast"):
            return f"{self.BASE}/{slug}"
        return f"{self.BASE}/design-mind-frogcast-{slug}"

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        url = self._episode_url(episode_slug)
        resp = self.fetch(url)
        soup = BeautifulSoup(resp.text, "lxml")
        return RawEpisode(
            episode_slug=episode_slug,
            title=self._extract_title(resp.text) or episode_slug,
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
        container = soup.find("article") or soup.find("main") or soup
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _meta_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    return ""
