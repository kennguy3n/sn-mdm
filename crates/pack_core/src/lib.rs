//! `pack_core` — Rust core of the **sn-mdm** content-pack pipeline.
//!
//! `sn-mdm` is a permanent, highly compact content-pack builder for
//! on-device agents. The Python `crawl/` layer fetches transcripts
//! from freely accessible podcast sources (Acquired, a16z, BCG,
//! McKinsey, …), normalises them to markdown, and emits two JSONL
//! streams (`packs/metadata/*.jsonl`, `packs/chunks/*.jsonl`) plus a
//! governance log (`packs/governance/rights_log.jsonl`).
//!
//! This crate is the consumer of those JSONL streams. It:
//!
//! 1. Ingests episodes and chunks into a SQLCipher database with a
//!    BLAKE3 content-hash dedup gate. The schema is patterned on
//!    `kennguy3n/knowledge`'s `evidence_store` — see
//!    [`schema::SCHEMA_SQL`] for the full bootstrap statements.
//! 2. Indexes chunk text into an FTS5 virtual table using the
//!    canonical `unicode61 remove_diacritics 2` tokenizer shared
//!    with the `knowledge` substrate.
//! 3. Serves multi-lane search ([`search::SearchEngine`]) that
//!    merges FTS5 BM25, tag-filtered structured matches, and an
//!    optional semantic-vector lane — the rank-merge formula mirrors
//!    `chat-storage-search`'s `QueryEngine`.
//! 4. Exports a single compact `.pack` artefact ([`export::PackBuilder`])
//!    that bundles compressed chunk text, metadata, FTS5 index, and a
//!    governance manifest for shipping to the on-device agent.
//!
//! Every public surface is documented inline; see `docs/ARCHITECTURE.md`
//! at the repo root for the cross-crate view.

pub mod error;
pub mod export;
pub mod ingest;
pub mod metadata;
pub mod schema;
pub mod search;

pub use error::{PackError, Result};
pub use export::{PackBuilder, PackHeader, PackManifest, PackReader};
pub use ingest::{IngestReport, IngestStats, PackStore};
pub use metadata::{
    Chunk, ChunkingPolicy, Episode, Publisher, RightsRecord, Series, TagFamilies, DEFAULT_CHUNKING,
};
pub use search::{RankWeights, SearchEngine, SearchHit, SearchQuery, SearchScope, TagFilter};

/// Schema version stamped into `PRAGMA user_version`. Bumped on every
/// breaking schema change.
///
/// History:
/// - v1: initial `episode` / `chunk` / `chunk_fts` /
///   `chunk_embeddings` / `governance_log` tables. Mirrors the
///   schema pattern from `kennguy3n/knowledge`'s `evidence_store`
///   (append-only, BLAKE3 content-hash dedup, FTS5 virtual table
///   with `unicode61 remove_diacritics 2`).
pub const SCHEMA_VERSION: i32 = 1;

/// Canonical pack-format magic header — `b"SNMDM"`. Written as the
/// first 5 bytes of every `.pack` file. Immediately followed on disk
/// by a single `u8` pack-format version byte (see
/// [`export::PACK_FORMAT_VERSION`]); split out from the magic so a
/// future pack with a bumped framing version still presents the same
/// magic prefix and returns the more informative
/// [`PackError::UnsupportedPackVersion`] rather than the generic
/// [`PackError::BadMagic`].
pub const PACK_MAGIC: [u8; 5] = *b"SNMDM";
