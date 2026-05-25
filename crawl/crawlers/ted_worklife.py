"""TED WorkLife with Adam Grant transcript crawler.

TED publishes full transcripts of every WorkLife episode under
``https://www.ted.com/podcasts/worklife/{slug}-transcript``. The
transcript pages are CC BY-NC-ND licensed (see ``docs/SOURCES.md``)
— that allows full ingest as long as the rights summary is
preserved on every chunk.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class TedWorklifeCrawler(BaseCrawler):
    publisher_id = "ted_worklife"
    publisher_name = "TED"

    def _episode_url(self, slug: str) -> str:
        return f"https://www.ted.com/podcasts/worklife/{slug}-transcript"

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        url = self._episode_url(episode_slug)
        resp = self.fetch(url)
        soup = BeautifulSoup(resp.text, "lxml")
        return RawEpisode(
            episode_slug=episode_slug,
            title=_extract_title(soup) or episode_slug,
            primary_url=url,
            publication_date=self._extract_publication_date(resp.text),
            raw_bytes=resp.content,
            content_type="text/html",
            hosts=["Adam Grant"],
            guests=[],
            asset_urls=[],
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        # TED renders the transcript inside a div with class
        # "Transcript__transcript" (CSS-Modules suffix may change;
        # match on the prefix).
        container = soup.find(
            lambda tag: tag.name == "div" and any(
                c.startswith("Transcript__transcript") or "transcript-text" in c
                for c in tag.get("class", [])
            )
        )
        if container is None:
            container = soup.find("article") or soup
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _extract_title(soup: BeautifulSoup) -> str:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    return soup.title.get_text(strip=True) if soup.title else ""


def _meta_description(soup: BeautifulSoup) -> str:
    for attr in (
        ("property", "og:description"),
        ("name", "description"),
    ):
        tag = soup.find("meta", attrs={attr[0]: attr[1]})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""
