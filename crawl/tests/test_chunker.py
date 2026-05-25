"""Tests for the speaker-turn chunker and base crawler helpers."""

from __future__ import annotations

from crawl.crawlers.base import (
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    canonicalise_text,
    chunk_normalised_text,
    content_hash,
    count_tokens,
    slugify,
    split_speaker_turns,
)


def test_default_chunking_constants() -> None:
    # Mirrors `pack_core::metadata::DEFAULT_CHUNKING`.
    assert DEFAULT_TARGET_TOKENS == 700
    assert DEFAULT_OVERLAP_TOKENS == 120


def test_slugify_basic() -> None:
    assert slugify("Costco's flywheel") == "costco-s-flywheel"
    assert slugify("AcQuired — IKEA / 2022") == "acquired-ikea-2022"
    assert slugify("") == ""


def test_canonicalise_text_normalises_whitespace_and_line_endings() -> None:
    # Line endings -> \n, tabs -> single space, trailing whitespace stripped
    # per line, leading + trailing blank lines trimmed. Runs of 3+ newlines
    # (2+ blank lines) collapse to exactly two so paragraph structure
    # survives hash canonicalisation but extra blank lines don't change the
    # digest.
    raw = "  \r\nHello\r\nworld   \r\n\n\nfoo\tbar  \r\n  "
    canon = canonicalise_text(raw)
    assert canon == "Hello\nworld\n\nfoo bar"


def test_canonicalise_text_collapses_blank_lines() -> None:
    # Mirrors ``pack_core::ingest::canonicalise_text_matches_python_blank_line_collapse``.
    # CRLF + bare CR + LF must all funnel into the same canonical
    # form; 4 newlines (3 blank lines) must collapse to 2 newlines
    # (1 blank line); whitespace-only "blank" lines must collapse
    # too because rstrip turns them into empty lines first.
    assert canonicalise_text("a\r\n\r\n\r\n\r\nb") == "a\n\nb"
    assert (
        canonicalise_text("  \r\nHello\tworld   \n\n\n\nfoo\tbar  \r\n  ")
        == "Hello world\n\nfoo bar"
    )
    assert canonicalise_text("first\n   \n\t\n  \nsecond") == "first\n\nsecond"


def test_canonicalise_text_blank_line_collapse_is_hash_stable() -> None:
    # Two visually-identical inputs that differ only in the number
    # of blank lines between paragraphs must hash identically once
    # canonicalised. This is the defense-in-depth invariant that
    # ``canonicalise_text`` was extended to enforce so a future
    # caller bypassing ``_collapse_blank_lines`` upstream cannot
    # silently produce a different ``content_hash``.
    sparse = "para one\n\npara two"
    dense = "para one\n\n\n\n\npara two"
    assert content_hash(canonicalise_text(sparse)) == content_hash(
        canonicalise_text(dense)
    )


def test_content_hash_is_canonical() -> None:
    # ``content_hash`` itself is a pure hash now — it expects the
    # caller to pre-canonicalise (see ``BaseCrawler.normalize``).
    # Two raw inputs that canonicalise to the same string must
    # therefore hash identically once both go through
    # ``canonicalise_text`` first.
    a = "Hello\r\nworld   "
    b = "Hello\nworld"
    assert content_hash(canonicalise_text(a)) == content_hash(canonicalise_text(b))
    # ``blake3`` is a hard dependency (see ``crawl/requirements.txt``)
    # so the digest is always a 64-char BLAKE3 hex string.
    assert len(content_hash(canonicalise_text(a))) == 64
    # Sanity check: two distinct canonical strings must hash
    # distinctly so the dedup gate doesn't false-positive.
    assert content_hash(canonicalise_text(a)) != content_hash(
        canonicalise_text("Hello\nworld!")
    )


def test_count_tokens_handles_punctuation() -> None:
    assert count_tokens("Hello, world!") == 4  # Hello , world !
    assert count_tokens("") == 0
    assert count_tokens("one two three") == 3


def test_split_speaker_turns_detects_labels_and_headings() -> None:
    transcript = (
        "# Introduction\n"
        "SIMON LONDON: Welcome to the show.\n"
        "ADAM GRANT: Glad to be here.\n"
        "We had a great chat about culture.\n"
        "# Closing\n"
        "SIMON LONDON: Thanks, Adam.\n"
    )
    turns = split_speaker_turns(transcript)
    speakers = [s for s, _ in turns]
    assert "SIMON LONDON" in speakers
    assert "ADAM GRANT" in speakers
    # Headings come through as speaker=None turns.
    assert any(s is None and body.startswith("#") for s, body in turns)


def test_chunk_normalised_text_respects_target_size() -> None:
    # Build a multi-turn transcript that exceeds the chunk target.
    body = "alpha " * 80  # ~80 tokens
    transcript = "\n".join(
        f"{name}: {body}" for name in ("ALICE", "BOB", "CHARLIE", "DAVE")
    )
    chunks = chunk_normalised_text(transcript, target_tokens=100, overlap_tokens=20)
    assert len(chunks) >= 2
    for c in chunks:
        # Chunks may slightly exceed the target because of the speaker label
        # + overlap rollover, but should never exceed target + (target * 2).
        assert c.token_count <= 300
        assert c.text.strip()


def test_chunk_normalised_text_carries_section_heading() -> None:
    transcript = (
        "# Welcome\nSIMON: Hello there\n"
        "# Body\nSIMON: This is the body of the episode.\n"
        "# Wrapup\nSIMON: Goodbye.\n"
    )
    chunks = chunk_normalised_text(transcript, target_tokens=200, overlap_tokens=20)
    sections = [c.section_heading for c in chunks]
    assert any(s == "Body" for s in sections), sections
    assert any(s == "Wrapup" for s in sections), sections


def test_chunk_normalised_text_handles_oversize_single_turn() -> None:
    # A single very long monologue should be windowed into multiple chunks.
    long_body = " ".join(f"word{i}" for i in range(2000))
    transcript = f"SIMON: {long_body}"
    chunks = chunk_normalised_text(transcript, target_tokens=300, overlap_tokens=50)
    assert len(chunks) >= 6
    # Subsequent chunks should overlap the previous chunk's tail.
    for prev, curr in zip(chunks[:-1], chunks[1:], strict=True):
        prev_tail_words = prev.text.split()[-50:]
        curr_head = " ".join(curr.text.split()[:50])
        assert any(w in curr_head for w in prev_tail_words)


def test_chunker_rejects_bad_policy() -> None:
    import pytest

    with pytest.raises(ValueError):
        chunk_normalised_text("hi", target_tokens=0, overlap_tokens=0)
    with pytest.raises(ValueError):
        chunk_normalised_text("hi", target_tokens=100, overlap_tokens=200)


def test_base_crawler_chunk_honours_explicit_zero_overlap(tmp_path) -> None:
    # Regression: ``BaseCrawler.chunk(overlap_tokens=0, ...)`` used
    # to silently fall through to the policy default (120) because
    # ``0`` is falsy in Python. The fix uses ``is not None`` so an
    # explicit zero is honoured. We assert this by spying on the
    # call to ``chunk_normalised_text`` from inside ``chunk``.
    from unittest.mock import patch

    from crawl.crawlers.base import (
        BaseCrawler,
        CrawlerConfig,
        NormalisedEpisode,
        RawEpisode,
    )

    config = CrawlerConfig(
        publisher_id="fake",
        publisher_name="Fake",
        base_url="https://example.com",
        rights_code="free_access_copyrighted",
        rights_summary="",
        chunking_policy={"target_tokens": 700, "overlap_tokens": 120},
    )
    crawler = BaseCrawler.__new__(BaseCrawler)
    crawler.config = config
    raw = RawEpisode(
        episode_slug="hello",
        title="Hello",
        primary_url="https://example.com/hello",
        publication_date="2024-01-01",
        raw_bytes=b"",
        content_type="text/html",
    )
    normalised = NormalisedEpisode(raw=raw, normalised_markdown="SIMON: hi", content_hash="x")

    captured: dict[str, int] = {}

    def fake_chunk(text, *, target_tokens, overlap_tokens):
        captured["target_tokens"] = target_tokens
        captured["overlap_tokens"] = overlap_tokens
        return []

    with patch("crawl.crawlers.base.chunk_normalised_text", fake_chunk):
        crawler.chunk(normalised, target_tokens=300, overlap_tokens=0)

    # Explicit ``overlap_tokens=0`` must arrive at the chunker as
    # 0, NOT silently rewritten to the policy default of 120.
    assert captured["target_tokens"] == 300
    assert captured["overlap_tokens"] == 0

    # And ``None`` (or the parameter being omitted) still falls
    # back to the policy default — that part of the contract is
    # unchanged.
    captured.clear()
    with patch("crawl.crawlers.base.chunk_normalised_text", fake_chunk):
        crawler.chunk(normalised)
    assert captured["target_tokens"] == 700
    assert captured["overlap_tokens"] == 120


def test_oversize_turn_carries_overlap_into_next_turn() -> None:
    # Regression: an earlier version of the oversize-turn branch
    # reset the overlap buffer to empty after windowing a single
    # very long monologue, breaking the cross-chunk overlap
    # invariant at the boundary between that monologue and the
    # next speaker. The first chunk of the next turn should still
    # contain words from the tail of the long monologue.
    long_body = " ".join(f"word{i}" for i in range(2000))
    next_turn = "BOB: This is a short follow-up that should pick up the overlap."
    transcript = f"SIMON: {long_body}\n{next_turn}"
    chunks = chunk_normalised_text(transcript, target_tokens=300, overlap_tokens=50)
    # Find the chunk that contains the short follow-up.
    boundary = next(
        (i for i, c in enumerate(chunks) if "short follow-up" in c.text),
        None,
    )
    assert boundary is not None, "follow-up chunk not produced"
    assert boundary > 0, "follow-up should not be the very first chunk"
    prev_tail_words = chunks[boundary - 1].text.split()[-50:]
    boundary_head = " ".join(chunks[boundary].text.split()[:60])
    # At least one tail word from the prior chunk should appear at
    # the head of the boundary chunk.
    assert any(w in boundary_head for w in prev_tail_words), (
        prev_tail_words,
        boundary_head,
    )


def test_fetch_does_not_retry_on_4xx() -> None:
    """Regression: ``BaseCrawler.fetch`` previously retried on every
    non-2xx response because both ``raise_for_status`` (4xx) and
    the explicit 5xx ``raise`` landed in the same retry-catch.
    The fix splits retryable (5xx, 408, 429) from non-retryable
    (other 4xx) statuses — 404 must fail on the first request,
    not the second.
    """

    from unittest.mock import MagicMock, patch

    import requests

    from crawl.crawlers.base import BaseCrawler, CrawlerConfig

    crawler = BaseCrawler.__new__(BaseCrawler)
    crawler.config = CrawlerConfig(
        publisher_id="fake",
        publisher_name="Fake",
        base_url="https://example.com",
        rights_code="free_access_copyrighted",
        rights_summary="",
    )
    crawler._last_request_at = 0.0
    crawler.rate_limit_seconds = 0.0
    crawler._robots_cache = {}
    crawler.session = MagicMock()
    crawler.session.headers = {"User-Agent": "test-agent"}

    # Build a 404 response with the standard ``raise_for_status``
    # contract — calling it must raise ``requests.HTTPError``.
    resp_404 = MagicMock(spec=requests.Response)
    resp_404.status_code = 404
    resp_404.ok = False
    resp_404.reason = "Not Found"
    resp_404.headers = {}
    resp_404.raise_for_status.side_effect = requests.HTTPError(
        "404 Not Found", response=resp_404
    )
    crawler.session.get.return_value = resp_404

    # Robots.txt always allows in this test.
    robots = MagicMock()
    robots.can_fetch.return_value = True
    with patch.object(crawler, "_robots_parser", return_value=robots):
        try:
            crawler.fetch("https://example.com/missing")
        except requests.HTTPError:
            pass
        else:
            raise AssertionError("404 must surface as HTTPError")

    # The critical assertion: exactly ONE call to session.get for a
    # 404. The earlier buggy version made two.
    assert crawler.session.get.call_count == 1, (
        f"4xx must not trigger a retry, got "
        f"{crawler.session.get.call_count} requests"
    )


def test_fetch_retries_on_5xx_then_succeeds() -> None:
    """5xx responses must be retried (and a follow-up 200 must be
    returned cleanly).
    """

    from unittest.mock import MagicMock, patch

    import requests

    from crawl.crawlers.base import BaseCrawler, CrawlerConfig

    crawler = BaseCrawler.__new__(BaseCrawler)
    crawler.config = CrawlerConfig(
        publisher_id="fake",
        publisher_name="Fake",
        base_url="https://example.com",
        rights_code="free_access_copyrighted",
        rights_summary="",
    )
    crawler._last_request_at = 0.0
    crawler.rate_limit_seconds = 0.0
    crawler._robots_cache = {}
    crawler.session = MagicMock()
    crawler.session.headers = {"User-Agent": "test-agent"}

    resp_503 = MagicMock(spec=requests.Response)
    resp_503.status_code = 503
    resp_503.ok = False
    resp_503.reason = "Service Unavailable"
    resp_503.headers = {}

    resp_200 = MagicMock(spec=requests.Response)
    resp_200.status_code = 200
    resp_200.ok = True

    crawler.session.get.side_effect = [resp_503, resp_200]

    robots = MagicMock()
    robots.can_fetch.return_value = True
    with (
        patch.object(crawler, "_robots_parser", return_value=robots),
        patch("crawl.crawlers.base.time.sleep"),  # silence backoff
    ):
        result = crawler.fetch("https://example.com/flaky")

    assert result is resp_200
    assert crawler.session.get.call_count == 2


def test_fetch_retries_on_429_with_retry_after() -> None:
    """429 must be retried and the ``Retry-After`` integer header
    must be honoured for backoff.
    """

    from unittest.mock import MagicMock, patch

    import requests

    from crawl.crawlers.base import BaseCrawler, CrawlerConfig

    crawler = BaseCrawler.__new__(BaseCrawler)
    crawler.config = CrawlerConfig(
        publisher_id="fake",
        publisher_name="Fake",
        base_url="https://example.com",
        rights_code="free_access_copyrighted",
        rights_summary="",
    )
    crawler._last_request_at = 0.0
    crawler.rate_limit_seconds = 0.0
    crawler._robots_cache = {}
    crawler.session = MagicMock()
    crawler.session.headers = {"User-Agent": "test-agent"}

    resp_429 = MagicMock(spec=requests.Response)
    resp_429.status_code = 429
    resp_429.ok = False
    resp_429.reason = "Too Many Requests"
    resp_429.headers = {"Retry-After": "7"}

    resp_200 = MagicMock(spec=requests.Response)
    resp_200.status_code = 200
    resp_200.ok = True

    crawler.session.get.side_effect = [resp_429, resp_200]

    robots = MagicMock()
    robots.can_fetch.return_value = True
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with (
        patch.object(crawler, "_robots_parser", return_value=robots),
        patch("crawl.crawlers.base.time.sleep", side_effect=fake_sleep),
    ):
        result = crawler.fetch("https://example.com/rate-limited")

    assert result is resp_200
    assert crawler.session.get.call_count == 2
    # The 429 path must have slept for the Retry-After interval.
    assert 7.0 in sleeps, sleeps
