"""IMD Business School podcast crawler.

IMD publishes podcasts under
``https://www.imd.org/ibyimd/podcasts/{slug}`` (the public
permalink). The ``/ibyimd/category/podcasts/`` index page lists
every episode and we walk that to enumerate slugs. The site
follows a standard WordPress layout — body text inside
``<article>``, header inside ``<header>``.

Some episodes live under a series prefix like
``ibyimd/podcasts/leaders-unplugged/<slug>``. The discovery
walker preserves the *full path tail* after ``/ibyimd/podcasts/``
as the slug so the canonical fetch hits the right URL.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

LOG = logging.getLogger(__name__)

_INDEX_URL = "https://www.imd.org/ibyimd/category/podcasts/"
# The slug capture allows ``/`` so that nested series prefixes
# such as ``leaders-unplugged/<episode>`` are preserved verbatim,
# but the negative lookahead rejects WordPress pagination links
# (``/podcasts/page/2``, ``/podcasts/page/3``, …) which would
# otherwise be ingested as if they were episode pages. The
# trailing ``[a-z0-9]`` anchor on the capture forces at least one
# non-slash terminal segment so we don't accept directory-style
# slugs like ``leaders-unplugged/`` either.
# The domain group is optional so the walker matches both
# absolute (``https://www.imd.org/ibyimd/podcasts/<slug>``) and
# relative (``/ibyimd/podcasts/<slug>``) hrefs. WordPress themes
# frequently emit relative links from the same template
# rendering pass that produces canonical permalinks elsewhere
# on the page, so anchoring on the absolute form alone would
# silently miss episodes — mirror the same handling our other
# HTML-index walkers use (acquired, exit_five, frog).
_HREF_RE = re.compile(
    r"^(?:https?://(?:www\.)?imd\.org)?/ibyimd/podcasts/"
    r"(?!page/)"
    r"([a-z0-9][a-z0-9\-/]*[a-z0-9])/?$"
)


class ImdCrawler(BaseCrawler):
    publisher_id = "imd"
    publisher_name = "IMD Business School"
    DISCOVER_CAP = 25

    def _episode_url(self, slug: str) -> str:
        return f"https://www.imd.org/ibyimd/podcasts/{slug}"

    def _discover_episode_slugs(self) -> list[str]:
        try:
            resp = self.fetch(_INDEX_URL)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("imd: index walk failed: %s", exc)
            return []
        slugs: list[str] = []
        seen: set[str] = set()
        for a in BeautifulSoup(resp.text, "lxml").find_all("a", href=True):
            m = _HREF_RE.match(a["href"])
            if not m:
                continue
            slug = m.group(1)
            if not slug or slug in seen:
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
            hosts=[],
            guests=_extract_guests(soup),
            asset_urls=[],
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        container = soup.find("article") or soup
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
            r"^(?:Guest|Featuring|Interviewed)\s*[:\u2014\u2013-]\s*(.+)$",
            p.get_text(" ", strip=True),
            flags=re.I,
        )
        if m:
            return [n.strip() for n in re.split(r",| and ", m.group(1)) if n.strip()]
    return []
