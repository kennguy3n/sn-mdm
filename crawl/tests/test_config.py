"""Tests for the source-registry loader.

These tests don't touch the network — they parse the on-disk TOML
and assert that every publisher mentioned in the registry has a
registered crawler class, that every rights_code is in the
allowlist, and that the chunking policy is consistent with the
default constants.
"""

from __future__ import annotations

from pathlib import Path

from crawl.crawlers import known_publishers
from crawl.crawlers.base import DEFAULT_OVERLAP_TOKENS, DEFAULT_TARGET_TOKENS
from crawl.pipeline import DEFAULT_RIGHTS_ALLOWLIST, load_config

REPO_CONFIG = Path(__file__).resolve().parent.parent / "crawl_config.toml"


def test_registry_covers_all_known_publishers() -> None:
    configs = load_config(REPO_CONFIG)
    for pid in known_publishers():
        assert pid in configs, f"missing source-registry entry for {pid}"


def test_every_config_has_known_rights_code() -> None:
    configs = load_config(REPO_CONFIG)
    for pid, cfg in configs.items():
        assert cfg.rights_code in DEFAULT_RIGHTS_ALLOWLIST, (
            f"{pid} has rights_code={cfg.rights_code!r} which is not in the allowlist; "
            "update DEFAULT_RIGHTS_ALLOWLIST or fix the registry"
        )


def test_every_config_specifies_chunking_policy() -> None:
    configs = load_config(REPO_CONFIG)
    for pid, cfg in configs.items():
        assert cfg.chunking_policy["target_tokens"] == DEFAULT_TARGET_TOKENS, pid
        assert cfg.chunking_policy["overlap_tokens"] == DEFAULT_OVERLAP_TOKENS, pid


def test_every_config_carries_tags() -> None:
    configs = load_config(REPO_CONFIG)
    for pid, cfg in configs.items():
        # Geography is captured on `country_region`; the other four
        # families on `*_tags`. At least one tag must be present in
        # each non-geography family so the structured-filter columns
        # have something to match on.
        assert cfg.industry_tags, f"{pid} missing industry_tags"
        assert cfg.function_tags, f"{pid} missing function_tags"
        assert cfg.business_model_tags, f"{pid} missing business_model_tags"
        assert cfg.country_region, f"{pid} missing country_region"


def test_ted_worklife_has_empty_host_to_avoid_legacy_misattribution() -> None:
    """``ted_worklife`` is the one publisher in the registry that
    must NOT carry a static config host. WorkLife has changed
    hosts (Adam Grant 2018-2024, Molly Graham 2024-present) and
    TED keeps older transcripts under the same hub. Falling back
    to a single "current host" in :meth:`BaseCrawler.emit_episode`
    would mis-attribute the legacy Adam Grant transcripts.

    The crawler also returns ``RawEpisode(hosts=[])`` — see
    ``test_ted_fetch_transcript_emits_empty_hosts`` — and the two
    must agree, because ``emit_episode``'s fallback expression
    ``raw.hosts or ([config.host] if config.host else [])``
    short-circuits to ``[]`` only when *both* are falsy.
    """
    configs = load_config(REPO_CONFIG)
    cfg = configs["ted_worklife"]
    assert cfg.host == "", (
        "ted_worklife must have host='' so emit_episode's fallback "
        "does not stamp legacy Adam Grant episodes with the current "
        "host"
    )
