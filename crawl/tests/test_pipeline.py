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

    def incremental_sync(self, known_episode_ids=None, *, log_prefix="incremental_sync"):
        """Override to wrap :meth:`initial_sync` with the
        pre-fetch skip filter so the test fixture conforms to
        the base contract documented at
        :meth:`BaseCrawler.incremental_sync` ("if you override
        ``initial_sync`` you MUST also override
        ``incremental_sync``").

        The base implementation in ``BaseCrawler`` uses
        :meth:`_enumerate_slugs` which composes
        ``config.episodes`` and :meth:`_discover_episode_slugs`.
        Test fixtures here override ``initial_sync`` directly
        and have empty ``config.episodes`` plus the default
        empty ``_discover_episode_slugs``, so the inherited base
        ``incremental_sync`` with a non-empty ``known`` set
        would silently yield zero episodes. This override
        bridges the gap by iterating the override
        ``initial_sync`` output and dropping any
        ``RawEpisode`` whose canonical ``episode_id`` is in the
        skip set, which is exactly what the production
        :meth:`_enumerate_slugs` + per-slug filter path does
        for the real crawlers.

        Subclasses of :class:`FakeCrawler` inherit this override,
        so :class:`PerEpisodeRightsCrawler` and the inline
        ``TwinSlugDedupCrawler`` in the regression-test below
        don't need their own copy.

        ``log_prefix`` is accepted (to match the base signature
        the pipeline threads it through with) but ignored — the
        fixture doesn't emit log lines because the test crawlers
        bypass :meth:`_enumerate_slugs` and :meth:`fetch_transcript`
        entirely. The production error-handling path
        (try/except around ``fetch_transcript``, error counter
        in the summary log line) is correspondingly also absent
        from this override; the test crawlers don't raise from
        :meth:`initial_sync`, so there's nothing to wrap. The
        production path is exercised by
        :func:`test_incremental_sync_log_line_uses_incremental_prefix`
        et al. which drive ``DiscoveryMergeCrawler`` through the
        real base implementation.
        """
        known = frozenset(known_episode_ids or ())
        self.last_incremental_skip_count = 0
        if not known:
            yield from self.initial_sync()
            return
        skipped = 0
        for raw in self.initial_sync():
            if self.episode_id_for_slug(raw.episode_slug) in known:
                skipped += 1
                continue
            yield raw
        self.last_incremental_skip_count = skipped

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


def test_default_mode_skips_persistently_rejected_pre_fetch(tmp_path: Path) -> None:
    """Default-mode re-runs must pre-fetch-skip persistently-rejected
    episodes the same way ``--incremental`` does.

    Without this, a publisher with a paywalled rights_code (or
    any code outside the current allowlist) would re-fetch the
    same episode, re-run the gate, and append another deprecated
    governance row on every run — producing unbounded log
    growth for zero new output. The fix splits the boot-time
    skip-set load so the rejected subset is fed to the crawler
    in both modes; only the admitted subset is gated behind
    ``--incremental``.

    Round-10 regression for the default-mode log-growth finding
    flagged on PR #7 (and resolved in a follow-up PR).
    """
    _register_fake()

    # First run: rejects the single fake episode under the
    # paywalled rights_code and writes one deprecated governance
    # entry.
    first_pipeline = Pipeline(
        configs={"fake": _config("paywalled")},
        packs_root=tmp_path,
    )
    first = first_pipeline.run(["fake"])
    first_stats = first.by_publisher["fake"]
    assert first_stats.episodes_seen == 1
    assert first_stats.episodes_rejected_rights == 1
    assert first_stats.episodes_skipped_incremental == 0
    governance_after_first = (
        (tmp_path / "governance" / "rights_log.jsonl")
        .read_text()
        .strip()
        .splitlines()
    )
    assert len(governance_after_first) == 1
    assert json.loads(governance_after_first[0])["deprecated"] is True

    # Second run: same default mode, same policy. The episode is
    # in the persistently-rejected set and must be skipped before
    # the (synthetic) HTTP fetch — i.e. ``episodes_seen == 0``,
    # the skip is attributed to the incremental counter, and no
    # new deprecated row is appended.
    second_pipeline = Pipeline(
        configs={"fake": _config("paywalled")},
        packs_root=tmp_path,
    )
    second = second_pipeline.run(["fake"])
    second_stats = second.by_publisher["fake"]
    assert second_stats.episodes_seen == 0, (
        "default-mode re-run must pre-fetch-skip the rejected episode"
    )
    assert second_stats.episodes_rejected_rights == 0
    assert second_stats.episodes_skipped_incremental == 1
    governance_after_second = (
        (tmp_path / "governance" / "rights_log.jsonl")
        .read_text()
        .strip()
        .splitlines()
    )
    assert len(governance_after_second) == 1, (
        "default-mode re-run must NOT append a duplicate deprecated row"
    )


def test_default_mode_does_not_skip_admitted_pre_fetch(tmp_path: Path) -> None:
    """Default-mode re-runs must still re-fetch previously-admitted
    episodes so the content-hash dedup gate can pick up a
    legitimate transcript update. Only the *rejected* subset is
    in the default-mode pre-fetch skip set — admitted episodes
    are routed through ``initial_sync`` \u2192 ``normalize`` \u2192 dedup
    as they were before the split.

    Companion test to
    :func:`test_default_mode_skips_persistently_rejected_pre_fetch`
    that pins the *other* half of the split: admitted episodes
    are NOT pre-skipped in default mode.
    """
    _register_fake()
    first_pipeline = Pipeline(
        configs={"fake": _config("free_access_copyrighted")},
        packs_root=tmp_path,
    )
    first = first_pipeline.run(["fake"])
    assert first.by_publisher["fake"].episodes_admitted == 1

    second_pipeline = Pipeline(
        configs={"fake": _config("free_access_copyrighted")},
        packs_root=tmp_path,
    )
    second = second_pipeline.run(["fake"])
    second_stats = second.by_publisher["fake"]
    # The episode WAS fetched (``episodes_seen == 1``) and got
    # absorbed by the content-hash dedup gate — NOT pre-fetch-
    # skipped. This is the contract that lets a legitimate
    # transcript update be detected on a re-run.
    assert second_stats.episodes_seen == 1
    assert second_stats.episodes_skipped_dedup == 1
    assert second_stats.episodes_skipped_incremental == 0


def test_default_mode_skip_yields_to_allowlist_change(tmp_path: Path) -> None:
    """If the operator extends the allowlist between runs to cover
    a previously-rejected ``rights_code``, the second default-mode
    run must NOT skip the episode — the rights gate has to get
    another shot at admitting it under the new policy.

    Exercises the policy-aware ``still_rejected`` guard in
    :meth:`Pipeline._load_governance_state` from the default-mode
    angle (the incremental-mode angle is covered by
    :func:`test_pipeline_deprecated_entry_rechecks_when_rights_now_allowed`).
    """
    _register_fake()
    first_pipeline = Pipeline(
        configs={"fake": _config("paywalled")},
        packs_root=tmp_path,
        rights_allowlist=("free_access_copyrighted",),
    )
    first_pipeline.run(["fake"])
    assert first_pipeline._rejected_ids_by_publisher["fake"] == {
        "fake_flagship_hello-world"
    }

    # Operator extends the allowlist between runs. The
    # deprecated entry's ``rights_code`` (``"paywalled"``) is now
    # in the active set, so ``_load_governance_state`` must not
    # add the episode_id to the rejected map, and the default-mode
    # pre-fetch skip set therefore does not contain it.
    relaxed = ("free_access_copyrighted", "paywalled")
    second_pipeline = Pipeline(
        configs={"fake": _config("paywalled")},
        packs_root=tmp_path,
        rights_allowlist=relaxed,
    )
    assert second_pipeline._rejected_ids_by_publisher["fake"] == set()
    second = second_pipeline.run(["fake"])
    second_stats = second.by_publisher["fake"]
    # The episode was re-fetched and the rights gate admitted it
    # under the relaxed allowlist — producing a fresh
    # non-deprecated governance row.
    assert second_stats.episodes_seen == 1
    assert second_stats.episodes_admitted == 1
    assert second_stats.episodes_skipped_incremental == 0


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
    try:
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
    finally:
        crawlers._REGISTRY.pop("fake", None)


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
    try:
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
    finally:
        crawlers._REGISTRY.pop("fake", None)


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


class DiscoveryMergeCrawler(BaseCrawler):
    """Concrete subclass that exercises the base ``initial_sync``.

    Unlike :class:`FakeCrawler`, this *does not* override
    ``initial_sync`` — we want the production merge logic
    (seed dedup + discovery merge + first-seen ordering + log
    line arithmetic) to run as-shipped. ``_discover_episode_slugs``
    and ``fetch_transcript`` are stubbed so the test never
    touches the network.
    """

    publisher_id = "merge_fake"
    publisher_name = "Merge Fake"

    def __init__(
        self,
        config: CrawlerConfig,
        packs_root: Path,
        discovered: list[str],
    ) -> None:
        self.config = config
        self.packs_root = Path(packs_root)
        self.sync_state = self._make_sync_state()
        self._last_request_at = 0.0
        self._robots_cache = {}
        self.session = None  # type: ignore[assignment]
        self._discovered = list(discovered)
        self.fetched: list[str] = []

    @staticmethod
    def _make_sync_state():
        from crawl.crawlers.base import SyncState

        return SyncState()

    def _discover_episode_slugs(self) -> list[str]:
        return list(self._discovered)

    def fetch_transcript(self, slug: str) -> RawEpisode:  # type: ignore[override]
        self.fetched.append(slug)
        return RawEpisode(
            episode_slug=slug,
            title=f"Title for {slug}",
            primary_url=f"https://example.com/episodes/{slug}",
            publication_date="2024-01-01",
            raw_bytes=b"<html><body><p>X: y</p></body></html>",
            content_type="text/html",
            hosts=["X"],
        )

    def normalize(self, raw):  # type: ignore[override]
        from crawl.crawlers.base import canonicalise_text, content_hash

        # Include the slug in the canonical text so each episode
        # produces a *distinct* content_hash. The previous fixture
        # hard-coded ``"# title\n\nX: y"`` for every slug, which
        # meant the content-hash dedup gate would silently absorb
        # all-but-one of any multi-episode admit. That was fine
        # for the seed/discovery enumeration tests (they only
        # exercise ``initial_sync()`` and assert on
        # ``crawler.fetched``), but for pipeline-level coverage
        # we want the dedup gate to be inert so each test really
        # exercises the admit path it claims to.
        text = canonicalise_text(
            f"# {raw.episode_slug}\n\nX: transcript for {raw.episode_slug}"
        )
        return NormalisedEpisode(raw=raw, normalised_markdown=text, content_hash=content_hash(text))


def _merge_config(seeds: list[str]) -> CrawlerConfig:
    cfg = _config()
    return CrawlerConfig(
        # Align with DiscoveryMergeCrawler.publisher_id so the
        # config would pass production __init__ validation (the
        # test bypasses super().__init__, but matching IDs makes
        # the fixture honest about the contract production
        # crawlers operate under).
        publisher_id="merge_fake",
        publisher_name=cfg.publisher_name,
        base_url=cfg.base_url,
        rights_code=cfg.rights_code,
        rights_summary=cfg.rights_summary,
        country_region=cfg.country_region,
        industry_tags=cfg.industry_tags,
        function_tags=cfg.function_tags,
        business_model_tags=cfg.business_model_tags,
        source_type=cfg.source_type,
        language=cfg.language,
        host=cfg.host,
        episodes=list(seeds),
    )


def test_initial_sync_merges_seeds_and_discovery_first_seen_order(
    tmp_path: Path,
) -> None:
    """Seed list comes first, discovery fills in the rest. Order
    is preserved so the configured priority is respected even
    when discovery happens to yield seeds again."""
    cfg = _merge_config(seeds=["seed-a", "seed-b"])
    crawler = DiscoveryMergeCrawler(
        cfg, tmp_path, discovered=["seed-b", "discovered-c", "discovered-d"]
    )
    list(crawler.initial_sync())
    assert crawler.fetched == ["seed-a", "seed-b", "discovered-c", "discovered-d"]


def test_initial_sync_dedupes_duplicate_seeds(tmp_path: Path) -> None:
    """A config that lists the same slug twice in ``episodes`` must
    not cause a double fetch, and the dedup must not displace a
    later discovery slug. This is the regression case that
    motivated the seed-side ``dict.fromkeys`` pass.
    """
    cfg = _merge_config(seeds=["seed-a", "seed-a", "seed-b"])
    crawler = DiscoveryMergeCrawler(
        cfg, tmp_path, discovered=["discovered-c"]
    )
    list(crawler.initial_sync())
    assert crawler.fetched == ["seed-a", "seed-b", "discovered-c"]


def test_initial_sync_pure_discovery_when_no_seeds(tmp_path: Path) -> None:
    cfg = _merge_config(seeds=[])
    crawler = DiscoveryMergeCrawler(cfg, tmp_path, discovered=["a", "b", "c"])
    list(crawler.initial_sync())
    assert crawler.fetched == ["a", "b", "c"]


def test_initial_sync_pure_seed_when_no_discovery(tmp_path: Path) -> None:
    cfg = _merge_config(seeds=["only-seed"])
    crawler = DiscoveryMergeCrawler(cfg, tmp_path, discovered=[])
    list(crawler.initial_sync())
    assert crawler.fetched == ["only-seed"]


def test_initial_sync_discovery_overlap_does_not_duplicate(tmp_path: Path) -> None:
    """If discovery yields a slug that's already in the seed set,
    ``fetch_transcript`` must run exactly once for that slug."""
    cfg = _merge_config(seeds=["same-slug"])
    crawler = DiscoveryMergeCrawler(
        cfg, tmp_path, discovered=["same-slug", "different-slug"]
    )
    list(crawler.initial_sync())
    assert crawler.fetched == ["same-slug", "different-slug"]


class _FailingDiscoveryCrawler(DiscoveryMergeCrawler):
    """Subclass whose ``_discover_episode_slugs`` raises. Used to
    verify that the seed list is still crawled when discovery
    blows up — the regression case the round-8 review flagged.
    """

    def _discover_episode_slugs(self) -> list[str]:
        raise RuntimeError("simulated index-walker failure")


def test_initial_sync_discovery_failure_does_not_swallow_seeds(
    tmp_path: Path, caplog
) -> None:
    """A future ``_discover_episode_slugs`` override that raises
    must NOT prevent the configured seed list from being crawled.
    The guard sits at the merge site so the contract holds for
    every subclass without each subclass having to remember to
    catch its own exceptions.
    """
    import logging

    cfg = _merge_config(seeds=["seed-a", "seed-b"])
    crawler = _FailingDiscoveryCrawler(cfg, tmp_path, discovered=[])
    with caplog.at_level(logging.WARNING, logger="crawl.crawlers.base"):
        list(crawler.initial_sync())
    # Seeds were still fetched in order.
    assert crawler.fetched == ["seed-a", "seed-b"]
    # And the failure was logged as a warning naming the publisher.
    assert any(
        "_discover_episode_slugs raised" in rec.getMessage()
        and "merge_fake" in rec.getMessage()
        for rec in caplog.records
    )


def test_initial_sync_log_counts_after_seed_dedup(
    tmp_path: Path, caplog
) -> None:
    """The INFO log line reports unique-seed + net-new-discovered.
    With duplicate seeds + an overlapping discovery slug, the
    arithmetic must still satisfy seed + discovered = total."""
    import logging

    cfg = _merge_config(seeds=["seed-a", "seed-a", "seed-b"])
    crawler = DiscoveryMergeCrawler(
        cfg, tmp_path, discovered=["seed-b", "discovered-c"]
    )
    with caplog.at_level(logging.INFO, logger="crawl.crawlers.base"):
        list(crawler.initial_sync())
    summary = next(
        rec for rec in caplog.records if "initial_sync" in rec.getMessage()
    )
    msg = summary.getMessage()
    # 2 unique seeds + 1 net-new discovered (seed-b is dropped as
    # an overlap) = 3 unique slugs total.
    assert "2 seed + 1 discovered" in msg
    assert "3 unique slugs" in msg


def test_exit_code_zero_on_empty_run() -> None:
    # No publishers / no episodes seen at all is also not a
    # regression — could be a config that filters out everything,
    # or a dry-run with no crawlers registered.
    totals = PublisherStats(publisher_id="__totals__")
    assert exit_code_for(totals) == 0


# ----------------------------------------------------------------------
# Milestone E — --incremental sync mode
# ----------------------------------------------------------------------


def test_exit_code_zero_on_fully_incremental_skipped_re_run() -> None:
    """``--incremental`` steady-state: every slug was short-circuited
    before the fetch because its episode_id was already in the
    governance log. That's the documented success path — re-running
    on a pack that already has every episode must NOT return 1.
    """
    totals = PublisherStats(
        publisher_id="__totals__",
        episodes_seen=0,
        episodes_admitted=0,
        episodes_skipped_incremental=42,
    )
    assert exit_code_for(totals) == 0


def test_incremental_sync_skips_known_episode_ids(tmp_path: Path) -> None:
    """The incremental skip predicate must fire *before*
    ``fetch_transcript`` so the HTTP cost of re-pulling already-
    ingested episodes is avoided. Slugs whose canonical
    ``episode_id`` is in the known set never reach the fetcher;
    unknown slugs do.
    """
    cfg = _merge_config(seeds=["seed-a", "seed-b"])
    crawler = DiscoveryMergeCrawler(
        cfg, tmp_path, discovered=["discovered-c", "discovered-d"]
    )
    # Pre-bake the canonical episode_id for "seed-a" + "discovered-c"
    # so those two slugs are claimed by a prior run.
    known = frozenset(
        {
            crawler.episode_id_for_slug("seed-a"),
            crawler.episode_id_for_slug("discovered-c"),
        }
    )
    list(crawler.incremental_sync(known_episode_ids=known))
    # The two unknown slugs are still fetched in first-seen order.
    assert crawler.fetched == ["seed-b", "discovered-d"]
    # The crawler exposes the skip count so the pipeline can roll
    # it into PublisherStats.
    assert crawler.last_incremental_skip_count == 2


def test_incremental_sync_empty_cursor_falls_back_to_full_walk(
    tmp_path: Path,
) -> None:
    """Bootstrap case: an empty / missing known set must run the
    full initial_sync walk so the first crawl of a fresh packs
    tree still produces every episode. This is the documented
    safe default and is what makes incremental mode additive
    over initial_sync rather than narrower.
    """
    cfg = _merge_config(seeds=["seed-a"])
    crawler = DiscoveryMergeCrawler(cfg, tmp_path, discovered=["discovered-b"])
    list(crawler.incremental_sync(known_episode_ids=frozenset()))
    assert crawler.fetched == ["seed-a", "discovered-b"]
    # No skips because we fell through to initial_sync; the
    # counter was reset on entry.
    assert crawler.last_incremental_skip_count == 0


def test_incremental_sync_none_cursor_also_falls_back(tmp_path: Path) -> None:
    """``None`` and ``frozenset()`` must both trigger the
    initial_sync fallback so callers don't have to guess which
    falsy sentinel to send. Pinned by a separate test because the
    ``or`` short-circuit in ``frozenset(known_episode_ids or ())``
    was the kind of mistake that would silently let ``None`` fall
    through as "no known IDs, run full walk" only by accident.
    """
    cfg = _merge_config(seeds=["seed-x"])
    crawler = DiscoveryMergeCrawler(cfg, tmp_path, discovered=[])
    list(crawler.incremental_sync(known_episode_ids=None))
    assert crawler.fetched == ["seed-x"]
    assert crawler.last_incremental_skip_count == 0


def test_incremental_sync_skips_all_when_all_known(tmp_path: Path) -> None:
    """If every slug is already in the known set, ``fetched``
    stays empty (no HTTP fetch issued) and the skip count equals
    the total slug count. This is the steady-state regression
    case for ``exit_code_for``.
    """
    cfg = _merge_config(seeds=["a", "b"])
    crawler = DiscoveryMergeCrawler(cfg, tmp_path, discovered=["c"])
    known = frozenset(
        crawler.episode_id_for_slug(s) for s in ("a", "b", "c")
    )
    list(crawler.incremental_sync(known_episode_ids=known))
    assert crawler.fetched == []
    assert crawler.last_incremental_skip_count == 3


def test_pipeline_incremental_mode_no_governance_log_runs_full_walk(
    tmp_path: Path,
) -> None:
    """``Pipeline(incremental=True)`` against a packs root with no
    governance log must produce the same output as the default
    mode: the per-publisher known set is empty, the base
    ``incremental_sync`` falls through to ``initial_sync``, and
    the regular content-hash dedup gate handles idempotency.
    This is the bootstrap regression case.
    """
    _register_fake()
    pipeline = Pipeline(
        configs={"fake": _config()},
        packs_root=tmp_path,
        incremental=True,
    )
    report = pipeline.run(["fake"])
    stats = report.by_publisher["fake"]
    # FakeCrawler overrides initial_sync directly, yielding one
    # episode. With an empty known set the base incremental_sync
    # delegates to initial_sync, so the episode flows through.
    assert stats.episodes_seen == 1
    assert stats.episodes_admitted == 1
    assert stats.episodes_skipped_incremental == 0


def test_pipeline_load_known_episode_ids_groups_by_publisher_prefix(
    tmp_path: Path,
) -> None:
    """Legacy entries that don't carry an explicit ``publisher_id``
    field (written before the round-3 review) must still be
    partitioned correctly by the prefix-matching fallback. Each
    crawler must see only its own admitted set, deprecated rows
    whose rights_code is *still* outside the allowlist must land
    in the skip set (so persistently-rejected episodes don't get
    re-fetched on every run — see round-5 ANALYSIS_0004), and a
    publisher with no config must not bleed into the fake
    publisher's skip set.
    """
    _register_fake()
    gov = tmp_path / "governance" / "rights_log.jsonl"
    gov.parent.mkdir(parents=True, exist_ok=True)
    gov.write_text(
        "\n".join(
            json.dumps(rec)
            for rec in [
                {
                    "episode_id": "fake_flagship_ep1",
                    "rights_code": "free_access_copyrighted",
                    "ingestion_date": 1700000000,
                    "content_hash": "h1",
                    "deprecated": False,
                },
                {
                    "episode_id": "fake_flagship_ep2",
                    "rights_code": "free_access_copyrighted",
                    "ingestion_date": 1700000001,
                    "content_hash": "h2",
                    "deprecated": False,
                },
                {
                    # Deprecated row whose ``rights_code`` is still
                    # outside the current allowlist — must land in
                    # the skip set so ``--incremental`` does NOT
                    # re-fetch + re-reject + re-append on every run
                    # (steady-state log-growth failure mode).
                    "episode_id": "fake_flagship_ep3-still-rejected",
                    "rights_code": "paywalled",
                    "ingestion_date": 1700000002,
                    "content_hash": "h3",
                    "deprecated": True,
                },
                {
                    # Different publisher prefix — must not bleed
                    # into the fake publisher's skip set.
                    "episode_id": "otherpub_flagship_xyz",
                    "rights_code": "free_access_copyrighted",
                    "ingestion_date": 1700000003,
                    "content_hash": "h4",
                    "deprecated": False,
                },
            ]
        )
        + "\n"
    )
    pipeline = Pipeline(
        configs={"fake": _config()},
        packs_root=tmp_path,
        incremental=True,
    )
    known = pipeline._known_ids_for("fake")
    assert known == frozenset(
        {
            "fake_flagship_ep1",
            "fake_flagship_ep2",
            "fake_flagship_ep3-still-rejected",
        }
    )
    # An unknown publisher (no config) returns an empty set
    # rather than raising — defensive against a registry reshuffle
    # that removes a publisher while the governance log still has
    # its history.
    assert pipeline._known_ids_for("otherpub") == frozenset()


def test_pipeline_deprecated_entry_skips_when_rights_still_rejected(
    tmp_path: Path,
) -> None:
    """A rights-rejected episode whose ``rights_code`` is still
    outside the current allowlist must NOT be re-fetched on the
    next ``--incremental`` run. This is the architecturally
    correct fix for round-5 ANALYSIS_0004: without it, every run
    re-fetches every persistently-rejected episode and appends
    another deprecated governance entry, producing unbounded log
    growth and pointless HTTP cost.

    The fix is policy-aware: if the operator later extends
    ``rights_allowlist`` to include the recorded code, the entry
    falls back out of the skip set on the next boot (see
    ``test_pipeline_deprecated_entry_rechecks_when_rights_now_allowed``).
    """
    _register_fake()
    gov = tmp_path / "governance" / "rights_log.jsonl"
    gov.parent.mkdir(parents=True, exist_ok=True)
    gov.write_text(
        json.dumps(
            {
                "publisher_id": "fake",
                "episode_id": "fake_flagship_paywalled-ep",
                "rights_code": "paywalled",
                "ingestion_date": 1700000000,
                "content_hash": "hash-of-rejected-content",
                "deprecated": True,
            }
        )
        + "\n"
    )
    pipeline = Pipeline(
        configs={"fake": _config()},
        packs_root=tmp_path,
        incremental=True,
    )
    known = pipeline._known_ids_for("fake")
    assert known == frozenset({"fake_flagship_paywalled-ep"})
    # The rejected content_hash must NOT be carried into the
    # dedup gate: if the operator extends the allowlist on a
    # future boot, the legitimate re-admission must not be
    # silently absorbed by content-hash dedup.
    assert "hash-of-rejected-content" not in pipeline._seen_content_hashes


def test_pipeline_deprecated_entry_rechecks_when_rights_now_allowed(
    tmp_path: Path,
) -> None:
    """A rights-rejected episode whose ``rights_code`` is NOW in
    the current allowlist (operator extended the allowlist
    between runs) must NOT be in the skip set — the incremental
    crawl must re-fetch so the rights gate can re-evaluate and
    admit. Otherwise a policy change would silently leave
    historically-rejected episodes stuck out of the pack.
    """
    _register_fake()
    gov = tmp_path / "governance" / "rights_log.jsonl"
    gov.parent.mkdir(parents=True, exist_ok=True)
    # Rights code that the operator has now decided to allow.
    now_allowed_code = "newly_allowed_for_test"
    gov.write_text(
        json.dumps(
            {
                "publisher_id": "fake",
                "episode_id": "fake_flagship_previously-rejected",
                "rights_code": now_allowed_code,
                "ingestion_date": 1700000000,
                "content_hash": "hash-of-then-rejected-content",
                "deprecated": True,
            }
        )
        + "\n"
    )
    pipeline = Pipeline(
        configs={"fake": _config()},
        packs_root=tmp_path,
        rights_allowlist=(*DEFAULT_RIGHTS_ALLOWLIST, now_allowed_code),
        incremental=True,
    )
    known = pipeline._known_ids_for("fake")
    assert known == frozenset(), (
        "deprecated entries whose rights_code is now in the "
        "allowlist must NOT be skipped — the crawler must "
        "re-fetch so the rights gate can re-admit."
    )
    # Content-hash dedup must also not block the re-admit.
    assert (
        "hash-of-then-rejected-content"
        not in pipeline._seen_content_hashes
    )


def test_pipeline_load_known_episode_ids_prefers_explicit_publisher_id(
    tmp_path: Path,
) -> None:
    """Governance entries written by the current
    ``emit_governance_entry`` carry an explicit ``publisher_id``
    field. ``_load_governance_state`` must prefer that field over
    the prefix-matching fallback so the loader is robust against
    the edge case where a future publisher_id collides with an
    existing ``{publisher_id}_{series_id}`` pair. We construct
    exactly that collision here: publishers ``foo`` (with
    series_id ``bar``) and ``foo_bar`` (with series_id
    ``flagship``) both produce episode_ids that start with
    ``foo_bar_``. Prefix matching alone would attribute both to
    ``foo_bar`` (the longer prefix wins); the explicit field
    routes each row to the right publisher.
    """
    from crawl import crawlers
    from crawl.crawlers.base import BaseCrawler, CrawlerConfig

    class _FooCrawler(BaseCrawler):
        publisher_id = "foo"
        series_id = "bar"

        def fetch_transcript(self, slug):  # type: ignore[override]
            raise NotImplementedError

    class _FooBarCrawler(BaseCrawler):
        publisher_id = "foo_bar"
        series_id = "flagship"

        def fetch_transcript(self, slug):  # type: ignore[override]
            raise NotImplementedError

    def _cfg(pid: str, series: str) -> CrawlerConfig:
        base = _config()
        return CrawlerConfig(
            publisher_id=pid,
            publisher_name=base.publisher_name,
            base_url=base.base_url,
            rights_code=base.rights_code,
            rights_summary=base.rights_summary,
            country_region=base.country_region,
            industry_tags=base.industry_tags,
            function_tags=base.function_tags,
            business_model_tags=base.business_model_tags,
            source_type=base.source_type,
            language=base.language,
            host=base.host,
            series_id=series,
        )

    gov = tmp_path / "governance" / "rights_log.jsonl"
    gov.parent.mkdir(parents=True, exist_ok=True)
    gov.write_text(
        "\n".join(
            json.dumps(rec)
            for rec in [
                # Belongs to ``foo`` (series ``bar``). Prefix
                # alone would route this to ``foo_bar`` (longer
                # prefix wins). The explicit publisher_id is the
                # tiebreaker.
                {
                    "publisher_id": "foo",
                    "episode_id": "foo_bar_collision-1",
                    "rights_code": "free_access_copyrighted",
                    "ingestion_date": 1700000000,
                    "content_hash": "h-foo-1",
                    "deprecated": False,
                },
                # Belongs to ``foo_bar`` (series ``flagship``).
                {
                    "publisher_id": "foo_bar",
                    "episode_id": "foo_bar_flagship_episode-1",
                    "rights_code": "free_access_copyrighted",
                    "ingestion_date": 1700000001,
                    "content_hash": "h-foobar-1",
                    "deprecated": False,
                },
            ]
        )
        + "\n"
    )
    crawlers._REGISTRY["foo"] = _FooCrawler  # type: ignore[attr-defined]
    crawlers._REGISTRY["foo_bar"] = _FooBarCrawler  # type: ignore[attr-defined]
    try:
        pipeline = Pipeline(
            configs={"foo": _cfg("foo", "bar"), "foo_bar": _cfg("foo_bar", "flagship")},
            packs_root=tmp_path,
            incremental=True,
        )
        assert pipeline._known_ids_for("foo") == frozenset({"foo_bar_collision-1"})
        assert pipeline._known_ids_for("foo_bar") == frozenset(
            {"foo_bar_flagship_episode-1"}
        )
    finally:
        crawlers._REGISTRY.pop("foo", None)
        crawlers._REGISTRY.pop("foo_bar", None)


def test_pipeline_incremental_skip_count_rolls_into_stats(
    tmp_path: Path,
) -> None:
    """When the crawler skips slugs via the base incremental flow,
    the pipeline must read ``last_incremental_skip_count`` from the
    crawler and roll it into :class:`PublisherStats` so the CLI
    summary and ``exit_code_for`` see the short-circuit count.
    """
    # Register the DiscoveryMergeCrawler under a unique publisher
    # id so the pipeline can drive it. We can't use the global
    # ``_register_fake`` helper because DiscoveryMergeCrawler's
    # ``__init__`` takes a ``discovered`` kwarg that the pipeline
    # doesn't know about. Wrap it in a thin factory.
    from crawl import crawlers
    from crawl.crawlers.base import CrawlerConfig

    class _PreDiscoveredCrawler(DiscoveryMergeCrawler):
        publisher_id = "predisc"

        def __init__(self, config: CrawlerConfig, packs_root: Path) -> None:
            super().__init__(
                config=config,
                packs_root=packs_root,
                discovered=["d-1", "d-2"],
            )

    crawlers._REGISTRY["predisc"] = _PreDiscoveredCrawler  # type: ignore[attr-defined]
    try:
        cfg = _merge_config(seeds=["s-1"])
        cfg = CrawlerConfig(
            publisher_id="predisc",
            publisher_name=cfg.publisher_name,
            base_url=cfg.base_url,
            rights_code=cfg.rights_code,
            rights_summary=cfg.rights_summary,
            country_region=cfg.country_region,
            industry_tags=cfg.industry_tags,
            function_tags=cfg.function_tags,
            business_model_tags=cfg.business_model_tags,
            source_type=cfg.source_type,
            language=cfg.language,
            host=cfg.host,
            episodes=["s-1"],
        )

        # Pre-seed governance log with one of the discovered ids so
        # the incremental skip fires for that slug.
        gov = tmp_path / "governance" / "rights_log.jsonl"
        gov.parent.mkdir(parents=True, exist_ok=True)
        gov.write_text(
            json.dumps(
                {
                    "episode_id": "predisc_flagship_d-1",
                    "rights_code": "free_access_copyrighted",
                    "ingestion_date": 1700000000,
                    "content_hash": "h-d-1",
                    "deprecated": False,
                }
            )
            + "\n"
        )

        pipeline = Pipeline(
            configs={"predisc": cfg},
            packs_root=tmp_path,
            incremental=True,
        )
        report = pipeline.run(["predisc"])
        stats = report.by_publisher["predisc"]
        # Three slugs enumerated (s-1, d-1, d-2); d-1 was skipped via
        # the cursor, s-1 and d-2 both reached the fetcher and the
        # admit path. The fixture's ``normalize`` produces a distinct
        # ``content_hash`` per slug so the dedup gate is inert here
        # — both s-1 and d-2 land as fresh admits, proving the
        # incremental flow doesn't accidentally rely on the dedup
        # gate to mask the missing skip arithmetic.
        assert stats.episodes_seen == 2
        assert stats.episodes_admitted == 2
        assert stats.episodes_skipped_dedup == 0
        assert stats.episodes_skipped_incremental == 1
    finally:
        crawlers._REGISTRY.pop("predisc", None)


def test_pipeline_in_memory_skip_set_updates_after_admission(
    tmp_path: Path,
) -> None:
    """Regression for ANALYSIS_0005: episodes admitted during the
    current ``run_publisher`` call must land in
    ``_known_ids_by_publisher`` so a second invocation for the same
    publisher inside the same :class:`Pipeline` lifetime (e.g. a
    duplicate entry in the ``targets`` list) correctly attributes
    the skip to the incremental counter rather than the
    content-hash dedup counter. Without the in-loop update, the
    content-hash gate would still suppress the duplicate emission
    (so the output stays correct) but the operator-facing stats
    would mis-bucket the skip.
    """
    _register_fake()
    pipeline = Pipeline(
        configs={"fake": _config()},
        packs_root=tmp_path,
        incremental=True,
    )
    # Bootstrap: empty governance log, base incremental_sync falls
    # back to initial_sync, FakeCrawler yields one episode, it gets
    # admitted, governance log gets the entry, the in-memory map
    # gets the entry.
    first = pipeline.run_publisher("fake")
    assert first.episodes_admitted == 1
    assert first.episodes_skipped_incremental == 0

    # The freshly-admitted episode_id must be visible in the
    # per-publisher skip set without re-reading the governance log.
    assert pipeline._known_ids_by_publisher["fake"] == {
        "fake_flagship_hello-world"
    }


def test_pipeline_in_memory_skip_set_updates_after_rights_rejection(
    tmp_path: Path,
) -> None:
    """Regression for round-6 ANALYSIS_0002: episodes *rejected* by
    the rights gate during the current ``run_publisher`` call must
    also land in ``_known_ids_by_publisher`` so a second invocation
    for the same publisher in the same :class:`Pipeline` lifetime
    correctly attributes the skip to the incremental counter and
    does not re-fetch, re-reject, and append a duplicate deprecated
    governance row.

    The boot-time policy is: a deprecated entry is in the skip set
    IFF its ``rights_code`` is still outside the current
    ``rights_allowlist``. A rejection that *just happened* during
    this run was by definition processed under the current
    allowlist, so the rejection is necessarily sticky for the
    remainder of the process — there's no need to re-evaluate the
    ``still_rejected`` predicate at the in-process update site.
    """
    from crawl import crawlers

    crawlers._REGISTRY["fake"] = PerEpisodeRightsCrawler  # type: ignore[attr-defined]
    try:
        pipeline = Pipeline(
            configs={"fake": _config("paywalled")},  # publisher-level: blocked
            packs_root=tmp_path,
            incremental=True,
        )
        first = pipeline.run_publisher("fake")
        # PerEpisodeRightsCrawler yields two episodes — one admitted
        # via the cc_by override, one rejected by the paywalled
        # publisher-level code. Both must land in the in-memory skip
        # set so a re-invocation doesn't re-fetch either.
        assert first.episodes_admitted == 1
        assert first.episodes_rejected_rights == 1
        assert first.episodes_skipped_incremental == 0
        assert pipeline._known_ids_by_publisher["fake"] == {
            "fake_flagship_allowed",
            "fake_flagship_rejected",
        }
    finally:
        # Reset the registry so subsequent tests use the vanilla
        # ``FakeCrawler``. Guarded by ``try/finally`` so an assertion
        # failure above doesn't leak ``PerEpisodeRightsCrawler``
        # into later tests (round-9 ANALYSIS_0001).
        crawlers._REGISTRY.pop("fake", None)


def test_pipeline_in_memory_skip_set_updates_after_dedup(
    tmp_path: Path,
) -> None:
    """Regression for round-7 ANALYSIS_0002: episodes content-hash-
    deduped during the current ``run_publisher`` call must also
    land in ``_known_ids_by_publisher`` so a second invocation for
    the same publisher in the same :class:`Pipeline` lifetime
    short-circuits at the incremental gate rather than paying for
    another HTTP fetch + normalisation just to re-discover the
    collision.

    The fixture is a ``FakeCrawler`` subclass that yields two
    episodes with different slugs but whose ``normalize`` produces
    identical canonical text (and therefore identical
    ``content_hash``). The first episode admits, the second hits
    the dedup gate. Both episode_ids must end up in the
    in-memory skip set.
    """
    from crawl import crawlers

    class TwinSlugDedupCrawler(FakeCrawler):
        """Yields two RawEpisodes with distinct slugs. The
        inherited ``normalize`` returns a fixed body, so both
        episodes hash to the same ``content_hash`` and the second
        one collides with the first.
        """

        def initial_sync(self):
            yield RawEpisode(
                episode_slug="twin-a",
                title="Twin A",
                primary_url="https://example.com/episodes/twin-a",
                publication_date="2024-01-01",
                raw_bytes=b"<html><body>a</body></html>",
                content_type="text/html",
                hosts=["Simon"],
            )
            yield RawEpisode(
                episode_slug="twin-b",
                title="Twin B",
                primary_url="https://example.com/episodes/twin-b",
                publication_date="2024-01-02",
                raw_bytes=b"<html><body>b</body></html>",
                content_type="text/html",
                hosts=["Simon"],
            )

    crawlers._REGISTRY["fake"] = TwinSlugDedupCrawler  # type: ignore[attr-defined]
    try:
        pipeline = Pipeline(
            configs={"fake": _config()},
            packs_root=tmp_path,
            incremental=True,
        )
        first = pipeline.run_publisher("fake")
        assert first.episodes_admitted == 1
        assert first.episodes_skipped_dedup == 1
        assert first.episodes_skipped_incremental == 0
        # Both the admitted slug AND the deduped slug must be in
        # the in-memory skip set — otherwise a repeat invocation
        # would re-fetch the deduped slug just to re-hit dedup.
        assert pipeline._known_ids_by_publisher["fake"] == {
            "fake_flagship_twin-a",
            "fake_flagship_twin-b",
        }
    finally:
        crawlers._REGISTRY.pop("fake", None)


def test_incremental_sync_log_line_uses_incremental_prefix(
    tmp_path: Path, caplog
) -> None:
    """The shared ``_enumerate_slugs`` machinery logs which mode is
    driving it so an operator reading a noisy run log can tell
    initial-mode noise from incremental-mode noise. The prefix
    flips based on the caller.
    """
    import logging

    cfg = _merge_config(seeds=["a"])
    crawler = DiscoveryMergeCrawler(cfg, tmp_path, discovered=[])
    known = frozenset({crawler.episode_id_for_slug("a")})
    with caplog.at_level(logging.INFO, logger="crawl.crawlers.base"):
        list(crawler.incremental_sync(known_episode_ids=known))
    msgs = [rec.getMessage() for rec in caplog.records]
    # The enumeration line is tagged with the incremental prefix.
    assert any("incremental_sync — 1 seed + 0 discovered" in m for m in msgs)
    # And the per-run summary names the skip + attempt + error counts
    # (the previous "candidates fetched" tag conflated successful
    # fetches with failed ones — see ANALYSIS_0004 from the round-1
    # Devin Review pass).
    assert any(
        "incremental_sync — 1 known skipped, 0 attempted, 0 failed" in m
        for m in msgs
    )


def test_fake_crawler_incremental_sync_filters_known(tmp_path: Path) -> None:
    """Pins the round-10 contract closure: :class:`FakeCrawler`
    (and its subclasses) override :meth:`BaseCrawler.incremental_sync`
    to filter the override-:meth:`initial_sync` output by
    ``known_episode_ids``.

    Without the override, the inherited base
    :meth:`BaseCrawler.incremental_sync` would route through
    :meth:`_enumerate_slugs` — which composes ``config.episodes``
    and :meth:`_discover_episode_slugs`, both empty/default on
    these fixtures — and therefore silently yield zero episodes
    for any non-empty ``known`` set. The bug was previously
    latent because no test exercised the FakeCrawler-incremental-
    with-non-empty-known path; pin it here so a future regression
    surfaces immediately.
    """
    crawler = FakeCrawler(_config(), tmp_path)
    # Empty known: behaves identically to ``initial_sync`` — one
    # episode yielded, skip counter stays at 0.
    yielded = list(crawler.incremental_sync(known_episode_ids=frozenset()))
    assert [r.episode_slug for r in yielded] == ["hello-world"]
    assert crawler.last_incremental_skip_count == 0

    # Non-empty known with the lone slug present: zero yields,
    # skip counter at 1.
    known = frozenset({crawler.episode_id_for_slug("hello-world")})
    yielded = list(crawler.incremental_sync(known_episode_ids=known))
    assert yielded == []
    assert crawler.last_incremental_skip_count == 1

    # Non-empty known with an unrelated slug: still yields the
    # one episode, skip counter back to 0.
    yielded = list(
        crawler.incremental_sync(
            known_episode_ids=frozenset({"some_other_pid_other_other-slug"})
        )
    )
    assert [r.episode_slug for r in yielded] == ["hello-world"]
    assert crawler.last_incremental_skip_count == 0


def test_pipeline_default_mode_second_call_pre_fetch_skips(tmp_path: Path) -> None:
    """Two ``run_publisher`` invocations against the same publisher
    inside one default-mode :class:`Pipeline` lifetime: the
    second must pre-fetch-skip the rejected episode_id added to
    ``_rejected_ids_by_publisher`` during the first call.

    Mirrors :func:`test_pipeline_in_memory_skip_set_updates_after_rights_rejection`
    but from the default-mode side. Pins the in-process invariant
    that the in-loop write at the rejected path goes to BOTH the
    union map (incremental-only) AND the rejected subset
    (default-mode-active).
    """
    _register_fake()
    pipeline = Pipeline(
        configs={"fake": _config("paywalled")},
        packs_root=tmp_path,
        # Default mode — ``incremental=False``.
    )
    first = pipeline.run_publisher("fake")
    assert first.episodes_seen == 1
    assert first.episodes_rejected_rights == 1
    assert pipeline._rejected_ids_by_publisher["fake"] == {
        "fake_flagship_hello-world"
    }

    # Second call within the same Pipeline lifetime: the rejected
    # set was updated in-process, the default-mode pre-fetch skip
    # picks up the change, and the episode is filtered before
    # being yielded to the pipeline.
    second = pipeline.run_publisher("fake")
    assert second.episodes_seen == 0
    assert second.episodes_rejected_rights == 0
    assert second.episodes_skipped_incremental == 1
    # The governance log still has exactly one deprecated row from
    # the first call — the second call did NOT append.
    governance = (
        (tmp_path / "governance" / "rights_log.jsonl")
        .read_text()
        .strip()
        .splitlines()
    )
    assert len(governance) == 1


def test_pipeline_default_mode_preserves_initial_sync_log_prefix(
    tmp_path: Path, caplog
) -> None:
    """Default-mode pipeline runs always emit the
    ``initial_sync —`` log tag, regardless of whether the
    per-publisher rejected set is empty or not.

    Round-10 follow-up regression. After the skip-set split,
    default-mode runs route episodes through
    :meth:`BaseCrawler.incremental_sync` with the
    rejected-only subset. Without an explicit log prefix
    override, the moment a publisher accumulated a persistently-
    rejected episode the operator-facing log tag would silently
    flip from ``initial_sync —`` to ``incremental_sync —``,
    breaking the long-standing convention that operators grep
    by mode rather than by implementation method. The
    :meth:`Pipeline._iter_episodes` call site threads
    ``log_prefix="initial_sync"`` so the tag remains stable.

    Drives the path via the pipeline (which is where the
    prefix decision lives), not directly via the crawler.
    """
    import logging

    from crawl import crawlers

    class _PreDiscoveredCrawler(DiscoveryMergeCrawler):
        publisher_id = "predisc"

        def __init__(self, config, packs_root) -> None:
            super().__init__(
                config=config,
                packs_root=packs_root,
                discovered=["disc-b"],
            )

    crawlers._REGISTRY["predisc"] = _PreDiscoveredCrawler  # type: ignore[attr-defined]
    try:
        cfg_base = _merge_config(seeds=["seed-a"])
        from crawl.crawlers.base import CrawlerConfig as _CC
        cfg = _CC(
            publisher_id="predisc",
            publisher_name=cfg_base.publisher_name,
            base_url=cfg_base.base_url,
            rights_code=cfg_base.rights_code,
            rights_summary=cfg_base.rights_summary,
            country_region=cfg_base.country_region,
            industry_tags=cfg_base.industry_tags,
            function_tags=cfg_base.function_tags,
            business_model_tags=cfg_base.business_model_tags,
            source_type=cfg_base.source_type,
            language=cfg_base.language,
            host=cfg_base.host,
            episodes=["seed-a"],
        )
        pipeline = Pipeline(
            configs={"predisc": cfg},
            packs_root=tmp_path,
        )
        # Seed the rejected set so the pipeline takes the
        # non-empty-known branch inside ``incremental_sync``.
        # Without seeding, the empty-known branch falls back to
        # ``initial_sync`` directly and the test would trivially
        # pass.
        pipeline._rejected_ids_by_publisher["predisc"].add(
            "predisc_flagship_phantom"
        )

        with caplog.at_level(logging.INFO, logger="crawl.crawlers.base"):
            pipeline.run(["predisc"])

        msgs = [rec.getMessage() for rec in caplog.records]
        # The enumeration line is tagged with the initial-sync
        # prefix — the historical default-mode convention.
        assert any(
            "initial_sync — 1 seed + 1 discovered" in m for m in msgs
        ), msgs
        # And the per-run summary uses the same prefix — a
        # default-mode operator grepping ``initial_sync —``
        # finds both the enumeration and summary lines.
        assert any(
            "initial_sync — 0 known skipped, 2 attempted, 0 failed" in m
            for m in msgs
        ), msgs
        # The ``incremental_sync —`` tag does NOT appear in
        # default-mode logs.
        assert not any("incremental_sync —" in m for m in msgs), msgs
    finally:
        crawlers._REGISTRY.pop("predisc", None)


def test_pipeline_incremental_mode_uses_incremental_log_prefix(
    tmp_path: Path, caplog
) -> None:
    """Mirror of
    :func:`test_pipeline_default_mode_preserves_initial_sync_log_prefix`
    for ``--incremental`` mode: the same pipeline path,
    different mode, different log tag. Pins the other half of
    the mode-aware prefix contract.
    """
    import logging

    from crawl import crawlers

    class _PreDiscoveredCrawler(DiscoveryMergeCrawler):
        publisher_id = "predisc"

        def __init__(self, config, packs_root) -> None:
            super().__init__(
                config=config,
                packs_root=packs_root,
                discovered=["disc-b"],
            )

    crawlers._REGISTRY["predisc"] = _PreDiscoveredCrawler  # type: ignore[attr-defined]
    try:
        cfg_base = _merge_config(seeds=["seed-a"])
        from crawl.crawlers.base import CrawlerConfig as _CC
        cfg = _CC(
            publisher_id="predisc",
            publisher_name=cfg_base.publisher_name,
            base_url=cfg_base.base_url,
            rights_code=cfg_base.rights_code,
            rights_summary=cfg_base.rights_summary,
            country_region=cfg_base.country_region,
            industry_tags=cfg_base.industry_tags,
            function_tags=cfg_base.function_tags,
            business_model_tags=cfg_base.business_model_tags,
            source_type=cfg_base.source_type,
            language=cfg_base.language,
            host=cfg_base.host,
            episodes=["seed-a"],
        )
        pipeline = Pipeline(
            configs={"predisc": cfg},
            packs_root=tmp_path,
            incremental=True,
        )
        # Seed the union set so the pipeline takes the
        # non-empty-known branch.
        pipeline._known_ids_by_publisher["predisc"].add(
            "predisc_flagship_phantom"
        )

        with caplog.at_level(logging.INFO, logger="crawl.crawlers.base"):
            pipeline.run(["predisc"])

        msgs = [rec.getMessage() for rec in caplog.records]
        assert any(
            "incremental_sync — 1 seed + 1 discovered" in m for m in msgs
        ), msgs
        assert any(
            "incremental_sync — 0 known skipped, 2 attempted, 0 failed" in m
            for m in msgs
        ), msgs
        # The ``initial_sync —`` tag does NOT appear in
        # incremental-mode logs (other than any legitimate
        # boot-phase ``initial_sync`` from a downstream fallback,
        # which isn't triggered here because the seeded skip set
        # forces the non-empty-known path).
        assert not any(
            "initial_sync — 1 seed + 1 discovered" in m for m in msgs
        ), msgs
    finally:
        crawlers._REGISTRY.pop("predisc", None)


def test_initial_sync_log_line_keeps_initial_prefix(
    tmp_path: Path, caplog
) -> None:
    """Regression: factoring ``_enumerate_slugs`` out of
    ``initial_sync`` must not change the initial-sync log line's
    tag. Operators rely on the substring ``initial_sync —`` to
    grep the boot phase of the pipeline.
    """
    import logging

    cfg = _merge_config(seeds=["a", "b"])
    crawler = DiscoveryMergeCrawler(cfg, tmp_path, discovered=["c"])
    with caplog.at_level(logging.INFO, logger="crawl.crawlers.base"):
        list(crawler.initial_sync())
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("initial_sync — 2 seed + 1 discovered → 3 unique slugs" in m for m in msgs)
