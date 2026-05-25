"""World Economic Forum Radio Davos crawler.

The WEF publishes Radio Davos episodes at
``https://www.weforum.org/podcasts/radio-davos/episodes/{slug}``.
Auto-generated transcripts sit behind a "Read the transcript" /
"Transcript" toggle and render in a div with ``id`` containing
"transcript".
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class WefRadioDavosCrawler(BaseCrawler):
    publisher_id = "wef_radio_davos"
    publisher_name = "World Economic Forum"

    def _episode_url(self, slug: str) -> str:
        return f"https://www.weforum.org/podcasts/radio-davos/episodes/{slug}"

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
            hosts=["Robin Pomeroy"],
            guests=_extract_guests(soup),
            asset_urls=[],
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        # Many WEF episode pages flag the transcript with id*=transcript.
        container = soup.find(
            lambda tag: tag.name in ("div", "section")
            and (
                "transcript" in (tag.get("id") or "").lower()
                or any("transcript" in c.lower() for c in tag.get("class", []))
            )
        ) or soup.find("article") or soup
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _meta_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    return ""


def _extract_guests(soup: BeautifulSoup) -> list[str]:
    for p in soup.find_all(["p", "li", "span"]):
        m = re.match(
            r"^(?:Guest|Featuring|With)\s*[:\u2014\u2013-]\s*(.+)$",
            p.get_text(" ", strip=True),
            flags=re.I,
        )
        if m:
            return [n.strip() for n in re.split(r",| and ", m.group(1)) if n.strip()]
    return []
