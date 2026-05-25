"""Exit Five podcast crawler.

Exit Five publishes B2B marketing podcast episodes at
``https://www.exitfive.com/podcast/{slug}``. Each episode page
carries a section headed ``"Transcription"`` with the full
transcript text in paragraph form (speaker labels follow the
``"NAME:"`` convention).
"""

from __future__ import annotations

import re
import urllib.parse

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class ExitFiveCrawler(BaseCrawler):
    publisher_id = "exit_five"
    publisher_name = "Exit Five"

    def _episode_url(self, slug: str) -> str:
        return f"https://www.exitfive.com/podcast/{slug}"

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
            hosts=["Dan Murphy"],
            guests=_extract_guests(soup),
            asset_urls=_collect_assets(soup, url),
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        # Find the "Transcription" heading and collect everything
        # that follows until the next h1/h2.
        target = None
        for h in soup.find_all(re.compile(r"^h[1-6]$")):
            if re.search(r"transcription|transcript", h.get_text(" ", strip=True), flags=re.I):
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
    for p in soup.find_all(["p", "li"]):
        m = re.match(
            r"^Guest(?:s)?\s*[:\u2014\u2013-]\s*(.+)$",
            p.get_text(" ", strip=True),
            flags=re.I,
        )
        if m:
            return [n.strip() for n in re.split(r",| and ", m.group(1)) if n.strip()]
    return []


def _collect_assets(soup: BeautifulSoup, base: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = urllib.parse.urljoin(base, href)
        if "exitfive.com" in href:
            continue
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out[:20]
