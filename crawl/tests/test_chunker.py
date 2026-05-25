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
    # per line, leading + trailing blank lines trimmed. Internal blank lines
    # are preserved so paragraph structure survives the hash canonicalisation.
    raw = "  \r\nHello\r\nworld   \r\n\n\nfoo\tbar  \r\n  "
    canon = canonicalise_text(raw)
    assert canon == "Hello\nworld\n\n\nfoo bar"


def test_content_hash_is_canonical() -> None:
    # ``content_hash`` itself is a pure hash now — it expects the
    # caller to pre-canonicalise (see ``BaseCrawler.normalize``).
    # Two raw inputs that canonicalise to the same string must
    # therefore hash identically once both go through
    # ``canonicalise_text`` first.
    a = "Hello\r\nworld   "
    b = "Hello\nworld"
    assert content_hash(canonicalise_text(a)) == content_hash(canonicalise_text(b))
    # The hex digest length is at least 32 (SHA-256 fallback) or 64
    # (BLAKE3) depending on which backend is installed.
    assert len(content_hash(canonicalise_text(a))) >= 32
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
