"""Base crawler — the substrate of the sn-mdm crawl layer.

Mirrors the ``Connector`` trait from ``kennguy3n/knowledge``'s
``crates/connector_framework/src/connector.rs``. Each concrete
crawler implements the same lifecycle:

1. ``initial_sync`` — full pull, walks the entire source surface.
2. ``incremental_sync`` — steady-state pull keyed off ``sync_state``.
3. ``fetch_transcript`` — download and HTML-clean one episode.
4. ``normalize`` — convert raw HTML / PDF to canonical markdown.
5. ``chunk`` — split normalised text into retrieval chunks by
   speaker turn + section heading.

The shared utilities — robots.txt enforcement, rate limiting,
speaker-turn chunking, content hashing, JSONL emission — live on
:class:`BaseCrawler` so individual crawlers only own their source-
specific HTML / PDF parsing.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.robotparser
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

LOG = logging.getLogger(__name__)


DEFAULT_USER_AGENT = (
    "sn-mdm-crawler/0.1 (+https://github.com/kennguy3n/sn-mdm; "
    "compliant; respects robots.txt; 1-2 req/sec)"
)
"""User-Agent header sent on every HTTP request.

Crawlers must identify themselves so site operators can contact the
maintainers if there's a problem. Override via the
``SN_MDM_USER_AGENT`` environment variable when a particular source
asks for a different identifier.
"""

DEFAULT_TARGET_TOKENS = 700
"""Default chunk size in approximate tokens. Matches the value in
``pack_core::metadata::DEFAULT_CHUNKING``.
"""

DEFAULT_OVERLAP_TOKENS = 120
"""Default chunk overlap in approximate tokens. Matches
``pack_core::metadata::DEFAULT_CHUNKING``.
"""


# Speaker labels look like "BEN GILBERT:", "ADAM GRANT:",
# "MODERATOR:" — uppercased words / spaces / dots / hyphens followed
# by a colon. Allow a leading whitespace run and a trailing space
# (which is what most transcripts have).
SPEAKER_LABEL_RE = re.compile(
    r"^\s*(?P<label>[A-Z][A-Z0-9 .\-']{1,40}):\s"
)
"""Regex used by :func:`split_speaker_turns` to detect speaker labels
at the start of a line. Designed to be conservative — it only
matches all-caps labels because mixed-case "Ben:" is far more likely
to be a quoted message inside the body than a real speaker turn.
"""


SECTION_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<heading>.+?)\s*$")
"""Markdown heading regex. Section headings are emitted by every
crawler's ``normalize`` so the chunker can use them as chunk
boundaries.
"""


@dataclass
class SyncState:
    """Persistent state for a crawler. Mirrors ``connector_framework``'s
    ``SyncState``.

    The cursor is opaque to the framework — concrete crawlers
    interpret it however they like (a "last-seen episode slug" for
    page-walked sources, a paginated URL for index-walked sources, …).
    """

    cursor: str | None = None
    """Source-specific cursor."""

    seen_content_hashes: set[str] = field(default_factory=set)
    """Set of BLAKE3 hex digests we've already chunked. Populated
    from the governance log on startup so re-runs of the same
    crawler skip already-known episodes."""


@dataclass
class RawEpisode:
    """Outcome of :meth:`BaseCrawler.fetch_transcript`. Captures every
    raw byte we received from the publisher plus the deterministic
    metadata needed to flow it through the rest of the pipeline.
    """

    episode_slug: str
    """Stable slug, used as the last segment of ``episode_id``."""

    title: str
    """Episode title as published on the source."""

    primary_url: str
    """Canonical URL of the episode landing page."""

    publication_date: str
    """ISO 8601 publication date (``YYYY-MM-DD`` minimum)."""

    raw_bytes: bytes
    """Raw HTML / PDF / TXT bytes as received. Cached to
    ``packs/raw/{publisher}/{slug}.html`` (or ``.pdf`` / ``.txt``)."""

    content_type: str
    """``"text/html"``, ``"application/pdf"``, or ``"text/plain"``.
    Used by :meth:`BaseCrawler.normalize` to pick a parser."""

    hosts: list[str] = field(default_factory=list)
    """Hosts for this episode. Crawlers fill from page metadata."""

    guests: list[str] = field(default_factory=list)
    """Guests for this episode. Crawlers fill from page metadata."""

    asset_urls: list[str] = field(default_factory=list)
    """Companion-resource URLs extracted from the page (reports,
    playbooks, whitepapers referenced inline by the transcript)."""

    summary: str = ""
    """Optional one-paragraph summary scraped from the page."""


@dataclass
class NormalisedEpisode:
    """Outcome of :meth:`BaseCrawler.normalize`. Carries the cleaned
    markdown plus everything :meth:`BaseCrawler.emit_jsonl` needs to
    serialise the JSONL line.
    """

    raw: RawEpisode
    normalised_markdown: str
    content_hash: str


def slugify(s: str) -> str:
    """Lower-snake-case slug suitable for use in ``episode_id``.

    Strips diacritics, replaces non-alphanumerics with ``-``, collapses
    runs of ``-``, trims leading / trailing ``-``.
    """
    norm = unicodedata.normalize("NFKD", s)
    no_diacritics = "".join(c for c in norm if not unicodedata.combining(c))
    lower = no_diacritics.lower()
    out = []
    for ch in lower:
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def canonicalise_text(s: str) -> str:
    """Canonicalise text for content-hash dedup.

    Mirrors ``pack_core::ingest::canonicalise_text``:

    - Line endings normalised to ``\\n``.
    - Tabs expanded to one space.
    - Trailing whitespace stripped per line.
    - Leading / trailing blank lines stripped.
    """
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.replace("\t", " ").rstrip() for line in s.split("\n")]
    return "\n".join(lines).strip()


def content_hash(text: str) -> str:
    """Return a stable hex digest used as the content-hash for
    dedup. Uses BLAKE3 if the ``blake3`` package is available; falls
    back to SHA-256 otherwise. The Rust ingest path always uses
    BLAKE3, but the Python side may run on hosts without a C
    toolchain — the digest is only used for in-process dedup, not
    for cross-language hashing.
    """
    canonical = canonicalise_text(text)
    try:  # pragma: no cover - depends on optional install
        import blake3 as _blake3  # type: ignore[import-not-found]

        return _blake3.blake3(canonical.encode("utf-8")).hexdigest()
    except ImportError:
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def count_tokens(text: str) -> int:
    """Approximate token count.

    The pack format records ``token_count`` so the on-device agent
    can budget context. A whitespace + punctuation split mirrors what
    a tokenizer-free retriever does at query time — exact tokenizer
    fidelity is not required because the count is only used for
    budgeting, not for inference.
    """
    return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def split_speaker_turns(text: str) -> list[tuple[str | None, str]]:
    """Split a normalised transcript into ``(speaker, body)`` pairs.

    The speaker label is preserved verbatim (uppercase, no trailing
    colon). Returns a list because most transcripts are short enough
    to fit comfortably in memory; streaming would only matter for
    multi-megabyte transcripts which the pack does not target.

    Lines that do not start with a recognised speaker label are
    appended to the previous turn's body. A heading line resets the
    current turn (the chunker handles heading boundaries separately).
    """
    turns: list[tuple[str | None, list[str]]] = []
    current_speaker: str | None = None
    current_body: list[str] = []

    def _flush() -> None:
        if current_body:
            turns.append((current_speaker, list(current_body)))
            current_body.clear()

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if SECTION_HEADING_RE.match(line):
            # Heading lines end the current turn and become standalone
            # turns with speaker=None so the chunker can carry the
            # heading forward.
            _flush()
            current_speaker = None
            turns.append((None, [line]))
            continue
        m = SPEAKER_LABEL_RE.match(line)
        if m:
            _flush()
            current_speaker = m.group("label").strip()
            remainder = line[m.end() :]
            if remainder:
                current_body.append(remainder)
        else:
            current_body.append(line)
    _flush()
    return [(spk, "\n".join(body).strip()) for spk, body in turns if any(line.strip() for line in body)]


@dataclass
class ChunkSpec:
    """One chunk produced by :func:`chunk_normalised_text`."""

    chunk_index: int
    section_heading: str | None
    text: str
    token_count: int


def chunk_normalised_text(
    text: str,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[ChunkSpec]:
    """Chunk normalised markdown by speaker turn and section heading.

    Rules:

    1. Headings reset the section heading carried forward on each
       chunk. The heading is NOT included in the chunk body — it's
       persisted on the ``section_heading`` column.
    2. Chunks try to stay near ``target_tokens`` long. A turn that
       would push the chunk above ``target_tokens + overlap_tokens``
       is emitted as its own chunk (very long single-speaker
       monologues are common in BCG / McKinsey transcripts).
    3. Each chunk overlaps the previous chunk by the last
       ``overlap_tokens`` tokens of the previous chunk's text. The
       overlap preserves cross-boundary references when a sentence
       straddles a chunk boundary.
    """
    if target_tokens <= 0:
        raise ValueError("target_tokens must be positive")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens must be non-negative")
    if overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be < target_tokens")

    turns = split_speaker_turns(text)
    chunks: list[ChunkSpec] = []
    current_heading: str | None = None
    buffer: list[str] = []
    buffer_tokens = 0

    def _flush_chunk() -> None:
        nonlocal buffer, buffer_tokens
        if not buffer:
            return
        body = "\n\n".join(buffer).strip()
        if not body:
            buffer = []
            buffer_tokens = 0
            return
        chunks.append(
            ChunkSpec(
                chunk_index=len(chunks),
                section_heading=current_heading,
                text=body,
                token_count=count_tokens(body),
            )
        )
        # Carry the trailing overlap tokens forward into the next
        # buffer. Operate on words because token counts are
        # approximate.
        words = body.split()
        if overlap_tokens > 0 and len(words) > overlap_tokens:
            tail = words[-overlap_tokens:]
            buffer = [" ".join(tail)]
            buffer_tokens = len(tail)
        else:
            buffer = []
            buffer_tokens = 0

    for speaker, body in turns:
        m = SECTION_HEADING_RE.match(body) if not speaker else None
        if m:
            # Heading: flush any open chunk, then update the carried
            # heading. Heading text itself is not emitted as a chunk.
            _flush_chunk()
            current_heading = m.group("heading")
            continue
        # Render the turn with the speaker label re-applied.
        rendered = f"{speaker}: {body}" if speaker else body
        rendered_tokens = count_tokens(rendered)

        if rendered_tokens > target_tokens:
            # Single very long turn — emit any pending buffer, then
            # split this turn into multiple chunks by hard word
            # window.
            _flush_chunk()
            words = rendered.split()
            step = max(1, target_tokens - overlap_tokens)
            for start in range(0, len(words), step):
                end = min(len(words), start + target_tokens)
                window = " ".join(words[start:end])
                chunks.append(
                    ChunkSpec(
                        chunk_index=len(chunks),
                        section_heading=current_heading,
                        text=window,
                        token_count=count_tokens(window),
                    )
                )
                if end >= len(words):
                    break
            buffer = []
            buffer_tokens = 0
            continue

        if buffer_tokens + rendered_tokens > target_tokens and buffer:
            _flush_chunk()
        buffer.append(rendered)
        buffer_tokens += rendered_tokens

    _flush_chunk()
    return chunks


# ---------------------------------------------------------------
# Crawler base class
# ---------------------------------------------------------------


@dataclass
class CrawlerConfig:
    """Per-crawler configuration — populated from the source registry
    (``crawl/crawl_config.toml``).
    """

    publisher_id: str
    publisher_name: str
    base_url: str
    rights_code: str
    rights_summary: str
    country_region: list[str] = field(default_factory=list)
    industry_tags: list[str] = field(default_factory=list)
    function_tags: list[str] = field(default_factory=list)
    business_model_tags: list[str] = field(default_factory=list)
    source_type: str = "podcast_transcript_html"
    language: str = "en"
    series_id: str = "flagship"
    series_title: str = ""
    host: str = ""
    primary_series_url: str = ""
    episodes: list[str] = field(default_factory=list)
    credibility_notes: str = ""
    chunking_policy: dict[str, int] = field(
        default_factory=lambda: {
            "target_tokens": DEFAULT_TARGET_TOKENS,
            "overlap_tokens": DEFAULT_OVERLAP_TOKENS,
        }
    )


class BaseCrawler:
    """Base class for all sn-mdm crawlers.

    Subclasses implement the source-specific bits
    (``_episode_urls``, ``_parse_episode_html``, ``normalize``);
    the base class owns everything else (robots.txt, rate limiting,
    speaker-turn chunking, JSONL emission, governance logging).
    """

    publisher_id: str = ""
    """Override in subclasses. Used to look up the crawler from the
    registry and as the file-prefix for JSONL outputs."""

    publisher_name: str = ""
    """Display name for the publisher."""

    rate_limit_seconds: float = 0.8
    """Seconds between requests. The default 0.8 s keeps the crawler
    under 2 req/sec on every source. Subclasses MAY lower this for
    sources known to permit higher rates."""

    def __init__(
        self,
        config: CrawlerConfig,
        packs_root: Path,
        session: requests.Session | None = None,
    ) -> None:
        if not config.publisher_id:
            raise ValueError("CrawlerConfig.publisher_id must be set")
        if self.publisher_id and self.publisher_id != config.publisher_id:
            raise ValueError(
                f"crawler {type(self).__name__} expects publisher_id="
                f"{self.publisher_id!r}, got {config.publisher_id!r}"
            )
        self.config = config
        self.packs_root = Path(packs_root)
        self.session = session or self._build_session()
        self.sync_state = SyncState()
        self._last_request_at: float = 0.0
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    # -- network helpers ---------------------------------------------------

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": os.environ.get("SN_MDM_USER_AGENT", DEFAULT_USER_AGENT),
                "Accept-Language": "en-US,en;q=0.8",
            }
        )
        return s

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.rate_limit_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _robots_parser(self, url: str) -> urllib.robotparser.RobotFileParser:
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots_cache:
            return self._robots_cache[origin]
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(urllib.parse.urljoin(origin, "/robots.txt"))
        try:
            rp.read()
        except Exception as exc:  # noqa: BLE001 - log + treat as allow
            LOG.warning("robots.txt fetch failed for %s: %s", origin, exc)
            # Treat a fetch failure as "allow" since none of the
            # configured sources block crawlers in their robots.txt.
            # Concrete crawlers are still bound by per-source manual
            # review in `docs/SOURCES.md`.
            rp.parse([])
        self._robots_cache[origin] = rp
        return rp

    def fetch(self, url: str, *, accept: str | None = None) -> requests.Response:
        """Politely fetch ``url``.

        Enforces robots.txt, the configured rate limit, and a single
        retry on a transient 5xx.
        """
        rp = self._robots_parser(url)
        ua = self.session.headers.get("User-Agent", DEFAULT_USER_AGENT)
        if not rp.can_fetch(ua, url):
            raise PermissionError(f"robots.txt disallows {url} for {ua}")
        self._respect_rate_limit()
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = self.session.get(url, headers=headers, timeout=30)
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"{resp.status_code} {resp.reason}")
                resp.raise_for_status()
                return resp
            except (requests.RequestException, requests.HTTPError) as exc:
                last_exc = exc
                LOG.warning("fetch %s failed (attempt %d): %s", url, attempt + 1, exc)
                time.sleep(1.0 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    # -- contract surface --------------------------------------------------

    def initial_sync(self) -> Iterator[RawEpisode]:
        """First-time pull — walk the entire source surface. Default
        implementation iterates over the configured episode slugs;
        sources with a discoverable index should override.
        """
        for slug in self.config.episodes:
            try:
                yield self.fetch_transcript(slug)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("fetch_transcript(%s) failed: %s", slug, exc)
                continue

    def incremental_sync(self, cursor: str | None = None) -> Iterator[RawEpisode]:
        """Steady-state pull. Default implementation re-runs the
        full initial_sync; concrete crawlers MAY override to short-
        circuit on the cursor.
        """
        _ = cursor  # base class ignores; concrete crawlers may use.
        yield from self.initial_sync()

    def fetch_transcript(self, episode_slug: str) -> RawEpisode:
        """Fetch and minimally-parse one episode's raw HTML / PDF.

        Concrete crawlers must override. The base class supplies a
        sane default that fetches the episode URL via :meth:`fetch`
        and returns a :class:`RawEpisode` with the response body —
        useful for sources where the entire transcript is on the
        landing page.
        """
        url = self._episode_url(episode_slug)
        resp = self.fetch(url)
        return RawEpisode(
            episode_slug=episode_slug,
            title=self._extract_title(resp.text) or episode_slug,
            primary_url=url,
            publication_date=self._extract_publication_date(resp.text),
            raw_bytes=resp.content,
            content_type=(resp.headers.get("Content-Type") or "text/html").split(";")[0].strip(),
            hosts=[self.config.host] if self.config.host else [],
            guests=[],
            asset_urls=[],
            summary="",
        )

    def normalize(self, raw: RawEpisode) -> NormalisedEpisode:
        """Convert raw bytes to canonical markdown.

        Concrete crawlers override. The base implementation strips
        HTML tags via :mod:`html.parser` so a quick-and-dirty
        normalisation is available for sources that supply
        pre-formatted text.
        """
        if raw.content_type == "application/pdf":
            text = self._normalize_pdf_bytes(raw.raw_bytes)
        else:
            text = self._normalize_html_bytes(raw.raw_bytes)
        canonical = canonicalise_text(text)
        return NormalisedEpisode(
            raw=raw,
            normalised_markdown=canonical,
            content_hash=content_hash(canonical),
        )

    def chunk(
        self,
        normalised: NormalisedEpisode,
        *,
        target_tokens: int | None = None,
        overlap_tokens: int | None = None,
    ) -> list[ChunkSpec]:
        """Chunk by speaker turn + section heading.

        Defaults fall back to the policy on :attr:`config.chunking_policy`
        — which is read from the source registry — so individual
        crawlers don't need to thread the policy through.
        """
        policy = self.config.chunking_policy
        return chunk_normalised_text(
            normalised.normalised_markdown,
            target_tokens=target_tokens or policy.get("target_tokens", DEFAULT_TARGET_TOKENS),
            overlap_tokens=overlap_tokens or policy.get("overlap_tokens", DEFAULT_OVERLAP_TOKENS),
        )

    # -- emit ------------------------------------------------------------

    def emit_episode(self, normalised: NormalisedEpisode) -> dict[str, Any]:
        """Build the JSONL line for one episode."""
        raw = normalised.raw
        episode_id = f"{self.config.publisher_id}_{self.config.series_id}_{raw.episode_slug}"
        return {
            "episode_id": episode_id,
            "publisher": self.config.publisher_id,
            "series": self.config.series_id,
            "title": raw.title,
            "host": raw.hosts or ([self.config.host] if self.config.host else []),
            "guest": raw.guests,
            "publication_date": raw.publication_date or "",
            "country_region": self.config.country_region,
            "industry_tags": self.config.industry_tags,
            "function_tags": self.config.function_tags,
            "business_model_tags": self.config.business_model_tags,
            "source_type": self.config.source_type,
            "language": self.config.language,
            "primary_url": raw.primary_url,
            "asset_urls": raw.asset_urls,
            "rights_code": self.config.rights_code,
            "rights_summary": self.config.rights_summary,
            "credibility_notes": self.config.credibility_notes,
            "summary": raw.summary,
            "chunking_policy": dict(self.config.chunking_policy),
        }

    def emit_chunks(
        self,
        normalised: NormalisedEpisode,
        chunks: Iterable[ChunkSpec],
    ) -> Iterator[dict[str, Any]]:
        """Build the JSONL lines for one episode's chunks."""
        raw = normalised.raw
        episode_id = f"{self.config.publisher_id}_{self.config.series_id}_{raw.episode_slug}"
        for c in chunks:
            anchor = raw.primary_url
            if c.section_heading:
                anchor = f"{anchor}#{slugify(c.section_heading)}"
            yield {
                "chunk_id": f"{episode_id}#{c.chunk_index:04}",
                "episode_id": episode_id,
                "token_count": c.token_count,
                "section_heading": c.section_heading,
                "chunk_text": c.text,
                "citation_anchor": anchor,
            }

    def emit_governance_entry(
        self,
        normalised: NormalisedEpisode,
        *,
        deprecated: bool = False,
    ) -> dict[str, Any]:
        """Build the rights-log JSONL line for one episode."""
        raw = normalised.raw
        episode_id = f"{self.config.publisher_id}_{self.config.series_id}_{raw.episode_slug}"
        return {
            "episode_id": episode_id,
            "rights_code": self.config.rights_code,
            "ingestion_date": int(time.time()),
            "content_hash": normalised.content_hash,
            "deprecated": deprecated,
        }

    # -- file system helpers --------------------------------------------

    def _publisher_dir(self, subdir: str) -> Path:
        d = self.packs_root / subdir / self.config.publisher_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_raw(self, raw: RawEpisode) -> Path:
        ext = {
            "text/html": ".html",
            "application/pdf": ".pdf",
            "text/plain": ".txt",
        }.get(raw.content_type, ".bin")
        path = self._publisher_dir("raw") / f"{raw.episode_slug}{ext}"
        path.write_bytes(raw.raw_bytes)
        return path

    def save_normalised(self, normalised: NormalisedEpisode) -> Path:
        path = self._publisher_dir("normalized") / f"{normalised.raw.episode_slug}.md"
        path.write_text(normalised.normalised_markdown, encoding="utf-8")
        return path

    def open_jsonl(self, subdir: str) -> Path:
        d = self.packs_root / subdir
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.config.publisher_id}.jsonl"

    # -- overrideables ---------------------------------------------------

    def _episode_url(self, slug: str) -> str:
        """Default URL composition. Override for sources where the
        slug-to-URL mapping isn't a straight ``base_url/slug``."""
        base = self.config.base_url
        if not base.endswith("/"):
            base = base + "/"
        return urllib.parse.urljoin(base, slug)

    def _extract_title(self, html: str) -> str:
        """Trivial title extractor — pulls the first ``<title>``."""
        m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
        if m:
            return _collapse_whitespace(m.group(1))
        return ""

    def _extract_publication_date(self, html: str) -> str:
        """Look for ISO-8601-ish dates in common meta tags."""
        for pattern in (
            r'property=["\']article:published_time["\']\s+content=["\']([^"\']+)["\']',
            r'name=["\']pubdate["\']\s+content=["\']([^"\']+)["\']',
            r'name=["\']date["\']\s+content=["\']([^"\']+)["\']',
        ):
            m = re.search(pattern, html, flags=re.I)
            if m:
                return m.group(1)[:10]
        return ""

    def _normalize_html_bytes(self, raw_bytes: bytes) -> str:
        from bs4 import (
            BeautifulSoup,  # local import keeps the base module importable without bs4 at test time
        )

        soup = BeautifulSoup(raw_bytes, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
            tag.decompose()
        # Translate basic heading elements to markdown so the chunker
        # can detect section boundaries.
        for level in range(1, 7):
            for h in soup.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text(strip=True)}\n")
        text = soup.get_text(separator="\n")
        return _collapse_blank_lines(text)

    def _normalize_pdf_bytes(self, raw_bytes: bytes) -> str:
        from io import BytesIO

        from pdfminer.high_level import extract_text  # local import

        text = extract_text(BytesIO(raw_bytes))
        return _collapse_blank_lines(text)


# ---------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------


def _collapse_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _collapse_blank_lines(s: str) -> str:
    # Collapse 3+ consecutive newlines to 2; strip leading + trailing blank
    # lines; strip per-line trailing whitespace.
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    lines = [line.rstrip() for line in s.split("\n")]
    return "\n".join(lines).strip()


# Make dataclass-as-dict round-trip-friendly for the tests + the
# pipeline orchestrator.


def dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Helper used by the pipeline tests to round-trip dataclasses
    through ``json``. Mirrors :func:`dataclasses.asdict` but handles
    ``set`` (which ``asdict`` would emit as a non-JSON-serialisable
    Python set)."""
    if dataclasses.is_dataclass(obj):
        return {f.name: dataclass_to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, set):
        return {"__set__": sorted(obj)}  # type: ignore[return-value]
    if isinstance(obj, list):
        return [dataclass_to_dict(x) for x in obj]  # type: ignore[return-value]
    if isinstance(obj, dict):
        return {k: dataclass_to_dict(v) for k, v in obj.items()}
    return obj


__all__ = [
    "BaseCrawler",
    "ChunkSpec",
    "CrawlerConfig",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_TARGET_TOKENS",
    "DEFAULT_USER_AGENT",
    "NormalisedEpisode",
    "RawEpisode",
    "SECTION_HEADING_RE",
    "SPEAKER_LABEL_RE",
    "SyncState",
    "canonicalise_text",
    "chunk_normalised_text",
    "content_hash",
    "count_tokens",
    "dataclass_to_dict",
    "slugify",
    "split_speaker_turns",
]
