"""Acquired (acquired.fm) crawler.

Acquired publishes long-form company-deep-dive episodes at
``https://www.acquired.fm/episodes/{slug}``. The episode pages
contain a long transcript section (Acquired explicitly publishes
free-access copyrighted transcripts; see ``docs/SOURCES.md`` for
the rights audit).

The HTML layout puts the transcript inside a ``<div>`` whose
class string contains ``"transcript"``. Show-notes and chapter
markers sit elsewhere on the page — we skip them so the chunker
sees only the transcript text.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

LOG = logging.getLogger(__name__)


_INDEX_URL = "https://www.acquired.fm/episodes"
# Accept relative ("/episodes/<slug>") and absolute
# ("https://www.acquired.fm/episodes/<slug>") hrefs — the index
# emits both depending on whether the link is a card or a deep
# link from a banner / "related episodes" tile.
_INDEX_HREF_RE = re.compile(
    r"^(?:https?://(?:www\.)?acquired\.fm)?/episodes/([a-z0-9][a-z0-9\-]*)/?$"
)


class AcquiredCrawler(BaseCrawler):
    publisher_id = "acquired"
    publisher_name = "Acquired"

    #: Cap on how many slugs ``_discover_episode_slugs`` may emit
    #: from a single index walk. Keeps a one-shot initial sync
    #: from trying to download Acquired's entire 250-episode
    #: archive in one run — the operator can bump the seed list
    #: explicitly when they want deeper coverage.
    DISCOVER_CAP = 25

    def _episode_url(self, slug: str) -> str:
        return f"https://www.acquired.fm/episodes/{slug}"

    def _discover_episode_slugs(self) -> list[str]:
        """Walk the Acquired episode index and pull the first
        :attr:`DISCOVER_CAP` per-episode slugs.

        The /episodes index is plain server-rendered HTML; each
        episode card carries an ``<a href=\"/episodes/<slug>\">``
        link we extract via :data:`_INDEX_HREF_RE`. We filter
        ``href`` to local paths only so the regex doesn't trip
        on absolute social-share URLs that contain the same
        substring.
        """
        try:
            resp = self.fetch(_INDEX_URL)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("acquired: index walk failed: %s", exc)
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        slugs: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            m = _INDEX_HREF_RE.match(a["href"])
            if not m:
                continue
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
        title = (soup.title.get_text(strip=True) if soup.title else episode_slug)
        # Acquired exposes guests / hosts in JSON-LD when present;
        # the layout has changed multiple times — keep a defensive
        # fallback that just uses the configured publisher host.
        hosts = ["Ben Gilbert", "David Rosenthal"]
        guests = _extract_guests_jsonld(soup)
        publication_date = _extract_jsonld_date(soup) or self._extract_publication_date(resp.text)
        summary = _extract_meta_description(soup)
        asset_urls = _extract_episode_asset_urls(soup)
        return RawEpisode(
            episode_slug=episode_slug,
            title=title,
            primary_url=url,
            publication_date=publication_date,
            raw_bytes=resp.content,
            content_type="text/html",
            hosts=hosts,
            guests=guests,
            asset_urls=asset_urls,
            summary=summary,
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()

        # Acquired's transcript container has a class that contains
        # the word "transcript". Match case-insensitively because the
        # layout has shipped both "Transcript" and "transcript".
        container = soup.find(
            lambda tag: tag.name == "div" and any("transcript" in c.lower() for c in tag.get("class", []))
        )
        if container is None:
            # Fall back to the section heading-driven extractor: find
            # the first heading whose text matches /transcript/i and
            # capture everything until the next h2.
            container = _grab_section_by_heading(soup, re.compile(r"transcript", re.I))
        if container is None:
            # Worst case: fall back to the page-wide text — the base
            # cleanup will still produce something useful for
            # debugging.
            return _collapse_blank_lines(soup.get_text("\n"))

        # Translate heading elements within the container to
        # markdown so the chunker can detect section boundaries.
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _extract_meta_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        return og["content"].strip()
    return ""


def _extract_jsonld_date(soup: BeautifulSoup) -> str:
    import json

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), {})
        if not isinstance(data, dict):
            continue
        date = data.get("datePublished") or data.get("uploadDate")
        if isinstance(date, str):
            return date[:10]
    return ""


def _extract_guests_jsonld(soup: BeautifulSoup) -> list[str]:
    import json

    out: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if not isinstance(c, dict):
                continue
            for key in ("actor", "performer", "guest"):
                value = c.get(key)
                if isinstance(value, list):
                    for v in value:
                        if isinstance(v, dict) and isinstance(v.get("name"), str):
                            out.append(v["name"])
                        elif isinstance(v, str):
                            out.append(v)
                elif isinstance(value, dict) and isinstance(value.get("name"), str):
                    out.append(value["name"])
                elif isinstance(value, str):
                    out.append(value)
    seen: set[str] = set()
    deduped: list[str] = []
    for name in out:
        if name not in seen:
            seen.add(name)
            deduped.append(name)
    return deduped


def _extract_episode_asset_urls(soup: BeautifulSoup) -> list[str]:
    """Companion-resource links: external whitepapers / books /
    company filings referenced in the show notes. Skip same-origin
    article links because those are usually navigation.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "javascript:")):
            continue
        if href.startswith("http") and "acquired.fm" not in href and href not in seen:
            seen.add(href)
            urls.append(href)
    return urls[:25]


def _grab_section_by_heading(soup: BeautifulSoup, pattern: re.Pattern[str]) -> Any | None:
    """Find a section whose heading matches `pattern`, return a new
    BeautifulSoup containing only that section's content."""
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        if pattern.search(h.get_text(strip=True) or ""):
            # Collect siblings until the next heading of equal or
            # higher level.
            collected = []
            level = int(h.name[1])
            for sib in h.next_siblings:
                if (
                    getattr(sib, "name", None)
                    and re.match(r"^h[1-6]$", sib.name)
                    and int(sib.name[1]) <= level
                ):
                    break
                collected.append(sib)
            wrapper = soup.new_tag("div")
            for c in collected:
                wrapper.append(c.extract() if hasattr(c, "extract") else c)
            return wrapper
    return None
