"""Deutsche Bank flow InCorporate Treasury podcast crawler.

Deutsche Bank publishes its ``flow InCorporate Treasury`` podcast
under ``https://flow.db.com/media/flow-incorporatetreasury-podcasts/``.
Each episode lives at
``/media/flow-incorporatetreasury-podcasts/{episode_slug}`` and
the corresponding verbatim transcript at
``/media/flow-incorporatetreasury-podcasts/{episode_slug}-transcript``.

History
-------

The original Tranche 1 crawler pointed at
``corporates.db.com/multimedia/podcasts`` and assumed that hub
listed transcript-bearing episodes. Reconnaissance for this
PR confirmed that hub only exposes two series — ``esg-insights``
and ``mark-to-market`` — both **audio-only**, and links out to
the actual transcripted series on ``flow.db.com``. Pointing the
crawler at ``flow.db.com`` directly gives us the only Deutsche
Bank transcripts that are actually published on the open web.

Why a plain ``requests`` GET works
----------------------------------

``flow.db.com`` ships fully server-rendered HTML for both the
hub and per-episode pages. No headless transport is required;
``BaseCrawler.fetch`` (which honours robots.txt and the polite
rate limit) is sufficient.

Extraction shape
----------------

* Hub page lists each episode as a permalink under
  ``/media/flow-incorporatetreasury-podcasts/{slug}`` — including
  episodes that don't (yet) have a transcript permalink. We
  enumerate all of those during discovery; the per-episode
  fetch resolves the transcript link if one is present.
* Episode pages embed an explicit link labelled with anchor
  text containing ``transcription`` or ``transcript`` and
  pointing at the matching ``-transcript`` permalink.
* Transcript pages render an ``<h2>Transcript - Episode N:
  …</h2>`` heading followed by ``<p>`` paragraphs in
  ``Speaker (initials): …`` format — the chunker's speaker-turn
  detector handles these directly.
* When an episode does not (yet) have a published transcript
  permalink, we fall back to the landing page as the raw
  payload so a future re-crawl after Deutsche Bank publishes
  the transcript picks up the change without code edits.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

LOG = logging.getLogger(__name__)

_HUB_URL = "https://flow.db.com/media/flow-incorporatetreasury-podcasts/"
_SERIES_PREFIX = "/media/flow-incorporatetreasury-podcasts/"

# Episode permalinks are flat under the series prefix. We
# deliberately reject the hub itself (no slug component) and
# any ``-transcript`` permalink (those are the transcript
# companion pages — discovered transitively from the episode
# page, not from the hub). ``[a-z0-9\-]+`` keeps the slug
# ASCII-only and dash-separated; live page slugs verified
# during reconnaissance.
_EPISODE_HREF_RE = re.compile(
    r'href="(?:https?://flow\.db\.com)?/media/flow-incorporatetreasury-podcasts/'
    r"([a-z0-9][a-z0-9\-]+[a-z0-9])"
    r'(?:[?#][^"]*)?/?"'
)
_TRANSCRIPT_SUFFIX = "-transcript"


class DeutscheBankCrawler(BaseCrawler):
    publisher_id = "deutsche_bank"
    publisher_name = "Deutsche Bank"
    DISCOVER_CAP = 25

    BASE_URL = "https://flow.db.com"

    def _episode_url(self, slug: str) -> str:
        return f"{self.BASE_URL}{_SERIES_PREFIX}{slug}"

    def _transcript_url(self, slug: str) -> str:
        return f"{self.BASE_URL}{_SERIES_PREFIX}{slug}{_TRANSCRIPT_SUFFIX}"

    def _discover_episode_slugs(self) -> list[str]:
        try:
            resp = self.fetch(_HUB_URL)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("deutsche_bank: hub fetch failed: %s", exc)
            return []
        slugs: list[str] = []
        seen: set[str] = set()
        for m in _EPISODE_HREF_RE.finditer(resp.text):
            slug = m.group(1)
            # Skip the ``-transcript`` companion pages — we
            # discover them via the episode page they're paired
            # with, never as standalone "episodes".
            if slug.endswith(_TRANSCRIPT_SUFFIX):
                continue
            if slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
            if len(slugs) >= self.DISCOVER_CAP:
                break
        return slugs

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        landing_url = self._episode_url(episode_slug)
        landing_resp = self.fetch(landing_url)
        soup = BeautifulSoup(landing_resp.text, "lxml")
        title = self._extract_title(landing_resp.text) or episode_slug
        publication_date = self._extract_publication_date(landing_resp.text)
        summary = _meta_description(soup)

        transcript_href = _find_transcript_link(soup, landing_url, episode_slug)
        if transcript_href:
            try:
                transcript_resp = self.fetch(transcript_href)
            except Exception as exc:  # noqa: BLE001
                # Best-effort: if the transcript page itself
                # 404s we still keep the landing page as the
                # ingest payload so the rights-gate + metadata
                # row are recorded and a future re-crawl can
                # pick up the transcript when it appears.
                LOG.warning(
                    "deutsche_bank: transcript fetch failed for %s: %s",
                    episode_slug,
                    exc,
                )
            else:
                return RawEpisode(
                    episode_slug=episode_slug,
                    title=title,
                    # Use the transcript URL as ``primary_url`` so
                    # every chunk's citation anchor lands on the
                    # transcript page (the actual content
                    # readers want to audit) rather than the
                    # audio-only landing.
                    primary_url=transcript_href,
                    publication_date=publication_date,
                    raw_bytes=transcript_resp.content,
                    content_type="text/html",
                    hosts=[],
                    guests=[],
                    asset_urls=[landing_url],
                    summary=summary,
                )

        return RawEpisode(
            episode_slug=episode_slug,
            title=title,
            primary_url=landing_url,
            publication_date=publication_date,
            raw_bytes=landing_resp.content,
            content_type="text/html",
            hosts=[],
            guests=[],
            asset_urls=[],
            summary=summary,
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        """Lift the verbatim transcript out of the page.

        Transcript pages render the body as a sequence of
        ``<p>`` tags after an ``<h2>Transcript - Episode N:
        …</h2>`` heading. Landing pages don't have that anchor
        — for those we fall back to ``<article>`` / ``<main>``.

        Stripping site chrome (header, footer, nav, scripts,
        forms) is universal across both shapes; the transcript
        body itself never legitimately lives inside any of
        those tags.
        """
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(
            [
                "script",
                "style",
                "noscript",
                "iframe",
                "nav",
                "footer",
                "header",
                "form",
                "button",
                "aside",
            ]
        ):
            tag.decompose()

        target = None
        for h in soup.find_all(re.compile(r"^h[1-3]$")):
            text = h.get_text(" ", strip=True)
            if re.match(r"^Transcript\b", text, flags=re.I):
                target = h
                break

        if target is not None:
            container = soup.new_tag("div")
            container.append(soup.new_tag("h2"))
            container.h2.string = target.get_text(" ", strip=True)
            for sib in list(target.next_siblings):
                if getattr(sib, "name", None) and re.match(
                    r"^h[1-2]$", sib.name
                ):
                    break
                if hasattr(sib, "extract"):
                    container.append(sib.extract())
        else:
            container = soup.find("article") or soup.find("main") or soup

        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _meta_description(soup: BeautifulSoup) -> str:
    for sel in (
        ("property", "og:description"),
        ("name", "description"),
    ):
        tag = soup.find("meta", attrs={sel[0]: sel[1]})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _find_transcript_link(
    soup: BeautifulSoup, base: str, episode_slug: str
) -> str | None:
    """Return the transcript permalink for the current episode.

    On live Deutsche Bank pages the transcript anchor often has
    bare anchor text (``"here"``) with the descriptive label
    living in the ``title=`` attribute. So we don't rely on
    anchor text alone — the strongest signal is the href shape
    itself.

    Three signals, in priority order:

    1. Any anchor whose href, resolved against the page base,
       points at the exact ``{episode_slug}-transcript`` permalink
       under the series prefix. This is the canonical signal —
       no anchor-text heuristic needed.
    2. Any anchor whose anchor text or ``title`` attribute
       mentions "transcript" / "transcription" and points back
       under the same series prefix. Catches future label
       reshuffles or unusual hash anchors.
    3. ``None`` — the caller falls back to the landing page as
       the ingest payload (rights gate + governance row still
       recorded; future re-crawl can pick the transcript up).
    """
    expected_path = f"{_SERIES_PREFIX}{episode_slug}{_TRANSCRIPT_SUFFIX}"
    for a in soup.find_all("a", href=True):
        full = urllib.parse.urljoin(base, a["href"])
        if expected_path in full:
            return full

    for a in soup.find_all("a", href=True):
        text = (a.get_text(" ", strip=True) or "").lower()
        title = (a.get("title") or "").lower()
        if (
            "transcript" not in text
            and "transcription" not in text
            and "transcript" not in title
            and "transcription" not in title
        ):
            continue
        full = urllib.parse.urljoin(base, a["href"])
        # Same-series filter: reject links to unrelated
        # transcripts that happen to share the keyword
        # (cross-series CTAs, "transcripts archive" links).
        if _SERIES_PREFIX in full and _TRANSCRIPT_SUFFIX in full:
            return full
    return None
