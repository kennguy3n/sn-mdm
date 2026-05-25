"""sn-mdm crawl orchestrator.

The pipeline drives every concrete crawler through the canonical
stages:

    crawl -> rights_gate -> normalize -> chunk -> tag -> metadata_emit -> governance_log

The three ingestion invariants from the research are enforced
here, not in the per-source crawlers:

1. **Rights gate before chunking.** Episodes whose ``rights_code``
   is not in the configured allowlist are recorded in the
   governance log with ``deprecated = True`` and their chunks are
   never written. This matches the Rust ``PackStore::ingest_episode``
   gate, so re-running ingest never resurfaces rejected episodes.

2. **Chunk by speaker turn and section heading.** Implemented inside
   :func:`crawl.crawlers.base.chunk_normalised_text` — the pipeline
   delegates here.

3. **Companion-resource links.** Crawlers extract these into
   :attr:`crawl.crawlers.base.RawEpisode.asset_urls` and the
   pipeline persists them onto the JSONL episode line so chunks
   can point to the deeper resource at query time.

The pipeline is idempotent: re-running on already-processed
episodes skips them via the ``content_hash`` audit trail, so an
operator can re-crawl any time without producing duplicates.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .crawlers import get_crawler, known_publishers
from .crawlers.base import (
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    BaseCrawler,
    CrawlerConfig,
    NormalisedEpisode,
    RawEpisode,
)

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 path
    import tomli as tomllib  # type: ignore[import-not-found]


LOG = logging.getLogger("sn_mdm.pipeline")


DEFAULT_RIGHTS_ALLOWLIST: tuple[str, ...] = (
    "ogl_v3",
    "cc_by",
    "cc_by_sa",
    "cc_by_nc",
    "cc_by_nc_nd",
    "free_access_copyrighted",
    "public_domain",
)
"""Mirror of ``pack_core::ingest::DEFAULT_RIGHTS_ALLOWLIST``."""


@dataclass
class PublisherStats:
    """Per-publisher tally returned from :meth:`Pipeline.run_publisher`."""

    publisher_id: str
    episodes_seen: int = 0
    episodes_admitted: int = 0
    episodes_rejected_rights: int = 0
    episodes_skipped_dedup: int = 0
    episodes_skipped_incremental: int = 0
    """Slugs short-circuited before the HTTP fetch because their
    canonical ``episode_id`` was already in the governance log.
    Only non-zero when the pipeline was constructed with
    ``incremental=True``. Distinct from
    :attr:`episodes_skipped_dedup` (which fires *after* the
    transcript fetch, on a ``content_hash`` collision)."""
    chunks_emitted: int = 0


@dataclass
class PipelineReport:
    """Aggregate report across all publishers in a pipeline run."""

    by_publisher: dict[str, PublisherStats] = field(default_factory=dict)

    def totals(self) -> PublisherStats:
        out = PublisherStats(publisher_id="__totals__")
        for s in self.by_publisher.values():
            out.episodes_seen += s.episodes_seen
            out.episodes_admitted += s.episodes_admitted
            out.episodes_rejected_rights += s.episodes_rejected_rights
            out.episodes_skipped_dedup += s.episodes_skipped_dedup
            out.episodes_skipped_incremental += s.episodes_skipped_incremental
            out.chunks_emitted += s.chunks_emitted
        return out


class Pipeline:
    """The orchestrator. Holds the registry-derived ``CrawlerConfig``
    objects, the packs root, and the rights allowlist. Designed to
    be re-used across publishers in a single run so the in-process
    governance log can accumulate.
    """

    def __init__(
        self,
        configs: dict[str, CrawlerConfig],
        packs_root: Path,
        rights_allowlist: tuple[str, ...] = DEFAULT_RIGHTS_ALLOWLIST,
        *,
        incremental: bool = False,
    ) -> None:
        self.configs = configs
        self.packs_root = Path(packs_root)
        self.rights_allowlist = {code.lower() for code in rights_allowlist}
        # ``incremental`` toggles between two episode-iteration
        # strategies on the crawler side. In the default mode the
        # pipeline calls :meth:`BaseCrawler.initial_sync` which
        # fetches every discovered + seeded slug; the content-hash
        # gate then short-circuits already-known episodes *after*
        # the HTTP fetch. In incremental mode the pipeline calls
        # :meth:`BaseCrawler.incremental_sync` with the set of
        # known ``episode_id``s for that publisher (computed from
        # the governance log), so the crawler can skip the fetch
        # entirely. The two modes are equivalent in output
        # (steady-state runs produce the same JSONL either way),
        # but incremental drops the HTTP cost of re-fetching
        # already-ingested episodes.
        self.incremental = incremental
        # Map content_hash -> episode_id so re-crawls collapse on
        # already-seen content (third invariant from the research).
        self._seen_content_hashes: set[str] = set()
        # Per-publisher set of ``episode_id``s admitted in some
        # previous run. Populated alongside ``_seen_content_hashes``
        # in a single pass over the governance log so the file is
        # never opened twice for the same data. In default-mode
        # runs the map is still computed (the cost is one extra
        # ``str.startswith`` per record) so a future call into
        # ``_known_ids_for`` doesn't have to re-read; this also
        # keeps the boot-time invariant readable — one method, one
        # pass.
        self._known_ids_by_publisher: dict[str, set[str]] = {
            pid: set() for pid in self.configs
        }
        self._load_governance_state()

    # -- public surface --------------------------------------------------

    def run(self, publisher_ids: list[str] | None = None) -> PipelineReport:
        """Run every configured publisher (or just the requested
        subset).
        """
        targets = publisher_ids or sorted(self.configs)
        report = PipelineReport()
        for pid in targets:
            if pid not in self.configs:
                LOG.warning("no config for publisher_id=%s; skipping", pid)
                continue
            report.by_publisher[pid] = self.run_publisher(pid)
        return report

    def run_publisher(self, publisher_id: str) -> PublisherStats:
        config = self.configs[publisher_id]
        crawler_cls = get_crawler(publisher_id)
        crawler = crawler_cls(config=config, packs_root=self.packs_root)
        stats = PublisherStats(publisher_id=publisher_id)
        # Reset the per-crawler incremental counter so we read the
        # count produced by *this* run, not a leftover from a prior
        # incremental_sync() on the same crawler instance.
        crawler.last_incremental_skip_count = 0

        metadata_path = crawler.open_jsonl("metadata")
        chunks_path = crawler.open_jsonl("chunks")
        governance_path = self.packs_root / "governance" / "rights_log.jsonl"
        governance_path.parent.mkdir(parents=True, exist_ok=True)

        with (
            metadata_path.open("a", encoding="utf-8") as metadata_fp,
            chunks_path.open("a", encoding="utf-8") as chunks_fp,
            governance_path.open("a", encoding="utf-8") as gov_fp,
        ):
            for raw in self._iter_episodes(crawler):
                stats.episodes_seen += 1
                # Rights gate — runs BEFORE normalisation so a
                # rejected source is never even chunked. Prefers a
                # per-episode override on the ``RawEpisode`` if the
                # concrete crawler set one (one-off CC BY guest
                # segment on an otherwise free-access feed, or a
                # show-notes-only fallback that has different
                # rights from the parent series); falls back to the
                # publisher-level rights_code from the registry.
                # Keeps the contract identical to the Rust
                # ``PackStore``.
                effective_rights = raw.rights_code or config.rights_code
                gate_ok = self._rights_gate_allows(effective_rights)
                if not gate_ok:
                    stats.episodes_rejected_rights += 1
                    normalised = self._best_effort_normalise(crawler, raw)
                    self._write_governance(
                        gov_fp, crawler, normalised, deprecated=True
                    )
                    continue

                # Normalise *first* so the content-hash is available
                # for the dedup short-circuit. Only persist the raw
                # bytes once we've confirmed this episode is genuinely
                # new — otherwise a re-crawl rewrites the same
                # ``packs/raw/{publisher}/{slug}.{ext}`` file on every
                # invocation even though the JSONL emission and
                # ``save_normalised`` calls are correctly suppressed
                # by the dedup gate. The previous order (``save_raw``
                # → ``normalize`` → dedup) made the raw cache
                # effectively write-through, defeating part of the
                # point of having a content-hash dedup at all.
                normalised = crawler.normalize(raw)
                if normalised.content_hash in self._seen_content_hashes:
                    stats.episodes_skipped_dedup += 1
                    continue
                self._seen_content_hashes.add(normalised.content_hash)
                crawler.save_raw(raw)
                crawler.save_normalised(normalised)

                episode_dict = crawler.emit_episode(normalised)
                metadata_fp.write(json.dumps(episode_dict, ensure_ascii=False) + "\n")
                metadata_fp.flush()

                chunks = crawler.chunk(normalised)
                for chunk_dict in crawler.emit_chunks(normalised, chunks):
                    chunks_fp.write(json.dumps(chunk_dict, ensure_ascii=False) + "\n")
                    stats.chunks_emitted += 1
                chunks_fp.flush()

                self._write_governance(gov_fp, crawler, normalised, deprecated=False)
                stats.episodes_admitted += 1
                # Keep the in-memory skip set aligned with the
                # governance log we just appended to so a second
                # ``run_publisher(publisher_id)`` call within the
                # same :class:`Pipeline` lifetime (duplicate entry
                # in the ``targets`` list, or a deliberate two-
                # pass run) attributes the skip to the incremental
                # counter rather than the content-hash dedup
                # counter. Without this update, the content-hash
                # gate would still suppress the duplicate emission
                # (output stays correct) but the operator-facing
                # stats would be misleading.
                self._known_ids_by_publisher.setdefault(
                    publisher_id, set()
                ).add(crawler.episode_id_for_slug(raw.episode_slug))
        # Read the incremental skip count the crawler accumulated
        # during this run. In non-incremental mode the counter
        # stays at 0 (we never call incremental_sync), so this is a
        # cheap no-op for the default flow.
        stats.episodes_skipped_incremental = getattr(
            crawler, "last_incremental_skip_count", 0
        )
        return stats

    # -- internals -------------------------------------------------------

    def _iter_episodes(self, crawler: BaseCrawler) -> list[RawEpisode]:
        # Materialise to a list so the iteration order is
        # deterministic across runs (the crawler's own generators
        # are deterministic but `list(...)` makes the contract
        # obvious in tracebacks).
        if self.incremental:
            known = self._known_ids_for(crawler.publisher_id)
            episodes = list(crawler.incremental_sync(known_episode_ids=known))
        else:
            episodes = list(crawler.initial_sync())
        return episodes

    def _known_ids_for(self, publisher_id: str) -> frozenset[str]:
        """Return the set of ``episode_id``s previously admitted
        for ``publisher_id``.

        The underlying ``_known_ids_by_publisher`` map is populated
        once at construction time (single pass over the governance
        log alongside ``_seen_content_hashes``) and *kept in sync*
        as the current run admits new episodes — see
        :meth:`run_publisher`, which adds each freshly-admitted
        episode_id to the publisher's set. This means a second
        ``run_publisher`` call for the same publisher inside one
        :class:`Pipeline` lifetime (e.g. duplicate entry in the
        ``targets`` list) correctly attributes the skip to the
        incremental counter, not the content-hash dedup counter.
        Only non-deprecated rows count — a rights-rejected episode
        is *not* in the skip set because a future crawl could
        legitimately re-admit it under a different rights code.
        """
        # frozenset() on every call would defeat the in-process
        # update path; we hand out a frozen view of the live set.
        return frozenset(self._known_ids_by_publisher.get(publisher_id, ()))

    def _rights_gate_allows(self, rights_code: str) -> bool:
        return rights_code.lower() in self.rights_allowlist

    def _best_effort_normalise(
        self, crawler: BaseCrawler, raw: RawEpisode
    ) -> NormalisedEpisode:
        """Run :meth:`normalize` on a rights-rejected episode so the
        governance log can still record a stable content_hash.
        Failures are swallowed (rejection has already happened —
        we don't fail the run because the audit log couldn't hash
        the body).
        """
        try:
            return crawler.normalize(raw)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("best-effort normalise failed for %s: %s", raw.episode_slug, exc)
            return NormalisedEpisode(
                raw=raw,
                normalised_markdown="",
                content_hash="0" * 64,
            )

    def _write_governance(
        self,
        fp: Any,
        crawler: BaseCrawler,
        normalised: NormalisedEpisode,
        *,
        deprecated: bool,
    ) -> None:
        entry = crawler.emit_governance_entry(normalised, deprecated=deprecated)
        fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fp.flush()

    def _load_governance_state(self) -> None:
        """Walk the governance log exactly once and populate both
        ``_seen_content_hashes`` and ``_known_ids_by_publisher``.

        The governance log is append-only and is the single source
        of truth for two boot-time facts:

        1. Which ``content_hash`` digests are already represented
           in the pack (used by the post-fetch dedup gate).
        2. Which ``episode_id``s have been admitted for each
           publisher (used by the pre-fetch incremental skip).

        Reading the file once and populating both maps in the same
        loop is strictly cheaper than two passes — the dominant
        cost is the JSON parse, which we'd otherwise pay twice per
        record — and keeps the boot-time semantics of the two
        gates colocated.

        Latest-wins note: the log is documented as effectively
        append-once per ``episode_id`` (the two upstream gates,
        content-hash dedup in default mode and incremental skip in
        ``--incremental`` mode, both prevent a second entry for an
        already-admitted episode from being written). If a future
        change ever permits a non-deprecated row followed by a
        deprecated row for the same episode_id, the deprecated row
        would NOT remove the earlier ``episode_id`` from the skip
        set here, and the incremental gate would continue to
        skip it. That contract is intentional today (a rejection
        happening *after* an admission is a curation decision the
        operator made deliberately, and we should not silently
        force a re-fetch of the same content), but a future
        latest-wins semantics would need to track the *last* row
        per episode_id rather than the *first* match.
        """
        gov = self.packs_root / "governance" / "rights_log.jsonl"
        if not gov.exists():
            return
        # Pre-build a list of ``(publisher_id, prefix)`` pairs
        # sorted by descending prefix length so a longer prefix
        # (e.g. ``acquired_long_form_``) wins over a shorter one
        # (e.g. ``acquired_``) on partition. Today no two
        # registered publishers share a prefix, so the order
        # doesn't matter; the sort is defence-in-depth for future
        # additions.
        prefixes = sorted(
            ((pid, f"{pid}_") for pid in self.configs),
            key=lambda kv: len(kv[1]),
            reverse=True,
        )
        with gov.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("deprecated"):
                    # Don't carry rejection hashes or ids forward
                    # — a re-crawl could later succeed under a
                    # different rights code.
                    continue
                ch = rec.get("content_hash")
                if isinstance(ch, str) and ch:
                    self._seen_content_hashes.add(ch)
                ep_id = rec.get("episode_id")
                if isinstance(ep_id, str) and ep_id:
                    for pid, prefix in prefixes:
                        if ep_id.startswith(prefix):
                            self._known_ids_by_publisher[pid].add(ep_id)
                            break


# ------------------------------------------------------------------
# Source-registry loader
# ------------------------------------------------------------------


def load_config(path: Path) -> dict[str, CrawlerConfig]:
    """Parse ``crawl_config.toml`` into a dict of
    :class:`CrawlerConfig`. Unknown publishers raise ``KeyError``
    (so the operator notices an unregistered crawler at boot, not
    at run-time).
    """
    with path.open("rb") as fp:
        raw = tomllib.load(fp)
    sources = raw.get("sources", {})
    configs: dict[str, CrawlerConfig] = {}
    for pid, body in sources.items():
        if pid not in known_publishers():
            raise KeyError(
                f"crawl_config.toml lists publisher_id={pid!r} but no crawler is registered; "
                f"known: {known_publishers()}"
            )
        # Defensive defaults — let operators omit empty fields.
        configs[pid] = CrawlerConfig(
            publisher_id=pid,
            publisher_name=body.get("publisher_name", pid),
            base_url=body.get("base_url", ""),
            rights_code=body.get("rights_code", "unknown"),
            rights_summary=body.get("rights_summary", ""),
            country_region=list(body.get("country_region", [])),
            industry_tags=list(body.get("industry_tags", [])),
            function_tags=list(body.get("function_tags", [])),
            business_model_tags=list(body.get("business_model_tags", [])),
            source_type=body.get("source_type", "podcast_transcript_html"),
            language=body.get("language", "en"),
            series_id=body.get("series_id", "flagship"),
            series_title=body.get("series_title", body.get("publisher_name", pid)),
            host=body.get("host", ""),
            primary_series_url=body.get("primary_series_url", ""),
            episodes=list(body.get("episodes", [])),
            credibility_notes=body.get("credibility_notes", ""),
            chunking_policy={
                "target_tokens": int(
                    body.get("chunking_policy", {}).get("target_tokens", DEFAULT_TARGET_TOKENS)
                ),
                "overlap_tokens": int(
                    body.get("chunking_policy", {}).get("overlap_tokens", DEFAULT_OVERLAP_TOKENS)
                ),
            },
        )
    return configs


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m crawl.pipeline",
        description=(
            "Drive sn-mdm crawlers through the canonical "
            "crawl -> rights-gate -> normalize -> chunk -> tag pipeline."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "crawl_config.toml",
        help="Path to the source registry TOML.",
    )
    parser.add_argument(
        "--packs-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "packs",
        help="Output root for raw/, normalized/, metadata/, chunks/, governance/.",
    )
    parser.add_argument(
        "publishers",
        nargs="*",
        help="Subset of publisher ids to run. Defaults to every registered publisher.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Steady-state mode. Skip slugs whose canonical episode_id is "
            "already present (non-deprecated) in the governance log, so the "
            "crawler avoids re-fetching transcripts the pack already has. "
            "Default mode runs the full discovery + transcript fetch loop and "
            "relies on the content_hash gate to skip duplicates *after* the "
            "HTTP fetch."
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    configs = load_config(args.config)
    pipeline = Pipeline(
        configs=configs,
        packs_root=args.packs_root,
        incremental=args.incremental,
    )
    report = pipeline.run(args.publishers or None)
    totals = report.totals()
    LOG.info(
        "pipeline complete: %d seen, %d admitted, %d rejected, %d deduped, %d incremental-skipped, %d chunks",
        totals.episodes_seen,
        totals.episodes_admitted,
        totals.episodes_rejected_rights,
        totals.episodes_skipped_dedup,
        totals.episodes_skipped_incremental,
        totals.chunks_emitted,
    )
    return exit_code_for(totals)


def exit_code_for(totals: PublisherStats) -> int:
    """Decide the CLI exit code from an aggregate :class:`PublisherStats`.

    Non-zero only when every publisher failed to admit anything *and*
    the failure can't be explained by any of the documented
    short-circuits: the rights gate, the content-hash dedup gate,
    or the incremental skip predicate. The pipeline is documented as
    idempotent — re-running on a packs root that already contains
    every episode will (correctly) admit nothing because every
    episode hashes to a ``content_hash`` that's already in the
    governance log (default mode) or its ``episode_id`` is in the
    incremental skip set (``--incremental`` mode). Likewise a run
    that only saw rights-rejected episodes is doing exactly what
    the gate asked of it. Only return non-zero when *all four*
    explanations are absent — that's the signal of a real crawl
    regression (e.g. every source's HTML changed shape and parses
    to empty), not normal steady-state.
    """
    if (
        totals.episodes_seen > 0
        and totals.episodes_admitted == 0
        and totals.episodes_rejected_rights == 0
        and totals.episodes_skipped_dedup == 0
        and totals.episodes_skipped_incremental == 0
    ):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
