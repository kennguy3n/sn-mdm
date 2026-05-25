"""Tests for the rights gate + JSONL emission contract.

These tests exercise the pipeline with a synthetic crawler that
never touches the network — the network-touching crawlers are
exercised by the real source registry in integration runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from crawl.crawlers.base import (
    BaseCrawler,
    CrawlerConfig,
    NormalisedEpisode,
    RawEpisode,
)
from crawl.pipeline import DEFAULT_RIGHTS_ALLOWLIST, Pipeline, PublisherStats, exit_code_for


class FakeCrawler(BaseCrawler):
    """Pure-Python crawler that produces a fixed episode without
    going to the network. Used in tests to exercise the pipeline
    end-to-end without depending on live sources.
    """

    publisher_id = "fake"
    publisher_name = "Fake Publisher"

    def __init__(self, config: CrawlerConfig, packs_root: Path) -> None:
        # Skip session setup — we'll never hit the network.
        self.config = config
        self.packs_root = Path(packs_root)
        self.sync_state = self._make_sync_state()
        self._last_request_at = 0.0
        self._robots_cache = {}
        # Lazy session — only built if something calls fetch().
        self.session = None  # type: ignore[assignment]

    @staticmethod
    def _make_sync_state():
        from crawl.crawlers.base import SyncState

        return SyncState()

    def initial_sync(self):
        yield RawEpisode(
            episode_slug="hello-world",
            title="Hello World",
            primary_url="https://example.com/episodes/hello-world",
            publication_date="2024-01-01",
            raw_bytes=b"<html><body><h1>Hello</h1><p>SIMON: hi</p></body></html>",
            content_type="text/html",
            hosts=["Simon"],
            guests=[],
            asset_urls=["https://example.com/report.pdf"],
            summary="A test episode.",
        )

    def normalize(self, raw):
        from crawl.crawlers.base import canonicalise_text, content_hash

        body = "# Hello\n\nSIMON: hi"
        text = canonicalise_text(body)
        return NormalisedEpisode(raw=raw, normalised_markdown=text, content_hash=content_hash(text))


def _register_fake() -> None:
    """Register the fake crawler in the registry so the pipeline
    can look it up by publisher_id.
    """
    from crawl import crawlers

    crawlers._REGISTRY["fake"] = FakeCrawler  # type: ignore[attr-defined]


def _config(rights_code: str = "free_access_copyrighted") -> CrawlerConfig:
    return CrawlerConfig(
        publisher_id="fake",
        publisher_name="Fake Publisher",
        base_url="https://example.com",
        rights_code=rights_code,
        rights_summary="Test summary",
        country_region=["US"],
        industry_tags=["tech"],
        function_tags=["strategy"],
        business_model_tags=["B2B"],
        source_type="podcast_transcript_html",
        language="en",
        host="Simon",
    )


def test_rights_gate_admits_known_codes(tmp_path: Path) -> None:
    _register_fake()
    pipeline = Pipeline(
        configs={"fake": _config("free_access_copyrighted")},
        packs_root=tmp_path,
    )
    report = pipeline.run(["fake"])
    stats = report.by_publisher["fake"]
    assert stats.episodes_seen == 1
    assert stats.episodes_admitted == 1
    assert stats.episodes_rejected_rights == 0
    assert stats.chunks_emitted >= 1

    metadata = (tmp_path / "metadata" / "fake.jsonl").read_text().strip().splitlines()
    assert len(metadata) == 1
    parsed = json.loads(metadata[0])
    assert parsed["episode_id"].startswith("fake_flagship_")
    assert parsed["rights_code"] == "free_access_copyrighted"
    assert parsed["asset_urls"] == ["https://example.com/report.pdf"]

    governance = (tmp_path / "governance" / "rights_log.jsonl").read_text().strip().splitlines()
    assert len(governance) == 1
    parsed_gov = json.loads(governance[0])
    assert parsed_gov["rights_code"] == "free_access_copyrighted"
    assert parsed_gov["deprecated"] is False


def test_rights_gate_rejects_unknown_codes(tmp_path: Path) -> None:
    _register_fake()
    pipeline = Pipeline(
        configs={"fake": _config("paywalled")},
        packs_root=tmp_path,
    )
    report = pipeline.run(["fake"])
    stats = report.by_publisher["fake"]
    assert stats.episodes_rejected_rights == 1
    assert stats.episodes_admitted == 0
    assert stats.chunks_emitted == 0

    # Metadata + chunks must remain empty when the gate refuses.
    assert not (tmp_path / "metadata" / "fake.jsonl").exists() or (
        (tmp_path / "metadata" / "fake.jsonl").read_text().strip() == ""
    )
    governance = (tmp_path / "governance" / "rights_log.jsonl").read_text().strip().splitlines()
    parsed = [json.loads(line) for line in governance]
    assert all(p["deprecated"] for p in parsed)
    assert parsed[0]["rights_code"] == "paywalled"


def test_dedup_skips_seen_content_hashes(tmp_path: Path) -> None:
    _register_fake()
    pipeline = Pipeline(
        configs={"fake": _config("free_access_copyrighted")},
        packs_root=tmp_path,
    )
    pipeline.run(["fake"])
    # Build a *second* pipeline so the in-process governance log is
    # re-loaded from disk (mirrors a fresh CLI run on the same
    # packs directory).
    pipeline2 = Pipeline(
        configs={"fake": _config("free_access_copyrighted")},
        packs_root=tmp_path,
    )
    report = pipeline2.run(["fake"])
    stats = report.by_publisher["fake"]
    assert stats.episodes_seen == 1
    assert stats.episodes_admitted == 0
    assert stats.episodes_skipped_dedup == 1


def test_dedup_skip_does_not_rewrite_raw_file(tmp_path: Path) -> None:
    """Regression: ``save_raw`` must run *after* the content-hash
    dedup gate, not before it. The previous order made the raw
    cache effectively write-through — a re-crawl rewrote
    ``packs/raw/{publisher}/{slug}.html`` on every invocation
    even though the JSONL emission and ``save_normalised`` calls
    were correctly suppressed.
    """

    save_raw_calls: list[str] = []

    class CountingCrawler(FakeCrawler):
        def save_raw(self, raw):  # type: ignore[override]
            save_raw_calls.append(raw.episode_slug)
            return super().save_raw(raw)

    from crawl import crawlers

    crawlers._REGISTRY["fake"] = CountingCrawler  # type: ignore[attr-defined]

    pipeline = Pipeline(
        configs={"fake": _config("free_access_copyrighted")},
        packs_root=tmp_path,
    )
    pipeline.run(["fake"])
    assert save_raw_calls == ["hello-world"], (
        "First run must persist the raw bytes (the file isn't on disk yet)"
    )

    # Second pipeline reads the governance log from disk and seeds
    # ``_seen_content_hashes`` so the second crawl of the same
    # episode hits the dedup gate.
    save_raw_calls.clear()
    pipeline2 = Pipeline(
        configs={"fake": _config("free_access_copyrighted")},
        packs_root=tmp_path,
    )
    report = pipeline2.run(["fake"])
    stats = report.by_publisher["fake"]
    assert stats.episodes_skipped_dedup == 1
    assert save_raw_calls == [], (
        "Dedup-skipped episode must not invoke save_raw — otherwise the "
        "raw cache is wastefully rewritten on every re-crawl."
    )


def test_default_rights_allowlist_mirrors_pack_core() -> None:
    # Mirrors `pack_core::ingest::DEFAULT_RIGHTS_ALLOWLIST`.
    assert "ogl_v3" in DEFAULT_RIGHTS_ALLOWLIST
    assert "cc_by_nc_nd" in DEFAULT_RIGHTS_ALLOWLIST
    assert "free_access_copyrighted" in DEFAULT_RIGHTS_ALLOWLIST


class PerEpisodeRightsCrawler(FakeCrawler):
    """Fake crawler that overrides the rights code on a per-episode
    basis via ``RawEpisode.rights_code``. Used to exercise the
    defense-in-depth rights gate.
    """

    def initial_sync(self):
        # Two episodes:
        #   - "allowed" piggybacks on a CC BY override even though
        #     the publisher-level code below is paywalled,
        #   - "rejected" leaves the override empty so the rejected
        #     publisher-level code applies.
        yield RawEpisode(
            episode_slug="allowed",
            title="Allowed",
            primary_url="https://example.com/episodes/allowed",
            publication_date="2024-01-01",
            raw_bytes=b"<html><body><p>SIMON: hi</p></body></html>",
            content_type="text/html",
            hosts=["Simon"],
            rights_code="cc_by",
            rights_summary="One-off CC BY guest segment.",
        )
        yield RawEpisode(
            episode_slug="rejected",
            title="Rejected",
            primary_url="https://example.com/episodes/rejected",
            publication_date="2024-01-02",
            raw_bytes=b"<html><body><p>SIMON: hi</p></body></html>",
            content_type="text/html",
            hosts=["Simon"],
        )


def test_per_episode_rights_override_admits_when_publisher_blocks(
    tmp_path: Path,
) -> None:
    from crawl import crawlers

    crawlers._REGISTRY["fake"] = PerEpisodeRightsCrawler  # type: ignore[attr-defined]
    pipeline = Pipeline(
        configs={"fake": _config("paywalled")},  # publisher-level: blocked
        packs_root=tmp_path,
    )
    report = pipeline.run(["fake"])
    stats = report.by_publisher["fake"]
    # One episode admitted (cc_by override), one rejected (no
    # override, publisher-level paywalled).
    assert stats.episodes_seen == 2
    assert stats.episodes_admitted == 1
    assert stats.episodes_rejected_rights == 1
    # Metadata line for the admitted episode must carry the
    # per-episode rights code, not the publisher-level one.
    metadata = (tmp_path / "metadata" / "fake.jsonl").read_text().strip().splitlines()
    assert len(metadata) == 1
    parsed = json.loads(metadata[0])
    assert parsed["episode_id"].endswith("_allowed")
    assert parsed["rights_code"] == "cc_by"
    assert parsed["rights_summary"] == "One-off CC BY guest segment."
    assert "paywalled" not in DEFAULT_RIGHTS_ALLOWLIST


def test_exit_code_zero_on_fresh_admit() -> None:
    # Happy path: ingest admitted at least one episode.
    totals = PublisherStats(
        publisher_id="__totals__",
        episodes_seen=3,
        episodes_admitted=3,
    )
    assert exit_code_for(totals) == 0


def test_exit_code_zero_on_fully_deduped_re_run() -> None:
    # Regression: idempotent re-runs admit 0 episodes because every
    # candidate hashes to a content_hash already in the governance
    # log. That's the documented happy-path for a steady-state pack —
    # the CLI must NOT return 1.
    totals = PublisherStats(
        publisher_id="__totals__",
        episodes_seen=5,
        episodes_admitted=0,
        episodes_rejected_rights=0,
        episodes_skipped_dedup=5,
    )
    assert exit_code_for(totals) == 0


def test_exit_code_zero_when_only_rejected_by_rights() -> None:
    # Rights gate doing its job is also success, not failure.
    totals = PublisherStats(
        publisher_id="__totals__",
        episodes_seen=2,
        episodes_admitted=0,
        episodes_rejected_rights=2,
    )
    assert exit_code_for(totals) == 0


def test_exit_code_one_on_silent_regression() -> None:
    # The only condition that should return non-zero: episodes were
    # seen, but none were admitted AND none were explained by either
    # the rights gate or the dedup short-circuit. This is the signal
    # that the HTML structure of every source changed at once and
    # the parser is silently producing empty episodes.
    totals = PublisherStats(
        publisher_id="__totals__",
        episodes_seen=4,
        episodes_admitted=0,
        episodes_rejected_rights=0,
        episodes_skipped_dedup=0,
    )
    assert exit_code_for(totals) == 1


def test_exit_code_zero_on_empty_run() -> None:
    # No publishers / no episodes seen at all is also not a
    # regression — could be a config that filters out everything,
    # or a dry-run with no crawlers registered.
    totals = PublisherStats(publisher_id="__totals__")
    assert exit_code_for(totals) == 0
