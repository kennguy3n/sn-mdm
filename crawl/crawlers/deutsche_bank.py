"""Deutsche Bank corporate podcast crawler.

Deutsche Bank publishes corporate banking podcasts at
``https://corporates.db.com/multimedia/{slug}`` and supporting
news / explainer content at ``https://www.db.com/news/detail/{slug}``.
The transcript / companion-guide PDF, when present, is linked from
the main page.
"""

from __future__ import annotations

import urllib.parse

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class DeutscheBankCrawler(BaseCrawler):
    publisher_id = "deutsche_bank"
    publisher_name = "Deutsche Bank"

    CORPORATES_BASE = "https://corporates.db.com/multimedia"

    def _episode_url(self, slug: str) -> str:
        if slug.startswith("news/"):
            return f"https://www.db.com/{slug}"
        return f"{self.CORPORATES_BASE}/{slug}"

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        url = self._episode_url(episode_slug)
        resp = self.fetch(url)
        soup = BeautifulSoup(resp.text, "lxml")
        title = self._extract_title(resp.text) or episode_slug
        publication_date = self._extract_publication_date(resp.text)

        # Prefer the transcript PDF when one exists.
        pdf_url = _find_pdf(soup, url, keywords=("transcript", "companion guide", "guide"))
        if pdf_url:
            pdf_resp = self.fetch(pdf_url, accept="application/pdf")
            return RawEpisode(
                episode_slug=episode_slug.replace("/", "_"),
                title=title,
                primary_url=url,
                publication_date=publication_date,
                raw_bytes=pdf_resp.content,
                content_type="application/pdf",
                hosts=[],
                guests=[],
                asset_urls=[pdf_url, *_other_pdfs(soup, url, pdf_url)],
                summary=_meta_description(soup),
            )
        return RawEpisode(
            episode_slug=episode_slug.replace("/", "_"),
            title=title,
            primary_url=url,
            publication_date=publication_date,
            raw_bytes=resp.content,
            content_type="text/html",
            hosts=[],
            guests=[],
            asset_urls=_other_pdfs(soup, url, None),
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


def _find_pdf(soup: BeautifulSoup, base: str, keywords: tuple[str, ...]) -> str | None:
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        text = (a.get_text(strip=True) or "").lower()
        if any(k in text for k in keywords):
            return urllib.parse.urljoin(base, href)
    return None


def _other_pdfs(soup: BeautifulSoup, base: str, exclude: str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        full = urllib.parse.urljoin(base, href)
        if full == exclude or full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out[:20]
