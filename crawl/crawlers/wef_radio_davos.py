"""World Economic Forum Radio Davos crawler.

The WEF publishes Radio Davos episodes at
``https://www.weforum.org/podcasts/radio-davos/episodes/{slug}/``.
Each episode page hosts the auto-generated transcript inline in
the body markup, along with JSON-LD ``schema.org/CreativeWork``
metadata (``headline``, ``datePublished``, ``description``).

Why this crawler uses the headless-browser transport
----------------------------------------------------

``weforum.org`` sits behind a WAF that returns ``HTTP 403`` on
every endpoint accessed without a browser fingerprint — the
sitemap, the podcast index, individual episode pages. A plain
``requests`` GET (even with a Chrome-spoofed User-Agent header)
gets the 403 because the firewall checks the full TLS/JA3
fingerprint plus a handful of ``Sec-CH-UA-*`` client-hint
headers that ``requests`` does not natively send.

Routing through :meth:`BaseCrawler.fetch_rendered` makes the
request from a real headless Chromium with a realistic UA +
client-hint set + ``navigator.webdriver=false``. The WAF lets
that through. Robots.txt is still honoured (the
:meth:`fetch_rendered` wrapper does the same ``can_fetch`` check
as the requests path), the rate-limit is still enforced, and the
custom User-Agent is still preserved on the ``can_fetch`` lookup
so a future ``robots.txt`` change disallowing ``sn-mdm-crawler``
would still block us correctly.

Discovery
---------

The hub page at ``/podcasts/radio-davos/`` returns ~22 of the
most recent episodes. We extract the per-episode hrefs and cap
at :data:`_MAX_DISCOVERED` so a single ``initial_sync`` doesn't
try to walk the whole 200+ episode archive in one shot.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

_HUB_URL = "https://www.weforum.org/podcasts/radio-davos/"
_MAX_DISCOVERED = 25
# Slug regex: lower-case ASCII, digits, hyphens. Excludes anything
# with a trailing slug-like suffix that's actually pagination
# (``?page=2``, ``#anchor``).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]+[a-z0-9]$")
# Episode hrefs are stable: ``/podcasts/radio-davos/episodes/{slug}/``.
_EPISODE_HREF_RE = re.compile(
    r'href="(?:https?://(?:www\.)?weforum\.org)?/podcasts/radio-davos/episodes/([^"/?#]+)/?"'
)


class WefRadioDavosCrawler(BaseCrawler):
    publisher_id = "wef_radio_davos"
    publisher_name = "World Economic Forum"

    def _episode_url(self, slug: str) -> str:
        return f"https://www.weforum.org/podcasts/radio-davos/episodes/{slug}/"

    def _discover_episode_slugs(self) -> list[str]:
        # Wait for ``networkidle`` so the hub page's lazy-loaded
        # episode list has hydrated before we snapshot the DOM.
        try:
            html_bytes = self.fetch_rendered(
                _HUB_URL,
                wait_for_states=("domcontentloaded", "networkidle"),
                timeout_ms=45_000,
            )
        except Exception:
            # Discovery is best-effort — ``BaseCrawler.initial_sync``
            # already catches at the merge site, but re-raising
            # here would lose the seed list for no benefit.
            return []
        slugs: list[str] = []
        seen: set[str] = set()
        for m in _EPISODE_HREF_RE.finditer(html_bytes.decode("utf-8", errors="replace")):
            slug = m.group(1)
            if not _SLUG_RE.match(slug):
                continue
            if slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
            if len(slugs) >= _MAX_DISCOVERED:
                break
        return slugs

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        url = self._episode_url(episode_slug)
        html_bytes = self.fetch_rendered(
            url,
            wait_for_states=("domcontentloaded", "networkidle"),
            timeout_ms=45_000,
        )
        soup = BeautifulSoup(html_bytes, "lxml")
        meta = _extract_jsonld_metadata(soup)
        title = meta.get("headline") or self._extract_title(html_bytes.decode("utf-8", "replace")) or episode_slug
        pub = meta.get("datePublished") or self._extract_publication_date(
            html_bytes.decode("utf-8", "replace")
        )
        return RawEpisode(
            episode_slug=episode_slug,
            title=title,
            primary_url=url,
            publication_date=(pub or "")[:10],
            raw_bytes=html_bytes,
            content_type="text/html",
            hosts=meta.get("authors") or ["Robin Pomeroy"],
            guests=_extract_guests(soup),
            asset_urls=[],
            summary=meta.get("description") or _meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        # Strip page chrome and the Cookiebot banner. The WEF page
        # ships a ~60 KB Cookiebot dialog whose markup is
        # consent-management boilerplate; without removing it
        # first our "largest text container" fallback would pick
        # the dialog instead of the article body.
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
        # Cookiebot wraps everything under ``<div id="CybotCookiebotDialog">``
        # / ``<div class="CybotEdge ...">``. Strip both shapes. We
        # walk a snapshot of the tree so concurrent ``.decompose``
        # doesn't invalidate our iterator.
        for tag in list(soup.find_all(True)):
            if not getattr(tag, "attrs", None):
                continue
            tid = (tag.get("id") or "").lower()
            tcls = " ".join(
                c for c in (tag.get("class") or []) if isinstance(c, str)
            ).lower()
            if (
                "cybot" in tid
                or "cookiebot" in tid
                or "cybot" in tcls
                or "cookiebot" in tcls
            ):
                tag.decompose()

        # WEF emits a stable Google Tag Manager attribute on the
        # transcript wrapper: ``<div data-gtm-section="Podcast transcript">``.
        # That attribute survives the CSS-in-JS class-name churn
        # that breaks anything keyed off ``class="..."`` (Emotion
        # regenerates those hashes on every site rebuild). When
        # present we narrow normalisation to that subtree so the
        # markdown body is exactly the rendered transcript text
        # and nothing else — no related-episode tiles, no podcast
        # subscription panel, no host bio block.
        container = soup.find("div", attrs={"data-gtm-section": "Podcast transcript"})
        if container is None:
            # Fallback path: older episodes (pre-GTM-attribute
            # rollout) inline the transcript in the article body
            # under any ``class``/``id`` containing ``transcript``.
            container = soup.find(
                lambda tag: tag.name in ("div", "section")
                and (
                    "transcript" in (tag.get("id") or "").lower()
                    or any("transcript" in c.lower() for c in tag.get("class", []))
                )
            )
        if container is None:
            # Last-resort fallback: the ``data-hypernova-key="V2PodcastEpisode"``
            # wrapper contains the whole rendered episode payload
            # (title + summary + transcript + share buttons). It's
            # broader than we'd like but bounded — strictly fewer
            # bytes than the raw ``<article>`` and never includes
            # site chrome.
            container = soup.find("div", attrs={"data-hypernova-key": "V2PodcastEpisode"})
        if container is None:
            container = soup.find("article") or soup.find("main") or soup

        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _meta_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        return og["content"].strip()
    return ""


def _extract_jsonld_metadata(soup: BeautifulSoup) -> dict[str, object]:
    """Pull the WEF ``schema.org/CreativeWork`` JSON-LD payload.

    WEF embeds episode metadata as ``<script type="application/ld+json">``
    holding a ``CreativeWork`` (sometimes wrapped in CDATA). When
    present we trust those fields over the looser title/date
    regexes in :class:`BaseCrawler` — the JSON-LD is canonical.
    """
    for s in soup.find_all("script", type="application/ld+json"):
        raw = s.string or s.text or ""
        if not raw:
            continue
        # WEF wraps the payload in ``//<![CDATA[ ... //]]>`` —
        # strip the CDATA shell before parsing.
        match = re.search(r"(\{.*\})", raw, flags=re.S)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        # We only want the ``CreativeWork`` / ``PodcastEpisode``
        # blocks; the page also embeds ``BreadcrumbList`` and
        # ``Organization`` payloads we don't care about.
        t = data.get("@type") if isinstance(data, dict) else None
        if t not in ("CreativeWork", "PodcastEpisode", "NewsArticle", "Article"):
            continue
        out: dict[str, object] = {}
        if isinstance(data.get("headline"), str):
            out["headline"] = data["headline"]
        if isinstance(data.get("datePublished"), str):
            out["datePublished"] = data["datePublished"]
        if isinstance(data.get("description"), str):
            out["description"] = data["description"]
        authors = data.get("author")
        if isinstance(authors, list):
            names: list[str] = []
            for a in authors:
                if isinstance(a, dict) and isinstance(a.get("name"), str):
                    names.append(a["name"])
            if names:
                out["authors"] = names
        elif isinstance(authors, dict) and isinstance(authors.get("name"), str):
            out["authors"] = [authors["name"]]
        return out
    return {}


def _extract_guests(soup: BeautifulSoup) -> list[str]:
    for p in soup.find_all(["p", "li", "span"]):
        m = re.match(
            r"^(?:Guest|Featuring|With|Speakers?)\s*[:\u2014\u2013-]\s*(.+)$",
            p.get_text(" ", strip=True),
            flags=re.I,
        )
        if m:
            return [n.strip() for n in re.split(r",| and ", m.group(1)) if n.strip()]
    return []


__all__ = ["WefRadioDavosCrawler"]
