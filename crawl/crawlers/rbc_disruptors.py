"""RBC Disruptors podcast crawler.

RBC Disruptors publishes episodes at
``https://www.rbc.com/en/thought-leadership/disruptors/{slug}``.
Each page hosts an audio transcript section.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class RbcDisruptorsCrawler(BaseCrawler):
    publisher_id = "rbc_disruptors"
    publisher_name = "RBC Disruptors"

    def _episode_url(self, slug: str) -> str:
        return f"https://www.rbc.com/en/thought-leadership/disruptors/{slug}"

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
            hosts=["John Stackhouse"],
            guests=_extract_guests(soup),
            asset_urls=[],
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        target = None
        for h in soup.find_all(re.compile(r"^h[1-6]$")):
            if "transcript" in h.get_text(" ", strip=True).lower():
                target = h
                break
        if target is None:
            container = soup.find("article") or soup
        else:
            container = soup.new_tag("div")
            for sib in list(target.next_siblings):
                if getattr(sib, "name", None) and re.match(r"^h[1-2]$", sib.name):
                    break
                if hasattr(sib, "extract"):
                    container.append(sib.extract())
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
            r"^Guest(?:s)?\s*[:\u2014\u2013-]\s*(.+)$",
            p.get_text(" ", strip=True),
            flags=re.I,
        )
        if m:
            return [n.strip() for n in re.split(r",| and ", m.group(1)) if n.strip()]
    return []
