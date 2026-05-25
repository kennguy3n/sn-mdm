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
        """Lift the verbatim transcript out of the Masters of Scale page.

        MoS pages introduce the transcript with an ``<h2>`` whose
        text starts with ``Transcript:``. The original Tranche-1
        implementation walked ``target.next_siblings`` to collect
        the transcript paragraphs — but the heading is nested
        inside a sticky wrapper ``<div class="flex md:block …">``
        that holds an "Open / Close" chevron toggle. The only
        direct sibling of the heading inside that wrapper is the
        chevron-toggle ``<div>``; the actual ``<p>`` paragraphs of
        the transcript are siblings of that wrapper, one level
        up. The previous behaviour therefore extracted exactly the
        chevron-toggle text ("Open chevron-down Close chevron-up")
        — identical across every episode page — and the content-
        hash dedup gate folded 24 of the 25 discovered episodes
        into one admitted row.

        The architecturally correct walk is :meth:`find_all_next`,
        which descends the entire forward DOM from the heading,
        capturing every ``<p>``, ``<li>`` and ``<blockquote>``
        regardless of which wrapper ``<div>`` they sit inside.
        We stop at the next top-level heading (``h1``/``h2``); on
        MoS pages there's no such heading after the transcript,
        so the walk runs to end-of-(content-)DOM. The ``nav`` /
        ``footer`` / ``header`` / ``iframe`` / ``script`` strip
        already ran above, so trailing site-chrome paragraphs
        ("Sign up for the newsletter…") aren't in scope.

        Episodes published before MoS started shipping
        transcripts return an empty string from this method.
        ``_collapse_blank_lines("")`` is the empty string, every
        such episode therefore lands in the same dedup bucket
        instead of polluting the catalogue with one "header-only"
        row per audio-only show — which is what we want.

        Two further defences-in-depth:

        * Captured candidates are tracked by ``id()`` and any
          descendant of a previously-captured candidate is
          skipped — otherwise a transcript paragraph wrapped in
          a ``<blockquote>`` would emit twice (once for the
          blockquote and once for the inner ``<p>``) and inflate
          the dedup key.

        * Structural sub-headings (``h3``-``h6``) inside the
          transcript section are rendered as markdown headings so
          the chunker can use them as section boundaries. Only
          ``h1``/``h2`` are stop conditions; they bound the
          transcript section, not its body.
        """
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        # Only headings that *start with* the word "Transcript"
        # qualify — bare ``"transcript" in text.lower()`` would
        # also match titles like "An older episode without a
        # transcript", which is the wrong anchor and would emit
        # the page chrome as a "transcript" for audio-only shows.
        target = None
        for h in soup.find_all(re.compile(r"^h[1-6]$")):
            text = h.get_text(" ", strip=True).lower()
            if re.match(r"^transcript\b", text):
                target = h
                break
        if target is None:
            return ""
        blocks: list[str] = []
        captured_ids: set[int] = set()
        sub_heading_re = re.compile(r"^h[3-6]$")
        for sib in target.find_all_next():
            name = getattr(sib, "name", None)
            if name is None:
                continue
            if re.match(r"^h[1-2]$", name):
                break
            # Skip descendants of an element we already emitted —
            # otherwise ``<blockquote><p>X</p></blockquote>``
            # yields X twice. ``parents`` iterates from immediate
            # parent up to the document root.
            if any(id(ancestor) in captured_ids for ancestor in sib.parents):
                continue
            if name in ("p", "li", "blockquote"):
                text = sib.get_text(" ", strip=True)
                if text:
                    blocks.append(text)
                    captured_ids.add(id(sib))
                continue
            if sub_heading_re.match(name):
                text = sib.get_text(" ", strip=True)
                if text:
                    level = int(name[1])
                    blocks.append(f"{'#' * level} {text}")
                    captured_ids.add(id(sib))
        return _collapse_blank_lines("\n\n".join(blocks))


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
