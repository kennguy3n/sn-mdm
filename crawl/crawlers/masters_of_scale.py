"""Masters of Scale crawler.

Masters of Scale publishes podcast episodes at
``https://mastersofscale.com/episode/{slug}``. Episode pages
include a transcript section behind a heading that contains the
word "Transcript" — extraction strategy matches Exit Five's.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

LOG = logging.getLogger(__name__)

_EPISODE_SITEMAP_URL = "https://mastersofscale.com/episode-sitemap.xml"
_EPISODE_LOC_RE = re.compile(
    r"<loc>\s*https?://mastersofscale\.com/episode/([a-z0-9][a-z0-9\-]*)/?\s*</loc>"
)


class MastersOfScaleCrawler(BaseCrawler):
    publisher_id = "masters_of_scale"
    publisher_name = "Masters of Scale"
    DISCOVER_CAP = 25

    def _episode_url(self, slug: str) -> str:
        return f"https://mastersofscale.com/episode/{slug}"

    def _discover_episode_slugs(self) -> list[str]:
        """Pull slugs from the WordPress ``episode-sitemap.xml``.
        Masters of Scale exposes 700+ episode permalinks there in
        publication order (newest first); the cap keeps the
        initial sync to a manageable window.
        """
        try:
            resp = self.fetch(_EPISODE_SITEMAP_URL)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("masters_of_scale: sitemap fetch failed: %s", exc)
            return []
        slugs: list[str] = []
        seen: set[str] = set()
        for m in _EPISODE_LOC_RE.finditer(resp.text):
            slug = m.group(1)
            if slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
            if len(slugs) >= self.DISCOVER_CAP:
                break
        return slugs

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
            hosts=["Reid Hoffman"],
            guests=_extract_guests(soup),
            asset_urls=_collect_links(soup, url),
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        # Match a heading whose text contains "Transcript" and collect siblings.
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
            r"^(?:Guest|Featuring|With)\s*[:\u2014\u2013-]\s*(.+)$",
            p.get_text(" ", strip=True),
            flags=re.I,
        )
        if m:
            return [n.strip() for n in re.split(r",| and ", m.group(1)) if n.strip()]
    return []


def _collect_links(soup: BeautifulSoup, base: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urllib.parse.urljoin(base, href)
        if "mastersofscale.com" in full or not full.startswith("http"):
            continue
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out[:20]
