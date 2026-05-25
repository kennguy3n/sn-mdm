"""a16z podcast crawler.

a16z (Andreessen Horowitz) publishes podcast episode pages at
``https://a16z.com/podcast/{slug}``. The flagship a16z Podcast,
``In the Vault``, ``Bio Eats World``, and the AI shows share the
same page template. The page does NOT always carry a full
transcript verbatim — for many episodes a16z publishes a dense,
quote-rich editorial summary that the show notes call out as the
canonical artefact for that episode. We capture both when both
are present, and fall back to the editorial summary otherwise.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class A16zCrawler(BaseCrawler):
    publisher_id = "a16z"
    publisher_name = "Andreessen Horowitz"

    # a16z asks crawlers to keep below 1 req/sec — match the base
    # default which already does.

    def _episode_url(self, slug: str) -> str:
        return f"https://a16z.com/podcast/{slug}"

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        url = self._episode_url(episode_slug)
        resp = self.fetch(url)
        soup = BeautifulSoup(resp.text, "lxml")
        title = _meta_content(soup, "og:title") or (
            soup.title.get_text(strip=True) if soup.title else episode_slug
        )
        guests = _extract_guests(soup)
        publication_date = self._extract_publication_date(resp.text)
        summary = _meta_content(soup, "og:description")
        return RawEpisode(
            episode_slug=episode_slug,
            title=title,
            primary_url=url,
            publication_date=publication_date,
            raw_bytes=resp.content,
            content_type="text/html",
            hosts=[self.config.host] if self.config.host else [],
            guests=guests,
            asset_urls=_collect_external_links(soup),
            summary=summary,
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        # a16z wraps the body in <article>; if that's absent fall
        # back to a div with id="content".
        container = soup.find("article") or soup.find("div", id="content") or soup
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _meta_content(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


def _extract_guests(soup: BeautifulSoup) -> list[str]:
    # a16z lists guests inside the byline or an explicit "Guests:"
    # paragraph. Match defensively.
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        m = re.match(r"^Guests?[:\u2014\u2013-]\s*(.+)$", text, flags=re.I)
        if m:
            return [g.strip() for g in re.split(r",| and ", m.group(1)) if g.strip()]
    return []


def _collect_external_links(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and "a16z.com" not in href and href not in seen:
            seen.add(href)
            out.append(href)
    return out[:25]
