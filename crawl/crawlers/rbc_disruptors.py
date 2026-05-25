"""RBC Disruptors podcast crawler.

RBC publishes Disruptors episodes at
``https://www.rbc.com/en/thought-leadership/disruptors/{slug}/``.
Each episode page hosts a full transcript section under an
``<h2>Transcript</h2>`` heading along with JSON-LD ``Article``
metadata (headline, datePublished, wordCount, author).

Why this crawler uses the headless-browser transport
----------------------------------------------------

The Disruptors archive lives at
``https://www.rbc.com/en/thought-leadership/disruptors/`` and is
a fully client-rendered React tree — a plain ``requests`` GET
returns the unhydrated shell with zero episode links. Individual
episode pages also need JS hydration to expose the transcript
container.

Routing through :meth:`BaseCrawler.fetch_rendered` gives us the
post-hydration DOM. Robots.txt + rate limiting are still
honoured by the wrapper.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines

_HUB_URL = "https://www.rbc.com/en/thought-leadership/disruptors/"
_MAX_DISCOVERED = 25
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]+[a-z0-9]$")
# RBC episode hrefs always sit under ``/en/thought-leadership/disruptors/``.
# We exclude the trailing slash variant and any hub-page anchors
# (``#subscribe``, ``#about`` etc.).
_EPISODE_HREF_RE = re.compile(
    r'href="(?:https?://(?:www\.)?rbc\.com)?'
    r"/en/thought-leadership/disruptors/"
    r'([a-z0-9][a-z0-9\-]*?)/?"'
)
# Slugs that point to the hub itself or to non-episode subsections.
_NON_EPISODE_SLUGS = frozenset(
    {
        "",
        "subscribe",
        "about",
        "podcast",
        "podcasts",
        "season-1",
        "season-2",
        "season-3",
        "season-4",
        "season-5",
        "season-6",
        "season-7",
        "season-8",
        "season-9",
        "season-10",
    }
)


class RbcDisruptorsCrawler(BaseCrawler):
    publisher_id = "rbc_disruptors"
    publisher_name = "RBC Disruptors"

    def _episode_url(self, slug: str) -> str:
        return f"https://www.rbc.com/en/thought-leadership/disruptors/{slug}/"

    def _discover_episode_slugs(self) -> list[str]:
        try:
            html_bytes = self.fetch_rendered(
                _HUB_URL,
                wait_for_states=("domcontentloaded", "networkidle"),
                timeout_ms=45_000,
            )
        except Exception:
            return []
        slugs: list[str] = []
        seen: set[str] = set()
        for m in _EPISODE_HREF_RE.finditer(html_bytes.decode("utf-8", errors="replace")):
            slug = m.group(1)
            if slug in _NON_EPISODE_SLUGS:
                continue
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
        title = (
            meta.get("headline")
            or self._extract_title(html_bytes.decode("utf-8", "replace"))
            or episode_slug
        )
        # RBC HTML-encodes ampersands and apostrophes inside the
        # JSON-LD headline (``AI&#8217;s Power``). Decode them so
        # the title that lands in the JSONL matches what a human
        # reader sees on the page.
        title = _decode_html_entities(title)
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
            hosts=["John Stackhouse"],
            guests=_extract_guests(soup),
            asset_urls=[],
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
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

        # Find the ``Transcript`` heading and collect every sibling
        # node up to the next H1/H2 (next major section).
        target = None
        for h in soup.find_all(re.compile(r"^h[1-6]$")):
            if "transcript" in h.get_text(" ", strip=True).lower():
                target = h
                break
        if target is None:
            container = soup.find("article") or soup.find("main") or soup
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
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        return og["content"].strip()
    return ""


def _decode_html_entities(text: str) -> str:
    """Decode the HTML numeric / named entities that RBC's
    JSON-LD payload leaks into ``headline`` (the WordPress JSON-LD
    plugin escapes apostrophes / ampersands as ``&#8217;`` etc.
    even though the surrounding JSON would parse them fine).
    """
    import html

    return html.unescape(text)


def _extract_jsonld_metadata(soup: BeautifulSoup) -> dict[str, object]:
    """Pull RBC's ``schema.org/Article`` JSON-LD payload.

    RBC ships two JSON-LD blocks per page — an ``@graph`` payload
    holding an ``Article`` + ``WebPage`` + ``Organization``, and a
    standalone ``NewsArticle`` payload. The ``Article`` inside the
    ``@graph`` is the canonical one (it has ``wordCount`` and the
    real ``author`` ID); the ``NewsArticle`` repeats most of the
    same fields but with a placeholder author. We prefer the
    ``Article`` graph node when present and fall back to the
    standalone ``NewsArticle``.
    """

    def _pick(article: dict[str, object]) -> dict[str, object]:
        out: dict[str, object] = {}
        if isinstance(article.get("headline"), str):
            out["headline"] = article["headline"]
        if isinstance(article.get("datePublished"), str):
            out["datePublished"] = article["datePublished"]
        if isinstance(article.get("description"), str):
            out["description"] = article["description"]
        return out

    # Walk JSON-LD blocks in document order; first acceptable
    # payload wins.
    for s in soup.find_all("script", type="application/ld+json"):
        raw = s.string or s.text or ""
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                for node in data["@graph"]:
                    if isinstance(node, dict) and node.get("@type") in (
                        "Article",
                        "NewsArticle",
                    ):
                        return _pick(node)
            elif data.get("@type") in ("Article", "NewsArticle"):
                return _pick(data)
    return {}


def _extract_guests(soup: BeautifulSoup) -> list[str]:
    for p in soup.find_all(["p", "li", "span"]):
        m = re.match(
            r"^Guest(?:s)?\s*[:\u2014\u2013-]\s*(.+)$",
            p.get_text(" ", strip=True),
            flags=re.I,
        )
        if m:
            return [n.strip() for n in re.split(r",| and ", m.group(1)) if n.strip()]
    return []


__all__ = ["RbcDisruptorsCrawler"]
