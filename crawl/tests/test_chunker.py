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
    a = "Hello\r\nworld   "
    b = "Hello\nworld"
    assert content_hash(a) == content_hash(b)
    assert len(content_hash(a)) >= 32


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
