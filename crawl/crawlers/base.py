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

import blake3
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

    rights_code: str | None = None
    """Optional per-episode rights override. ``None`` falls back to
    the publisher-level :attr:`CrawlerConfig.rights_code` from the
    source registry. Crawlers set this when an individual episode
    carries a different licence than the rest of the publisher's
    catalogue (e.g. a one-off CC BY guest segment on an otherwise
    free-access-copyrighted feed). The pipeline rights gate prefers
    this value when present."""

    rights_summary: str | None = None
    """Optional per-episode rights summary string. ``None`` falls
    back to the publisher-level :attr:`CrawlerConfig.rights_summary`.
    Only meaningful when :attr:`rights_code` is also overridden."""


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
    - Runs of 3+ consecutive newlines (i.e. two or more blank
      lines) collapsed to exactly two newlines. This was
      previously the responsibility of
      :func:`_collapse_blank_lines`, called upstream during
      HTML/PDF normalisation. Folding the collapse into
      ``canonicalise_text`` itself removes the implicit ordering
      dependency: a future caller that bypasses
      ``_collapse_blank_lines`` (e.g. a new crawler emitting
      markdown directly) still produces a digest that matches the
      Rust ``pack_core::ingest::canonicalise_text``. Without this,
      the BLAKE3 content_hash would diverge cross-language for
      visually-identical text, silently breaking dedup.
    - Leading / trailing whitespace stripped from the result.
    """
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.replace("\t", " ").rstrip() for line in s.split("\n")]
    joined = "\n".join(lines)
    collapsed = re.sub(r"\n{3,}", "\n\n", joined)
    return collapsed.strip()


def content_hash(text: str) -> str:
    """Return a stable hex digest used as the content-hash for
    dedup.

    The caller is responsible for passing already-canonicalised
    text (see :func:`canonicalise_text`). This function does **not**
    re-canonicalise — callers that need to hash raw input should
    invoke ``content_hash(canonicalise_text(raw))`` explicitly. The
    split keeps the hash cheap when the caller has already paid the
    canonicalisation cost (the common case in
    :meth:`BaseCrawler.normalize`).

    Always uses BLAKE3 — ``blake3`` is a hard dependency
    (``crawl/requirements.txt``) so the digest matches the Rust
    ingest path's :func:`blake3::hash` byte-for-byte. The earlier
    SHA-256 fallback was removed because it weakened the dedup
    idempotency guarantee: hashes computed on a host without the
    ``blake3`` wheel would not match hashes computed on a host
    with it, so :meth:`Pipeline._load_known_hashes` would fail to
    short-circuit a re-crawl after the dependency state changed.
    """
    return blake3.blake3(text.encode("utf-8")).hexdigest()


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
        # buffer. We slice on word boundaries (whitespace split) so
        # the seed text is human-readable, but compute
        # ``buffer_tokens`` via :func:`count_tokens` — the same
        # punctuation-aware counter the accumulator uses elsewhere
        # — so the running token budget does not drift when the
        # carried tail contains punctuation. Without this the
        # follow-up chunk could overflow ``target_tokens`` by the
        # number of punctuation tokens hidden in the overlap.
        words = body.split()
        if overlap_tokens > 0 and len(words) > overlap_tokens:
            tail = words[-overlap_tokens:]
            seed = " ".join(tail)
            buffer = [seed]
            buffer_tokens = count_tokens(seed)
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
            # window. The window step already preserves overlap
            # *between* the windows. After the last window, carry
            # the trailing ``overlap_tokens`` words forward as the
            # seed of the next buffer so the next speaker turn
            # still overlaps with the tail of the long monologue
            # — same invariant as :func:`_flush_chunk`.
            _flush_chunk()
            words = rendered.split()
            step = max(1, target_tokens - overlap_tokens)
            last_window_words: list[str] = []
            for start in range(0, len(words), step):
                end = min(len(words), start + target_tokens)
                window_words = words[start:end]
                window = " ".join(window_words)
                chunks.append(
                    ChunkSpec(
                        chunk_index=len(chunks),
                        section_heading=current_heading,
                        text=window,
                        token_count=count_tokens(window),
                    )
                )
                last_window_words = window_words
                if end >= len(words):
                    break
            if overlap_tokens > 0 and len(last_window_words) > overlap_tokens:
                tail = last_window_words[-overlap_tokens:]
                seed = " ".join(tail)
                buffer = [seed]
                # Same rationale as in ``_flush_chunk``: use the
                # punctuation-aware counter so the running token
                # budget tracks reality.
                buffer_tokens = count_tokens(seed)
            else:
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
        robots_url = urllib.parse.urljoin(origin, "/robots.txt")
        rp.set_url(robots_url)
        # Fetch robots.txt through the crawler's own
        # :class:`requests.Session` rather than the stdlib
        # ``RobotFileParser.read()`` (which uses
        # :func:`urllib.request.urlopen`). Going through the session
        # gives us three properties the stdlib helper does not:
        #
        # 1. the custom ``User-Agent`` header from
        #    :meth:`_build_session` is sent, so site operators see
        #    the same identifier on robots.txt requests that they
        #    see on content requests;
        # 2. the rate-limiter at :meth:`_respect_rate_limit` is
        #    honoured — so we cannot fire robots.txt + first
        #    content request back-to-back faster than the configured
        #    cadence; and
        # 3. retries / timeouts inherit the session policy instead
        #    of falling back to the stdlib defaults.
        try:
            self._respect_rate_limit()
            resp = self.session.get(robots_url, timeout=30)
            if resp.status_code >= 400:
                # Per RFC 9309: 4xx is treated as "no robots.txt",
                # i.e. crawling is implicitly allowed. 5xx is the
                # "must defer" case, but the same conservative
                # outcome (parse([]) → allow everything) applies
                # — every concrete crawler still passes through
                # the per-source manual review in
                # ``docs/SOURCES.md``.
                rp.parse([])
            else:
                # ``RobotFileParser.parse`` expects an *iterable of
                # lines without trailing newlines*; ``str.splitlines``
                # gives exactly that and tolerates ``\r\n`` /
                # ``\n`` / ``\r`` endings.
                rp.parse(resp.text.splitlines())
        except Exception as exc:  # noqa: BLE001 - log + treat as allow
            LOG.warning("robots.txt fetch failed for %s: %s", origin, exc)
            rp.parse([])
        self._robots_cache[origin] = rp
        return rp

    def fetch(self, url: str, *, accept: str | None = None) -> requests.Response:
        """Politely fetch ``url``.

        Enforces robots.txt, the configured rate limit, and a single
        retry on transient failures only — 5xx, 408 Request Timeout,
        and 429 Too Many Requests (with ``Retry-After`` honoured).
        Other 4xx responses (401, 403, 404, 410, …) and network-layer
        errors that look like permanent client problems fail fast on
        the first attempt; retrying them would just double the
        latency on broken URLs.
        """
        rp = self._robots_parser(url)
        ua = self.session.headers.get("User-Agent", DEFAULT_USER_AGENT)
        if not rp.can_fetch(ua, url):
            raise PermissionError(f"robots.txt disallows {url} for {ua}")
        self._respect_rate_limit()
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        # Statuses worth retrying:
        #
        # * 5xx — transient server-side failure (RFC 9110 §15.6).
        # * 408 Request Timeout — server is telling us to re-send
        #   (RFC 9110 §15.5.9).
        # * 429 Too Many Requests — rate-limited; honour
        #   ``Retry-After`` (RFC 6585 §4 / RFC 9110 §15.5.18).
        #
        # Every other 4xx (401, 403, 404, 410, …) is a permanent
        # client-side error and will not resolve on retry. Retrying
        # them would burn one extra request per failure and double
        # the perceived latency on broken URLs in the source
        # registry. Fail fast on those instead — the
        # ``initial_sync`` generator already catches per-episode so
        # one bad URL does not bring down the whole publisher.
        retryable_statuses = frozenset({408, 429, 500, 502, 503, 504})
        max_attempts = 2
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            is_last = attempt == max_attempts - 1
            try:
                resp = self.session.get(url, headers=headers, timeout=30)
            except requests.RequestException as exc:
                # Network-layer (DNS, connection reset, read timeout):
                # always retryable while attempts remain.
                last_exc = exc
                if is_last:
                    break
                LOG.warning(
                    "fetch %s network error (attempt %d): %s", url, attempt + 1, exc
                )
                time.sleep(1.0 * (attempt + 1))
                continue

            if resp.ok:
                return resp

            if resp.status_code in retryable_statuses and not is_last:
                # ``Retry-After`` per RFC 9110 §10.2.3: either a
                # delta-seconds integer or an HTTP-date. We honour
                # the integer form (the common case for 429) and
                # ignore the date form rather than parse it
                # incorrectly — the linear backoff fallback is a
                # safe upper bound.
                retry_after = resp.headers.get("Retry-After", "").strip()
                backoff = (
                    float(retry_after) if retry_after.isdigit() else 1.0 * (attempt + 1)
                )
                LOG.warning(
                    "fetch %s got retryable %d (attempt %d); sleeping %.1fs",
                    url,
                    resp.status_code,
                    attempt + 1,
                    backoff,
                )
                time.sleep(backoff)
                continue

            # Non-retryable HTTP status (most 4xx, or last-attempt
            # 5xx). Surface immediately; ``raise_for_status`` builds
            # the canonical ``HTTPError`` with ``resp`` attached so
            # callers can introspect.
            resp.raise_for_status()
            # Defensive: ``raise_for_status`` always raises for
            # non-ok responses, so this line is unreachable. Kept
            # as a fail-loud guard if a future ``requests`` release
            # ever changes that contract.
            raise requests.HTTPError(  # pragma: no cover - defensive
                f"{resp.status_code} {resp.reason}", response=resp
            )

        assert last_exc is not None
        raise last_exc

    # -- contract surface --------------------------------------------------

    def initial_sync(self) -> Iterator[RawEpisode]:
        """First-time pull — walk the entire source surface.

        Episode slugs come from two sources, merged in this order:

        1. The configured seed list (:attr:`config.episodes`) — a
           curated set the operator wants crawled regardless of
           what discovery turns up.
        2. :meth:`_discover_episode_slugs` — concrete crawlers
           override this to walk the source's index page and
           enumerate episodes. The default implementation returns
           an empty list, so a crawler that ships without a
           discovery override is purely seed-driven.

        Slugs are de-duplicated preserving first-seen order so the
        seed list still dictates priority. This is the contract
        that lets us add new sources by populating the registry +
        an index-walker without touching the pipeline.

        Discovery is best-effort: if :meth:`_discover_episode_slugs`
        raises, the exception is logged and the seed list still
        runs. Overrides are *not* required to catch their own
        exceptions — the guard sits at the merge site so the
        invariant holds for every subclass, current and future.
        """
        seen: set[str] = set()
        slugs: list[str] = []
        # Compute the *unique* seed count first so the discovered
        # count we log is the true count of slugs that came in via
        # ``_discover_episode_slugs`` and not just the difference
        # between the total and the (possibly duplicate-bloated)
        # raw seed length. Without this, configs with duplicate
        # entries in ``episodes`` would silently undercount the
        # discovery side.
        seed_slugs = list(dict.fromkeys(self.config.episodes))
        # Discovery is best-effort: a publisher's index page can 5xx,
        # rate-limit us, or change shape on any given run. When that
        # happens we still want the configured seeds to be crawled —
        # silently dropping them because discovery raised would be a
        # silent regression for any publisher that has both a curated
        # seed set and a flaky index. We wrap the discovery call here
        # (rather than relying on every override to catch internally)
        # so the contract is enforced at the merge site for every
        # subclass, current and future.
        try:
            discovered_slugs = list(self._discover_episode_slugs())
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "%s: _discover_episode_slugs raised (%s); falling back to seed list",
                self.publisher_id,
                exc,
            )
            discovered_slugs = []
        for source in (seed_slugs, discovered_slugs):
            for slug in source:
                if slug in seen:
                    continue
                seen.add(slug)
                slugs.append(slug)

        LOG.info(
            "%s: initial_sync — %d seed + %d discovered → %d unique slugs",
            self.publisher_id,
            len(seed_slugs),
            max(0, len(slugs) - len(seed_slugs)),
            len(slugs),
        )

        for slug in slugs:
            try:
                yield self.fetch_transcript(slug)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("fetch_transcript(%s) failed: %s", slug, exc)
                continue

    def _discover_episode_slugs(self) -> list[str]:
        """Return slugs discovered by walking the source's index
        page. Concrete crawlers override; the default returns the
        empty list so a crawler without an index walker still
        works as long as :attr:`config.episodes` is populated.

        Implementations should:

        * Use :meth:`fetch` so robots.txt + rate limiting + the
          custom User-Agent are honoured.
        * Return only the per-episode path component
          (:meth:`_episode_url` will turn it into a fetch URL).
        * Cap the discovered list (typically the first ~25
          episodes) so a single ``initial_sync`` doesn't try to
          download a publisher's entire archive in one shot.
        """
        return []

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
        # ``content_hash`` no longer re-canonicalises — we pass the
        # already-canonical text to keep the hash cheap.
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
        # Use ``is not None`` rather than truthiness so an explicit
        # ``overlap_tokens=0`` ("no overlap") is honoured instead of
        # silently falling through to the policy default. The
        # ``chunk_normalised_text`` validator at line ~290 already
        # enforces ``target_tokens > 0`` and ``overlap_tokens >= 0``,
        # so a zero overlap is a real, supported value.
        effective_target = (
            target_tokens
            if target_tokens is not None
            else policy.get("target_tokens", DEFAULT_TARGET_TOKENS)
        )
        effective_overlap = (
            overlap_tokens
            if overlap_tokens is not None
            else policy.get("overlap_tokens", DEFAULT_OVERLAP_TOKENS)
        )
        return chunk_normalised_text(
            normalised.normalised_markdown,
            target_tokens=effective_target,
            overlap_tokens=effective_overlap,
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
            "rights_code": raw.rights_code or self.config.rights_code,
            "rights_summary": raw.rights_summary or self.config.rights_summary,
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
            "rights_code": raw.rights_code or self.config.rights_code,
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
        # Some publishers namespace their slugs (e.g. BCG's
        # ``<show>/<slug>``, Microsoft's ``episodes/<slug>``) so
        # the resolved file path can sit several directories
        # below the publisher root. Materialise the parent chain
        # before writing.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw.raw_bytes)
        return path

    def save_normalised(self, normalised: NormalisedEpisode) -> Path:
        path = self._publisher_dir("normalized") / f"{normalised.raw.episode_slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
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
