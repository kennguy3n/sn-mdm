"""McKinsey insights podcast crawler.

McKinsey publishes podcast episodes under
``https://www.mckinsey.com/featured-insights/`` and a few sibling
hubs (``/capabilities/strategy-and-corporate-finance/``,
``/industries/financial-services/``, …). Each episode page carries
a verbatim transcript section that's prefixed by ``"Subscribe to
the …"`` and ends just before the ``"Related Articles"`` block.
"""

from __future__ import annotations

import re
import urllib.parse

from bs4 import BeautifulSoup

from .base import BaseCrawler, RawEpisode, _collapse_blank_lines


class McKinseyCrawler(BaseCrawler):
    publisher_id = "mckinsey"
    publisher_name = "McKinsey & Company"

    BASE_URL = "https://www.mckinsey.com"

    def _episode_url(self, slug: str) -> str:
        # `slug` here is the full path under www.mckinsey.com (e.g.
        # "featured-insights/leadership/why-strategic-foresight-matters"),
        # which gives operators of the source registry the freedom
        # to mix the three insight hubs without a separate config
        # key per hub.
        return urllib.parse.urljoin(self.BASE_URL + "/", slug.lstrip("/"))

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        url = self._episode_url(episode_slug)
        resp = self.fetch(url)
        soup = BeautifulSoup(resp.text, "lxml")
        title = self._extract_title(resp.text) or episode_slug
        publication_date = self._extract_publication_date(resp.text)
        return RawEpisode(
            episode_slug=episode_slug.replace("/", "_"),
            title=title,
            primary_url=url,
            publication_date=publication_date,
            raw_bytes=resp.content,
            content_type="text/html",
            hosts=_extract_byline(soup, r"Host|Hosted by"),
            guests=_extract_byline(soup, r"Guest(?:s)?|Interview(?:ed)?"),
            asset_urls=_collect_pdf_assets(soup, url),
            summary=_meta_description(soup),
        )

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        container = soup.find("article") or soup.find("main") or soup
        # Cut everything after the "Related Articles" heading.
        for h in container.find_all(re.compile(r"^h[1-6]$")):
            if re.search(r"related (articles|insights)", h.get_text(" ", strip=True), flags=re.I):
                for sib in list(h.next_siblings):
                    if hasattr(sib, "decompose"):
                        sib.decompose()
                h.decompose()
                break
        for level in range(1, 7):
            for h in container.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        return _collapse_blank_lines(container.get_text("\n"))


def _meta_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    return ""


def _extract_byline(soup: BeautifulSoup, label_pattern: str) -> list[str]:
    pat = re.compile(rf"^({label_pattern})\s*[:\u2014\u2013-]\s*(.+)$", re.I)
    for tag in soup.find_all(["p", "span", "li"]):
        text = tag.get_text(" ", strip=True)
        m = pat.match(text)
        if m:
            return [n.strip() for n in re.split(r",| and ", m.group(2)) if n.strip()]
    return []


def _collect_pdf_assets(soup: BeautifulSoup, base: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf"):
            full = urllib.parse.urljoin(base, href)
            if full not in seen:
                seen.add(full)
                out.append(full)
    return out[:20]
