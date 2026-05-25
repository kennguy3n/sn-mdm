"""RICS (Royal Institution of Chartered Surveyors) podcast crawler.

RICS publishes podcasts and policy briefings under
``https://www.rics.org/news-insights/rics-podcasts``. Each
podcast post links to an episode page with a transcript block plus
companion-guide PDFs (RICS' Construction Outlook / Real Estate
Insights, etc.).
"""

from __future__ import annotations

import urllib.parse

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class RicsCrawler(BaseCrawler):
    publisher_id = "rics"
    publisher_name = "Royal Institution of Chartered Surveyors"

    BASE = "https://www.rics.org/news-insights"

    def _episode_url(self, slug: str) -> str:
        return f"{self.BASE}/{slug}"

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        url = self._episode_url(episode_slug)
        resp = self.fetch(url)
        soup = BeautifulSoup(resp.text, "lxml")
        return RawEpisode(
            episode_slug=episode_slug.replace("/", "_"),
            title=self._extract_title(resp.text) or episode_slug,
            primary_url=url,
            publication_date=self._extract_publication_date(resp.text),
            raw_bytes=resp.content,
            content_type="text/html",
            hosts=[],
            guests=[],
            asset_urls=_collect_pdfs(soup, url),
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


def _collect_pdfs(soup: BeautifulSoup, base: str) -> list[str]:
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
