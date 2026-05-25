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
    """The governance-log reader must partition episode_ids by
    publisher prefix so each crawler sees only its own admitted
    set. Deprecated rows must be excluded so a future re-crawl
    under a different rights code can still admit them.
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
                    # Deprecated row — must NOT land in the skip set.
                    "episode_id": "fake_flagship_ep3-rejected",
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
        {"fake_flagship_ep1", "fake_flagship_ep2"}
    )
    # An unknown publisher (no config) returns an empty set
    # rather than raising — defensive against a registry reshuffle
    # that removes a publisher while the governance log still has
    # its history.
    assert pipeline._known_ids_for("otherpub") == frozenset()


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
