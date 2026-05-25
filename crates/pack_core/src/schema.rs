//! SQL schema for the SQLCipher-backed local pack store.
//!
//! The schema mirrors the pattern from `kennguy3n/knowledge`'s
//! `crates/evidence_store/src/schema.rs`:
//!
//! * **Append-only** at the `episode` and `chunk` table — UPDATE /
//!   DELETE attempts are rejected by triggers. Re-crawls that
//!   produce new content land as a new row with a fresh
//!   `created_at`; supersession is signalled via the
//!   `governance_log.deprecated` flag, not by mutation.
//! * **BLAKE3 content-hash dedup**. The chunk and governance
//!   tables both carry a `content_hash BLOB` column. The hash is
//!   computed from the canonicalised chunk text (Unicode NFKC,
//!   leading + trailing whitespace stripped, line endings
//!   normalised to `\n`) so byte-for-byte re-crawls collapse on
//!   ingest.
//! * **FTS5 with `unicode61 remove_diacritics 2`** — the canonical
//!   tokenizer used across the `knowledge` and `chat-storage-search`
//!   substrates. Keeps cross-script behaviour (Latin, Cyrillic,
//!   Han) consistent so query expansion behaves the same in the
//!   pack as in the on-device agent.
//! * **Composite primary key on embeddings** —
//!   `(chunk_id, model_tag)` — so a single chunk can carry
//!   embeddings under multiple model tags (e.g. an MPNet vector
//!   for the desktop agent and an XLM-R vector for the mobile
//!   agent) without an `INSERT OR REPLACE` destroying the other.
//! * **Mutable governance log** — rights decisions are append-only
//!   but the `deprecated` column on a *new* row may flip the
//!   status of an older row by overlay. The log itself is never
//!   rewritten.

/// Schema bootstrap statements. Executed in a single transaction
/// by [`crate::ingest::PackStore::open`].
pub const SCHEMA_SQL: &str = r#"
-- ---------------------------------------------------------------
-- Episodes. One row per crawled episode. Mirrors the append-only
-- pattern of knowledge's `evidence` table — UPDATE / DELETE are
-- rejected by triggers below.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episode (
    episode_id    TEXT PRIMARY KEY,
    publisher     TEXT NOT NULL,
    series        TEXT NOT NULL,
    title         TEXT NOT NULL,
    -- Full Episode struct as JSON. Denormalised so the pack ships
    -- a single self-contained row per episode; the read-heavy
    -- on-device agent prefers a single JSON parse over an n-way
    -- join across normalised dimension tables.
    metadata_json TEXT NOT NULL,
    rights_code   TEXT NOT NULL,
    -- Tags broken out as JSON arrays for fast structured-filter
    -- queries. SQLite's `json_each` makes these efficient enough
    -- without dedicated bridge tables — a pack carries O(10^4)
    -- episodes, not O(10^8).
    industry_tags       TEXT NOT NULL DEFAULT '[]',
    function_tags       TEXT NOT NULL DEFAULT '[]',
    business_model_tags TEXT NOT NULL DEFAULT '[]',
    geography_tags      TEXT NOT NULL DEFAULT '[]',
    evidence_type       TEXT NOT NULL DEFAULT '[]',
    created_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episode_publisher
    ON episode (publisher);
CREATE INDEX IF NOT EXISTS idx_episode_series
    ON episode (publisher, series);
CREATE INDEX IF NOT EXISTS idx_episode_rights
    ON episode (rights_code);

-- Append-only enforcement (same pattern as knowledge's evidence_store).
CREATE TRIGGER IF NOT EXISTS episode_no_update
BEFORE UPDATE ON episode
BEGIN
    SELECT RAISE(ABORT, 'episode is append-only');
END;

CREATE TRIGGER IF NOT EXISTS episode_no_delete
BEFORE DELETE ON episode
BEGIN
    SELECT RAISE(ABORT, 'episode is append-only');
END;

-- ---------------------------------------------------------------
-- Chunks. One row per chunk emitted by the speaker-turn chunker.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunk (
    chunk_id        TEXT PRIMARY KEY,
    episode_id      TEXT NOT NULL REFERENCES episode(episode_id),
    section_heading TEXT,
    chunk_text      TEXT NOT NULL,
    token_count     INTEGER NOT NULL,
    citation_anchor TEXT NOT NULL,
    -- BLAKE3-32 content hash of the canonicalised chunk text. Used
    -- to dedup re-ingests of bit-identical chunks (publisher
    -- republishes are common). Indexed because the dedup check
    -- on ingest is `SELECT 1 FROM chunk WHERE content_hash = ?`.
    content_hash    BLOB NOT NULL,
    created_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunk_episode
    ON chunk (episode_id);
CREATE INDEX IF NOT EXISTS idx_chunk_hash
    ON chunk (content_hash);

CREATE TRIGGER IF NOT EXISTS chunk_no_update
BEFORE UPDATE ON chunk
BEGIN
    SELECT RAISE(ABORT, 'chunk is append-only');
END;

CREATE TRIGGER IF NOT EXISTS chunk_no_delete
BEFORE DELETE ON chunk
BEGIN
    SELECT RAISE(ABORT, 'chunk is append-only');
END;

-- ---------------------------------------------------------------
-- FTS5 index over chunk text. Tokenizer matches the substrate
-- canonical 'unicode61 remove_diacritics 2' (same as knowledge's
-- evidence_fts and chat-storage-search's search_fts).
-- ---------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    chunk_text,
    chunk_id   UNINDEXED,
    episode_id UNINDEXED,
    tokenize   = 'unicode61 remove_diacritics 2'
);

-- ---------------------------------------------------------------
-- Optional embedding cache. Populated by an external embedder
-- (mirrors knowledge's evidence_embeddings v3 shape: composite
-- (chunk_id, model_tag) PK so multiple model vintages can
-- coexist).
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id     TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    model_tag    TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, model_tag)
);

-- ---------------------------------------------------------------
-- Governance log. Append-only audit trail of rights decisions and
-- content-hash provenance for every ingested episode. Re-ingest
-- of a known-good content_hash writes a no-op row so the log
-- captures the cadence (useful for monitoring publisher drift).
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS governance_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id      TEXT NOT NULL,
    rights_code     TEXT NOT NULL,
    ingestion_date  INTEGER NOT NULL,
    content_hash    BLOB NOT NULL,
    deprecated      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_governance_episode
    ON governance_log (episode_id);
CREATE INDEX IF NOT EXISTS idx_governance_hash
    ON governance_log (content_hash);

-- governance_log is append-only: existing rows are never
-- rewritten. Status changes flow through a new row.
CREATE TRIGGER IF NOT EXISTS governance_no_update
BEFORE UPDATE ON governance_log
BEGIN
    SELECT RAISE(ABORT, 'governance_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS governance_no_delete
BEFORE DELETE ON governance_log
BEGIN
    SELECT RAISE(ABORT, 'governance_log is append-only');
END;
"#;

/// Pragmas applied at every connection open. Mirrors the set used
/// by `knowledge`'s `evidence_store::store::open_connection`.
pub const CONNECTION_PRAGMAS: &str = r#"
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
"#;
