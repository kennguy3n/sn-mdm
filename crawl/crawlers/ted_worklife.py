"""TED WorkLife transcript crawler.

TED publishes per-episode transcripts at
``https://www.ted.com/podcasts/{slug}-transcript`` and lists the
currently available transcripts on the hub page
``https://www.ted.com/podcasts/worklife-transcripts``.

History
-------

When ``sn-mdm`` first ran Tranche 1 the WorkLife show was hosted
by Adam Grant and the transcript URL pattern was
``/podcasts/worklife/{slug}-transcript``. TED has since rebranded
the show to ``WorkLife with Molly Graham`` and flattened the URL
structure to ``/podcasts/{slug}-transcript`` (the show prefix
``worklife/`` is gone). The hub
``/podcasts/worklife-transcripts`` is the canonical list of
publicly hosted transcripts under the new URL scheme.

That migration is why this crawler is HTML-discovery driven (no
sitemap covers the ``/podcasts/`` namespace): TED's own
``/sitemap.xml`` indexes talks, speakers, playlists and topics
— not podcast episodes. The hub page is the only canonical
listing TED ships.

Why a plain ``requests`` GET works
----------------------------------

Both the hub page and the transcript pages are server-rendered
HTML — no JS hydration is required to read the listing or the
transcript text. We therefore avoid the headless-browser
transport (which is slower, heavier, and needs Chromium
installed) and let :meth:`BaseCrawler.fetch` do the work.
``robots.txt`` and rate-limiting are still honoured.

Rights
------

TED publishes WorkLife transcripts under CC BY-NC-ND 4.0
(``cc_by_nc_nd``). The rights gate admits ``cc_by_nc_nd`` via
the NC + citation_anchor carve-out documented in
``docs/SOURCES.md`` — every chunk preserves the source URL so a
reader can audit the lineage.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

LOG = logging.getLogger(__name__)

_HUB_URL = "https://www.ted.com/podcasts/worklife-transcripts"

# Transcript permalinks always end in ``-transcript`` and live
# directly under ``/podcasts/``. We capture the per-episode slug
# (everything between ``/podcasts/`` and ``-transcript``) so the
# stored ``episode_slug`` is the same human-readable handle the
# URL exposes.
#
# The live TED hub mixes three href shapes for the same
# canonical URL — verified during Milestone-A2 reconnaissance:
#
#   1. ``/podcasts/{slug}-transcript``                (root-relative)
#   2. ``https://www.ted.com/podcasts/{slug}-transcript``  (absolute)
#   3. ``ted.com/podcasts/{slug}-transcript``        (bare host, no scheme)
#
# Shape #3 is unusual (it's a relative href that browsers
# resolve oddly) but TED's hub really does emit it for some
# entries — likely a NextJS data-binding quirk. The host
# component is therefore optional in the regex; the
# ``/podcasts/`` anchor is what disambiguates an episode
# permalink from other links on the hub.
#
# We deliberately do NOT match the hub page itself
# (``/podcasts/worklife-transcripts``) — see the
# ``_NON_EPISODE_SLUGS`` allow-list below.
_TRANSCRIPT_HREF_RE = re.compile(
    r'href="'
    r"(?:https?://)?(?:www\.)?(?:ted\.com)?/?"
    r"podcasts/"
    r"([a-z0-9][a-z0-9\-]*?)-transcript/?"
    r'(?:[?#][^"]*)?"'
)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]+[a-z0-9]$")

# Slugs to skip: the hub page, the (legacy) show landing page,
# and a handful of TED-wide CTAs we've observed on the hub.
_NON_EPISODE_SLUGS = frozenset(
    {
        "",
        "worklife",
        "worklife-transcripts",
        "subscribe",
    }
)


class TedWorklifeCrawler(BaseCrawler):
    publisher_id = "ted_worklife"
    publisher_name = "TED"
    DISCOVER_CAP = 25

    def _episode_url(self, slug: str) -> str:
        return f"https://www.ted.com/podcasts/{slug}-transcript"

    def _discover_episode_slugs(self) -> list[str]:
        try:
            resp = self.fetch(_HUB_URL)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ted_worklife: hub fetch failed: %s", exc)
            return []
        slugs: list[str] = []
        seen: set[str] = set()
        for m in _TRANSCRIPT_HREF_RE.finditer(resp.text):
            slug = m.group(1)
            if slug in _NON_EPISODE_SLUGS:
                continue
            if not _SLUG_RE.match(slug):
                continue
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
        title = _extract_episode_title(soup) or episode_slug
        publication_date = _extract_publication_date(soup)
        return RawEpisode(
            episode_slug=episode_slug,
            title=title,
            primary_url=url,
            publication_date=publication_date,
            raw_bytes=resp.content,
            content_type="text/html",
            # The current run is hosted by Molly Graham (the show
            # rebranded from Adam Grant in 2024). Recording the
            # *current* host on every episode would be wrong for
            # the older Adam Grant transcripts if TED ever
            # re-publishes them — so we leave ``hosts`` empty and
            # let the normaliser surface the speaker labels from
            # the transcript body, which is what ``chunk_normalised_text``
            # uses anyway.
            hosts=[],
            guests=[],
            asset_urls=[],
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        """Lift the verbatim transcript out of TED's NextJS layout.

        TED's transcript pages put the entire body inside
        ``<main>`` and render the transcript as a sequence of
        ``<p>`` tags. The first paragraph is the show + date
        line (e.g. ``How to make AI worth your time…  May 19, 2026``)
        and the second is a disclaimer (``Please note the
        following transcript may not exactly match the final
        audio…``) — both are kept because they're real content
        users would expect to read at the top of a transcript.

        Everything outside ``<main>`` is site chrome (header,
        sidebar, related-shows rail, footer) and is dropped.
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
        container = soup.find("main") or soup.find("article") or soup
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


def _extract_episode_title(soup: BeautifulSoup) -> str:
    """Pull the episode title from the most authoritative source.

    Order:
      1. ``<meta property="og:title">`` — TED keeps the
         human-readable title here even on transcript pages
         where the ``<title>`` element is suffixed with
         ``" (Transcript)"``.
      2. ``<title>`` — strip the trailing ``" (Transcript)"``
         suffix TED appends so the stored title matches the
         episode page.
    """
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return _strip_transcript_suffix(og["content"].strip())
    if soup.title:
        return _strip_transcript_suffix(soup.title.get_text(strip=True))
    return ""


_TRANSCRIPT_SUFFIX_RE = re.compile(r"\s*[(\[]?\s*transcript\s*[)\]]?\s*$", re.I)


def _strip_transcript_suffix(title: str) -> str:
    return _TRANSCRIPT_SUFFIX_RE.sub("", title).strip()


def _extract_publication_date(soup: BeautifulSoup) -> str:
    """Try several signals in priority order:

    1. ``<meta property="article:published_time">`` — TED ships
       this on most transcript pages.
    2. ``<meta property="og:updated_time">`` — TED's fallback
       for older posts that pre-date the article:published_time
       roll-out.
    3. ``<time datetime="...">`` element inside ``<main>``.

    Returns an empty string when no signal is available rather
    than guessing; the pipeline tolerates missing dates.
    """
    for prop in ("article:published_time", "og:updated_time"):
        meta = soup.find("meta", attrs={"property": prop})
        if meta and meta.get("content"):
            return meta["content"][:10]
    container = soup.find("main") or soup
    time_tag = container.find("time")
    if time_tag and time_tag.get("datetime"):
        return time_tag["datetime"][:10]
    return ""
